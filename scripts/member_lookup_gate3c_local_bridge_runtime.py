"""Gate 3C local-only AC2 lookup bridge runtime harness.

This wraps the disabled-by-default lookup worker with filesystem idempotency
markers so an operator can repeatedly run a tiny pending queue on the AC2 host.
It prints only aggregate evidence and never writes to AutoCount.
"""

import argparse
import json
from pathlib import Path

import ac2_member_lookup_bridge_worker as worker


GATE = "gate3c_ac2_local_bridge_runtime_hardening"
LOOKUP_ERROR_REVIEW = worker.LOOKUP_ERROR_REVIEW


def read_pending_jsonl_lenient(path):
    rows = []
    malformed_count = 0
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return rows, malformed_count
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if not isinstance(parsed, list):
            return [], 1
        return parsed, malformed_count

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            malformed_count += 1
    return rows, malformed_count


def marker_path(directory, marker_id):
    return Path(directory) / f"{marker_id}.json"


def safe_marker_id(job, index):
    if isinstance(job, dict):
        job_id = worker.safe_job_id(job.get("job_id"))
        if job_id:
            return job_id
    return f"invalid-pending-row-{index}"


def load_markers(directory):
    markers = {}
    directory = Path(directory)
    if not directory.exists():
        return markers
    for path in directory.glob("*.json"):
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(marker, dict):
            continue
        job_id = worker.safe_job_id(marker.get("job_id"))
        if job_id:
            markers[job_id] = marker
    return markers


