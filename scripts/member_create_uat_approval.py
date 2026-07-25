"""Laptop-side reviewer approval and immutable package builder for the
single-member creation UAT.

State ownership (per approved design):

* This tool runs on the LAPTOP DEVELOPMENT MACHINE. It owns the human decision
  record only: controlled approve / reject / hold decisions bound to a stable source
  identity and a change-detection fingerprint, plus a record of which immutable package
  was built.
* Reviewer AUTHORITY lives in a transactional, append-only SQLite decision store
  (``member_create_uat_decision_store``). A decision authorises a package only when a
  committed ACTIVATION row exists for it.
* The JSONL approval ledger is an append-only AUDIT RECORD ONLY. A readable JSONL line -
  including a complete one left behind by a flush/fsync failure - never grants authority.
* It also owns a durable publication reservation: a write-ahead intent marker created
  exclusively BEFORE any final package can be published, so the single-use approval
  boundary survives a ledger persistence failure. Its existence TERMINALLY consumes that
  approval for package-building purposes; a readable ledger event never releases it. It
  is a safety backstop, never a second mutable business ledger.
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
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import member_create_uat_contract as contract  # noqa: E402
import member_create_uat_decision_store as decisions  # noqa: E402
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
#   7  nothing attempted: a reservation object already exists for this approval, so the
#      approval is terminally consumed; also returned for the retired same-approval
#      --rebuild request, which can never authorise a further package
#   8  nothing attempted: the approval ledger could not be read as a structurally intact
#      append-only record, so no decision in it may be trusted
#   9  decision authority cannot be trusted: a pending decision was left non-authoritative,
#      a commit outcome could not be resolved, or the decision store is not intact
#  10  no activated decision authorises a build: the store is absent, the newest activated
#      decision is a rejection or hold, or a newer decision is still pending
EXIT_CLEANUP_INCOMPLETE = 3
EXIT_LEDGER_RECORD_INCOMPLETE = 4
EXIT_RESERVATION_INCOMPLETE = 5
EXIT_PUBLICATION_BLOCKED = 6
EXIT_APPROVAL_CONSUMED = 7
EXIT_LEDGER_INTEGRITY_UNCERTAIN = 8
EXIT_DECISION_AUTHORITY_UNCERTAIN = 9
EXIT_DECISION_NOT_AUTHORITATIVE = 10

# Ledger events that mark an approval's package as already published (clean OR published
# with an incomplete temporary cleanup). Either one blocks a further build for that approval.
BUILD_LEDGER_EVENTS = ("build", "build_cleanup_incomplete")
CLEANUP_INCOMPLETE_LEDGER_EVENTS = ("build_cleanup_incomplete",)

# --------------------------------------------------------------------------- #
# Durable publication reservation (single-use safety backstop)
#
# The append-only ledger remains the audit log, but it cannot be the ONLY durable record
# of approval consumption: it is written AFTER the package is published, so a ledger
# open/write/flush/fsync failure would leave a real published package with no durable
# build event, and a later invocation with a fresh output path could mint another package.
#
# Therefore a build must durably and EXCLUSIVELY reserve its approval BEFORE any final
# package can be published. The reservation is a write-ahead intent marker, not a second
# mutable business ledger: it is created exactly once, never rewritten, never truncated and
# never deleted by this tool.
#
# A reservation lives beside the approval ledger (the approval-state home) at a path
# derived deterministically from the approval id and a bounded slot index, so this
# approval's reservations are found by direct path lookup - the directory is never listed,
# globbed or swept.
RESERVATION_SCHEMA_VERSION = "member_create_uat_reservation/v1"
RESERVATION_PREFIX = "member_create_uat_reservation_"
RESERVATION_SUFFIX = ".reservation"

# --------------------------------------------------------------------------- #
# THE TERMINAL RESERVATION RULE
#
# A reservation used to be treated as spent - releasing the approval for another build - as
# soon as a MATCHING build event was READABLE in the ledger. That is unsound, because
# readable is not durable:
#
#   * A ledger flush()/fsync() failure can leave a COMPLETE, perfectly parseable JSON line
#     whose durability was never confirmed. On restart it is indistinguishable from a
#     properly persisted event, so it looked like proof of a finished build and authorised
#     minting a second package from the same approval.
#   * A torn (partial) append can leave malformed JSONL behind instead.
#
# Acknowledging the acknowledgement cannot fix this: whatever confirms the ledger would
# itself need confirming. So the ledger simply stops being authority over approval reuse:
#
#   Once ANY reservation object for an approval exists - or may exist - that approval is
#   TERMINALLY CONSUMED for package-building purposes.
#
# This holds whether the reservation is confirmed durable, durability-uncertain, reconciled
# to a `build` event, reconciled to a `build_cleanup_incomplete` event, unmatched,
# malformed, foreign, accompanied by a complete-but-unconfirmed ledger line, or accompanied
# by a torn one. The ledger remains the audit record; it no longer grants reuse authority.
#
# Consequently exactly ONE slot is ever created. The bounded scan window is kept so a slot
# left by an earlier tool version still blocks the approval, and so slot discovery stays a
# fixed, small number of direct path lookups rather than a directory scan.
RESERVATION_ATTEMPT = 1
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

    ``state`` is one of:

    ``"not_created"``  the tool POSITIVELY established that no reservation object exists, so
                       no approval state was consumed and a retry is safe. This is the only
                       retryable reservation outcome, and it is claimed only after a
                       non-following existence re-check.
    ``"consumed"``     an object definitely occupies the slot - the exclusive create lost to
                       a competitor. The approval is terminally consumed, never retryable.
    ``"uncertain"``    an entry may exist but is not confirmed durable, or its existence
                       could not be determined. Treated exactly like ``consumed``.

    A ``consumed`` or ``uncertain`` reservation is deliberately left in place: removing it
    would turn "maybe consumed" into "definitely free", which is the unsafe direction.
    """

    NOT_CREATED = "not_created"
    CONSUMED = "consumed"
    UNCERTAIN = "uncertain"

    def __init__(self, message, *, state):
        super().__init__(message)
        self.state = state


