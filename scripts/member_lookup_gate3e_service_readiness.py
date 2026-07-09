"""Gate 3E local-only AC2 lookup bridge service-readiness discipline wrapper.

This wraps the Gate 3C filesystem runtime harness with the operational
discipline a future worker/service would need: an explicit opt-in flag, a
single-instance lock file, an operator stop/kill switch file, and a bounded
cycle count. It does not install or activate a Windows service, Task
Scheduler entry, daemon, webhook, or any network surface. It prints only
aggregate evidence and never writes to AutoCount.
"""

import argparse
import json
import os
import time
from pathlib import Path

import member_lookup_gate3c_local_bridge_runtime as gate3c


GATE = "gate3e_ac2_local_bridge_service_readiness"
EXECUTION_MODE = "manual_local_service_readiness_review"
WORKER_MODE = "bounded_local_filesystem_worker"
MAX_CYCLES_LIMIT = 10
QUEUE_ARG_NAMES = ("pending_jsonl", "results_jsonl", "processed_dir", "failed_dir")


def zero_counts():
    return {
        "pending_rows_loaded_count": 0,
        "lookup_attempt_count": 0,
        "lookup_success_count": 0,
        "lookup_error_count": 0,
        "processed_or_archived_count": 0,
        "failed_or_dead_letter_count": 0,
        "duplicate_or_already_processed_count": 0,
    }


def accumulate_counts(total, cycle_counts):
    for key in total:
        total[key] += cycle_counts[key]


def bool_text(value):
    return "true" if value else "false"


def evidence_rows(status, flags, counts):
    return [
        ("status", status),
        ("gate", GATE),
        ("runtime_location", "windows_ac2_bridge_host_only"),
        ("execution_mode", EXECUTION_MODE),
        ("worker_mode", WORKER_MODE),
        ("lock_acquired", bool_text(flags["lock_acquired"])),
        ("stale_lock_detected", bool_text(flags["stale_lock_detected"])),
        ("stop_requested", bool_text(flags["stop_requested"])),
        ("cycles_requested", flags["cycles_requested"]),
        ("cycles_completed", flags["cycles_completed"]),
        ("pending_rows_loaded_count", counts["pending_rows_loaded_count"]),
        ("lookup_attempt_count", counts["lookup_attempt_count"]),
        ("lookup_success_count", counts["lookup_success_count"]),
        ("lookup_error_count", counts["lookup_error_count"]),
        ("processed_or_archived_count", counts["processed_or_archived_count"]),
        ("failed_or_dead_letter_count", counts["failed_or_dead_letter_count"]),
        ("duplicate_or_already_processed_count", counts["duplicate_or_already_processed_count"]),
        ("member_create_or_update_invoked", "false"),
        ("autocount_write_attempted", "false"),
        ("direct_sql_write_attempted", "false"),
        ("final_write_automation", "false"),
        ("n8n_required", "false"),
        ("google_sheets_required", "false"),
        ("hosted_or_vps_service_called", "false"),
        ("scheduler_enabled", "false"),
        ("windows_service_installed", "false"),
        ("public_inbound_to_ac2_host", "false"),
        ("no_row_values_printed", "true"),
    ]


def print_evidence(rows):
    for key, value in rows:
        print(f"{key} = {value}")


def write_health(path, rows):
    if not path:
        return
    health_path = Path(path)
    health_path.parent.mkdir(parents=True, exist_ok=True)
    health_path.write_text(
        json.dumps(dict(rows), sort_keys=True) + "\n", encoding="utf-8"
    )


def read_lock_created_epoch(path):
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(lock, dict):
        return None
    created = lock.get("created_at_epoch")
    if isinstance(created, (int, float)):
        return float(created)
    return None


