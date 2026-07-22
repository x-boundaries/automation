"""Aggregate-only precheck for the sanitized single-member creation UAT terminal
result, before it is mapped into controlled Google Sheet fields.

This mirrors the discipline of the consumed Gate 5A result precheck but is an
independent module for the create UAT: it opens the runner's sanitized result
JSON read-only, confirms the object carries only the allowed sanitized fields
(never raw member values), confirms the terminal code is in the controlled
vocabulary, and - when an expected identity is supplied - revalidates the stable
``source_record_id`` and the change-detection ``source_fingerprint`` so a result
can only ever be mapped onto the exact source record it belongs to. It prints
aggregate ``key = value`` lines only and never writes, performs no Google Sheets
access, no AutoCount call, no SQL, and no n8n action.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import member_create_uat_contract as contract  # noqa: E402

# The exact sanitized fields the VM runner emits. Anything else is unexpected.
ALLOWED_RESULT_FIELDS = {
    "mode",
    "runtime_location",
    "terminal_code",
    "package_structural_valid",
    "package_fingerprint_problem",
    "approval_not_expired",
    "write_confirmed",
    "business_confirmed",
    "lock_acquired",
    "already_consumed",
    "authentication_success",
    "member_command_found",
    "get_member_found",
    "member_exists_initial",
    "new_member_success",
    "assignment_success",
    "assigned_field_count",
    "expiry_date_assigned",
    "member_exists_recheck",
    "write_intent_recorded",
    "consumed_marker_written",
    "save_member_attempted",
    "save_member_confirmed",
    "save_outcome",
    "readback_found",
    "readback_match",
    "masked_member_no",
    "operation_id",
    "source_record_id",
    "source_fingerprint",
    "error",
}

# Fields that must never appear in a sanitized result (raw member/identity values).
FORBIDDEN_RESULT_FIELDS = {
    "member_payload",
    "desired_business_fields",
    "MemberNo",
    "Name",
    "EmailAddress",
    "MobilePhone",
    "DOB",
    "member_no",
    "name",
    "email_address",
    "dob",
    "birthday",
    "birthday_month",
    "full_name",
    "payload_hash",
    "approval",
    "password",
    "server",
    "database",
    "user_id",
    "sheet_id",
    "sheet_url",
}

MASKED_MEMBER_NO_RE = re.compile(r"^(\*\*\*|.{2}\*\*\*.)$")
SAVE_OUTCOMES = {"not_attempted", "confirmed", "uncertain"}


def read_single_result(path):
    """Read exactly one JSON object from the result file; report shape only."""
    p = Path(path)
    if not p.exists():
        return None, "result_file_missing"
    try:
        # utf-8-sig tolerates the BOM that Windows PowerShell Set-Content writes.
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return None, "result_file_unreadable"
    if not text.strip():
        return None, "result_file_empty"
    try:
        obj = json.loads(text)
    except ValueError:
        return None, "result_not_json"
    if not isinstance(obj, dict):
        return None, "result_not_object"
    return obj, None


def evaluate(result, expected_record_id=None, expected_fingerprint=None):
    counts = {
        "status": "needs_fix",
        "field_set_ok": False,
        "forbidden_field_count": 0,
        "terminal_code_valid": False,
        "terminal_code": "none",
        "masked_member_no_ok": False,
        "identity_revalidated": "not_requested",
        "fingerprint_revalidated": "not_requested",
        "pii_free": True,
    }
    reasons = []

    forbidden = set(result) & FORBIDDEN_RESULT_FIELDS
    counts["forbidden_field_count"] = len(forbidden)
    if forbidden:
        counts["pii_free"] = False
        reasons.append("forbidden_fields_present")

    extra = set(result) - ALLOWED_RESULT_FIELDS
    missing = {"terminal_code", "operation_id", "source_record_id", "source_fingerprint"} - set(result)
    counts["field_set_ok"] = not extra and not missing
    if extra:
        reasons.append("unexpected_fields")
    if missing:
        reasons.append("missing_required_fields")

    terminal = result.get("terminal_code")
    counts["terminal_code"] = terminal if isinstance(terminal, str) else "none"
    counts["terminal_code_valid"] = terminal in contract.TERMINAL_CODES
    if not counts["terminal_code_valid"]:
        reasons.append("terminal_code_invalid")

    masked = result.get("masked_member_no")
    counts["masked_member_no_ok"] = masked is None or (
        isinstance(masked, str) and bool(MASKED_MEMBER_NO_RE.fullmatch(masked))
    )
    if not counts["masked_member_no_ok"]:
        counts["pii_free"] = False
        reasons.append("masked_member_no_malformed")

    if not (
        isinstance(result.get("operation_id"), str)
        and contract.OPERATION_ID_RE.fullmatch(result["operation_id"])
    ):
        reasons.append("operation_id_invalid")
    if not (
        isinstance(result.get("source_record_id"), str)
        and contract.SOURCE_RECORD_ID_RE.fullmatch(result["source_record_id"])
    ):
        reasons.append("source_record_id_invalid")
    if not (
        isinstance(result.get("source_fingerprint"), str)
        and contract.SOURCE_FINGERPRINT_RE.fullmatch(result["source_fingerprint"])
    ):
        reasons.append("source_fingerprint_invalid")

    save_outcome = result.get("save_outcome")
    if save_outcome is not None and save_outcome not in SAVE_OUTCOMES:
        reasons.append("save_outcome_invalid")

    # Identity and fingerprint revalidation for result mapping (amendment #4):
    # a result may only be mapped onto the exact source record it belongs to.
    if expected_record_id is not None:
        ok = result.get("source_record_id") == expected_record_id
        counts["identity_revalidated"] = "ok" if ok else "mismatch"
        if not ok:
            reasons.append("source_record_id_mismatch")
    if expected_fingerprint is not None:
        ok = result.get("source_fingerprint") == expected_fingerprint
        counts["fingerprint_revalidated"] = "ok" if ok else "mismatch"
        if not ok:
            reasons.append("source_fingerprint_mismatch")

    counts["status"] = "ok" if not reasons else "needs_fix"
    counts["_reasons"] = reasons
    return counts


def build_evidence(counts):
    return [
        ("status", counts["status"]),
        ("gate", "member_create_uat_result_precheck"),
        ("field_set_ok", str(counts["field_set_ok"]).lower()),
        ("forbidden_field_count", counts["forbidden_field_count"]),
        ("pii_free", str(counts["pii_free"]).lower()),
        ("terminal_code", counts["terminal_code"]),
        ("terminal_code_valid", str(counts["terminal_code_valid"]).lower()),
        ("masked_member_no_ok", str(counts["masked_member_no_ok"]).lower()),
        ("identity_revalidated", counts["identity_revalidated"]),
        ("fingerprint_revalidated", counts["fingerprint_revalidated"]),
        ("google_sheets_access", "false"),
        ("autocount_call", "false"),
        ("n8n_action", "false"),
        ("result_file_modified", "false"),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Aggregate-only precheck for a sanitized create UAT terminal result."
    )
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--expect-source-record-id", default=None)
    parser.add_argument("--expect-source-fingerprint", default=None)
    args = parser.parse_args(argv)

    result, shape_error = read_single_result(args.result_json)
    if shape_error:
        print(f"status = needs_fix")
        print(f"gate = member_create_uat_result_precheck")
        print(f"shape_error = {shape_error}")
        return 2

    counts = evaluate(result, args.expect_source_record_id, args.expect_source_fingerprint)
    for key, value in build_evidence(counts):
        print(f"{key} = {value}")
    return 0 if counts["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
