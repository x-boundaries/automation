"""Gate 3D local-only AC2 lookup bridge small-batch wrapper.

This reuses the Gate 3C filesystem runtime harness and changes only the
operator-facing evidence labels/status semantics for the mixed small-batch
proof. It prints aggregate evidence only and never writes to AutoCount.
"""

import member_lookup_gate3c_local_bridge_runtime as gate3c


GATE = "gate3d_ac2_local_bridge_small_batch"
EXECUTION_MODE = "manual_local_filesystem_small_batch"


def status_from_counts(args, counts):
    if counts["pending_rows_loaded_count"] == 0:
        return "no_work"
    if args.lookup_mode != "powershell" or not args.enable_powershell_lookup:
        return "dry_run_only"
    if counts["lookup_error_count"]:
        return "needs_fix"
    if (
        counts["lookup_attempt_count"] >= 1
        and counts["lookup_success_count"] >= 1
        and counts["failed_or_dead_letter_count"] >= 1
        and counts["duplicate_or_already_processed_count"] >= 1
    ):
        return "mixed_expected"
    if counts["failed_or_dead_letter_count"]:
        return "needs_fix"
    if counts["lookup_attempt_count"] >= 1 and counts["lookup_success_count"] >= 1:
        return "ok"
    if (
        counts["lookup_attempt_count"] == 0
        and counts["lookup_success_count"] == 0
        and counts["processed_or_archived_count"] == 0
        and counts["duplicate_or_already_processed_count"] >= 1
    ):
        return "already_processed"
    return "needs_fix"


def evidence_rows(status, counts, args):
    rows = gate3c.evidence_rows(status, counts, args)
    return [
        ("gate", GATE) if key == "gate" else
        ("execution_mode", EXECUTION_MODE) if key == "execution_mode" else
        (key, value)
        for key, value in rows
    ]


def print_evidence(status, counts, args):
    for key, value in evidence_rows(status, counts, args):
        print(f"{key} = {value}")


def build_parser():
    parser = gate3c.build_parser()
    parser.description = "Run a local-only Gate 3D AC2 lookup bridge small-batch proof."
    parser.add_argument("--enable-local-bridge-small-batch-review", action="store_true")
    return parser


def zero_counts(failed_count=0):
    return {
        "pending_rows_loaded_count": 0,
        "lookup_attempt_count": 0,
        "lookup_success_count": 0,
        "lookup_error_count": 0,
        "processed_or_archived_count": 0,
        "failed_or_dead_letter_count": failed_count,
        "duplicate_or_already_processed_count": 0,
    }


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.enable_local_bridge_small_batch_review:
        print_evidence("refused", zero_counts(), args)
        return 2
    if args.lookup_mode == "powershell" and not args.enable_powershell_lookup:
        print_evidence("refused", zero_counts(), args)
        return 2

    try:
        counts = gate3c.run_harness(args)
    except (OSError, gate3c.json.JSONDecodeError, gate3c.worker.BridgeWorkerError):
        print_evidence("needs_fix", zero_counts(failed_count=1), args)
        return 2

    status = status_from_counts(args, counts)
    print_evidence(status, counts, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