def write_json(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")


def append_results(path, results):
    if not results:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for result in results:
            handle.write(json.dumps(result, sort_keys=True) + "\n")


def marker_from_result(result, marker_type):
    return {
        "marker_type": marker_type,
        "job_id": worker.safe_job_id(result.get("job_id")),
        "payload_hash": worker.safe_source_value(result.get("payload_hash")),
        "state": result.get("state"),
        "status": result.get("status"),
        "error_code": result.get("error_code"),
        "dry_run_only": True,
        "final_write_automation": False,
        "marked_at": worker.utc_now(),
    }


def write_marker(directory, marker_id, result, marker_type, payload_hash=None):
    marker = marker_from_result(result, marker_type)
    marker["payload_hash"] = worker.safe_source_value(payload_hash)
    write_json(marker_path(directory, marker_id), marker)


def build_failed_result(job, error_code):
    safe_job = job if isinstance(job, dict) else {}
    return worker.result_envelope(safe_job, LOOKUP_ERROR_REVIEW, error_code=error_code)


def already_handled(job, processed_markers, failed_markers):
    job_id = worker.safe_job_id(job.get("job_id")) if isinstance(job, dict) else None
    if not job_id:
        return False
    return job_id in processed_markers or job_id in failed_markers


def has_payload_conflict(job, processed_markers, failed_markers):
    if not isinstance(job, dict):
        return False
    job_id = worker.safe_job_id(job.get("job_id"))
    payload_hash = worker.safe_source_value(job.get("payload_hash"))
    if not job_id or not payload_hash:
        return False
    marker = processed_markers.get(job_id) or failed_markers.get(job_id)
    if not marker:
        return False
    marker_hash = worker.safe_source_value(marker.get("payload_hash"))
    return marker_hash is not None and marker_hash != payload_hash


def run_harness(args):
    rows, malformed_count = read_pending_jsonl_lenient(args.pending_jsonl)
    processed_markers = load_markers(args.processed_dir)
    failed_markers = load_markers(args.failed_dir)
    mock_results = worker.read_mock_results(args.fixture_mock_results)

    new_results = []
    counts = {
        "pending_rows_loaded_count": len(rows) + malformed_count,
        "lookup_attempt_count": 0,
        "lookup_success_count": 0,
        "lookup_error_count": 0,
        "processed_or_archived_count": 0,
        "failed_or_dead_letter_count": malformed_count,
        "duplicate_or_already_processed_count": 0,
    }

    for index in range(malformed_count):
        result = build_failed_result({}, "pending_json_parse_error")
        marker_id = f"invalid-json-line-{index + 1}"
        write_marker(args.failed_dir, marker_id, result, "dead_letter")
        new_results.append(result)

    for index, job in enumerate(rows, start=1):
        marker_id = safe_marker_id(job, index)
        payload_hash = job.get("payload_hash") if isinstance(job, dict) else None

        if already_handled(job, processed_markers, failed_markers):
            if has_payload_conflict(job, processed_markers, failed_markers):
                result = build_failed_result(job, "idempotency_payload_hash_conflict")
                write_marker(args.failed_dir, f"{marker_id}-payload-conflict", result, "dead_letter", payload_hash)
                new_results.append(result)
                counts["failed_or_dead_letter_count"] += 1
            else:
                counts["duplicate_or_already_processed_count"] += 1
            continue

        try:
            worker.validate_job(job)
        except worker.BridgeWorkerError:
            result = build_failed_result(job, "request_or_lookup_contract_error")
            write_marker(args.failed_dir, marker_id, result, "dead_letter", payload_hash)
            new_results.append(result)
            counts["failed_or_dead_letter_count"] += 1
            continue

        if job.get("attempt", 0) >= worker.safe_max_attempts(job, args.max_attempts):
            result = build_failed_result(job, "retry_exhausted")
            write_marker(args.failed_dir, marker_id, result, "dead_letter", payload_hash)
            new_results.append(result)
            counts["failed_or_dead_letter_count"] += 1
            counts["lookup_error_count"] += 1
            continue

        counts["lookup_attempt_count"] += 1
        result = worker.process_job(job, args, mock_results=mock_results)
        new_results.append(result)
        if result["state"] == LOOKUP_ERROR_REVIEW:
            counts["lookup_error_count"] += 1
            counts["failed_or_dead_letter_count"] += 1
            write_marker(args.failed_dir, marker_id, result, "dead_letter", payload_hash)
        else:
            counts["lookup_success_count"] += 1
            counts["processed_or_archived_count"] += 1
            write_marker(args.processed_dir, marker_id, result, "processed", payload_hash)

    append_results(args.results_jsonl, new_results)
    return counts


def status_from_counts(args, counts):
    if counts["failed_or_dead_letter_count"] or counts["lookup_error_count"]:
        return "needs_fix"
    if counts["pending_rows_loaded_count"] == 0:
        return "no_work"
    if args.lookup_mode != "powershell" or not args.enable_powershell_lookup:
        return "dry_run_only"
    if counts["lookup_attempt_count"] >= 1 and counts["lookup_success_count"] >= 1:
        return "ok"
    return "needs_fix"


def evidence_rows(status, counts, args):
    rows = [
        ("status", status),
        ("gate", GATE),
        ("runtime_location", "windows_ac2_bridge_host_only"),
        ("execution_mode", "manual_local_filesystem_runtime_hardening"),
        ("lookup_mode", args.lookup_mode),
        ("powershell_lookup_enabled", "true" if args.enable_powershell_lookup else "false"),
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
        ("public_inbound_to_ac2_host", "false"),
        ("no_row_values_printed", "true"),
    ]
    return rows


def print_evidence(status, counts, args):
    for key, value in evidence_rows(status, counts, args):
        print(f"{key} = {value}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run a local-only Gate 3C AC2 lookup bridge queue harness."
    )
    parser.add_argument("--enable-local-bridge-runtime-review", action="store_true")
    parser.add_argument("--pending-jsonl", required=True)
    parser.add_argument("--results-jsonl", required=True)
    parser.add_argument("--processed-dir", required=True)
    parser.add_argument("--failed-dir", required=True)
    parser.add_argument("--lookup-mode", choices=["mock", "powershell"], default="powershell")
    parser.add_argument("--fixture-mock-results", default=None)
    parser.add_argument("--enable-powershell-lookup", action="store_true")
    parser.add_argument("--lookup-script", default=str(Path("scripts") / "ac2_member_lookup_review.ps1"))
    parser.add_argument("--powershell-exe", default="powershell")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--allow-root-login", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.enable_local_bridge_runtime_review:
        print_evidence("refused", {
            "pending_rows_loaded_count": 0,
            "lookup_attempt_count": 0,
            "lookup_success_count": 0,
            "lookup_error_count": 0,
            "processed_or_archived_count": 0,
            "failed_or_dead_letter_count": 0,
            "duplicate_or_already_processed_count": 0,
        }, args)
        return 2
    if args.lookup_mode == "powershell" and not args.enable_powershell_lookup:
        print_evidence("refused", {
            "pending_rows_loaded_count": 0,
            "lookup_attempt_count": 0,
            "lookup_success_count": 0,
            "lookup_error_count": 0,
            "processed_or_archived_count": 0,
            "failed_or_dead_letter_count": 0,
            "duplicate_or_already_processed_count": 0,
        }, args)
        return 2

    try:
        counts = run_harness(args)
    except (OSError, json.JSONDecodeError, worker.BridgeWorkerError):
        counts = {
            "pending_rows_loaded_count": 0,
            "lookup_attempt_count": 0,
            "lookup_success_count": 0,
            "lookup_error_count": 0,
            "processed_or_archived_count": 0,
            "failed_or_dead_letter_count": 1,
            "duplicate_or_already_processed_count": 0,
        }
        print_evidence("needs_fix", counts, args)
        return 2

    status = status_from_counts(args, counts)
    print_evidence(status, counts, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