def try_acquire_lock(lock_path, stale_seconds):
    """Return (acquired, stale_lock_detected) without printing process details.

    A readable lock younger than stale_seconds is fresh: refuse and leave it
    untouched. A lock older than stale_seconds, or one that cannot be parsed,
    is deterministically treated as stale and taken over.
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_lock_detected = False
    if path.exists():
        created = read_lock_created_epoch(path)
        if created is not None and (time.time() - created) < stale_seconds:
            return False, False
        stale_lock_detected = True
        try:
            path.unlink()
        except OSError:
            return False, True
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False, stale_lock_detected
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "gate": GATE,
                    "created_at_epoch": time.time(),
                    "dry_run_only": True,
                    "final_write_automation": False,
                },
                sort_keys=True,
            )
            + "\n"
        )
    return True, stale_lock_detected


def release_lock(lock_path):
    try:
        Path(lock_path).unlink()
    except OSError:
        pass


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run a local-only Gate 3E AC2 lookup bridge service-readiness "
            "discipline review with a bounded cycle count."
        )
    )
    parser.add_argument("--enable-local-bridge-service-readiness-review", action="store_true")
    parser.add_argument("--lock-json", required=True)
    parser.add_argument("--stop-flag", required=True)
    parser.add_argument("--health-json", default=None)
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--lock-stale-seconds", type=int, default=3600)
    parser.add_argument("--pending-jsonl", default=None)
    parser.add_argument("--results-jsonl", default=None)
    parser.add_argument("--processed-dir", default=None)
    parser.add_argument("--failed-dir", default=None)
    parser.add_argument("--lookup-mode", choices=["mock", "powershell"], default="powershell")
    parser.add_argument("--fixture-mock-results", default=None)
    parser.add_argument("--enable-powershell-lookup", action="store_true")
    parser.add_argument("--lookup-script", default=str(Path("scripts") / "ac2_member_lookup_review.ps1"))
    parser.add_argument("--powershell-exe", default="powershell")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--allow-root-login", action="store_true")
    return parser


def runtime_requested(args):
    return any(getattr(args, name) for name in QUEUE_ARG_NAMES)


def runtime_fully_configured(args):
    return all(getattr(args, name) for name in QUEUE_ARG_NAMES)


def main(argv=None):
    args = build_parser().parse_args(argv)
    flags = {
        "lock_acquired": False,
        "stale_lock_detected": False,
        "stop_requested": False,
        "cycles_requested": max(args.max_cycles, 0),
        "cycles_completed": 0,
    }
    counts = zero_counts()

    def emit(status, *, write_health_file=True):
        rows = evidence_rows(status, flags, counts)
        print_evidence(rows)
        if write_health_file and args.enable_local_bridge_service_readiness_review:
            write_health(args.health_json, rows)

    if not args.enable_local_bridge_service_readiness_review:
        emit("refused", write_health_file=False)
        return 2
    if args.max_cycles < 1 or args.max_cycles > MAX_CYCLES_LIMIT:
        emit("refused")
        return 2
    if args.lock_stale_seconds < 1:
        emit("refused")
        return 2
    if runtime_requested(args) and not runtime_fully_configured(args):
        emit("refused")
        return 2
    if (
        runtime_requested(args)
        and args.lookup_mode == "powershell"
        and not args.enable_powershell_lookup
    ):
        emit("refused")
        return 2

    if Path(args.stop_flag).exists():
        flags["stop_requested"] = True
        emit("stopped_by_operator")
        return 0

    acquired, stale_lock_detected = try_acquire_lock(args.lock_json, args.lock_stale_seconds)
    flags["lock_acquired"] = acquired
    flags["stale_lock_detected"] = stale_lock_detected
    if not acquired:
        emit("lock_held")
        return 2

    try:
        for _ in range(args.max_cycles):
            if Path(args.stop_flag).exists():
                flags["stop_requested"] = True
                break
            if runtime_fully_configured(args):
                cycle_counts = gate3c.run_harness(args)
                accumulate_counts(counts, cycle_counts)
            flags["cycles_completed"] += 1
    except (OSError, json.JSONDecodeError, gate3c.worker.BridgeWorkerError):
        emit("needs_fix")
        return 2
    finally:
        release_lock(args.lock_json)

    if flags["stop_requested"] and flags["cycles_completed"] == 0:
        emit("stopped_by_operator")
        return 0
    if not runtime_fully_configured(args):
        emit("ok")
        return 0
    emit(gate3c.status_from_counts(args, counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
