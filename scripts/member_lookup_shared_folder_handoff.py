"""Shared-folder lookup handoff runner for the AC2 member lookup bridge.

This is the VM-side manual runner for the private host-to-VM shared-folder
handoff topology: n8n runs on the main physical PC and stages the sanitized
Gate 4A `PENDING_LOOKUP` queue file; the AutoCount lookup worker runs inside
the Windows VM and reads that file from the private shared-folder inbox. The
worker reuses the Gate 3C filesystem runtime harness for the read-only lookup
and writes the sanitized Gate 5A result copy into the shared-folder outbox for
the operator to move into the approved n8n container file area.

It is lookup-only and review-only. It never writes to AutoCount, never runs
direct SQL, never creates/updates/deletes members, never exposes a network
service, tunnel, webhook, or queue API, and never activates any n8n workflow.
Idempotency markers stay VM-local and must not be placed inside the share.
"""

import argparse
from pathlib import Path

import member_lookup_gate3c_local_bridge_runtime as gate3c


GATE = "shared_folder_lookup_bridge_handoff"
EXECUTION_MODE = "manual_shared_folder_lookup_handoff"
INBOX_DIR_NAME = "inbox"
OUTBOX_DIR_NAME = "outbox"
PENDING_FILENAME = "member_lookup_bridge_gate4a_pending_queue.jsonl"
RESULT_FILENAME = "member_lookup_bridge_gate5a_result_copy.jsonl"
MAX_APPROVED_BATCH = 10


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


def bool_text(value):
    return "true" if value else "false"


def evidence_rows(status, args, counts, markers_outside_share):
    return [
        ("status", status),
        ("gate", GATE),
        ("runtime_location", "autocount_vm_bridge_worker"),
        ("n8n_runtime_location", "main_physical_pc"),
        ("transport", "private_host_vm_shared_folder"),
        ("execution_mode", EXECUTION_MODE),
        ("lookup_mode", args.lookup_mode),
        ("powershell_lookup_enabled", bool_text(args.enable_powershell_lookup)),
        ("approved_batch_size", args.approved_batch_size),
        ("pending_rows_loaded_count", counts["pending_rows_loaded_count"]),
        ("lookup_attempt_count", counts["lookup_attempt_count"]),
        ("lookup_success_count", counts["lookup_success_count"]),
        ("lookup_error_count", counts["lookup_error_count"]),
        ("processed_or_archived_count", counts["processed_or_archived_count"]),
        ("failed_or_dead_letter_count", counts["failed_or_dead_letter_count"]),
        ("duplicate_or_already_processed_count", counts["duplicate_or_already_processed_count"]),
        ("idempotency_markers_outside_share", bool_text(markers_outside_share)),
        ("member_create_or_update_invoked", "false"),
        ("autocount_write_attempted", "false"),
        ("direct_sql_write_attempted", "false"),
        ("n8n_result_mapping_run", "false"),
        ("workflow_activation", "inactive"),
        ("queue_api_used", "false"),
        ("tunnel_or_reverse_proxy_used", "false"),
        ("webhook_used", "false"),
        ("scheduler_enabled", "false"),
        ("windows_service_installed", "false"),
        ("public_inbound_to_ac2_host", "false"),
        ("final_write_automation", "false"),
        ("no_row_values_printed", "true"),
    ]


def print_evidence(rows):
    for key, value in rows:
        print(f"{key} = {value}")


def is_inside(path, root):
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def marker_dirs_outside_share(args, share_root):
    for raw in (args.processed_dir, args.failed_dir):
        marker_dir = Path(raw)
        if is_inside(marker_dir, share_root) or is_inside(share_root, marker_dir):
            return False
    return True


def share_layout_ready(share_root, pending_path, outbox_dir):
    if not share_root.is_dir():
        return False
    if not pending_path.parent.is_dir():
        return False
    if not outbox_dir.is_dir():
        return False
    if not pending_path.is_file():
        return False
    return True


def results_file_nonempty(path):
    try:
        return Path(path).stat().st_size > 0
    except OSError:
        return False


def all_rows_already_handled(rows, malformed_count, processed_markers, failed_markers):
    if malformed_count or not rows:
        return False
    for job in rows:
        if not gate3c.already_handled(job, processed_markers, failed_markers):
            return False
        if gate3c.has_payload_conflict(job, processed_markers, failed_markers):
            return False
    return True


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run the VM-side manual shared-folder lookup handoff over the "
            "Gate 4A pending-queue and Gate 5A result-copy contract files."
        )
    )
    parser.add_argument("--enable-shared-folder-handoff-review", action="store_true")
    parser.add_argument("--share-root", required=True)
    parser.add_argument("--processed-dir", required=True)
    parser.add_argument("--failed-dir", required=True)
    parser.add_argument("--approved-batch-size", type=int, default=1)
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
    counts = zero_counts()
    share_root = Path(args.share_root)
    markers_outside_share = marker_dirs_outside_share(args, share_root)

    def emit(status):
        print_evidence(evidence_rows(status, args, counts, markers_outside_share))

    if not args.enable_shared_folder_handoff_review:
        emit("refused")
        return 2
    if args.lookup_mode == "powershell" and not args.enable_powershell_lookup:
        emit("refused")
        return 2
    if args.approved_batch_size < 1 or args.approved_batch_size > MAX_APPROVED_BATCH:
        emit("refused")
        return 2
    if not markers_outside_share:
        emit("refused")
        return 2

    pending_path = share_root / INBOX_DIR_NAME / PENDING_FILENAME
    outbox_dir = share_root / OUTBOX_DIR_NAME
    results_path = outbox_dir / RESULT_FILENAME
    if not share_layout_ready(share_root, pending_path, outbox_dir):
        emit("needs_fix")
        return 2

    # Map the shared-folder contract paths onto the Gate 3C harness arguments.
    args.pending_jsonl = str(pending_path)
    args.results_jsonl = str(results_path)

    try:
        rows, malformed_count = gate3c.read_pending_jsonl_lenient(args.pending_jsonl)
    except (OSError, ValueError):
        counts["failed_or_dead_letter_count"] = 1
        emit("needs_fix")
        return 2

    total_rows = len(rows) + malformed_count
    if total_rows == 0:
        emit("no_work")
        return 0
    if total_rows > args.approved_batch_size:
        counts["pending_rows_loaded_count"] = total_rows
        emit("needs_fix")
        return 2

    if results_file_nonempty(results_path):
        processed_markers = gate3c.load_markers(args.processed_dir)
        failed_markers = gate3c.load_markers(args.failed_dir)
        if all_rows_already_handled(rows, malformed_count, processed_markers, failed_markers):
            counts["pending_rows_loaded_count"] = total_rows
            counts["duplicate_or_already_processed_count"] = total_rows
            emit("already_processed")
            return 0
        counts["pending_rows_loaded_count"] = total_rows
        emit("needs_fix")
        return 2

    try:
        harness_counts = gate3c.run_harness(args)
    except (OSError, ValueError, gate3c.worker.BridgeWorkerError):
        counts["failed_or_dead_letter_count"] = 1
        emit("needs_fix")
        return 2

    counts.update(harness_counts)
    status = gate3c.status_from_counts(args, counts)
    emit(status)
    return 0 if status in ("ok", "no_work", "already_processed", "dry_run_only") else 2


if __name__ == "__main__":
    raise SystemExit(main())
