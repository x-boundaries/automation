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

- fixed inbox/outbox contract paths inside the private share, where the share
  root, inbox, and outbox directory entries must themselves be genuine
  directories (lstat-based, reparse-point-aware): a symlink, junction, or any
  other redirected/reparse entry is rejected fail-closed and never followed or
  resolved;
- one VM-local exclusive execution claim (atomic ``O_CREAT | O_EXCL``) that is
  acquired before any state inspection or lookup and held through staging,
  publication, and marker completion; acquisition is complete only once the
  claim entry is durable for the platform contract (POSIX adds a parent
  directory fsync), an unconfirmed durable commit fails closed before any
  lookup with the indeterminate entry left in place, and a claim left behind
  by an interrupted run causes ``needs_fix`` and never an automatic retry;
- VM-local result staging outside the share (the Gate 4 runner never touches
  the shared outbox directly);
- fail-closed final-path classification: any pre-existing artifact at the
  fixed final result path (including a zero-byte file, directory, symbolic or
  broken link, or an unreadable/stat-failing entry) blocks the run before any
  lookup; only a regular nonempty file can enter idempotent verification; the
  fixed publication temp path is classified the same way (only a completely
  absent entry permits a fresh lookup), a Windows reparse-point entry at
  either fixed path is likewise blocked (never read through, truncated,
  replaced, renamed, repaired, or deleted), and the fixed pending-queue entry
  must itself be a non-link regular file (lstat-based, reparse-point-aware,
  links never followed);
- a POSIX-only, no-data publication-capability preflight before the Gate 4
  delegation: empty synthetic probe entries prove exclusive create-new,
  hard-link create-new, and directory-fsync support in the outbox so a
  filesystem that cannot honour durable no-replace publication fails closed
  with zero AC2 lookups (the Windows publication path is neither exercised
  nor weakened by the preflight);
- durable atomic no-replace publication: the validated one-row staging result
  is copied to a same-directory temporary file in the outbox, flushed, and
  moved to the fixed Gate 5A result-copy filename with a durable create-new
  primitive (Windows ``MoveFileExW`` with write-through and without
  replace-existing; POSIX ``os.link`` create-new plus directory fsync), so the
  final filename is never visible with partial content, an existing
  destination is never overwritten -- even one created by the other share
  participant during the lookup -- success is reported only after the commit
  is durable, and an unconfirmed commit fails closed with the claim retained;
- fail-closed claim release: if the exclusive claim cannot be released after
  a completed run, the run reports ``needs_fix`` (never ``ok`` or
  ``already_processed``), the claim stays in place for operator recovery, and
  the evidence carries ``claim_release_failed = true``.

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
import stat
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
PREFLIGHT_PROBE_SRC_FILENAME = RESULT_FILENAME + ".preflight_probe_src.tmp"
PREFLIGHT_PROBE_LINK_FILENAME = RESULT_FILENAME + ".preflight_probe_link.tmp"

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
        ("claim_durability_unconfirmed", bool_text(flags["claim_durability_unconfirmed"])),
        ("claim_release_failed", bool_text(flags["claim_release_failed"])),
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


