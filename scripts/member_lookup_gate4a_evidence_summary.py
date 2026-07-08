"""Aggregate-only Gate 4A evidence summary for member lookup bridge results.

The script reads one local sanitized bridge result JSONL file and prints only
the approved Gate 4A paste-back shape. It never prints row-level identifiers or
payload values.
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
    "row-level output, raw/encoded/normalized member values, names, emails, "
    "phone numbers, command transcripts, stderr/stdout, execution payloads, "
    "node raw input/output dumps, or PII are pasted."
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
    lookup_existing_member_review_count = 0
    lookup_manual_review_count = 0
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
            if state == EXISTING_MEMBER_REVIEW:
                lookup_existing_member_review_count += 1
            elif state == MANUAL_REVIEW_REQUIRED:
                lookup_manual_review_count += 1

    if line_count != len(rows):
        missing_or_unsafe = True

    status = "needs_fix" if lookup_error_count > 0 or missing_or_unsafe else "ok"
    return {
        "status": status,
        "lookup_attempt_count": line_count,
        "lookup_success_count": lookup_success_count,
        "lookup_existing_member_review_count": lookup_existing_member_review_count,
        "lookup_manual_review_count": lookup_manual_review_count,
        "lookup_error_count": lookup_error_count,
        "local_review_result_rows_written_count": line_count,
    }


def build_evidence(args, counts):
    return [
        ("status", counts["status"]),
        ("gate", "gate4a_manual_queue_handoff_ac2_lookup_only"),
        ("runtime_location", "local_operator_pc_non_ac2_n8n_stack"),
        ("execution_mode", "manual_inactive_review_only_handoff"),
        ("approved_batch_size", args.approved_batch_size),
        ("n8n_queue_rows_written_count", args.n8n_queue_rows_written_count),
        ("local_queue_rows_loaded_count", args.local_queue_rows_loaded_count),
        ("lookup_attempt_count", counts["lookup_attempt_count"]),
        ("lookup_success_count", counts["lookup_success_count"]),
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
        ("n8n_result_mapping_run", "false"),
        ("member_create_or_update_invoked", "false"),
        ("autocount_write_attempted", "false"),
        ("direct_sql_write_attempted", "false"),
        ("workflow_activation", "inactive"),
        ("scheduler_enabled", "false"),
        ("public_inbound_to_ac2_host", "false"),
        ("final_write_automation", "false"),
        ("sanitized_note", SANITIZED_NOTE),
    ]


def aggregate_counts_match(args, lookup_attempt_count):
    expected_counts = {
        args.approved_batch_size,
        args.n8n_queue_rows_written_count,
        args.local_queue_rows_loaded_count,
        lookup_attempt_count,
    }
    return len(expected_counts) == 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print aggregate-only Gate 4A evidence from local bridge result JSONL."
    )
    parser.add_argument("--results-jsonl", required=True)
    parser.add_argument("--approved-batch-size", type=nonnegative_int, required=True)
    parser.add_argument(
        "--n8n-queue-rows-written-count",
        type=nonnegative_int,
        required=True,
    )
    parser.add_argument(
        "--local-queue-rows-loaded-count",
        type=nonnegative_int,
        required=True,
    )
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
