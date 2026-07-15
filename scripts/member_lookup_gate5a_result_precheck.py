"""Aggregate-only Gate 5A copied-recovery-result precheck.

Gate 5A maps the single sanitized Gate 4 recovery durable result into controlled
Google Sheet review/status fields through a manual inactive n8n workflow. Before the
operator copies the result into the approved n8n container file area, this script
strictly validates the operator-PC staging copy and prints aggregate counters only.

The precheck accepts exactly one complete nonblank JSON result object that matches the
exact durable Gate 4 result contract (``ac2_member_lookup_bridge_worker``
``ALLOWED_RESULT_FIELDS``) and the canonical Gate 4A identity shape. Everything else is
``needs_fix``: a missing/empty/whitespace-only file, more than one row, malformed JSON,
a non-object row, missing fields, unexpected or forbidden fields, invalid field types,
an unrecognised state, ``dry_run_only != true``, ``final_write_automation != false``, a
non-null ``result_applied_at``, or an error/warning combination inconsistent with the
stored review state (the state is always recomputed with the existing worker
classifier, never trusted as a string).

It never prints row identifiers, job IDs, hashes, raw/encoded/decoded member values,
timestamps, names, emails, phone numbers, or any other row-level value. It opens the
copied file read-only and never writes, so original Gate 4 evidence and the staging
copy stay byte-for-byte unchanged. It performs no Google Sheets access, no AC2 lookup,
no AutoCount write, no SQL, and no n8n action.
"""

import argparse
import json
import re
from pathlib import Path

import ac2_member_lookup_bridge_worker as worker
import member_lookup_gate4_real_queue_lookup as gate4


GATE = "gate5a_manual_inactive_n8n_result_mapping"
RUNTIME_LOCATION = "local_operator_pc_non_ac2_n8n_stack"
EXECUTION_MODE = "manual_inactive_single_result_copy_precheck"

# Canonical Gate 4A identity constants shared with the merged Gate 4 contract.
CANONICAL_SOURCE_LABEL = "google_sheets_uat_gate4a"
CANONICAL_JOB_ID_RE = re.compile(r"^gate4a_fnv1a_[0-9a-f]{8}$")
SAFE_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
SAFE_TIMESTAMP_RE = re.compile(r"^[0-9T:+.Z-]{1,64}$")

SUCCESS_REVIEW_STATES = gate4.SUCCESS_REVIEW_STATES
ALL_REVIEW_STATES = gate4.ALL_REVIEW_STATES

# Forbidden result surface: the worker's forbidden job fields plus queue-only and
# raw-value fields that must never appear in a durable result row.
FORBIDDEN_RESULT_FIELDS = worker.FORBIDDEN_JOB_FIELDS | {
    "submitted_member_no_base64_utf8",
    "payload_hash",
    "intake_id",
    "normalized_member_no",
    "decoded_member_no",
    "raw_member_no",
    "birthday",
    "birthday_month",
    "full_name",
    "sheet_id",
    "sheet_url",
    "document_id",
    "command",
    "command_text",
    "payload",
}

SANITIZED_NOTE = (
    "No credentials, connection strings, Sheet IDs/URLs, credential IDs, "
    "row-level output, raw/encoded/decoded/normalized member values, names, "
    "emails, phone numbers, birthdays, job IDs, hashes, timestamps, command "
    "transcripts, stderr/stdout, execution payloads, node raw input/output "
    "dumps, screenshots, or PII are pasted."
)


def read_result_rows(path):
    """Read the staging copy read-only; report line/parse structure only."""
    line_count = 0
    rows = []
    has_shape_error = False
    path = Path(path)
    if not path.exists():
        return 0, [], True
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0, [], True
    if not text.strip():
        return 0, [], True
    for line in text.splitlines():
        if not line.strip():
            continue
        line_count += 1
        try:
            row = json.loads(line)
        except ValueError:
            has_shape_error = True
            continue
        if not isinstance(row, dict):
            has_shape_error = True
            continue
        rows.append(row)
    return line_count, rows, has_shape_error


def result_types_valid(result):
    """Exact per-field type/shape validation for one durable result row."""
    if worker.safe_job_id(result.get("job_id")) is None:
        return False
    if not CANONICAL_JOB_ID_RE.fullmatch(result["job_id"]):
        return False
    if result.get("intake_source") != CANONICAL_SOURCE_LABEL:
        return False
    if result.get("source_reference") != CANONICAL_SOURCE_LABEL:
        return False
    row_number = result.get("row_number")
    if not isinstance(row_number, int) or isinstance(row_number, bool) or row_number < 1:
        return False
    if result.get("source_row_ref") != f"row_{row_number}":
        return False
    if result.get("state") not in ALL_REVIEW_STATES:
        return False
    if result.get("status") not in {"ok", "error", "refused"}:
        return False
    for flag in (
        "authentication_success",
        "user_session_available",
        "member_command_found",
        "get_member_found",
        "member_exists",
        "manual_review_required",
    ):
        if not isinstance(result.get(flag), bool):
            return False
    if result.get("submitted_member_no_status") not in worker.ALLOWED_MEMBER_STATUS_LABELS:
        return False
    length = result.get("normalized_member_no_length")
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        return False
    found_by = result.get("member_found_by")
    if found_by is not None and worker.safe_source_value(found_by) is None:
        return False
    warning_count = result.get("warning_count")
    if not isinstance(warning_count, int) or isinstance(warning_count, bool) or warning_count < 0:
        return False
    error_code = result.get("error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or not SAFE_ERROR_CODE_RE.fullmatch(error_code)
    ):
        return False
    if result.get("consent_status") != gate4.CANONICAL_CONSENT_STATUS:
        return False
    if result.get("pdpa_status") != "yes":
        return False
    if not gate4.exact_int(result.get("attempt"), gate4.CANONICAL_ATTEMPT):
        return False
    created_at = result.get("result_created_at")
    if not isinstance(created_at, str) or not SAFE_TIMESTAMP_RE.fullmatch(created_at):
        return False
    if result.get("dry_run_only") is not True:
        return False
    if result.get("final_write_automation") is not False:
        return False
    if result.get("result_applied_at") is not None:
        return False
    return True


