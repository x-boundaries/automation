"""Laptop-side reviewer approval and immutable package builder for the
single-member creation UAT.

State ownership (per approved design):

* This tool runs on the LAPTOP DEVELOPMENT MACHINE. It owns the human decision
  record only: an append-only approval ledger recording controlled approve /
  reject / hold decisions bound to a stable source identity and a change-detection
  fingerprint, plus a record of which immutable package was built.
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

# Distinct nonzero exit code for a truthful "cleanup incomplete" terminal outcome, kept
# separate from ordinary error (2) and success (0) so a stale temporary package can never
# be mistaken for a clean build at the process level.
EXIT_CLEANUP_INCOMPLETE = 3

# Ledger events that mark an approval's package as already published (clean OR published
# with an incomplete temporary cleanup). Either blocks a plain re-build (single-use).
BUILD_LEDGER_EVENTS = ("build", "build_cleanup_incomplete")


class ApprovalError(ValueError):
    """Local approval/package contract failure."""


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
    if build_already_exists(entries, srid) and not args.rebuild:
        raise ApprovalError(
            "A package was already built for that source record; refuse (pass --rebuild only "
            "if the prior package was never sent to the VM)."
        )

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

    result = _write_package_atomically(args.package_out, package)
    package_file_name = Path(args.package_out).name

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

    common = {
        "source_record_id": srid,
        "source_fingerprint": fingerprint,
        "approval_id": decision["approval_id"],
        "operation_id": package["operation_id"],
        "bound_package_payload_hash": payload_hash,
        "package_file_name": package_file_name,
    }

    if result.state == _PublishState.PUBLISHED_CLEANUP_COMPLETE:
        append_ledger(args.ledger, dict(common, event="build", recorded_at=utc_now_iso()))
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
                # LAPTOP-side builder: records only that ExpiryDate is present in the
                # immutable package payload. It performs no AutoCount member assignment, so
                # it never emits `expiry_date_assigned` (that field is the VM runner's,
                # set only after the assignment loop actually completes).
                "expiry_date_in_payload": True,
            }
        )
        return 0

    # PUBLISHED_CLEANUP_INCOMPLETE: the final package IS published (committed boundary
    # crossed). Do not roll it back or claim ordinary success. Append exactly one durable
    # cleanup-incomplete publication event binding the published package to the approval
    # transaction, so the same approval cannot mint another package and later validation
    # cannot mistake this for a clean build.
    append_ledger(
        args.ledger,
        dict(
            common,
            event="build_cleanup_incomplete",
            recorded_at=utc_now_iso(),
            cleanup_incomplete=True,
            stale_temp_basename=result.temp_basename,
        ),
    )
    _print_summary(
        {
            "status": "cleanup_incomplete",
            "event": "build_cleanup_incomplete",
            "publication": "succeeded",
            "temp_cleanup": "failed",
            "manual_cleanup_required": True,
            "do_not_retry": True,
            "stale_temp_basename": result.temp_basename,
            "source_record_id": srid,
            "operation_id": package["operation_id"],
            "payload_hash": payload_hash,
            "package_file_name": package_file_name,
            "expiry_date_in_payload": True,
        }
    )
    return EXIT_CLEANUP_INCOMPLETE


class _PublishState:
    """The three publication/cleanup states of one package build. The distinction is
    material: pre-publication and post-publication cleanup failures leave different
    committed states and are handled separately by the caller."""

    NOT_PUBLISHED = "not_published"
    PUBLISHED_CLEANUP_COMPLETE = "published_cleanup_complete"
    PUBLISHED_CLEANUP_INCOMPLETE = "published_cleanup_incomplete"


class _PublishResult:
    """Outcome of one publication attempt. The writer NEVER silently swallows a
    temporary-cleanup failure: a failed unlink is surfaced via ``cleanup_error`` and the
    ``PUBLISHED_CLEANUP_INCOMPLETE`` / ``NOT_PUBLISHED``-with-stale-temp states."""

    def __init__(self, state, temp_basename=None, cause=None, cleanup_error=None):
        self.state = state
        self.temp_basename = temp_basename        # sanitised, PII-free basename or None
        self.cause = cause                        # original pre-publication failure (NOT_PUBLISHED)
        self.cleanup_error = cleanup_error        # temporary-unlink failure, if any
        self.temp_stale = cleanup_error is not None  # an operation-owned temp remains


def _write_package_atomically(package_out, package):
    """Publish the immutable package with strict no-clobber, atomic visibility, AND
    truthful temporary-file cleanup. Returns a ``_PublishResult``; the strict no-clobber
    precondition (an already-occupied final path) still raises before any temporary file
    is created.

    Strict no-clobber: an existing/competing final path (e.g. a historical v1 artifact)
    is NEVER deleted, truncated, replaced, renamed away, or overwritten; a concurrent
    creator wins safely.

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
    Never sweeps other ``.mcuat_pkg_*`` files.
    """
    out_path = contract.assert_safe_local_path(package_out)
    if os.path.lexists(out_path):
        raise ApprovalError(
            "The package output path already exists; refuse fail-closed to preserve the "
            "existing package (never overwritten). Choose a fresh, version-distinct "
            "output filename."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(out_path.parent), prefix=".mcuat_pkg_", suffix=".tmp")
    temp_basename = os.path.basename(temp_name)
    published = False
    cause = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(package, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        os.link(temp_name, out_path)  # atomic no-replace publication of the complete file
        published = True
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
    # touch the final/competing path; never sweep other temporaries. A failed unlink is
    # recorded (not swallowed) so the outcome stays truthful.
    cleanup_error = None
    try:
        os.unlink(temp_name)
    except OSError as unlink_err:
        cleanup_error = unlink_err

    if not published:
        # NOT_PUBLISHED: we never created the final path (a race competitor is untouched).
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
        return _PublishResult(_PublishState.PUBLISHED_CLEANUP_COMPLETE)
    return _PublishResult(_PublishState.PUBLISHED_CLEANUP_INCOMPLETE,
                          temp_basename=temp_basename, cleanup_error=cleanup_error)


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
            "only when --package-out is a fresh, absent path. It never overwrites an "
            "existing package (the output path is always no-clobber)."
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
