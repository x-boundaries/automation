"""Shared-folder lookup handoff runner for the AC2 member lookup bridge.

This is the VM-side manual runner for the private host-to-VM shared-folder
handoff topology: n8n runs on the main physical PC and stages the sanitized
Gate 4A ``PENDING_LOOKUP`` queue file; the AutoCount lookup worker runs inside
the Windows VM and reads that file from the private shared-folder inbox.

The runner adds no lookup or validation logic of its own. The entire queue
validation, lookup, and marker/result state machine is delegated in-process to
the already-proven Gate 4 real-queue runner
(``member_lookup_gate4_real_queue_lookup``), which enforces the exact Gate 4A
single-row precheck, canonical decoded numeric member contract, canonical
payload-hash/job-identity relationships, canonical retry metadata, strict
marker/result inspection, and durable result-before-marker persistence. Like
Gate 4 it is PowerShell-only: there is no mock route, and only a genuine
read-only PowerShell lookup can produce ``status = ok``.

On top of that delegation this wrapper adds only the shared-folder layer:

- fixed inbox/outbox contract paths inside the private share;
- one VM-local exclusive execution claim (atomic ``O_CREAT | O_EXCL``) that is
  acquired before any state inspection or lookup and held through staging,
  publication, and marker completion; a claim left behind by an interrupted
  run causes ``needs_fix`` and never an automatic retry;
- VM-local result staging outside the share (the Gate 4 runner never touches
  the shared outbox directly);
- atomic publication: the validated one-row staging result is copied to a
  same-directory temporary file in the outbox, flushed, and atomically
  renamed to the fixed Gate 5A result-copy filename, so the final filename is
  never visible with partial content; a nonempty final outbox file is never
  appended to or overwritten.

It is lookup-only and review-only. It never writes to AutoCount, never runs
direct SQL, never creates/updates/deletes members, never exposes a network
service, tunnel, webhook, or queue API, and never activates any n8n workflow.
All VM-local state (claim, staging result, idempotency markers) must live
outside the share.
"""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path

import member_lookup_gate4_real_queue_lookup as gate4


GATE = "shared_folder_lookup_bridge_handoff"
EXECUTION_MODE = "manual_shared_folder_lookup_handoff"
LOOKUP_MODE = "powershell"
APPROVED_BATCH_SIZE = 1
INBOX_DIR_NAME = "inbox"
OUTBOX_DIR_NAME = "outbox"
PENDING_FILENAME = "member_lookup_bridge_gate4a_pending_queue.jsonl"
RESULT_FILENAME = "member_lookup_bridge_gate5a_result_copy.jsonl"
PUBLISH_TMP_FILENAME = RESULT_FILENAME + ".tmp"

INNER_COUNT_KEYS = (
    "queue_rows_read_count",
    "lookup_attempt_count",
    "lookup_success_count",
    "lookup_existing_member_review_count",
    "lookup_manual_review_count",
    "lookup_ready_for_create_review_count",
    "lookup_error_count",
    "review_rows_written_count",
)

VM_LOCAL_ARG_NAMES = ("claim_json", "staging_results_jsonl", "processed_dir", "failed_dir")

# Test-only fault-injection hook. It can only cause failures (never a false PASS):
# it simulates a hard interruption (process death without cleanup) so tests can
# prove every interrupted state stays detectable and never relaunches a lookup.
FAULT_ENV = "SHARED_FOLDER_TEST_FAULT_INJECT"


def bool_text(value):
    return "true" if value else "false"


def zero_counts():
    return {key: "0" for key in INNER_COUNT_KEYS}


