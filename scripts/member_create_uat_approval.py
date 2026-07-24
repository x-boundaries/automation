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
    _write_private_marker(ledger_path.parent)


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
    return any(
        entry.get("event") == "build" and entry.get("source_record_id") == source_record_id
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

    _write_package_atomically(args.package_out, package)

    append_ledger(
        args.ledger,
        {
            "event": "build",
            "recorded_at": utc_now_iso(),
            "source_record_id": srid,
            "source_fingerprint": fingerprint,
            "approval_id": decision["approval_id"],
            "operation_id": package["operation_id"],
            "bound_package_payload_hash": payload_hash,
            "package_file_name": Path(args.package_out).name,
        },
    )
    _print_summary(
        {
            "status": "ok",
            "event": "build",
            "source_record_id": srid,
            "operation_id": package["operation_id"],
            "payload_hash": payload_hash,
            "package_file_name": Path(args.package_out).name,
            "assign_fields_count": len(contract.ASSIGNABLE_FIELDS),
            # This is the LAPTOP-side package builder: it only records that ExpiryDate is
            # present in the immutable package payload. It performs no AutoCount member
            # assignment, so it must never emit `expiry_date_assigned` (that field is
            # reserved for the VM runner's sanitised result, set only after the assignment
            # loop has actually completed successfully).
            "expiry_date_in_payload": True,
        }
    )
    return 0


def _unlink_quietly(path):
    """Best-effort remove of a path we own (a temporary file); never raises."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_package_atomically(package_out, package):
    """Publish the immutable package with BOTH strict no-clobber AND atomic visibility.

    Strict no-clobber: an existing final path (a historical package, e.g. a prior v1
    artifact) is NEVER deleted, truncated, replaced, renamed away, or overwritten. A
    concurrent creator of the final path wins safely.

    Atomic publication: the complete package is written to a unique temporary file in the
    SAME directory (exclusive-created, non-PII name), flushed and fsynced, then published
    at the final path with ``os.link`` - a no-replace hard link that atomically exposes
    the already-complete inode and fails closed if the final path exists. A reader never
    observes a partial file at the final pathname, and a crash cannot leave a partial
    FINAL package (only a stray temporary, which never blocks a future build). If the
    filesystem cannot provide the atomic no-replace link, the build fails closed and only
    the temporary file is cleaned - it never falls back to progressive writing at the
    final path. ``assert_safe_local_path`` rejects reparse points; the ``lexists`` check
    rejects directories, symlinks, and any other occupied final entry.
    """
    out_path = contract.assert_safe_local_path(package_out)
    if os.path.lexists(out_path):
        raise ApprovalError(
            "The package output path already exists; refuse fail-closed to preserve the "
            "existing package (never overwritten). Choose a fresh, version-distinct "
            "output filename."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temporary file in the same directory. mkstemp exclusive-creates it with a
    # random, PII-free name and 0600 permissions where the platform supports them.
    fd, temp_name = tempfile.mkstemp(dir=str(out_path.parent), prefix=".mcuat_pkg_", suffix=".tmp")
    temp_path = temp_name
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(package, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        # Atomic no-clobber publication of the COMPLETE temporary file.
        try:
            os.link(temp_path, out_path)
        except FileExistsError as error:
            # A competing final path appeared; it wins. Preserve it byte-for-byte.
            raise ApprovalError(
                "The package output path was created concurrently; refuse fail-closed to "
                "preserve the existing package (never overwritten)."
            ) from error
        except OSError as error:
            # No atomic no-replace link available on this filesystem: fail closed. Never
            # fall back to progressive writing at the final path.
            raise ApprovalError(
                "Atomic no-clobber package publication is unavailable on this filesystem; "
                "refuse fail-closed rather than write progressively at the final path."
            ) from error
    except BaseException:
        # Clean ONLY our temporary file on every unsuccessful path; never touch the final.
        _unlink_quietly(temp_path)
        raise
    # Publication succeeded: drop the temporary link so no stale temp remains.
    _unlink_quietly(temp_path)
    # Best-effort private marker; a marker failure must never fail a completed
    # publication or touch the package.
    try:
        _write_private_marker(out_path.parent)
    except OSError:
        pass


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
