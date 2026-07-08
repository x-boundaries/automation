"""Aggregate-only Gate 4A pre-bridge queue checks.

The script reads one local sanitized Gate 4A queue JSONL file, decodes lookup
values only in memory, and prints aggregate counters only. It never prints row
identifiers, raw values, encoded values, decoded values, normalized values, or
PII.
"""

import argparse
import base64
import json
from pathlib import Path


PENDING_LOOKUP = "PENDING_LOOKUP"

REQUIRED_QUEUE_FIELDS = {
    "job_id",
    "intake_source",
    "source_reference",
    "source_row_ref",
    "row_number",
    "intake_id",
    "state",
    "submitted_member_no_base64_utf8",
    "consent_status",
    "pdpa_status",
    "payload_hash",
    "attempt",
    "max_attempts",
    "created_at",
    "updated_at",
    "timeout_at",
}

DUMMY_MARKERS = {
    "dummy",
    "fixture",
    "placeholder",
    "rehearsal",
    "sample",
    "synthetic",
}

SANITIZED_NOTE = (
    "No credentials, connection strings, Sheet IDs/URLs, credential IDs, "
    "row-level output, raw/encoded/decoded/normalized member values, names, "
    "emails, phone numbers, birthdays, command transcripts, stderr/stdout, "
    "execution payloads, node raw input/output dumps, screenshots, or PII are "
    "pasted."
)


def read_queue_rows(path):
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


def decode_member_value(row):
    encoded = row.get("submitted_member_no_base64_utf8")
    if not isinstance(encoded, str) or not encoded.strip():
        return False, "", False
    try:
        decoded_bytes = base64.b64decode(encoded, validate=True)
        decoded = decoded_bytes.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False, "", False
    normalized = decoded.strip()
    looks_dummy = any(marker in normalized.lower() for marker in DUMMY_MARKERS)
    return True, normalized, looks_dummy


def summarize(rows, line_count, has_shape_error):
    queue_base64_decode_ok_count = 0
    queue_base64_decode_fail_count = 0
    queue_decoded_blank_count = 0
    queue_decoded_looks_dummy_count = 0
    unexpected_shape_count = 1 if has_shape_error else 0

    for row in rows:
        if not REQUIRED_QUEUE_FIELDS.issubset(row):
            unexpected_shape_count += 1
        if row.get("state") != PENDING_LOOKUP or row.get("pdpa_status") != "yes":
            unexpected_shape_count += 1

        ok, decoded, looks_dummy = decode_member_value(row)
        if ok:
            queue_base64_decode_ok_count += 1
            if not decoded:
                queue_decoded_blank_count += 1
            if looks_dummy:
                queue_decoded_looks_dummy_count += 1
        else:
            queue_base64_decode_fail_count += 1

    if line_count != len(rows):
        unexpected_shape_count += 1

    expected_pass = (
        line_count == 1
        and queue_base64_decode_ok_count == 1
        and queue_base64_decode_fail_count == 0
        and queue_decoded_blank_count == 0
        and queue_decoded_looks_dummy_count == 0
        and unexpected_shape_count == 0
    )
    return {
        "status": "ok" if expected_pass else "needs_fix",
        "queue_row_count": line_count,
        "queue_base64_decode_ok_count": queue_base64_decode_ok_count,
        "queue_base64_decode_fail_count": queue_base64_decode_fail_count,
        "queue_decoded_blank_count": queue_decoded_blank_count,
        "queue_decoded_looks_dummy_count": queue_decoded_looks_dummy_count,
        "unexpected_queue_shape_count": unexpected_shape_count,
    }


def build_evidence(counts):
    return [
        ("status", counts["status"]),
        ("gate", "gate4a_real_queue_write_pre_bridge_check"),
        ("runtime_location", "local_operator_pc_non_ac2_n8n_stack"),
        ("execution_mode", "manual_inactive_queue_write_pre_bridge_check"),
        ("queue_row_count", counts["queue_row_count"]),
        ("queue_base64_decode_ok_count", counts["queue_base64_decode_ok_count"]),
        ("queue_base64_decode_fail_count", counts["queue_base64_decode_fail_count"]),
        ("queue_decoded_blank_count", counts["queue_decoded_blank_count"]),
        ("queue_decoded_looks_dummy_count", counts["queue_decoded_looks_dummy_count"]),
        ("unexpected_queue_shape_count", counts["unexpected_queue_shape_count"]),
        ("bridge_handoff_approved", "false"),
        ("ac2_lookup_invoked", "false"),
        ("n8n_result_mapping_run", "false"),
        ("workflow_activation", "inactive"),
        ("scheduler_enabled", "false"),
        ("public_inbound_to_ac2_host", "false"),
        ("member_create_or_update_invoked", "false"),
        ("autocount_write_attempted", "false"),
        ("direct_sql_write_attempted", "false"),
        ("final_write_automation", "false"),
        ("no_row_values_printed", "true"),
        ("sanitized_note", SANITIZED_NOTE),
    ]


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print aggregate-only Gate 4A pre-bridge queue checks."
    )
    parser.add_argument("--queue-jsonl", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    line_count, rows, has_shape_error = read_queue_rows(args.queue_jsonl)
    counts = summarize(rows, line_count, has_shape_error)
    for key, value in build_evidence(counts):
        print(f"{key} = {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