def evidence_rows(status, counts, flags):
    return [
        ("status", status),
        ("gate", GATE),
        ("runtime_location", "autocount_vm_bridge_worker"),
        ("n8n_runtime_location", "main_physical_pc"),
        ("transport", "private_host_vm_shared_folder"),
        ("execution_mode", EXECUTION_MODE),
        ("lookup_mode", LOOKUP_MODE),
        ("powershell_lookup_enabled", bool_text(flags["powershell_lookup_enabled"])),
        ("ac2_lookup_invoked", bool_text(flags["ac2_lookup_invoked"])),
        ("approved_batch_size", APPROVED_BATCH_SIZE),
        *[(key, counts[key]) for key in INNER_COUNT_KEYS],
        ("claim_acquired", bool_text(flags["claim_acquired"])),
        ("preexisting_claim_detected", bool_text(flags["preexisting_claim_detected"])),
        ("outbox_published", bool_text(flags["outbox_published"])),
        ("vm_local_state_outside_share", bool_text(flags["vm_local_state_outside_share"])),
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
        Path(path).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


def vm_local_state_outside_share(args, share_root):
    for name in VM_LOCAL_ARG_NAMES:
        vm_path = Path(getattr(args, name))
        if is_inside(vm_path, share_root) or is_inside(share_root, vm_path):
            return False
    return True


def share_layout_ready(share_root, pending_path, outbox_dir):
    return share_root.is_dir() and pending_path.parent.is_dir() and outbox_dir.is_dir() and pending_path.is_file()


def maybe_fault(point):
    if os.environ.get(FAULT_ENV) == point:
        os._exit(9)


def acquire_claim(claim_path):
    """Atomically create the VM-local exclusive execution claim.

    Returns True when this process created the claim. An existing claim (from a
    concurrent or interrupted run) makes acquisition fail; the claim is never
    inspected, repaired, or removed here.
    """
    path = Path(claim_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "gate": GATE,
                    "claim_type": "exclusive_execution",
                    "dry_run_only": True,
                    "final_write_automation": False,
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    return True


def release_claim(claim_path):
    try:
        Path(claim_path).unlink()
    except OSError:
        pass


def run_gate4(args, pending_path):
    """Delegate the whole validation/lookup/state machine to the Gate 4 runner.

    Runs in-process with stdout captured; Gate 4 evidence is aggregate-only and
    sanitized, so parsing it leaks no row-level data. Returns (status, evidence).
    """
    argv = [
        "--enable-gate4-real-queue-lookup",
        "--enable-powershell-lookup",
        "--queue-jsonl",
        str(pending_path),
        "--results-jsonl",
        args.staging_results_jsonl,
        "--processed-dir",
        args.processed_dir,
        "--failed-dir",
        args.failed_dir,
        "--lookup-script",
        args.lookup_script,
        "--powershell-exe",
        args.powershell_exe,
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.allow_root_login:
        argv.append("--allow-root-login")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        gate4.main(argv)
    inner = {}
    for line in buffer.getvalue().splitlines():
        if " = " in line:
            key, value = line.split(" = ", 1)
            inner[key.strip()] = value.strip()
    return inner.get("status", "needs_fix"), inner


def inner_counts(inner):
    return {key: inner.get(key, "0") for key in INNER_COUNT_KEYS}


def read_single_result_row(path):
    """Return the parsed single sanitized result row, or None on any deviation.

    Exactly one nonblank JSON-object line is required; a missing file, blank
    file, malformed line, non-object line, or extra row all return None. No
    content is printed or logged.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        rows.append(value)
    if len(rows) != 1:
        return None
    return rows[0]


def outbox_nonempty(final_path):
    try:
        return final_path.is_file() and final_path.stat().st_size > 0
    except OSError:
        return True


def staging_has_content(path):
    try:
        file_path = Path(path)
        if not file_path.is_file():
            return False
        return any(line.strip() for line in file_path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return True


def publish_atomically(staging_path, outbox_dir, final_path):
    """Copy the validated staging content to the outbox via temp file + atomic rename.

    The fixed final filename never becomes visible with partial content. This
    relies on same-directory atomic rename/replace semantics on the share; the
    runbook documents the supported share/filesystem expectation. Failure leaves
    at most the temporary file behind and never a partial final file.
    """
    text = Path(staging_path).read_text(encoding="utf-8")
    tmp_path = outbox_dir / PUBLISH_TMP_FILENAME
    with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, final_path)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run the VM-side manual shared-folder lookup handoff for exactly one "
            "canonical Gate 4A queue row, delegating validation, lookup, and "
            "marker/result state to the proven Gate 4 real-queue runner. "
            "PowerShell-only; there is no mock route."
        )
    )
    parser.add_argument("--enable-shared-folder-handoff-review", action="store_true")
    parser.add_argument("--enable-powershell-lookup", action="store_true")
    parser.add_argument("--share-root", required=True)
    parser.add_argument("--claim-json", required=True)
    parser.add_argument("--staging-results-jsonl", required=True)
    parser.add_argument("--processed-dir", required=True)
    parser.add_argument("--failed-dir", required=True)
    parser.add_argument("--lookup-script", default=str(Path("scripts") / "ac2_member_lookup_review.ps1"))
    parser.add_argument("--powershell-exe", default="powershell")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--allow-root-login", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    share_root = Path(args.share_root)
    flags = {
        "powershell_lookup_enabled": bool(args.enable_powershell_lookup),
        "ac2_lookup_invoked": False,
        "claim_acquired": False,
        "preexisting_claim_detected": False,
        "outbox_published": False,
        "vm_local_state_outside_share": vm_local_state_outside_share(args, share_root),
    }

    def emit(status, counts=None):
        print_evidence(evidence_rows(status, counts or zero_counts(), flags))

    # A. Both explicit opt-ins are required before anything else.
    if not args.enable_shared_folder_handoff_review or not args.enable_powershell_lookup:
        emit("refused")
        return 2

    # B. Every piece of VM-local state must live outside the share.
    if not flags["vm_local_state_outside_share"]:
        emit("refused")
        return 2

    pending_path = share_root / INBOX_DIR_NAME / PENDING_FILENAME
    outbox_dir = share_root / OUTBOX_DIR_NAME
    final_path = outbox_dir / RESULT_FILENAME
    if not share_layout_ready(share_root, pending_path, outbox_dir):
        emit("needs_fix")
        return 2

    # C. Exclusive VM-local claim, atomically, before any marker/result inspection
    # and before any lookup. A claim left by an interrupted run blocks here and
    # requires the documented operator recovery; it is never auto-removed.
    if not acquire_claim(args.claim_json):
        flags["preexisting_claim_detected"] = True
        emit("needs_fix")
        return 2
    flags["claim_acquired"] = True
    maybe_fault("after_claim")

    def finish(status, counts=None, *, exit_code):
        # Controlled completion: the run reached a deterministic terminal state,
        # so the exclusive claim is released. A hard interruption never reaches
        # this point and leaves the claim in place (fail closed).
        release_claim(args.claim_json)
        emit(status, counts)
        return exit_code

    if outbox_nonempty(final_path):
        # D. A nonempty final outbox is acceptable only as the exact idempotent
        # result of a completed run. It is never appended to, overwritten, or
        # repaired. Without staged evidence there is nothing to correlate, and
        # delegating with empty VM-local state would run a fresh lookup, so
        # fail closed first.
        if not staging_has_content(args.staging_results_jsonl):
            return finish("needs_fix", exit_code=2)
        inner_status, inner = run_gate4(args, pending_path)
        flags["ac2_lookup_invoked"] = inner.get("ac2_lookup_invoked") == "true"
        counts = inner_counts(inner)
        if inner_status != "already_processed":
            return finish("needs_fix", counts, exit_code=2)
        staged_row = read_single_result_row(args.staging_results_jsonl)
        outbox_row = read_single_result_row(final_path)
        if staged_row is None or outbox_row is None or staged_row != outbox_row:
            return finish("needs_fix", counts, exit_code=2)
        return finish("already_processed", counts, exit_code=0)

    # E. Empty outbox: delegate the full canonical validation, lookup, and
    # marker/result state machine to the Gate 4 runner against VM-local staging.
    inner_status, inner = run_gate4(args, pending_path)
    flags["ac2_lookup_invoked"] = inner.get("ac2_lookup_invoked") == "true"
    counts = inner_counts(inner)

    if inner_status == "already_processed":
        # Staged result and marker are complete but the outbox was never
        # published: an interrupted publication. Detectable, never auto-repaired,
        # and never a reason to run another lookup.
        return finish("needs_fix", counts, exit_code=2)
    if inner_status != "ok":
        return finish("needs_fix", counts, exit_code=2)

    # F. Fresh success: Gate 4 validated exactly one sanitized result in staging.
    if read_single_result_row(args.staging_results_jsonl) is None:
        return finish("needs_fix", counts, exit_code=2)
    maybe_fault("after_staging_before_publish")
    try:
        publish_atomically(args.staging_results_jsonl, outbox_dir, final_path)
    except OSError:
        return finish("needs_fix", counts, exit_code=2)
    flags["outbox_published"] = True
    maybe_fault("after_publish_before_claim_cleanup")
    return finish("ok", counts, exit_code=0)


if __name__ == "__main__":
    raise SystemExit(main())