def pending_entry_is_plain_regular_file(pending_path):
    """Fail-closed lstat classification of the fixed pending-queue entry.

    The entry must itself be a regular file: symbolic links are never followed,
    and broken links, directories, other non-regular entries, Windows
    reparse-point/redirection entries (where ``st_file_attributes`` permits
    detection), and any metadata/stat failure are all rejected. The rejected
    artifact is never read, resolved, repaired, renamed, or deleted. This is a
    filesystem-boundary check only; the delegated Gate 4 canonical content
    validation still applies afterwards.
    """
    try:
        info = os.lstat(pending_path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if reparse_flag and (attributes & reparse_flag):
        return False
    return True


def directory_entry_is_plain_directory(directory_path):
    """Fail-closed lstat classification of an approved share directory entry.

    The share root, inbox, and outbox entries must themselves be genuine
    directories: symbolic links are never followed, and links, Windows
    junctions and other reparse-point/redirection entries (where
    ``st_file_attributes`` permits detection), non-directory entries, and any
    metadata/stat failure are all rejected. The rejected entry is never
    followed, resolved, repaired, renamed, or deleted.
    """
    try:
        info = os.lstat(directory_path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if reparse_flag and (attributes & reparse_flag):
        return False
    return True


def share_layout_ready(share_root, pending_path, outbox_dir):
    return (
        directory_entry_is_plain_directory(share_root)
        and directory_entry_is_plain_directory(pending_path.parent)
        and directory_entry_is_plain_directory(outbox_dir)
        and pending_entry_is_plain_regular_file(pending_path)
    )


def maybe_fault(point):
    if os.environ.get(FAULT_ENV) == point:
        os._exit(9)


def acquire_claim(claim_path):
    """Atomically create and durably commit the VM-local exclusive claim.

    Returns ``"acquired"`` only when this process created the claim entry and
    the platform durability contract is satisfied: the claim file is fsynced
    on every platform, and on POSIX/local filesystems the parent directory is
    additionally fsynced so the new directory entry itself is durable (Windows
    behaviour is unchanged). Returns ``"exists"`` when a concurrent or
    interrupted run already holds the claim; the existing claim is never
    inspected, repaired, or removed here. Returns ``"durability_unconfirmed"``
    when the entry was created but its durable commit could not be confirmed:
    the caller must fail closed before any lookup and leave the indeterminate
    entry in place -- never deleted, repaired, retried, or recreated.
    """
    path = Path(claim_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return "exists"
    except OSError:
        return "exists"
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
    # Test-only durability-failure injection; it can only cause a failure.
    if os.environ.get(FAULT_ENV) == "fail_claim_durability":
        return "durability_unconfirmed"
    if os.name != "nt":
        try:
            fsync_directory(path.parent)
        except OSError:
            return "durability_unconfirmed"
    return "acquired"


def release_claim(claim_path):
    """Release the exclusive claim; returns True only when it is verifiably gone.

    A release failure is reported to the caller and must fail the run closed:
    the claim is left in place (never retried, repaired, or force-deleted) so
    the blocked state stays visible for operator recovery.
    """
    if os.environ.get(FAULT_ENV) == "fail_claim_release":
        return False
    try:
        Path(claim_path).unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


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
    try:
        with contextlib.redirect_stdout(buffer):
            gate4.main(argv)
    except (OSError, ValueError):
        # Includes UnicodeDecodeError from corrupted (non-UTF-8) VM-local state:
        # fail closed through the normal aggregate-only needs_fix path without
        # letting an exception (or any byte/path content) escape.
        return "needs_fix", {}
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
    except (OSError, UnicodeError):
        # Unreadable or non-UTF-8 (corrupted) content is a deviation, handled
        # through the normal needs_fix path; nothing is printed or logged.
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


class PublicationDurabilityError(OSError):
    """The final entry may exist but its durable commit or cleanup is unconfirmed.

    Distinguished from a determinate publication failure (such as an existing
    destination refusing the no-replace move, where nothing was committed) so
    the caller can retain the execution claim and block automatic progression
    after an uncertain commit.
    """


def classify_final_path(final_path):
    """Classify the fixed final result path fail-closed before any lookup.

    Returns ``absent`` (no directory entry), ``regular_nonempty`` (a plain
    regular file with content, the only state eligible for idempotent
    verification), or ``blocked`` for every other pre-existing artifact: a
    zero-byte file, directory, symbolic or broken link, Windows
    reparse-point/redirection entry (where ``st_file_attributes`` permits
    detection), unreadable entry, or any metadata/stat failure. ``os.lstat``
    is used so a link is classified as the link itself, never followed, and a
    blocked artifact is never read through, truncated, replaced, renamed,
    repaired, or deleted.
    """
    try:
        info = os.lstat(final_path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "blocked"
    if not stat.S_ISREG(info.st_mode):
        return "blocked"
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if reparse_flag and (attributes & reparse_flag):
        return "blocked"
    if info.st_size == 0:
        return "blocked"
    return "regular_nonempty"


def staging_has_content(path):
    """True when VM-local staging holds (or may hold) prior work.

    An unreadable or non-UTF-8 (corrupted) staging file conservatively counts
    as content: a fresh lookup must not run over it, and the delegated Gate 4
    inspection then routes the corrupted state to needs_fix.
    """
    try:
        file_path = Path(path)
        if not file_path.is_file():
            return False
        return any(line.strip() for line in file_path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError):
        return True


def open_exclusive_no_follow(path):
    """Open a brand-new file for writing, atomically and without following links.

    ``O_CREAT | O_EXCL`` guarantees the entry is created by this call (an
    existing regular file, symlink -- including a broken one -- directory, or
    any other entry at the path makes the open fail without modifying it, so a
    pre-existing artifact is never truncated or followed). ``O_NOFOLLOW`` is
    added where the platform supports it as defence in depth. Failure raises
    OSError and the caller fails closed.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="")


def fsync_directory(directory):
    """Fsync a directory through a real directory file descriptor.

    Raises OSError when the platform or filesystem cannot open or fsync the
    directory; the caller must fail closed rather than assume durability.
    """
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_move_no_replace_write_through(tmp_path, final_path):
    """Windows durable no-replace move via MoveFileExW.

    MOVEFILE_REPLACE_EXISTING is deliberately absent so an existing destination
    atomically refuses the move (raised as FileExistsError, a determinate
    failure that commits nothing). MOVEFILE_WRITE_THROUGH requires the move to
    be flushed to disk before the call returns, which is the metadata-durability
    guarantee plain ``os.rename`` does not provide. Any other Windows error is
    raised as PublicationDurabilityError because the commit state is
    unconfirmed.
    """
    import ctypes

    movefile_write_through = 0x8
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.MoveFileExW(str(tmp_path), str(final_path), movefile_write_through):
        error = ctypes.get_last_error()
        if error in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
            raise ctypes.WinError(error)
        raise PublicationDurabilityError(
            0, f"windows_move_write_through_failed_error_{error}"
        )


def _posix_link_publish_durable(tmp_path, final_path, outbox_dir):
    """POSIX durable no-replace publication via hard-link create-new.

    ``os.link`` atomically creates the final name only when it does not exist
    (FileExistsError and unsupported-hard-link errors propagate as determinate
    failures that commit nothing). After the link, the containing directory is
    fsynced so the new entry is durable, the temp name is removed, and the
    directory is fsynced again so the cleanup is durable. Any failure after the
    link is PublicationDurabilityError: the commit state is unconfirmed and the
    caller must not report success.
    """
    os.link(tmp_path, final_path)
    try:
        fsync_directory(outbox_dir)
        os.unlink(tmp_path)
        fsync_directory(outbox_dir)
    except OSError as error:
        raise PublicationDurabilityError(0, "publication_durability_unconfirmed") from error


def posix_publication_capability_preflight(outbox_dir):
    """Bounded no-data POSIX publication-capability preflight.

    Proves, before any AC2 lookup can be consumed, that the outbox filesystem
    supports the exact primitives durable no-replace publication requires:
    exclusive create-new (``O_CREAT | O_EXCL``), hard-link create-new
    (``os.link``), and directory fsync. Only empty synthetic probe entries at
    fixed bridge-owned names are used; no member data or staged result content
    is ever written or exposed. On success both probes are removed (each was
    verifiably created by this preflight via exclusive create / create-new
    link) and the cleanup is made durable. On any failure the preflight
    returns False, deletes nothing, and leaves whatever exists at the probe
    names for operator inspection -- including a pre-existing entry at a probe
    name, which fails the preflight closed without being touched. The Windows
    publication path is neither exercised nor weakened; this preflight never
    runs on Windows.
    """
    # Test-only injection simulating an unsupported publication primitive; it
    # can only cause a failure.
    if os.environ.get(FAULT_ENV) == "fail_publication_preflight":
        return False
    probe_src = outbox_dir / PREFLIGHT_PROBE_SRC_FILENAME
    probe_link = outbox_dir / PREFLIGHT_PROBE_LINK_FILENAME
    try:
        with open_exclusive_no_follow(probe_src):
            pass  # deliberately empty: the probe carries no data
        os.link(probe_src, probe_link)
        fsync_directory(outbox_dir)
        os.unlink(probe_link)
        os.unlink(probe_src)
        fsync_directory(outbox_dir)
    except OSError:
        return False
    return True


def publish_atomically(staging_path, outbox_dir, final_path):
    """Copy the validated staging content to the outbox via temp file + durable no-replace move.

    The fixed final filename never becomes visible with partial content, is
    never appended to, and an existing destination is never overwritten,
    renamed, repaired, or deleted -- a collision leaves it byte-for-byte
    unchanged, keeps the temp file for operator diagnosis, and raises so the
    run fails closed. Success is returned only after the platform primitive
    has durably committed the final directory entry (Windows MoveFileExW with
    write-through; POSIX hard-link create-new plus directory fsync). The
    runbook documents the exact supported filesystem expectations.
    """
    text = Path(staging_path).read_text(encoding="utf-8")
    tmp_path = outbox_dir / PUBLISH_TMP_FILENAME
    with open_exclusive_no_follow(tmp_path) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    # Test-only durability-failure injection; it can only cause a failure.
    if os.environ.get(FAULT_ENV) == "fail_publication_durability":
        raise PublicationDurabilityError(0, "test_injected_durability_failure")
    if os.name == "nt":
        _windows_move_no_replace_write_through(tmp_path, final_path)
    else:
        _posix_link_publish_durable(tmp_path, final_path, outbox_dir)


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
        "claim_durability_unconfirmed": False,
        "claim_release_failed": False,
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
    claim_state = acquire_claim(args.claim_json)
    if claim_state == "exists":
        flags["preexisting_claim_detected"] = True
        emit("needs_fix")
        return 2
    if claim_state != "acquired":
        # The claim entry was created but its durable commit is unconfirmed, so
        # acquisition is not complete: fail closed before any lookup and leave
        # the indeterminate entry in place for operator recovery (never
        # deleted, repaired, retried, or recreated). The next run detects it as
        # a preexisting claim and stays blocked until the documented manual
        # recovery.
        flags["claim_durability_unconfirmed"] = True
        emit("needs_fix")
        return 2
    flags["claim_acquired"] = True
    maybe_fault("after_claim")

    def finish(status, counts=None, *, exit_code):
        # Controlled completion: the run reached a deterministic terminal state,
        # so the exclusive claim is released. A hard interruption never reaches
        # this point and leaves the claim in place (fail closed). A failed
        # release also fails closed: it can never surface as ok or
        # already_processed, the claim stays for operator recovery (no retry,
        # repair, or force-delete), and the evidence reports the failure.
        if not release_claim(args.claim_json):
            flags["claim_release_failed"] = True
            if status in ("ok", "already_processed"):
                status = "needs_fix"
            exit_code = 2
        emit(status, counts)
        return exit_code

    # D. Fail-closed final-path classification before any lookup. Any pre-existing
    # artifact at the fixed final path -- including a zero-byte file, directory,
    # symbolic or broken link, or an entry whose metadata cannot be read -- blocks
    # the run, because no-replace publication could never succeed and a lookup
    # would be consumed for nothing. The artifact is never deleted, truncated,
    # renamed, replaced, or repaired.
    final_state = classify_final_path(final_path)
    if final_state == "blocked":
        return finish("needs_fix", exit_code=2)

    # The fixed publication temp path is classified with the same fail-closed
    # rules: any pre-existing entry there (regular file, symlink, broken link,
    # directory, or an unreadable/stat-failing artifact) means publication is
    # already guaranteed to be blocked, so no lookup may be consumed and the
    # artifact is left untouched for operator review.
    if classify_final_path(outbox_dir / PUBLISH_TMP_FILENAME) != "absent":
        return finish("needs_fix", exit_code=2)

    if final_state == "regular_nonempty":
        # A regular nonempty final outbox is acceptable only as the exact
        # idempotent result of a completed run. It is never appended to,
        # overwritten, or repaired. Without staged evidence there is nothing to
        # correlate, and delegating with empty VM-local state would run a fresh
        # lookup, so fail closed first.
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

    # POSIX-only publication-capability preflight, after the claim and the
    # final/temp-path classification but before any Gate 4 delegation: if the
    # outbox filesystem cannot honour the durable no-replace publication
    # primitives, no AC2 lookup may be consumed. The empty synthetic probes
    # carry no data; a failed preflight deletes nothing and leaves the probe
    # names for operator inspection. The Windows publication path is neither
    # exercised nor weakened here.
    if os.name != "nt" and not posix_publication_capability_preflight(outbox_dir):
        return finish("needs_fix", exit_code=2)

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
    except PublicationDurabilityError:
        # The commit state is unconfirmed (write-through or directory-fsync
        # failure). The claim is deliberately retained so the state cannot
        # progress automatically after an uncertain commit; operator recovery
        # per the runbook is required. Nothing is deleted or repaired.
        emit("needs_fix", counts)
        return 2
    except OSError:
        # Determinate publication failure (for example an existing destination
        # refusing the no-replace move): nothing was committed, the destination
        # is untouched, and the claim is released via the normal terminal path.
        return finish("needs_fix", counts, exit_code=2)
    flags["outbox_published"] = True
    maybe_fault("after_publish_before_claim_cleanup")
    return finish("ok", counts, exit_code=0)


if __name__ == "__main__":
    raise SystemExit(main())
