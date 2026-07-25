"""Laptop-side reviewer approval and immutable package builder for the
single-member creation UAT.

State ownership (per approved design):

* This tool runs on the LAPTOP DEVELOPMENT MACHINE. It owns the human decision
  record only: an append-only approval ledger recording controlled approve /
  reject / hold decisions bound to a stable source identity and a change-detection
  fingerprint, plus a record of which immutable package was built.
* It also owns a durable publication reservation per build attempt: a write-ahead
  intent marker created exclusively BEFORE any final package can be published, so
  the single-use approval boundary survives a ledger persistence failure. It is a
  safety backstop, never a second mutable business ledger.
* The AUTOCOUNT VM owns the exclusive execution lock, the write-intent marker, the
  consumed marker, and the terminal result. This tool never performs those.

It performs no AutoCount, n8n, Google, SQL, or network action, loads no
credentials, and never prints raw member values. ``source_record_id`` and
``source_fingerprint`` are one-way hashes and are safe to print.

Controlled decisions only: the reviewer chooses the subcommand ``approve``,
``reject`` or ``hold``. There is no free-text approval. Only ``approved`` ever
produces a package, and only for a row whose latest decision-review state is
``READY_FOR_CREATE_REVIEW``.

The optional ``validate-package`` subcommand is a laptop-side audit helper only.
The VM runner validates packages in-process against the JSON Schema and never
shells out to this tool.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import member_create_uat_contract as contract  # noqa: E402
import member_intake_validate as validator  # noqa: E402

DEFAULT_BUSINESS_CONFIG = (
    SCRIPT_DIR.parent / "config" / "member_create_uat_business_confirmation.json"
)

PRIVATE_MARKER_NAME = "PRIVATE_DO_NOT_COMMIT_MEMBER_CREATE_UAT.txt"
PRIVATE_MARKER_TEXT = (
    "PRIVATE - DO NOT COMMIT\n"
    "Member create UAT approval ledgers and packages are local-only artifacts that\n"
    "bind to real member identity. Do not commit, attach to PRs, paste into chat, or\n"
    "screenshot. Keep under C:\\XB\\autocount_outputs or an ignored local folder.\n"
)

# Distinct nonzero exit codes for the truthful terminal outcomes of a build, kept separate
# from ordinary error (2) and success (0) so no partial/uncertain state can be mistaken for
# a clean build at the process level. Documented in the runbook.
#
#   3  published, but the operation-owned temporary file could not be removed
#      (the durable ledger event WAS recorded)
#   4  published, but the durable ledger event could not be persisted
#   5  nothing published: the durable publication reservation was not created, or was
#      created without confirmed durability
#   6  nothing published: the reservation is confirmed durable but publication failed, so
#      the approval is permanently blocked by an unmatched reservation
EXIT_CLEANUP_INCOMPLETE = 3
EXIT_LEDGER_RECORD_INCOMPLETE = 4
EXIT_RESERVATION_INCOMPLETE = 5
EXIT_PUBLICATION_BLOCKED = 6

# Ledger events that mark an approval's package as already published (clean OR published
# with an incomplete temporary cleanup). Either blocks a plain re-build (single-use).
BUILD_LEDGER_EVENTS = ("build", "build_cleanup_incomplete")

# --------------------------------------------------------------------------- #
# Durable publication reservation (single-use safety backstop)
#
# The append-only ledger remains the audit log, but it cannot be the ONLY durable record
# of approval consumption: it is written AFTER the package is published, so a ledger
# open/write/flush/fsync failure would leave a real published package with no durable
# build event, and a later invocation with a fresh output path could mint another package.
#
# Therefore every build attempt must durably and EXCLUSIVELY reserve its approval BEFORE
# any final package can be published. The reservation is a write-ahead intent marker, not
# a second mutable business ledger: it is created exactly once per attempt, never
# rewritten, never truncated and never deleted by this tool.
#
# A reservation lives beside the approval ledger (the approval-state home) at a path
# derived deterministically from the approval id and a bounded attempt index, so this
# approval's reservations are found by direct path lookup - the directory is never listed,
# globbed or swept.
RESERVATION_SCHEMA_VERSION = "member_create_uat_reservation/v1"
RESERVATION_PREFIX = "member_create_uat_reservation_"
RESERVATION_SUFFIX = ".reservation"

# One slot per build attempt for a single approval. Slot 1 is the normal build; a further
# slot is only ever consumed by an explicitly requested --rebuild whose predecessors are
# all reconciled to clean `build` events. Bounded so slot discovery stays a fixed, small
# number of direct path lookups rather than a directory scan.
RESERVATION_MAX_ATTEMPTS = 16

# The sanitised fields that bind a reservation to exactly one build attempt. Every value is
# a random identifier, a one-way hash or an operator-chosen output basename - never a raw
# member value, never a credential, never a private absolute path. The same field names
# appear on the ledger build events, so a reservation reconciles against the ledger by
# exact equality on all of them.
RESERVATION_BINDING_FIELDS = (
    "approval_id",
    "source_record_id",
    "source_fingerprint",
    "operation_id",
    "bound_package_payload_hash",
    "package_file_name",
)


class ApprovalError(ValueError):
    """Local approval/package contract failure."""


class ReservationError(ApprovalError):
    """The durable publication reservation could not be created or confirmed durable.

    ``state`` is ``"not_created"`` when nothing was created (no approval state was
    consumed) or ``"uncertain"`` when an entry may exist but its durability could not be
    confirmed. An uncertain reservation is deliberately left in place: removing it would
    turn "maybe consumed" into "definitely free", which is the unsafe direction.
    """

    def __init__(self, message, *, state):
        super().__init__(message)
        self.state = state


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Source row resolution
# --------------------------------------------------------------------------- #
def load_decision_code(decision_rows_path, row_number):
    """Return the authoritative DecisionCode for the row, from the PII-free CSV."""
    with open(decision_rows_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "RowNumber" not in reader.fieldnames or "DecisionCode" not in reader.fieldnames:
            raise ApprovalError("The decision rows CSV does not match the expected contract.")
        for entry in reader:
            try:
                current = int(entry["RowNumber"])
            except (TypeError, ValueError):
                continue
            if current == row_number:
                return entry["DecisionCode"]
    raise ApprovalError("The requested row number is not present in the decision rows CSV.")


def resolve_source_row(input_path, decision_rows_path, row_number):
    """Normalize one form row and confirm it is READY_FOR_CREATE_REVIEW.

    Returns (member_payload, desired_business_fields, source_record_id,
    source_fingerprint). Never returns or prints raw values beyond the payload the
    caller writes into a local package.
    """
    if row_number < 2:
        raise ApprovalError("Row number must be 2 or greater (row 1 is the header).")

    decision_code = load_decision_code(decision_rows_path, row_number)
    if decision_code != contract.READY_FOR_CREATE_REVIEW:
        raise ApprovalError(
            "The latest decision state for that row is not READY_FOR_CREATE_REVIEW; "
            "approval is refused."
        )

    rows = validator.load_form_rows(input_path)
    index = row_number - 2
    if index < 0 or index >= len(rows):
        raise ApprovalError("The requested row number is beyond the form response rows.")

    result = validator.validate_row(rows[index])
    normalized = result["normalized"] or {}
    if not result["valid"] or result["manual_review"] or result["pdpa_blocked"]:
        raise ApprovalError("The form row is not eligible for creation approval.")
    if normalized.get("member_no_status") != "canonical":
        raise ApprovalError("Only a canonical member number may be approved for creation.")

    desired = dict(contract.INTENDED_BUSINESS_VALUES)
    member_payload = {
        "MemberNo": normalized["member_no"],
        "Name": normalized["name"],
        "EmailAddress": normalized["email_address"],
        "MobilePhone": "",
        "DOB": normalized["dob"],
        "MemberType": desired["MemberType"],
        "RegisterDate": desired["RegisterDate"],
        "ExpiryDate": desired["ExpiryDate"],
        "OpeningPoints": desired["OpeningPoints"],
    }
    srid = contract.source_record_id(member_payload["MemberNo"])
    fingerprint = contract.source_fingerprint(
        contract.build_fingerprint_fields(member_payload, desired)
    )
    return member_payload, desired, srid, fingerprint


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def append_ledger(ledger_path, entry):
    ledger_path = contract.assert_safe_local_path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    # The private marker is advisory only; its failure must never fail a durable ledger
    # append (and, at the package writer, must never mask a temporary-cleanup failure).
    try:
        _write_private_marker(ledger_path.parent)
    except OSError:
        pass


def read_ledger(ledger_path):
    path = Path(ledger_path)
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def latest_decision(entries, source_record_id):
    """Latest approve/reject/hold decision for a source record, or None."""
    found = None
    for entry in entries:
        if entry.get("event") == "decision" and entry.get("source_record_id") == source_record_id:
            found = entry
    return found


def build_already_exists(entries, source_record_id):
    # A package counts as already built if a normal build OR a cleanup-incomplete
    # publication event exists, so a cleanup failure never becomes a loophole that lets a
    # plain re-build proceed.
    return any(
        entry.get("event") in BUILD_LEDGER_EVENTS and entry.get("source_record_id") == source_record_id
        for entry in entries
    )


def published_cleanup_incomplete_exists(entries, source_record_id):
    """True if a prior publication for this source record succeeded but its temporary
    cleanup did not complete. Such an operation must not be retried as a new package
    build at all (not even with --rebuild): the package was published and requires manual
    temporary cleanup plus a fresh reviewer decision."""
    return any(
        entry.get("event") == "build_cleanup_incomplete"
        and entry.get("source_record_id") == source_record_id
        for entry in entries
    )


def _write_private_marker(directory):
    marker = Path(directory) / PRIVATE_MARKER_NAME
    if not marker.exists():
        marker.write_text(PRIVATE_MARKER_TEXT, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Durable publication reservation
# --------------------------------------------------------------------------- #
class _ReservationStatus:
    """How one existing reservation relates to the durable ledger."""

    RECONCILED_BUILD = "reconciled_build"                          # clean, fully recorded
    RECONCILED_CLEANUP_INCOMPLETE = "reconciled_cleanup_incomplete"  # published, temp stale
    UNMATCHED = "unmatched"      # reserved, but no durable build event ever matched it
    MALFORMED = "malformed"      # unreadable, non-JSON, wrong schema or missing bindings
    FOREIGN = "foreign"          # well-formed but bound to a different approval/record


def reservation_path(state_dir, approval_id, attempt):
    """The deterministic path of one (approval, attempt) reservation slot."""
    if not (isinstance(approval_id, str) and contract.APPROVAL_ID_RE.fullmatch(approval_id)):
        raise ApprovalError("A durable publication reservation requires a well-formed approval id.")
    if not isinstance(attempt, int) or not 1 <= attempt <= RESERVATION_MAX_ATTEMPTS:
        raise ApprovalError("The publication reservation attempt index is out of range.")
    return Path(state_dir) / f"{RESERVATION_PREFIX}{approval_id}.{attempt}{RESERVATION_SUFFIX}"


def _fsync_parent_directory(directory):
    """Make a newly created directory ENTRY durable and report the mode actually achieved.

    POSIX: fsync a directory handle, so the reservation's existence - not merely its bytes -
    survives a crash. Windows: there is no portable directory-handle fsync; NTFS journals
    the directory metadata for the reservation's own create-and-fsync, so the entry is
    durable once that fsync returns. The achieved mode is returned (never assumed) so the
    terminal output can never overstate durability.
    """
    if not hasattr(os, "O_DIRECTORY"):
        return "file_fsync_only"
    dir_fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return "file_and_directory_fsync"


def write_reservation(path, record):
    """Exclusively create ONE durable publication reservation; return the durability mode.

    Exclusive: ``O_CREAT | O_EXCL`` means exactly one build attempt can ever own a slot, so
    two concurrent builds for the same approval cannot both proceed to publication.

    Durable: the record is written, flushed and fsynced, then the parent directory entry is
    synced where the platform supports it.

    Fail-closed: raises ``ReservationError``. ``state="not_created"`` means nothing was
    created, so no approval state was consumed. ``state="uncertain"`` means an entry may
    exist but is not confirmed durable; it is deliberately NOT deleted, truncated or
    rewritten, so the approval stays blocked until a controller reconciles it.
    """
    try:
        # Converted rather than propagated: a bare ContractError would escape the publication
        # writer's typed handlers and leave the operation-owned temporary file uncleaned.
        # Path validation precedes any create, so nothing was created.
        path = contract.assert_safe_local_path(path)
    except contract.ContractError as error:
        raise ReservationError(
            "The durable publication reservation path is unsafe (for example a reparse "
            "point created concurrently); refuse fail-closed and publish nothing.",
            state="not_created",
        ) from error
    body = json.dumps(record, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ReservationError(
            "A durable publication reservation already exists for this approval attempt; "
            "refuse fail-closed (a concurrent build attempt owns it).",
            state="not_created",
        ) from error
    except OSError as error:
        raise ReservationError(
            "The durable publication reservation could not be created; refuse fail-closed "
            "and publish nothing.",
            state="not_created",
        ) from error
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise ReservationError(
            "The durable publication reservation was created but could not be written or "
            "flushed durably; refuse fail-closed and publish nothing.",
            state="uncertain",
        ) from error
    try:
        return _fsync_parent_directory(path.parent)
    except OSError as error:
        raise ReservationError(
            "The durable publication reservation was written but its directory entry could "
            "not be made durable; refuse fail-closed and publish nothing.",
            state="uncertain",
        ) from error


def read_reservation(path):
    """Parse one reservation slot, or return None when it cannot be trusted.

    Unreadable, non-JSON, non-object and reparse-point slots all return None and are
    classified ``malformed`` by the caller: an untrustworthy reservation must block the
    approval, never be repaired or ignored.
    """
    candidate = Path(path)
    if contract.is_reparse_point(candidate):
        return None
    try:
        record = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def reservation_reconciliation(entries, record, approval_id, source_record_id):
    """Classify one reservation record against the durable ledger entries."""
    if record is None or record.get("schema_version") != RESERVATION_SCHEMA_VERSION:
        return _ReservationStatus.MALFORMED
    for field in RESERVATION_BINDING_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value:
            return _ReservationStatus.MALFORMED
    if record["approval_id"] != approval_id or record["source_record_id"] != source_record_id:
        return _ReservationStatus.FOREIGN
    for entry in entries:
        if entry.get("event") not in BUILD_LEDGER_EVENTS:
            continue
        if all(entry.get(field) == record[field] for field in RESERVATION_BINDING_FIELDS):
            if entry["event"] == "build":
                return _ReservationStatus.RECONCILED_BUILD
            return _ReservationStatus.RECONCILED_CLEANUP_INCOMPLETE
    # Reserved, but no durable build event ever matched: this attempt may have published a
    # package whose ledger record was lost. The approval must not progress automatically.
    return _ReservationStatus.UNMATCHED


def survey_reservations(state_dir, approval_id, source_record_id, entries):
    """Inspect this approval's bounded reservation slots and report what blocks a build.

    Only paths derived from THIS approval's id are examined, so the state directory is
    never listed, globbed or swept and unrelated reservations, packages, ledgers and
    temporary files are never read or touched.

    Returns ``(unreconciled, free_attempt)``: ``unreconciled`` lists
    ``(basename, status)`` for every slot that is not a cleanly reconciled ``build``, and
    ``free_attempt`` is the lowest unused slot index (None when every slot is used).
    """
    unreconciled = []
    free_attempt = None
    for attempt in range(1, RESERVATION_MAX_ATTEMPTS + 1):
        path = reservation_path(state_dir, approval_id, attempt)
        if not os.path.lexists(path):
            if free_attempt is None:
                free_attempt = attempt
            continue
        status = reservation_reconciliation(
            entries, read_reservation(path), approval_id, source_record_id
        )
        if status != _ReservationStatus.RECONCILED_BUILD:
            unreconciled.append((path.name, status))
    return unreconciled, free_attempt


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_decision(args, decision):
    payload, desired, srid, fingerprint = resolve_source_row(
        args.input, args.decision_rows, args.row_number
    )
    now = utc_now_iso()
    entry = {
        "event": "decision",
        "recorded_at": now,
        "reviewer_id": args.reviewer,
        "decision": decision,
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "row_number_hint": args.row_number,
        "approval_id": None,
        "approved_at": None,
        "expires_at": None,
    }
    if decision == "approved":
        ttl_hours = args.ttl_hours if args.ttl_hours is not None else contract.DEFAULT_APPROVAL_TTL_HOURS
        if ttl_hours <= 0 or ttl_hours > contract.DEFAULT_APPROVAL_TTL_HOURS:
            raise ApprovalError(
                "Approval TTL must be positive and no longer than the contract default."
            )
        expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        entry["approval_id"] = "appr_" + uuid.uuid4().hex
        entry["approved_at"] = now
        entry["expires_at"] = expires.isoformat(timespec="seconds")
    append_ledger(args.ledger, entry)
    _print_summary(
        {
            "status": "ok",
            "event": "decision",
            "decision": decision,
            "reviewer_id": args.reviewer,
            "source_record_id": srid,
            "source_fingerprint": fingerprint,
            "approval_id": entry["approval_id"],
            "expires_at": entry["expires_at"],
        }
    )
    return 0


def cmd_build_package(args):
    payload, desired, srid, fingerprint = resolve_source_row(
        args.input, args.decision_rows, args.row_number
    )
    entries = read_ledger(args.ledger)
    decision = latest_decision(entries, srid)
    if decision is None or decision.get("decision") != "approved":
        raise ApprovalError("No current approval exists for that source record.")
    if decision.get("source_fingerprint") != fingerprint:
        raise ApprovalError(
            "The source data changed after approval (fingerprint mismatch); approval is invalid."
        )
    expires_at = decision.get("expires_at")
    if not expires_at or datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
        raise ApprovalError("The approval has expired; re-approval is required.")
    # A prior publication whose temporary cleanup did not complete is terminal for this
    # source record: the package WAS published and must not be retried as a new build
    # (not even with --rebuild). It requires manual temporary cleanup and a fresh reviewer
    # decision, so the same approval can never mint another package merely because cleanup
    # failed.
    if published_cleanup_incomplete_exists(entries, srid):
        raise ApprovalError(
            "A prior package for this source record was published but its temporary "
            "cleanup did not complete; this operation must not be retried as a new package "
            "build. Complete the manual temporary cleanup and start a fresh reviewer "
            "decision."
        )
    approval_id = decision.get("approval_id")
    if not (isinstance(approval_id, str) and contract.APPROVAL_ID_RE.fullmatch(approval_id)):
        raise ApprovalError(
            "The approval record carries no well-formed approval id, so no durable "
            "publication reservation can bind to it; refuse fail-closed."
        )

    # Durable reservation gate. A reservation that is not cleanly reconciled to a normal
    # `build` ledger event means a prior attempt for THIS approval may already have
    # published a package whose ledger record was lost, or left state that cannot be
    # trusted. Such an approval must not progress automatically - neither as a plain build
    # nor with --rebuild - so this gate precedes the ordinary already-built check.
    state_dir = contract.assert_safe_local_path(args.ledger).parent
    unreconciled, free_attempt = survey_reservations(state_dir, approval_id, srid, entries)
    if unreconciled:
        _print_summary(
            {
                "status": "reservation_blocked",
                "event": "none",
                "publication": "not_attempted",
                "reservation": "unreconciled",
                "unreconciled_reservations": [
                    {"reservation_basename": name, "reservation_status": status}
                    for name, status in unreconciled
                ],
                "approval_blocked": True,
                "do_not_retry": True,
                "rebuild_blocked": True,
                "fresh_approval_required": True,
                "recovery": "fresh_approval_or_controlled_recovery",
                "source_record_id": srid,
            }
        )
        return EXIT_RESERVATION_INCOMPLETE
    if build_already_exists(entries, srid) and not args.rebuild:
        raise ApprovalError(
            "A package was already built for that source record; refuse (pass --rebuild only "
            "if the prior package was never sent to the VM)."
        )
    if free_attempt is None:
        raise ApprovalError(
            "Every durable publication reservation slot for this approval is used; refuse "
            "fail-closed and start a fresh reviewer decision."
        )

    # Strict no-clobber precondition, checked BEFORE any reservation so an occupied output
    # path (an operator typo, or a preserved historical artifact) never consumes approval
    # state. The writer re-checks it and `os.link` remains the real guard against a
    # concurrent creator.
    out_path = contract.assert_safe_local_path(args.package_out)
    _assert_final_path_absent(out_path)
    package_file_name = out_path.name

    package = {
        "schema_version": contract.SCHEMA_VERSION,
        "operation_id": "mcuat_" + uuid.uuid4().hex,
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "row_number_hint": args.row_number,
        "created_at": utc_now_iso(),
        "approval": {
            "approval_id": decision["approval_id"],
            "reviewer_id": decision["reviewer_id"],
            "decision": "approved",
            "approved_at": decision["approved_at"],
            "expires_at": decision["expires_at"],
            "source_record_id": srid,
            "source_fingerprint": fingerprint,
            "bound_package_payload_hash": "sha256:" + ("0" * 64),
        },
        "assignable_fields": list(contract.ASSIGNABLE_FIELDS),
        "member_payload": payload,
        "desired_business_fields": desired,
        "business_confirmation_required": list(contract.BUSINESS_CONFIRMATION_REQUIRED),
        "payload_hash": "sha256:" + ("0" * 64),
    }
    payload_hash = contract.compute_payload_hash(package)
    package["payload_hash"] = payload_hash
    package["approval"]["bound_package_payload_hash"] = payload_hash

    ok, reasons = contract.validate_package(package)
    if not ok:
        raise ApprovalError("Internal error: generated package failed validation: " + ",".join(reasons))

    # The reservation record binds this exact build attempt. Every value is a random
    # identifier, a one-way hash or the operator-chosen output basename: no raw member
    # value, no credential and no private absolute path is stored.
    reservation_record = {
        "schema_version": RESERVATION_SCHEMA_VERSION,
        "reserved_at": utc_now_iso(),
        "attempt": free_attempt,
        "approval_id": approval_id,
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "operation_id": package["operation_id"],
        "bound_package_payload_hash": payload_hash,
        "package_file_name": package_file_name,
    }
    reservation_target = reservation_path(state_dir, approval_id, free_attempt)
    reservation_basename = reservation_target.name

    result = _write_package_atomically(
        args.package_out,
        package,
        lambda: write_reservation(reservation_target, reservation_record),
    )

    if result.state == _PublishState.RESERVATION_FAILED:
        # Nothing was published (the reservation boundary is strictly before os.link), so no
        # final path was created and no competitor was touched. An `uncertain` reservation
        # entry is never deleted, recreated or retried: it keeps this approval blocked until
        # a controller reconciles it.
        uncertain = result.reservation_state == "uncertain"
        _print_summary(
            {
                "status": "reservation_incomplete",
                "event": "none",
                "publication": "not_published",
                "reservation": result.reservation_state,
                "reservation_basename": reservation_basename,
                "reservation_durability": "unconfirmed",
                "temp_cleanup": "failed" if result.temp_stale else "complete",
                "stale_temp_basename": result.temp_basename if result.temp_stale else None,
                "manual_cleanup_required": bool(result.temp_stale),
                "approval_blocked": uncertain,
                "do_not_retry": uncertain,
                "fresh_approval_required": uncertain,
                "recovery": (
                    "fresh_approval_or_controlled_recovery"
                    if uncertain
                    else "resolve_reservation_failure_then_retry"
                ),
                "source_record_id": srid,
                "operation_id": package["operation_id"],
            }
        )
        return EXIT_RESERVATION_INCOMPLETE

    if result.state == _PublishState.RESERVED_NOT_PUBLISHED:
        # The reservation IS durable but publication did not complete, so no package was
        # published and any competing final path is untouched. No ledger build event will
        # ever match this reservation, so the approval is permanently blocked: recovery is a
        # fresh reviewer decision or a controlled reconciliation, never a silent retry.
        _print_summary(
            {
                "status": "publication_failed_after_reservation",
                "event": "none",
                "publication": "not_published",
                "reservation": "confirmed_durable",
                "reservation_basename": reservation_basename,
                "reservation_durability": result.reservation_durability,
                "temp_cleanup": "failed" if result.temp_stale else "complete",
                "stale_temp_basename": result.temp_basename if result.temp_stale else None,
                "manual_cleanup_required": bool(result.temp_stale),
                "approval_blocked": True,
                "do_not_retry": True,
                "fresh_approval_required": True,
                "recovery": "fresh_approval_or_controlled_recovery",
                "source_record_id": srid,
                "operation_id": package["operation_id"],
            }
        )
        return EXIT_PUBLICATION_BLOCKED

    if result.state == _PublishState.NOT_PUBLISHED:
        # No package was published. If the operation-owned temporary file was cleaned, the
        # original failure is authoritative - propagate it (no success, no build event). If
        # the temporary file could NOT be removed, emit a truthful nonzero cleanup-incomplete
        # result and still append no build event, without masking the original failure.
        if not result.temp_stale:
            raise result.cause if result.cause is not None else ApprovalError(
                "The package could not be published; refuse fail-closed."
            )
        _print_summary(
            {
                "status": "cleanup_incomplete",
                "event": "none",
                "publication": "not_published",
                "temp_cleanup": "failed",
                "original_failure": "package_write_or_publication_failed",
                "manual_cleanup_required": True,
                "do_not_retry": True,
                "stale_temp_basename": result.temp_basename,
                "source_record_id": srid,
                "operation_id": package["operation_id"],
            }
        )
        return EXIT_CLEANUP_INCOMPLETE

    # PUBLISHED: the committed boundary is crossed and the reservation is durable. The
    # published final package is never deleted, rolled back, truncated, renamed or modified
    # from here on, whatever else fails.
    common = {
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "approval_id": approval_id,
        "operation_id": package["operation_id"],
        "bound_package_payload_hash": payload_hash,
        "package_file_name": package_file_name,
        # Recorded for audit so the ledger event and its reservation are linked explicitly;
        # reconciliation itself matches on RESERVATION_BINDING_FIELDS.
        "reservation_file_name": reservation_basename,
    }
    published_common = {
        "publication": "succeeded",
        "reservation": "confirmed_durable",
        "reservation_basename": reservation_basename,
        "reservation_durability": result.reservation_durability,
        "source_record_id": srid,
        "operation_id": package["operation_id"],
        "payload_hash": payload_hash,
        "package_file_name": package_file_name,
        "expiry_date_in_payload": True,
    }

    if result.state == _PublishState.PUBLISHED_CLEANUP_COMPLETE:
        ledger_error = _append_build_event(
            args.ledger, dict(common, event="build", recorded_at=utc_now_iso())
        )
        if ledger_error is not None:
            # Publication succeeded and temporary cleanup completed, but the durable build
            # event could not be persisted. Ordinary success is impossible. The durable
            # reservation - written before publication - is now unmatched, so this approval
            # cannot build again at ANY fresh path, including with --rebuild.
            _print_summary(
                dict(
                    published_common,
                    status="ledger_record_incomplete",
                    event="none",
                    ledger_record="incomplete",
                    temp_cleanup="complete",
                    manual_cleanup_required=False,
                    do_not_retry=True,
                    approval_blocked=True,
                    fresh_approval_required=True,
                    recovery="fresh_approval_or_controlled_recovery",
                )
            )
            return EXIT_LEDGER_RECORD_INCOMPLETE
        _print_summary(
            {
                "status": "ok",
                "event": "build",
                "source_record_id": srid,
                "operation_id": package["operation_id"],
                "payload_hash": payload_hash,
                "package_file_name": package_file_name,
                "assign_fields_count": len(contract.ASSIGNABLE_FIELDS),
                "temp_cleanup": "complete",
                "ledger_record": "complete",
                "reservation": "confirmed_durable",
                "reservation_basename": reservation_basename,
                "reservation_durability": result.reservation_durability,
                # LAPTOP-side builder: records only that ExpiryDate is present in the
                # immutable package payload. It performs no AutoCount member assignment, so
                # it never emits `expiry_date_assigned` (that field is the VM runner's,
                # set only after the assignment loop actually completes).
                "expiry_date_in_payload": True,
            }
        )
        return 0

    # PUBLISHED_CLEANUP_INCOMPLETE: the final package IS published. Do not roll it back or
    # claim ordinary success. Append exactly one durable cleanup-incomplete publication
    # event binding the published package to the approval transaction, so later validation
    # cannot mistake this for a clean build.
    ledger_error = _append_build_event(
        args.ledger,
        dict(
            common,
            event="build_cleanup_incomplete",
            recorded_at=utc_now_iso(),
            cleanup_incomplete=True,
            stale_temp_basename=result.temp_basename,
        ),
    )
    if ledger_error is not None:
        # Published, temporary cleanup incomplete AND the durable build event could not be
        # persisted. All three facts are reported distinctly; the durable reservation still
        # blocks the approval, so no `build` or clean-success claim is ever emitted.
        _print_summary(
            dict(
                published_common,
                status="ledger_record_incomplete",
                event="none",
                ledger_record="incomplete",
                temp_cleanup="failed",
                stale_temp_basename=result.temp_basename,
                manual_cleanup_required=True,
                do_not_retry=True,
                approval_blocked=True,
                fresh_approval_required=True,
                recovery="fresh_approval_or_controlled_recovery",
            )
        )
        return EXIT_LEDGER_RECORD_INCOMPLETE
    _print_summary(
        dict(
            published_common,
            status="cleanup_incomplete",
            event="build_cleanup_incomplete",
            ledger_record="complete",
            temp_cleanup="failed",
            manual_cleanup_required=True,
            do_not_retry=True,
            stale_temp_basename=result.temp_basename,
        )
    )
    return EXIT_CLEANUP_INCOMPLETE


def _append_build_event(ledger_path, entry):
    """Append one durable build event, classifying a persistence failure INSIDE the build
    state machine rather than leaving the generic top-level handler to describe a published
    package as an ordinary error.

    Returns None once the append is confirmed durable, otherwise the failure. A failure here
    never rewrites, truncates or replaces the ledger, and never deletes, rolls back,
    truncates, renames or modifies the already-published final package.
    """
    try:
        append_ledger(ledger_path, entry)
    except (OSError, contract.ContractError) as error:
        return error
    return None


class _PublishState:
    """The publication/reservation/cleanup states of one package build. Each distinction is
    material: they leave different committed states and are handled separately by the
    caller, ordered by the boundaries the attempt crossed.

    ``NOT_PUBLISHED``          failed BEFORE the reservation boundary; no approval state
                               was consumed and the attempt may be retried.
    ``RESERVATION_FAILED``     the durable reservation was not created, or was created
                               without confirmed durability. Nothing was published.
    ``RESERVED_NOT_PUBLISHED`` the reservation is durable but publication failed. Nothing
                               was published, yet the approval is permanently blocked.
    ``PUBLISHED_*``            the final package exists and is never rolled back.
    """

    NOT_PUBLISHED = "not_published"
    RESERVATION_FAILED = "reservation_failed"
    RESERVED_NOT_PUBLISHED = "reserved_not_published"
    PUBLISHED_CLEANUP_COMPLETE = "published_cleanup_complete"
    PUBLISHED_CLEANUP_INCOMPLETE = "published_cleanup_incomplete"


class _PublishResult:
    """Outcome of one publication attempt. The writer NEVER silently swallows a
    temporary-cleanup failure: a failed unlink is surfaced via ``cleanup_error`` and the
    ``PUBLISHED_CLEANUP_INCOMPLETE`` / stale-temp states."""

    def __init__(self, state, temp_basename=None, cause=None, cleanup_error=None,
                 reservation_state=None, reservation_durability=None):
        self.state = state
        self.temp_basename = temp_basename        # sanitised, PII-free basename or None
        self.cause = cause                        # original pre-publication failure (NOT_PUBLISHED)
        self.cleanup_error = cleanup_error        # temporary-unlink failure, if any
        self.temp_stale = cleanup_error is not None  # an operation-owned temp remains
        self.reservation_state = reservation_state          # not_created | uncertain | None
        self.reservation_durability = reservation_durability  # achieved durability mode


def _assert_final_path_absent(out_path):
    """Strict no-clobber precondition: an existing/competing final path (for example a
    preserved historical v1 artifact) is NEVER deleted, truncated, replaced, renamed away
    or overwritten."""
    if os.path.lexists(out_path):
        raise ApprovalError(
            "The package output path already exists; refuse fail-closed to preserve the "
            "existing package (never overwritten). Choose a fresh, version-distinct "
            "output filename."
        )


def _write_package_atomically(package_out, package, reserve):
    """Publish the immutable package with strict no-clobber, a DURABLE PRE-PUBLICATION
    RESERVATION, atomic visibility, AND truthful temporary-file cleanup. Returns a
    ``_PublishResult``; the strict no-clobber precondition (an already-occupied final path)
    still raises before any temporary file is created.

    Strict no-clobber: an existing/competing final path (e.g. a historical v1 artifact)
    is NEVER deleted, truncated, replaced, renamed away, or overwritten; a concurrent
    creator wins safely.

    Durable reservation boundary: ``reserve`` is called only once the complete package
    exists as an operation-owned temporary, and strictly BEFORE publication. Until it
    confirms an exclusive durable reservation for this exact build attempt, no final
    package can be published. A reservation failure therefore cannot leave a published
    package, and a later publication or ledger failure cannot leave a published package
    without durable evidence that the approval was consumed.

    Atomic publication: the complete package is written to a unique, PII-free temporary
    file in the SAME directory (exclusive-created via ``mkstemp``), flushed and fsynced,
    then published at the final path with ``os.link`` - a no-replace hard link that
    atomically exposes the already-complete inode and fails closed if the final path
    exists. A reader never sees a partial file at the final pathname; a crash leaves at
    most a stray temporary (never a partial FINAL package). No fallback to progressive
    writing at the final path.

    Truthful cleanup: after resolving publication, exactly the ONE operation-owned
    temporary path is unlinked. A failed unlink is NOT swallowed - it is reported so the
    caller emits a nonzero cleanup-incomplete terminal result rather than false success.
    Never sweeps other ``.mcuat_pkg_*`` files, and never removes a reservation.
    """
    out_path = contract.assert_safe_local_path(package_out)
    _assert_final_path_absent(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(out_path.parent), prefix=".mcuat_pkg_", suffix=".tmp")
    temp_basename = os.path.basename(temp_name)
    published = False
    reserved = False
    durability = None
    cause = None
    reservation_error = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(package, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        # ---- DURABLE RESERVATION BOUNDARY ------------------------------------------- #
        # The complete package exists only as an operation-owned temporary. Nothing is
        # published until an exclusive durable reservation for THIS attempt is confirmed.
        durability = reserve()
        reserved = True
        # ---------------------------------------------------------------------------- #
        os.link(temp_name, out_path)  # atomic no-replace publication of the complete file
        published = True
    except ReservationError as error:
        # Kept out of the OSError arms below so a reservation failure can never be reported
        # as a publication failure (or vice versa).
        reservation_error = error
    except FileExistsError as error:
        cause = ApprovalError(
            "The package output path was created concurrently; refuse fail-closed to "
            "preserve the existing package (never overwritten)."
        )
        cause.__cause__ = error
    except OSError as error:
        # Covers temporary write, flush/fsync, and non-FileExists link failures (e.g. a
        # filesystem without atomic no-replace hard links). Never falls back to a
        # progressive write at the final path.
        cause = ApprovalError(
            "The package could not be written or atomically published; refuse fail-closed "
            "(never falls back to progressive writing at the final path)."
        )
        cause.__cause__ = error

    # Attempt cleanup of ONLY the operation-owned temporary path, in every case. Never
    # touch the final/competing path, another temporary, or any reservation. A failed unlink
    # is recorded (not swallowed) so the outcome stays truthful.
    cleanup_error = None
    try:
        os.unlink(temp_name)
    except OSError as unlink_err:
        cleanup_error = unlink_err

    if reservation_error is not None:
        # RESERVATION_FAILED: no final path was created, so no competitor was touched. An
        # `uncertain` reservation entry is deliberately left in place.
        return _PublishResult(_PublishState.RESERVATION_FAILED, temp_basename=temp_basename,
                              cause=reservation_error, cleanup_error=cleanup_error,
                              reservation_state=reservation_error.state)

    if not published:
        # We never created the final path (a race competitor is untouched). Whether the
        # approval is now blocked depends on which boundary was crossed: a pre-reservation
        # failure leaves the approval free, a post-reservation failure does not.
        if reserved:
            return _PublishResult(_PublishState.RESERVED_NOT_PUBLISHED,
                                  temp_basename=temp_basename, cause=cause,
                                  cleanup_error=cleanup_error, reservation_durability=durability)
        # If cleanup succeeded, cleanup_error is None and the caller re-raises `cause`.
        return _PublishResult(_PublishState.NOT_PUBLISHED, temp_basename=temp_basename,
                              cause=cause, cleanup_error=cleanup_error)

    # PUBLISHED: the final package exists. Best-effort private marker; a marker failure
    # can never change the publication/cleanup state or touch the package.
    try:
        _write_private_marker(out_path.parent)
    except OSError:
        pass
    if cleanup_error is None:
        return _PublishResult(_PublishState.PUBLISHED_CLEANUP_COMPLETE,
                              reservation_durability=durability)
    return _PublishResult(_PublishState.PUBLISHED_CLEANUP_INCOMPLETE,
                          temp_basename=temp_basename, cleanup_error=cleanup_error,
                          reservation_durability=durability)


def cmd_validate_package(args):
    """Laptop-side audit helper. Not used by the VM runner."""
    path = contract.assert_safe_local_path(args.package, must_exist=True)
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        _print_kv([("status", "needs_fix"), ("reason", "package_not_json")])
        return 2
    ok, reasons = contract.validate_package(package)
    lines = [("status", "ok" if ok else "needs_fix"), ("structural_valid", str(ok).lower())]
    if not ok:
        lines.append(("reason_count", len(reasons)))
        lines.append(("first_reason", reasons[0] if reasons else "none"))
    if args.for_write:
        write_ok, write_reason = _for_write_checks(package, args.business_config)
        lines.append(("for_write_ok", str(write_ok).lower()))
        lines.append(("for_write_terminal_code", write_reason))
        ok = ok and write_ok
    _print_kv(lines)
    return 0 if ok else 2


def _for_write_checks(package, business_config):
    """Return (ok, terminal_code) for the write-readiness of a structurally valid
    package: expiry and business confirmation. Single-use/lock are VM-owned."""
    approval = package.get("approval") if isinstance(package, dict) else None
    if not isinstance(approval, dict):
        return False, "APPROVAL_INVALID"
    expires_at = approval.get("expires_at")
    try:
        if not expires_at or datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
            return False, "APPROVAL_INVALID"
    except ValueError:
        return False, "APPROVAL_INVALID"
    try:
        confirmations = contract.load_business_confirmation(business_config or DEFAULT_BUSINESS_CONFIG)
    except contract.ContractError:
        return False, "OPERATOR_CONFIG_REQUIRED"
    all_confirmed, _ = contract.business_fields_confirmed(confirmations)
    if not all_confirmed:
        return False, "OPERATOR_CONFIG_REQUIRED"
    return True, "DRY_RUN_VALIDATED"


# --------------------------------------------------------------------------- #
# PII-free console output
# --------------------------------------------------------------------------- #
def _print_summary(summary):
    print(json.dumps(summary, indent=2, sort_keys=True))


def _print_kv(pairs):
    for key, value in pairs:
        print(f"{key} = {value}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Reviewer approval and immutable package builder for the single-member "
            "creation UAT (laptop-side; no live action)."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--input", required=True, help="Google Form response CSV path (local, private).")
        p.add_argument("--decision-rows", required=True, help="member_intake_decision_rows.csv path.")
        p.add_argument("--row-number", required=True, type=int, help="Spreadsheet row number (>= 2).")
        p.add_argument("--ledger", required=True, help="Local approval ledger JSONL path (never commit).")

    for name in ("approve", "reject", "hold"):
        p = sub.add_parser(name, help=f"Record a controlled {name} decision for one row.")
        add_common(p)
        p.add_argument("--reviewer", required=True, help="Non-secret operator handle [a-z0-9_-]{2,32}.")
        if name == "approve":
            p.add_argument(
                "--ttl-hours",
                type=int,
                default=None,
                help=f"Approval TTL in hours (default {contract.DEFAULT_APPROVAL_TTL_HOURS}; cannot exceed it).",
            )

    build = sub.add_parser("build-package", help="Build the immutable one-record package for an approved row.")
    add_common(build)
    build.add_argument("--package-out", required=True, help="Output package JSON path (local, never commit).")
    build.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Allow another ledger build for a source record that was already built, but "
            "only when --package-out is a fresh, absent path AND every prior durable "
            "publication reservation for this approval is reconciled to a clean build "
            "event. It never overwrites an existing package (the output path is always "
            "no-clobber) and never bypasses an unmatched or uncertain reservation."
        ),
    )

    vp = sub.add_parser("validate-package", help="Laptop-side audit of a package (not used by the VM runner).")
    vp.add_argument("--package", required=True)
    vp.add_argument("--for-write", action="store_true", help="Also check expiry and business confirmation.")
    vp.add_argument("--business-config", default=None, help="Business confirmation config path.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "approve":
            return cmd_decision(args, "approved")
        if args.command == "reject":
            return cmd_decision(args, "rejected")
        if args.command == "hold":
            return cmd_decision(args, "hold")
        if args.command == "build-package":
            return cmd_build_package(args)
        if args.command == "validate-package":
            return cmd_validate_package(args)
    except (ApprovalError, contract.ContractError, validator.FormContractError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2, sort_keys=True))
        return 2
    raise ApprovalError("Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