# The sanitised classifiers for a ledger the tool refuses to trust. Each names the SHAPE of
# the defect only: no ledger content, no member value, no credential and no absolute path is
# ever derived from the file for reporting.
LEDGER_INTEGRITY_REASONS = (
    "unreadable",            # the file exists but could not be read (OSError)
    "undecodable_text",      # the bytes are not valid UTF-8
    "torn_final_record",     # the last record is not newline-terminated: a partial append
    "malformed_json_record",  # a record is not parseable JSON
    "invalid_record_shape",  # a record parses but is not a JSON object
    "unknown_event_type",    # a record's event is missing or not a supported audit event
    "decision_field_set_mismatch",     # a decision record has missing/extra fields
    "decision_field_invalid",          # a decision field has the wrong type or format
    "publication_field_set_mismatch",  # a build record has missing/extra fields
    "publication_field_invalid",       # a build field has the wrong type or format
)


class LedgerIntegrityError(ApprovalError):
    """The approval ledger could not be read as a structurally intact append-only record.

    Raised for a torn (partially appended) final record, malformed JSON, a record that is
    not a JSON object, and read/decode failures. Every one of these means the recorded
    decisions can no longer be trusted, so the build refuses fail-closed instead of
    tracebacking out of an uncontrolled ``json`` decoding failure.

    The malformed record is never silently discarded, and the ledger is never truncated,
    repaired, rewritten or replaced: recovery is a controlled reconciliation under review.
    ``reason`` is one of ``LEDGER_INTEGRITY_REASONS`` and carries no ledger content.
    """

    def __init__(self, message, *, reason):
        super().__init__(message)
        self.reason = reason


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# JSONL AUDIT-RECORD SCHEMAS
#
# The JSONL ledger is an append-only AUDIT RECORD ONLY - it never grants package-building
# authority (that is the transactional decision store's job). It is still validated
# strictly, because rejecting only malformed JSON and non-object JSON left arbitrary
# DICTIONARIES trusted: a malformed decision or publication dictionary reached downstream
# timestamp parsing and field lookups, producing uncontrolled exceptions or partially
# trusted state.
#
# Every shape below is one this repository has actually produced. Nothing is accepted "for
# compatibility" that the tool never wrote.
#
#   decision                  - unchanged across every version: exactly these 10 fields.
#   build                     - the 8 core fields every version wrote, plus
#                               `reservation_file_name` from Amendment 4 onwards.
#   build_cleanup_incomplete  - the same, plus `cleanup_incomplete` and
#                               `stale_temp_basename` (introduced with Amendment 3).
DECISION_AUDIT_FIELDS = frozenset({
    "event", "recorded_at", "reviewer_id", "decision", "source_record_id",
    "source_fingerprint", "row_number_hint", "approval_id", "approved_at", "expires_at",
})
PUBLICATION_AUDIT_REQUIRED = frozenset({
    "event", "recorded_at", "source_record_id", "source_fingerprint", "approval_id",
    "operation_id", "bound_package_payload_hash", "package_file_name",
})
PUBLICATION_AUDIT_OPTIONAL = frozenset({"reservation_file_name"})
CLEANUP_AUDIT_REQUIRED = frozenset({"cleanup_incomplete", "stale_temp_basename"})

LEDGER_DECISION_VALUES = ("approved", "rejected", "hold")

# A generous upper bound on a spreadsheet row hint. Its purpose is to bound the type check,
# not to model any real sheet.
MAX_ROW_NUMBER_HINT = 1_048_576


