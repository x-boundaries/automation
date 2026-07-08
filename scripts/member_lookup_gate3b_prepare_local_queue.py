"""Prepare one ignored local Gate 3B AC2 lookup queue row.

The helper writes a one-row JSONL fixture for the local AC2 bridge worker and
prints only aggregate metadata. It never prints the raw, encoded, decoded, or
normalized member value.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


PENDING_LOOKUP = "PENDING_LOOKUP"


class QueuePrepError(ValueError):
    """Raised when the one-row local queue cannot be prepared safely."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_member_value(args):
    if args.member_value_stdin:
        value = sys.stdin.read()
    else:
        value = os.environ.get(args.member_value_env or "")
    if value is None:
        raise QueuePrepError("member_value_missing")
    value = value.strip()
    if not value:
        raise QueuePrepError("member_value_blank")
    return value


def encode_member_value(value):
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    if not encoded:
        raise QueuePrepError("encoded_member_value_blank")
    return encoded


def write_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def build_row(encoded, args):
    created_at = utc_now()
    timeout_at = (datetime.now(timezone.utc) + timedelta(minutes=args.timeout_minutes)).isoformat(
        timespec="seconds"
    )
    safe_id = uuid.uuid4().hex
    payload_hash = hashlib.sha256(
        f"gate3b|{args.intake_source}|{safe_id}|{encoded}".encode("utf-8")
    ).hexdigest()
    return {
        "job_id": f"gate3b-local-{safe_id}",
        "intake_source": args.intake_source,
        "source_reference": "gate3b-local-one-row",
        "source_row_ref": "local-one-row",
        "row_number": 1,
        "intake_id": f"gate3b-intake-{safe_id}",
        "state": PENDING_LOOKUP,
        "submitted_member_no_base64_utf8": encoded,
        "consent_status": args.consent_status,
        "pdpa_status": "yes",
        "payload_hash": payload_hash,
        "attempt": 0,
        "max_attempts": args.max_attempts,
        "created_at": created_at,
        "updated_at": created_at,
        "lease_owner": None,
        "lease_expires_at": None,
        "timeout_at": timeout_at,
        "last_error_code": None,
    }


def build_summary(status, *, queue_file_written=False, queue_row_count=0, encoded_present_count=0):
    return {
        "status": status,
        "gate": "gate3b_ac2_local_bridge_readiness_queue_prep",
        "queue_file_written": queue_file_written,
        "queue_row_count": queue_row_count,
        "encoded_present_count": encoded_present_count,
        "no_row_values_printed": True,
        "n8n_required": False,
        "google_sheets_required": False,
        "dry_run_only": True,
        "final_write_automation": False,
    }


def positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser():
    parser = argparse.ArgumentParser(
        description="Write one local ignored Gate 3B pending lookup queue row."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--member-value-stdin", action="store_true")
    source.add_argument("--member-value-env")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--intake-source", default="ac2_local_gate3b")
    parser.add_argument("--consent-status", default="operator_supplied_test")
    parser.add_argument("--max-attempts", type=positive_int, default=1)
    parser.add_argument("--timeout-minutes", type=positive_int, default=15)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        member_value = read_member_value(args)
        encoded = encode_member_value(member_value)
        row = build_row(encoded, args)
        write_jsonl(args.queue_jsonl, row)
    except (OSError, QueuePrepError):
        print(json.dumps(build_summary("error"), indent=2, sort_keys=True))
        return 2

    print(
        json.dumps(
            build_summary(
                "ok",
                queue_file_written=True,
                queue_row_count=1,
                encoded_present_count=1,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