def result_state_consistent(result):
    """Recompute the review state from stored lookup fields; never trust the string."""
    try:
        lookup_like = {field: result.get(field) for field in worker.LOOKUP_PUBLIC_FIELDS}
        computed_state, _ = worker.classify_lookup_result(lookup_like)
    except worker.BridgeWorkerError:
        return False
    if computed_state != result.get("state"):
        return False
    if result.get("status") == "ok":
        if result.get("error_code") is not None:
            return False
        if computed_state not in SUCCESS_REVIEW_STATES:
            return False
        for flag in (
            "authentication_success",
            "user_session_available",
            "member_command_found",
            "get_member_found",
        ):
            if result.get(flag) is not True:
                return False
    else:
        if computed_state != gate4.LOOKUP_ERROR_REVIEW:
            return False
        if result.get("error_code") is None:
            return False
    return True


def summarize(path):
    line_count, rows, has_shape_error = read_result_rows(path)
    counts = {
        "input_result_row_count": line_count,
        "result_schema_valid_count": 0,
        "result_state_consistent_count": 0,
        "forbidden_field_count": 0,
        "unexpected_shape_count": 1 if has_shape_error else 0,
        "mapped_lookup_error_review_count": 0,
        "mapped_manual_review_required_count": 0,
        "mapped_existing_member_review_count": 0,
        "mapped_ready_for_create_review_count": 0,
    }
    if line_count != len(rows):
        counts["unexpected_shape_count"] += 1

    for row in rows:
        forbidden = set(row) & FORBIDDEN_RESULT_FIELDS
        if forbidden:
            counts["forbidden_field_count"] += 1
            continue
        if set(row) != worker.ALLOWED_RESULT_FIELDS:
            counts["unexpected_shape_count"] += 1
            continue
        if not result_types_valid(row):
            counts["unexpected_shape_count"] += 1
            continue
        counts["result_schema_valid_count"] += 1
        if not result_state_consistent(row):
            counts["unexpected_shape_count"] += 1
            continue
        counts["result_state_consistent_count"] += 1
        state = row["state"]
        if state == gate4.LOOKUP_ERROR_REVIEW:
            counts["mapped_lookup_error_review_count"] += 1
        elif state == gate4.MANUAL_REVIEW_REQUIRED:
            counts["mapped_manual_review_required_count"] += 1
        elif state == gate4.EXISTING_MEMBER_REVIEW:
            counts["mapped_existing_member_review_count"] += 1
        elif state == gate4.READY_FOR_CREATE_REVIEW:
            counts["mapped_ready_for_create_review_count"] += 1

    expected_pass = (
        counts["input_result_row_count"] == 1
        and counts["result_schema_valid_count"] == 1
        and counts["result_state_consistent_count"] == 1
        and counts["forbidden_field_count"] == 0
        and counts["unexpected_shape_count"] == 0
    )
    counts["status"] = "ok" if expected_pass else "needs_fix"
    return counts


def build_evidence(counts):
    return [
        ("status", counts["status"]),
        ("gate", GATE),
        ("runtime_location", RUNTIME_LOCATION),
        ("execution_mode", EXECUTION_MODE),
        ("input_result_row_count", counts["input_result_row_count"]),
        ("result_schema_valid_count", counts["result_schema_valid_count"]),
        ("result_state_consistent_count", counts["result_state_consistent_count"]),
        ("forbidden_field_count", counts["forbidden_field_count"]),
        ("unexpected_shape_count", counts["unexpected_shape_count"]),
        ("mapped_lookup_error_review_count", counts["mapped_lookup_error_review_count"]),
        ("mapped_manual_review_required_count", counts["mapped_manual_review_required_count"]),
        ("mapped_existing_member_review_count", counts["mapped_existing_member_review_count"]),
        ("mapped_ready_for_create_review_count", counts["mapped_ready_for_create_review_count"]),
        ("original_gate4_artifacts_modified", "false"),
        ("autocount_lookup_invoked", "false"),
        ("member_create_or_update_invoked", "false"),
        ("autocount_write_attempted", "false"),
        ("direct_sql_write_attempted", "false"),
        ("n8n_result_mapping_run", "false"),
        ("workflow_activation", "inactive"),
        ("scheduler_enabled", "false"),
        ("public_inbound_to_ac2_host", "false"),
        ("final_write_automation", "false"),
        ("no_row_values_printed", "true"),
        ("sanitized_note", SANITIZED_NOTE),
    ]


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Print aggregate-only Gate 5A checks for the copied sanitized Gate 4 "
            "recovery result staging file. Read-only; never edits any file."
        )
    )
    parser.add_argument("--result-jsonl", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    counts = summarize(args.result_jsonl)
    for key, value in build_evidence(counts):
        print(f"{key} = {value}")
    return 0 if counts["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
