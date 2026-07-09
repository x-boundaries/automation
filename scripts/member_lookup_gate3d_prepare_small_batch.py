"""Prepare ignored local Gate 3D small-batch queue rows.

The helper writes local JSONL input for the Gate 3D wrapper and prints only
aggregate metadata. It never prints raw, encoded, decoded, or normalized member
values.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PENDING_LOOKUP = "PENDING_LOOKUP"
GATE = "gate3d_ac2_local_bridge_small_batch_queue_prep"
DUPLICATE_SEED_JOB_ID = "gate3d-duplicate-seed"
FRESH_JOB_ID = "gate3d-fresh-lookup"
MALFORMED_JOB_ID = "gate3d-malformed-dead-letter"


class QueuePrepError(ValueError):
    """Raised when the local queue cannot be prepared safely."""


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


def payload_hash(label, encoded):
    return hashlib.sha256(f"gate3d|{label}|{encoded}".encode("utf-8")).hexdigest()


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def build_valid_row(*, job_id, source_reference, source_row_ref, row_number, encoded, args):
    created_at = utc_now()
    timeout_at = (datetime.now(timezone.utc) + timedelta(minutes=args.timeout_minutes)).isoformat(
        timespec="seconds"
    )
    return {
        "job_id": job_id,
        "intake_source": args.intake_source,
        "source_reference": source_reference,
        "source_row_ref": source_row_ref,
        "row_number": row_number,
        "intake_id": f"{job_id}-intake",
        "state": PENDING_LOOKUP,
        "submitted_member_no_base64_utf8": encoded,
        "consent_status": args.consent_status,
        "pdpa_status": "yes",
        "payload_hash": payload_hash(job_id, encoded),
        "attempt": 0,
        "max_attempts": args.max_attempts,
        "created_at": created_at,
        "updated_at": created_at,
        "lease_owner": None,
        "lease_expires_at": None,
        "timeout_at": timeout_at,
        "last_error_code": None,
    }


def build_malformed_row(args):
    created_at = utc_now()
    return {
        "job_id": MALFORMED_JOB_ID,
        "intake_source": args.intake_source,
        "source_reference": "gate3d-malformed-local",
        "source_row_ref": "local-malformed",
        "row_number": 3,
        "state": PENDING_LOOKUP,
        "pdpa_status": "yes",
        "payload_hash": "gate3d-malformed-hash",
        "attempt": 0,
        "max_attempts": args.max_attempts,
        "created_at": created_at,
        "updated_at": created_at,
        "unexpected_gate3d_field": "gate3d-local-malformed-control",
    }


def build_rows(mode, encoded, args):
    seed_row = build_valid_row(
        job_id=DUPLICATE_SEED_JOB_ID,
        source_reference="gate3d-duplicate-seed-local",
        source_row_ref="local-duplicate-seed",
        row_number=1,
        encoded=encoded,
        args=args,
    )
    if mode == "duplicate-seed":
        return [seed_row]
    fresh_row = build_valid_row(
        job_id=FRESH_JOB_ID,
        source_reference="gate3d-fresh-lookup-local",
        source_row_ref="local-fresh-lookup",
        row_number=2,
        encoded=encoded,
        args=args,
    )
    return [seed_row, fresh_row, build_malformed_row(args)]


def build_summary(status, *, mode, queue_file_written=False, queue_row_count=0, encoded_present_count=0):
    return {
        "status": status,
        "gate": GATE,
        "mode": mode,
        "queue_file_written": queue_file_written,
        "queue_row_count": queue_row_count,
        "encoded_present_count": encoded_present_count,
        "malformed_row_count": 1 if mode == "mixed-batch" and queue_file_written else 0,
        "no_row_values_printed": True,
        "n8n_required": False,
        "google_sheets_required": False,
        "hosted_or_vps_service_called": False,
        "scheduler_enabled": False,
        "public_inbound_to_ac2_host": False,
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
        description="Write local ignored Gate 3D small-batch pending queue rows."
    )
    parser.add_argument("--enable-local-bridge-small-batch-review", action="store_true")
    parser.add_argument("--mode", choices=["duplicate-seed", "mixed-batch"], required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--member-value-stdin", action="store_true")
    source.add_argument("--member-value-env")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--intake-source", default="ac2_local_gate3d")
    parser.add_argument("--consent-status", default="operator_supplied_test")
    parser.add_argument("--max-attempts", type=positive_int, default=1)
    parser.add_argument("--timeout-minutes", type=positive_int, default=15)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.enable_local_bridge_small_batch_review:
        print(json.dumps(build_summary("refused", mode=args.mode), indent=2, sort_keys=True))
        return 2
    try:
        member_value = read_member_value(args)
        encoded = encode_member_value(member_value)
        rows = build_rows(args.mode, encoded, args)
        write_jsonl(args.queue_jsonl, rows)
    except (OSError, QueuePrepError):
        print(json.dumps(build_summary("error", mode=args.mode), indent=2, sort_keys=True))
        return 2

    print(
        json.dumps(
            build_summary(
                "ok",
                mode=args.mode,
                queue_file_written=True,
                queue_row_count=len(rows),
                encoded_present_count=sum(1 for row in rows if "submitted_member_no_base64_utf8" in row),
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