def _is_plain_int(value):
    """True for a real integer. ``bool`` is an ``int`` subclass and is not accepted."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_audit_timestamp(value):
    """A safe-charset ISO-8601 timestamp that actually parses."""
    if not isinstance(value, str) or not contract.SAFE_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _matches(value, pattern):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _validate_decision_audit(record):
    """Return a sanitised reason when this decision audit record is not exactly the shape
    the tool writes, else None."""
    if set(record) != DECISION_AUDIT_FIELDS:
        return "decision_field_set_mismatch"
    if record["decision"] not in LEDGER_DECISION_VALUES:
        return "decision_field_invalid"
    if not _matches(record["reviewer_id"], contract.REVIEWER_ID_RE):
        return "decision_field_invalid"
    if not _matches(record["source_record_id"], contract.SOURCE_RECORD_ID_RE):
        return "decision_field_invalid"
    if not _matches(record["source_fingerprint"], contract.SOURCE_FINGERPRINT_RE):
        return "decision_field_invalid"
    if not (_is_plain_int(record["row_number_hint"])
            and 2 <= record["row_number_hint"] <= MAX_ROW_NUMBER_HINT):
        return "decision_field_invalid"
    if not _valid_audit_timestamp(record["recorded_at"]):
        return "decision_field_invalid"
    if record["decision"] == "approved":
        # An approval audit record must carry a well-formed approval id and two parseable
        # timestamps; an unparseable expiry previously reached `datetime.fromisoformat` in
        # the build path and escaped as an uncontrolled ValueError.
        if not _matches(record["approval_id"], contract.APPROVAL_ID_RE):
            return "decision_field_invalid"
        if not (_valid_audit_timestamp(record["approved_at"])
                and _valid_audit_timestamp(record["expires_at"])):
            return "decision_field_invalid"
    elif not (record["approval_id"] is None
              and record["approved_at"] is None
              and record["expires_at"] is None):
        # A rejection or hold grants nothing, so it must carry no approval fields at all.
        return "decision_field_invalid"
    return None


def _validate_publication_audit(record):
    """Return a sanitised reason when this publication audit record is not exactly a shape
    the tool writes, else None."""
    required = set(PUBLICATION_AUDIT_REQUIRED)
    allowed = required | PUBLICATION_AUDIT_OPTIONAL
    if record["event"] == "build_cleanup_incomplete":
        required |= CLEANUP_AUDIT_REQUIRED
        allowed |= CLEANUP_AUDIT_REQUIRED
    present = set(record)
    if not required <= present or not present <= allowed:
        return "publication_field_set_mismatch"
    if not _matches(record["approval_id"], contract.APPROVAL_ID_RE):
        return "publication_field_invalid"
    if not _matches(record["operation_id"], contract.OPERATION_ID_RE):
        return "publication_field_invalid"
    if not _matches(record["bound_package_payload_hash"], contract.PAYLOAD_HASH_RE):
        return "publication_field_invalid"
    if not _matches(record["package_file_name"], contract.SAFE_BASENAME_RE):
        return "publication_field_invalid"
    if not _matches(record["source_record_id"], contract.SOURCE_RECORD_ID_RE):
        return "publication_field_invalid"
    if not _matches(record["source_fingerprint"], contract.SOURCE_FINGERPRINT_RE):
        return "publication_field_invalid"
    if not _valid_audit_timestamp(record["recorded_at"]):
        return "publication_field_invalid"
    if "reservation_file_name" in present and not _matches(
        record["reservation_file_name"], contract.SAFE_BASENAME_RE
    ):
        return "publication_field_invalid"
    if record["event"] == "build_cleanup_incomplete":
        if record["cleanup_incomplete"] is not True:
            return "publication_field_invalid"
        if not _matches(record["stale_temp_basename"], contract.SAFE_BASENAME_RE):
            return "publication_field_invalid"
    return None


def _validate_ledger_record(record):
    """Classify one audit record against the exact supported schemas.

    Returns None when the record matches a supported shape, otherwise a sanitised reason.
    The offending record is never returned, logged or printed.
    """
    event = record.get("event")
    if event == "decision":
        return _validate_decision_audit(record)
    if event in BUILD_LEDGER_EVENTS:
        return _validate_publication_audit(record)
    return "unknown_event_type"


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
    """Read the append-only ledger, failing closed on ANY structural integrity defect.

    A missing ledger is legitimately empty. Anything else that cannot be read as an intact
    sequence of newline-terminated JSON objects raises ``LedgerIntegrityError`` so the
    caller can emit an explicit sanitised non-success instead of letting a ``JSONDecodeError``
    escape as an uncontrolled traceback.

    ``append_ledger`` always terminates a record with a newline, so an unterminated tail is
    positive evidence of a torn append: those bytes are a truncated prefix of a real record
    and are rejected even in the rare case where the prefix happens to parse.

    Nothing here truncates, repairs, rewrites or replaces the ledger, and no malformed record
    is skipped: the defect is reported, and the file is left exactly as found for a
    controlled recovery.
    """
    path = Path(ledger_path)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise LedgerIntegrityError(
            "The approval ledger is not valid UTF-8, so its decisions cannot be trusted; "
            "refuse fail-closed and leave the file untouched.",
            reason="undecodable_text",
        ) from error
    except OSError as error:
        raise LedgerIntegrityError(
            "The approval ledger exists but could not be read, so its decisions cannot be "
            "trusted; refuse fail-closed and leave the file untouched.",
            reason="unreadable",
        ) from error
    if text and not text.endswith("\n"):
        raise LedgerIntegrityError(
            "The approval ledger ends with an unterminated record, so a prior append was "
            "torn; refuse fail-closed and leave the file untouched for controlled recovery.",
            reason="torn_final_record",
        )
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise LedgerIntegrityError(
                "The approval ledger contains a record that is not parseable JSON; refuse "
                "fail-closed and leave the file untouched for controlled recovery.",
                reason="malformed_json_record",
            ) from error
        if not isinstance(record, dict):
            raise LedgerIntegrityError(
                "The approval ledger contains a record that is not a JSON object; refuse "
                "fail-closed and leave the file untouched for controlled recovery.",
                reason="invalid_record_shape",
            )
        problem = _validate_ledger_record(record)
        if problem is not None:
            raise LedgerIntegrityError(
                "The approval ledger contains a record that does not match a supported "
                "audit event schema; refuse fail-closed and leave the file untouched for "
                "controlled recovery.",
                reason=problem,
            )
        entries.append(record)
    return entries


def _publication_events_for(entries, approval_id, events):
    """Ledger publication events attributable to THIS approval.

    Single use is a property of the APPROVAL, not of the source record. The documented
    recovery from every blocked state is a fresh reviewer decision, which mints a new approval
    id; keying these guards on the source record instead would make one publication block that
    member's row forever and leave no recovery path at all.

    Every version of this tool that has written a publication event recorded a well-formed
    ``approval_id``, and audit-schema validation now rejects any publication record without
    one, so matching on the approval id alone covers the whole history.
    """
    return [
        entry for entry in entries
        if entry.get("event") in events and entry.get("approval_id") == approval_id
    ]


def build_already_exists(entries, approval_id):
    """True when the ledger already records a publication for this approval - clean OR
    published-with-incomplete-cleanup - so a cleanup failure is never a loophole.

    SECONDARY guard only. The reservation is the authority (see the terminal reservation
    rule); this catches a ledger whose reservation object is absent, for example one recorded
    by a tool version that predates reservations.
    """
    return bool(_publication_events_for(entries, approval_id, BUILD_LEDGER_EVENTS))


def published_cleanup_incomplete_exists(entries, approval_id):
    """True if a prior publication for this approval succeeded but its temporary cleanup did
    not complete. Such an operation must not be retried as a new package build: the package
    WAS published, and it requires manual temporary cleanup plus a fresh reviewer decision."""
    return bool(
        _publication_events_for(entries, approval_id, CLEANUP_INCOMPLETE_LEDGER_EVENTS)
    )


def _write_private_marker(directory):
    marker = Path(directory) / PRIVATE_MARKER_NAME
    if not marker.exists():
        marker.write_text(PRIVATE_MARKER_TEXT, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Durable publication reservation
# --------------------------------------------------------------------------- #
class _ReservationStatus:
    """How one existing reservation relates to the durable ledger.

    DIAGNOSTIC ONLY. Every status below blocks a further build for that approval; the
    distinction exists so the operator knows what a controlled recovery has to look at. In
    particular ``RECONCILED_BUILD`` does NOT release the approval - see the terminal
    reservation rule above.
    """

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


def _slot_absence_state(path):
    """Classify a reservation slot after a failed create, WITHOUT following links.

    ``not_created`` - and with it the only retryable reservation outcome - is returned solely
    when ``os.path.lexists`` positively reports that no object occupies the exact slot path.
    If an object is there, or existence cannot be determined at all, the approval must be
    treated as consumed/uncertain. This is a single direct path lookup: the state directory is
    never listed, globbed or swept.
    """
    try:
        if os.path.lexists(path):
            return ReservationError.CONSUMED
    except OSError:
        return ReservationError.UNCERTAIN
    return ReservationError.NOT_CREATED


def write_reservation(path, record):
    """Exclusively create ONE durable publication reservation; return the durability mode.

    Exclusive: ``O_CREAT | O_EXCL`` means exactly one build attempt can ever own a slot, so
    two concurrent builds for the same approval cannot both proceed to publication.

    Durable: the record is written, flushed and fsynced, then the parent directory entry is
    synced where the platform supports it.

    Fail-closed: raises ``ReservationError``. Only ``state="not_created"`` is retryable, and
    it is claimed solely when a non-following existence re-check positively proves no
    reservation object is present. ``state="consumed"`` and ``state="uncertain"`` entries are
    deliberately NOT deleted, truncated or rewritten, so the approval stays blocked until a
    controller reconciles it.
    """
    try:
        # Converted rather than propagated: a bare ContractError would escape the publication
        # writer's typed handlers and leave the operation-owned temporary file uncleaned.
        path = contract.assert_safe_local_path(path)
    except contract.ContractError as error:
        raise ReservationError(
            "The durable publication reservation path is unsafe (for example a reparse "
            "point created concurrently); refuse fail-closed and publish nothing.",
            state=_slot_absence_state(path),
        ) from error
    body = json.dumps(record, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        # An object DEFINITELY occupies the slot: the exclusive create lost to a competitor
        # that already reserved this approval. Reporting this as `not_created` (retryable) was
        # false - the competing reservation terminally consumed the approval, so retry
        # guidance must never be emitted and the competitor is left untouched.
        raise ReservationError(
            "A durable publication reservation already exists for this approval; the "
            "approval is terminally consumed by the competing owner. Refuse fail-closed, "
            "publish nothing and leave the competitor untouched.",
            state=ReservationError.CONSUMED,
        ) from error
    except OSError as error:
        # Any other create failure: the slot may or may not have been created. Re-check the
        # exact path without following links before claiming anything is retryable.
        raise ReservationError(
            "The durable publication reservation could not be created; refuse fail-closed "
            "and publish nothing.",
            state=_slot_absence_state(path),
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
            state=ReservationError.UNCERTAIN,
        ) from error
    try:
        return _fsync_parent_directory(path.parent)
    except OSError as error:
        raise ReservationError(
            "The durable publication reservation was written but its directory entry could "
            "not be made durable; refuse fail-closed and publish nothing.",
            state=ReservationError.UNCERTAIN,
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
    """Classify one reservation record against the ledger entries, for reporting only.

    A ledger match is evidence about what a prior attempt did, not permission to reuse the
    approval: a matching event may itself be a readable-but-never-fsynced line. Callers must
    treat every classification as blocking.
    """
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
    """Return ``(basename, status)`` for EVERY reservation slot this approval already has.

    A non-empty result means the approval is terminally consumed. Under the terminal
    reservation rule the mere existence of a reservation object consumes the approval, so the
    classified ``status`` is diagnostic detail that shapes the operator's recovery decision -
    never authority to build again. A cleanly reconciled ``build`` status therefore blocks
    exactly as firmly as an unmatched, malformed or foreign one.

    Only paths derived from THIS approval's id are examined, so the state directory is never
    listed, globbed or swept and unrelated reservations, packages, ledgers and temporary
    files are never read or touched. Slots beyond the single one this tool now creates are
    still inspected, so a reservation left by an earlier tool version also blocks.
    """
    consumed = []
    for attempt in range(1, RESERVATION_MAX_ATTEMPTS + 1):
        path = reservation_path(state_dir, approval_id, attempt)
        if not os.path.lexists(path):
            continue
        status = reservation_reconciliation(
            entries, read_reservation(path), approval_id, source_record_id
        )
        consumed.append((path.name, status))
    return consumed


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_decision(args, decision):
    """Record one controlled reviewer decision through the transactional authority store.

    Three ordered, separately committed steps. A decision becomes AUTHORITATIVE only at the
    end of step 3; until then it is durably recorded but grants nothing:

      1. commit the PENDING decision row transactionally;
      2. append the JSONL AUDIT event (audit evidence only, never authority);
      3. commit the separate ACTIVATION row, only after step 2 returned confirmed success.

    Every commit failure is resolved by closing the connection, REOPENING the database and
    looking for the exact row - never by inferring the outcome from the exception, which
    proves only that the client did not hear the answer.
    """
    payload, desired, srid, fingerprint = resolve_source_row(
        args.input, args.decision_rows, args.row_number
    )
    # Confirm the audit ledger is structurally intact BEFORE appending to it. Appending onto
    # an unterminated torn record would splice the new decision into those bytes, turning one
    # recoverable partial record into a single unrecoverable malformed line - a silent loss
    # of audit evidence. Nothing is read out of the ledger here for authority purposes.
    read_ledger(args.ledger)

    now = utc_now_iso()
    decision_id = "dec_" + uuid.uuid4().hex
    record = {
        "decision_id": decision_id,
        "decision_type": decision,
        "reviewer_id": args.reviewer,
        "recorded_at": now,
        "approval_id": None,
        "approved_at": None,
        "expires_at": None,
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "schema_version": decisions.SCHEMA_VERSION,
    }
    if decision == "approved":
        ttl_hours = args.ttl_hours if args.ttl_hours is not None else contract.DEFAULT_APPROVAL_TTL_HOURS
        if ttl_hours <= 0 or ttl_hours > contract.DEFAULT_APPROVAL_TTL_HOURS:
            raise ApprovalError(
                "Approval TTL must be positive and no longer than the contract default."
            )
        expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        record["approval_id"] = "appr_" + uuid.uuid4().hex
        record["approved_at"] = now
        record["expires_at"] = expires.isoformat(timespec="seconds")
    record["record_hash"] = decisions.decision_record_hash(record)

    store = decisions.store_path_for(args.ledger)
    common = {
        "decision": decision,
        "decision_id": decision_id,
        "reviewer_id": args.reviewer,
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "approval_id": record["approval_id"],
        "expires_at": record["expires_at"],
    }

    # ---- STEP 1: commit the PENDING decision transactionally ------------------------- #
    # A reviewer decision command legitimately establishes the store on first use.
    conn = decisions.open_store(store, create=True)
    commit_state = decisions.CommitState.COMMITTED
    try:
        decisions.insert_pending_decision(conn, record)
    except (sqlite3.Error, OSError):
        commit_state = None
    finally:
        conn.close()
    if commit_state is None:
        commit_state, _row = decisions.recover_decision_commit(
            store, decision_id, record["record_hash"]
        )
    if commit_state == decisions.CommitState.ABSENT:
        # Nothing committed: no decision exists at all, so a clean retry is safe.
        _print_summary(
            dict(
                common,
                status="decision_not_recorded",
                event="none",
                decision_authority="none",
                decision_activated=False,
                decision_recorded=False,
                audit_append="not_attempted",
                approval_blocked=False,
                do_not_retry=False,
                recovery="resolve_decision_store_failure_then_retry",
            )
        )
        return EXIT_DECISION_AUTHORITY_UNCERTAIN
    if commit_state == decisions.CommitState.UNCERTAIN:
        raise decisions.DecisionStoreError(
            "The pending reviewer decision commit could not be resolved by reopening the "
            "decision store; refuse fail-closed and require controlled recovery.",
            reason="commit_state_uncertain",
        )

    sequence = _decision_sequence(store, decision_id)

    # ---- STEP 2: append the JSONL AUDIT event ---------------------------------------- #
    # Audit evidence only. A readable line here never activates the decision; activation is
    # the separate committed row in step 3.
    audit_entry = {
        "event": "decision",
        "recorded_at": now,
        "reviewer_id": args.reviewer,
        "decision": decision,
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "row_number_hint": args.row_number,
        "approval_id": record["approval_id"],
        "approved_at": record["approved_at"],
        "expires_at": record["expires_at"],
    }
    try:
        append_ledger(args.ledger, audit_entry)
    except (OSError, contract.ContractError):
        # The audit append did not return confirmed success. The pending decision is NOT
        # activated, NOT deleted and NOT rewritten; the ledger is not truncated, replaced or
        # repaired. Even a complete, readable line left behind by a flush/fsync failure
        # leaves the decision pending and non-authoritative.
        _print_summary(
            dict(
                common,
                status="decision_audit_incomplete",
                event="none",
                decision_authority="pending",
                decision_activated=False,
                decision_recorded=True,
                decision_sequence=sequence,
                audit_append="unconfirmed",
                approval_blocked=True,
                do_not_retry=True,
                fresh_approval_required=True,
                controlled_recovery_required=True,
                recovery="controlled_reconciliation_then_fresh_decision",
            )
        )
        return EXIT_DECISION_AUTHORITY_UNCERTAIN

    # ---- STEP 3: commit the ACTIVATION row ------------------------------------------- #
    activated_at = utc_now_iso()
    activation_hash = decisions.activation_record_hash(
        decision_id, activated_at, record["record_hash"]
    )
    conn = decisions.open_store(store, create=False)
    activation_state = decisions.CommitState.COMMITTED
    try:
        decisions.insert_activation(conn, decision_id, activated_at, activation_hash)
    except (sqlite3.Error, OSError):
        activation_state = None
    finally:
        conn.close()
    if activation_state is None:
        activation_state, _row = decisions.recover_activation_commit(
            store, decision_id, activation_hash
        )
    if activation_state == decisions.CommitState.UNCERTAIN:
        raise decisions.DecisionStoreError(
            "The reviewer decision activation commit could not be resolved by reopening the "
            "decision store; refuse fail-closed and require controlled recovery.",
            reason="activation_state_uncertain",
        )
    if activation_state == decisions.CommitState.ABSENT:
        # The audit line is durable but no activation committed, so the decision remains
        # recorded-but-not-authoritative. It is never promoted implicitly.
        _print_summary(
            dict(
                common,
                status="decision_not_activated",
                event="decision",
                decision_authority="pending",
                decision_activated=False,
                decision_recorded=True,
                decision_sequence=sequence,
                audit_append="confirmed",
                approval_blocked=True,
                do_not_retry=True,
                fresh_approval_required=True,
                controlled_recovery_required=True,
                recovery="controlled_reconciliation_then_fresh_decision",
            )
        )
        return EXIT_DECISION_AUTHORITY_UNCERTAIN

    # Ordinary success: pending row committed, audit append confirmed, activation committed
    # (or transactionally recovered as committed by reopening the database).
    _print_summary(
        dict(
            common,
            status="ok",
            event="decision",
            decision_authority="activated",
            decision_activated=True,
            decision_recorded=True,
            decision_sequence=sequence,
            audit_append="confirmed",
            decision_durability="transactional_commit_synchronous_full",
        )
    )
    return 0


def _decision_sequence(store, decision_id):
    """The monotonic sequence assigned to a committed decision, read on a fresh connection.

    A read failure here cannot change what was committed, so it degrades to ``None`` in the
    report rather than turning a completed decision into a failure.
    """
    try:
        conn = decisions.open_store(store, create=False)
    except decisions.DecisionStoreError:
        return None
    try:
        return decisions.decision_sequence(conn, decision_id)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def cmd_build_package(args):
    # ---- RETIRED SAME-APPROVAL --rebuild ------------------------------------------------ #
    # Refused first, before any private source row is read and before any filesystem object
    # is created, inspected or touched. Under the terminal reservation rule a second package
    # for one approval can never be authorised, so the flag has no remaining meaning and is
    # rejected outright rather than left as a path that sometimes appears to work. Refusing
    # here guarantees no reservation slot, no temporary file, no output path and no ledger
    # event is created or modified.
    if args.rebuild:
        _print_summary(
            {
                "status": "rebuild_requires_fresh_approval",
                "event": "none",
                "publication": "not_attempted",
                "reservation": "not_attempted",
                "rebuild_supported": False,
                "approval_blocked": True,
                "do_not_retry": True,
                "fresh_approval_required": True,
                "recovery": "fresh_reviewer_decision_new_approval_id_and_fresh_output_path",
            }
        )
        return EXIT_APPROVAL_CONSUMED

    payload, desired, srid, fingerprint = resolve_source_row(
        args.input, args.decision_rows, args.row_number
    )
    entries = read_ledger(args.ledger)

    # ---- TRANSACTIONAL DECISION AUTHORITY GATE --------------------------------------- #
    # Authority comes ONLY from a committed activation row in the decision store. A readable
    # JSONL decision line - including a complete one left by a flush/fsync failure - grants
    # nothing. A legacy ledger-only approval recorded before this store existed is therefore
    # not authority either: a fresh reviewer decision is required (deliberate compatibility
    # decision, documented in the runbook).
    #
    # `create=False`: build-package must never manufacture an empty store. A missing store
    # means no transactional approval authority exists.
    store = decisions.store_path_for(args.ledger)
    try:
        conn = decisions.open_store(store, create=False)
    except decisions.DecisionStoreError as error:
        if error.reason == "store_missing":
            _print_summary(
                {
                    "status": "decision_store_missing",
                    "event": "none",
                    "publication": "not_attempted",
                    "reservation": "not_attempted",
                    "decision_authority": "none",
                    "legacy_ledger_approval_accepted": False,
                    "approval_blocked": True,
                    "fresh_approval_required": True,
                    "recovery": "fresh_reviewer_decision",
                    "source_record_id": srid,
                }
            )
            return EXIT_DECISION_NOT_AUTHORITATIVE
        raise
    try:
        authority = decisions.resolve_authority(conn, srid)
    finally:
        conn.close()

    if authority.state != decisions.AuthorityState.APPROVED:
        # Every non-approved outcome refuses before any filesystem object is created or
        # touched: no package, no temporary, no reservation, no ledger event.
        pending = authority.state == decisions.AuthorityState.PENDING_NEWER
        _print_summary(
            {
                "status": (
                    "decision_pending_or_uncertain" if pending
                    else "no_activated_decision"
                    if authority.state == decisions.AuthorityState.NONE
                    else "decision_not_approved"
                ),
                "event": "none",
                "publication": "not_attempted",
                "reservation": "not_attempted",
                "decision_authority": authority.state,
                "decision_activated": authority.activated_sequence is not None,
                "newer_pending_decisions": len(authority.pending_sequences),
                "legacy_ledger_approval_accepted": False,
                "approval_blocked": True,
                # A pending decision may have been an attempted hold or rejection, so it must
                # never fall back to an older activated approval.
                "do_not_retry": pending,
                "controlled_recovery_required": pending,
                "fresh_approval_required": True,
                "recovery": "fresh_reviewer_decision",
                "source_record_id": srid,
            }
        )
        return EXIT_DECISION_NOT_AUTHORITATIVE

    decision = authority.decision
    if decision["source_fingerprint"] != fingerprint:
        raise ApprovalError(
            "The source data changed after approval (fingerprint mismatch); approval is invalid."
        )
    # `resolve_authority` has already proven this timestamp parses and the approval id is
    # well-formed, so neither check can escape as an uncontrolled exception here.
    if datetime.fromisoformat(decision["expires_at"]) <= datetime.now(timezone.utc):
        raise ApprovalError("The approval has expired; re-approval is required.")
    approval_id = decision["approval_id"]

    # ---- TERMINAL RESERVATION GATE ------------------------------------------------------ #
    # Any reservation object that exists for this approval terminally consumes it, whatever
    # its reconciliation status. A prior attempt may already have published a package - and a
    # readable ledger build event is NOT proof that the attempt finished durably - so the
    # approval can never mint a second package. The reported status is recovery guidance
    # only. This gate precedes every ledger-based check because it is the authority.
    state_dir = contract.assert_safe_local_path(args.ledger).parent
    consumed = survey_reservations(state_dir, approval_id, srid, entries)
    if consumed:
        _print_summary(
            {
                "status": "approval_consumed",
                "event": "none",
                "publication": "not_attempted",
                "reservation": "terminally_consumed",
                "consumed_reservations": [
                    {"reservation_basename": name, "reservation_status": status}
                    for name, status in consumed
                ],
                "approval_blocked": True,
                "do_not_retry": True,
                "rebuild_blocked": True,
                "rebuild_supported": False,
                "fresh_approval_required": True,
                # A published-but-unclean prior attempt additionally leaves a stray temporary
                # for the operator to remove by hand; its basename was reported by that run.
                "manual_temp_cleanup_required": published_cleanup_incomplete_exists(
                    entries, approval_id
                ),
                "recovery": "fresh_approval_or_controlled_recovery",
                "source_record_id": srid,
            }
        )
        return EXIT_APPROVAL_CONSUMED

    # Secondary ledger guards, reached only when NO reservation object exists for this
    # approval - i.e. legacy state recorded by a tool version that predates reservations.
    # The approval must stay single-use even then, and --rebuild no longer bypasses either
    # guard because it no longer exists as a build path at all.
    if published_cleanup_incomplete_exists(entries, approval_id):
        raise ApprovalError(
            "A prior package for this approval was published but its temporary cleanup did "
            "not complete; this operation must not be retried as a new package build. "
            "Complete the manual temporary cleanup and start a fresh reviewer decision."
        )
    if build_already_exists(entries, approval_id):
        raise ApprovalError(
            "A package was already built for that approval; refuse fail-closed. A further "
            "package requires a fresh reviewer decision, a new approval id and a fresh "
            "output path."
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
        "attempt": RESERVATION_ATTEMPT,
        "approval_id": approval_id,
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "operation_id": package["operation_id"],
        "bound_package_payload_hash": payload_hash,
        "package_file_name": package_file_name,
    }
    reservation_target = reservation_path(state_dir, approval_id, RESERVATION_ATTEMPT)
    reservation_basename = reservation_target.name

    result = _write_package_atomically(
        args.package_out,
        package,
        lambda: write_reservation(reservation_target, reservation_record),
    )

    if result.state == _PublishState.RESERVATION_FAILED:
        # Nothing was published (the reservation boundary is strictly before os.link), so no
        # final path was created and no competitor was touched.
        #
        # `not_created` is the ONLY retryable reservation outcome, and it is claimed solely
        # when a non-following existence re-check positively proved the slot is empty.
        #
        # `consumed` means a competitor's reservation definitely occupies the slot, which
        # terminally consumed this approval - it was previously mislabelled `not_created`, so
        # this invocation emitted retry guidance for an approval that must never be retried.
        # `uncertain` means an entry may exist, which is the same terminal condition. Neither
        # is ever deleted, recreated or retried, and the terminal gate blocks every later
        # invocation.
        # Exit codes follow the states exactly: `consumed` is the documented
        # terminally-consumed approval (exit 7); `uncertain` is a reservation created without
        # confirmed durability (exit 5, still blocked); `not_created` is the sole retryable
        # outcome (exit 5).
        state = result.reservation_state
        consumed = state == ReservationError.CONSUMED
        blocked = state != ReservationError.NOT_CREATED
        _print_summary(
            {
                "status": "approval_consumed" if consumed else "reservation_incomplete",
                "event": "none",
                "publication": "not_published",
                "reservation": state,
                "reservation_basename": reservation_basename,
                "reservation_durability": "unconfirmed",
                "competing_reservation_preserved": consumed,
                "temp_cleanup": "failed" if result.temp_stale else "complete",
                "stale_temp_basename": result.temp_basename if result.temp_stale else None,
                "manual_cleanup_required": bool(result.temp_stale),
                "approval_blocked": blocked,
                "do_not_retry": blocked,
                "fresh_approval_required": blocked,
                "recovery": (
                    "fresh_approval_or_controlled_recovery"
                    if blocked
                    else "resolve_reservation_failure_then_retry"
                ),
                "source_record_id": srid,
                "operation_id": package["operation_id"],
            }
        )
        return EXIT_APPROVAL_CONSUMED if consumed else EXIT_RESERVATION_INCOMPLETE

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
            # reservation - written before publication - terminally consumed this approval,
            # so it cannot build again at ANY fresh path, by any invocation.
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
            "RETIRED and always refused. A reservation terminally consumes its approval, so "
            "no second package can ever be built from one approval. Passing this flag "
            "returns rebuild_requires_fresh_approval and creates nothing: no reservation, no "
            "temporary file, no output file and no ledger event. A further package needs a "
            "fresh reviewer decision, a new approval id and a fresh output pathname."
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
    except LedgerIntegrityError as error:
        # Handled before the generic arm (it is an ApprovalError subclass) so a torn or
        # malformed ledger produces an explicit sanitised terminal state rather than a
        # traceback or a generic error that an operator might retry. Nothing was created,
        # nothing was published, no reservation was created or deleted, no package was
        # modified, and the ledger is left exactly as found. Only the shape classifier is
        # reported - never a ledger record, a member value, a credential or an absolute path.
        _print_summary(
            {
                "status": "ledger_integrity_uncertain",
                "event": "none",
                "publication": "not_attempted",
                "reservation": "not_attempted",
                "ledger_integrity": error.reason,
                "ledger_modified": False,
                "approval_blocked": True,
                "do_not_retry": True,
                "controlled_recovery_required": True,
                "fresh_approval_required": True,
                "recovery": "controlled_ledger_reconciliation_then_fresh_approval",
            }
        )
        return EXIT_LEDGER_INTEGRITY_UNCERTAIN
    except decisions.DecisionStoreError as error:
        # The transactional decision store could not be trusted, or a commit outcome could
        # not be resolved by reopening the database. Explicit sanitised terminal state, never
        # a traceback and never a generic error an operator might retry. Nothing was created,
        # published, activated or repaired: the store and the ledger are left exactly as
        # found, and only the shape classifier is reported - never a row, a member value, a
        # credential or an absolute path.
        _print_summary(
            {
                "status": "decision_store_integrity_uncertain",
                "event": "none",
                "publication": "not_attempted",
                "reservation": "not_attempted",
                "decision_store_integrity": error.reason,
                "decision_authority": "uncertain",
                "decision_store_modified": False,
                "ledger_modified": False,
                "approval_blocked": True,
                "do_not_retry": True,
                "controlled_recovery_required": True,
                "fresh_approval_required": True,
                "recovery": "controlled_decision_store_reconciliation_then_fresh_decision",
            }
        )
        return EXIT_DECISION_AUTHORITY_UNCERTAIN
    except (ApprovalError, contract.ContractError, validator.FormContractError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2, sort_keys=True))
        return 2
    raise ApprovalError("Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
