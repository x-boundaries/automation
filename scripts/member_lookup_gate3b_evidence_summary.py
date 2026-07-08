"""Aggregate-only Gate 3B local bridge readiness evidence summary.

The script reads one local sanitized bridge result JSONL file and prints only
the AC2-side local bridge readiness paste-back shape. It never prints row-level
identifiers or payload values.
"""

import argparse
import json
from pathlib import Path


LOOKUP_ERROR_REVIEW = "LOOKUP_ERROR_REVIEW"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
EXISTING_MEMBER_REVIEW = "EXISTING_MEMBER_REVIEW"
READY_FOR_CREATE_REVIEW = "READY_FOR_CREATE_REVIEW"

ALLOWED_STATES = {
    LOOKUP_ERROR_REVIEW,
    MANUAL_REVIEW_REQUIRED,
    EXISTING_MEMBER_REVIEW,
    READY_FOR_CREATE_REVIEW,
}

REQUIRED_SAFE_FIELDS = {
    "state",
    "status",
    "dry_run_only",
    "final_write_automation",
}

SANITIZED_NOTE = (
    "No credentials, connection strings, Sheet IDs/URLs, credential IDs, "
    "row-level output, raw/encoded/decoded/normalized member values, names, "
    "emails, phone numbers, command transcripts, stderr/stdout, execution "
    "payloads, node raw input/output dumps, or PII are pasted."
)


def nonnegative_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return parsed


def read_result_rows(path):
    rows = []
    line_count = 0
    has_shape_error = False
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        line_count += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            has_shape_error = True
            continue
        if not isinstance(row, dict):
            has_shape_error = True
            continue
        rows.append(row)
    return line_count, rows, has_shape_error


def summarize(rows, line_count, has_shape_error):
    missing_or_unsafe = has_shape_error
    lookup_success_count = 0
    ready_for_create_review_count = 0
    existing_member_review_count = 0
    manual_review_count = 0
    lookup_error_count = 0

    for row in rows:
        if not REQUIRED_SAFE_FIELDS.issubset(row):
            missing_or_unsafe = True
            continue
        if row.get("dry_run_only") is not True:
            missing_or_unsafe = True
        if row.get("final_write_automation") is not False:
            missing_or_unsafe = True

        state = row.get("state")
        status = row.get("status")
        if state not in ALLOWED_STATES:
            missing_or_unsafe = True
            continue
        if status not in {"ok", "error", "refused"}:
            missing_or_unsafe = True
            continue

        if state == LOOKUP_ERROR_REVIEW:
            lookup_error_count += 1
        else:
            lookup_success_count += 1
            if state == READY_FOR_CREATE_REVIEW:
                ready_for_create_review_count += 1
            elif state == EXISTING_MEMBER_REVIEW:
                existing_member_review_count += 1
            elif state == MANUAL_REVIEW_REQUIRED:
                manual_review_count += 1

    if line_count != len(rows):
        missing_or_unsafe = True

    status = "needs_fix" if lookup_error_count > 0 or missing_or_unsafe else "ok"
    return {
        "status": status,
        "lookup_attempt_count": line_count,
        "lookup_success_count": lookup_success_count,
        "lookup_ready_for_create_review_count": ready_for_create_review_count,
        "lookup_existing_member_review_count": existing_member_review_count,
        "lookup_manual_review_count": manual_review_count,
        "lookup_error_count": lookup_error_count,
        "local_review_result_rows_written_count": line_count,
    }


def build_evidence(args, counts):
    return [
        ("status", counts["status"]),
        ("gate", "gate3b_ac2_local_bridge_readiness"),
        ("runtime_location", "windows_ac2_bridge_host_only"),
        ("execution_mode", "manual_local_one_row_read_only_lookup"),
        ("local_queue_row_count", args.local_queue_row_count),
        ("local_queue_rows_loaded_count", args.local_queue_rows_loaded_count),
        ("lookup_attempt_count", counts["lookup_attempt_count"]),
        ("lookup_success_count", counts["lookup_success_count"]),
        (
            "lookup_ready_for_create_review_count",
            counts["lookup_ready_for_create_review_count"],
        ),
        (
            "lookup_existing_member_review_count",
            counts["lookup_existing_member_review_count"],
        ),
        ("lookup_manual_review_count", counts["lookup_manual_review_count"]),
        ("lookup_error_count", counts["lookup_error_count"]),
        (
            "local_review_result_rows_written_count",
            counts["local_review_result_rows_written_count"],
        ),
        ("n8n_required", "false"),
        ("n8n_involved", "false"),
        ("google_sheets_required", "false"),
        ("hosted_or_vps_service_called", "false"),
        ("scheduler_enabled", "false"),
        ("public_inbound_to_ac2_host", "false"),
        ("member_create_or_update_invoked", "false"),
        ("autocount_write_attempted", "false"),
        ("direct_sql_write_attempted", "false"),
        ("final_write_automation", "false"),
        ("no_row_values_printed", "true"),
        ("sanitized_note", SANITIZED_NOTE),
    ]


def aggregate_counts_match(args, lookup_attempt_count):
    expected_counts = {
        args.local_queue_row_count,
        args.local_queue_rows_loaded_count,
        lookup_attempt_count,
    }
    return len(expected_counts) == 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print aggregate-only Gate 3B local bridge readiness evidence."
    )
    parser.add_argument("--results-jsonl", required=True)
    parser.add_argument("--local-queue-row-count", type=nonnegative_int, required=True)
    parser.add_argument("--local-queue-rows-loaded-count", type=nonnegative_int, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    line_count, rows, has_shape_error = read_result_rows(args.results_jsonl)
    counts = summarize(rows, line_count, has_shape_error)
    if not aggregate_counts_match(args, counts["lookup_attempt_count"]):
        counts["status"] = "needs_fix"
    for key, value in build_evidence(args, counts):
        print(f"{key} = {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
