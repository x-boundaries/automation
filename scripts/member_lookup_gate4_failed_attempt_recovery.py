"""Gate 4 failed-attempt recovery wrapper (exactly one approved read-only retry).

The first genuine Gate 4 run against the single approved Gate 4A queue row failed with
``LOOKUP_ERROR_REVIEW`` solely because the four required ``AC2_PROBE_*`` runtime
environment values were absent from the Gate 4 process. Subsequent diagnosis proved the
AutoCount assemblies, the queue row, and the authentication path are all healthy. This
wrapper authorizes exactly one explicitly approved recovery retry of the same approved
queue row, and nothing else.

Fail-closed recovery contract:

- **Explicit opt-in.** ``--enable-gate4-failed-attempt-recovery``,
  ``--confirm-original-evidence-preserved``, and ``--enable-powershell-lookup`` are all
  required. Default invocation refuses before reading the queue, before authentication,
  and before any lookup. PowerShell is the only lookup route; there is no mock route.
- **Original evidence is immutable.** The original Gate 4 queue, results file, processed
  directory, and failed directory are opened read-only for aggregate structural
  validation and are never deleted, renamed, moved, overwritten, truncated, appended to,
  or repaired by any code path in this wrapper. Validation refusals write nothing at all.
- **Strict original-attempt validation before anything else.** The original attempt must
  be exactly: one canonical approved Gate 4A queue row (revalidated with the existing
  Gate 4A precheck plus the Gate 4 canonical member-number and payload-identity
  checks), one complete ``LOOKUP_ERROR_REVIEW`` durable result row matching that queue
  job and matching the exact diagnosed original failure signature (``status = error``,
  authentication/session/command/lookup flags all false, the recorded
  ``already_65_mobile`` length-10 lookup shape, ``error_code = runtimeexception``,
  ``dry_run_only = true``, ``final_write_automation = false``), an absent or empty
  processed directory, and exactly one clean matching dead-letter failed marker. Any
  mismatch, extra, malformed, unreadable, temporary, unrelated, or duplicate artifact is
  ``needs_fix`` with no authentication preflight and no lookup. Recovery is authorized
  only for that one diagnosed incident, never for an arbitrary failed attempt.
- **Isolated recovery paths.** The retry writes only to dedicated recovery
  claim/result/marker paths that must be disjoint from every original path. This wrapper
  defines exactly one recovery generation; it never derives another numbered recovery
  generation, and any existing recovery artifact blocks another lookup fail-closed. The
  recovery results path must be completely absent before an attempt: a zero-byte file,
  whitespace-only file, directory, or any other existing filesystem object at that path
  is blocking, not just a file with rows.
- **Atomic permanent attempt claim.** After every validation and a successful
  authentication preflight, and immediately before the member lookup, the wrapper
  atomically and exclusively creates a permanent attempt-claim file (equivalent of
  ``O_CREAT | O_EXCL``) holding a fixed sanitized non-PII structure. Exclusive creation
  alone is insufficient: every byte of the claim payload is written with short-write
  handling and durably flushed, and the persisted claim is then re-read through the
  strict claim validator; only after it validates exactly may the lookup run. Only the
  process that wins, persists, and revalidates the claim may invoke the lookup, so two
  concurrent processes can never both run it. Claim creation consumes the single
  approved lookup attempt — including when persistence fails partway (short write,
  flush failure, corruption), in which case the partial claim object stays in place,
  evidence reports its presence accurately from the filesystem, and every future lookup
  is permanently blocked with no automated repair or retry. The claim is never removed,
  reset, overwritten, renamed, or repaired by any code path; any later or concurrent
  process that encounters it performs zero lookups (terminal ``already_processed`` only
  for a valid claim plus a fully valid successful result/processed pair, otherwise
  terminal ``needs_fix``), so a crash after claim creation blocks every future lookup.
  Authentication preflight failure happens before claim creation and therefore does not
  consume the attempt.
- **Authentication preflight.** Before the recovery lookup, the existing
  authentication-only probe ``scripts/ac2_session_auth_probe.ps1`` runs in the inherited
  process environment with ``-JsonOut`` to a local temporary file (deleted after
  reading). All four ``AC2_PROBE_*`` values must be present and nonblank in this process
  (their values are never printed or persisted), the root-login switch is never passed,
  and the probe must report ``authentication_success``, ``user_session_available``, and
  ``instance_login_success`` all true with no error before any member lookup starts. An
  authentication failure stops before MemberCommand or GetMember is ever invoked.
- **Exactly one real read-only retry.** The lookup reuses the proven worker path
  (``ac2_member_lookup_bridge_worker`` calling ``scripts/ac2_member_lookup_review.ps1``
  with ``-EnableMemberLookupReview`` and ``-MemberNoBase64Utf8`` only), classifies only
  into the existing review states, and preserves the Gate 4 result-first/marker-second
  durability discipline. ``READY_FOR_CREATE_REVIEW`` remains review-only.
- **Aggregate-only evidence.** Output is booleans, statuses, and counts only. No queue
  rows, result rows, marker contents or filenames, job IDs, hashes, member values,
  credentials, AC2 target values, transcripts, or PII are ever printed.

This wrapper never writes to AutoCount, never performs direct SQL, never activates n8n,
never maps results to n8n, and installs no scheduler, service, webhook, or tunnel.
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import ac2_member_lookup_bridge_worker as worker
import member_lookup_gate4_real_queue_lookup as gate4
import member_lookup_gate4a_queue_precheck as gate4a_precheck


GATE = "gate4_failed_attempt_recovery_lookup_only"
RUNTIME_LOCATION = "windows_ac2_lookup_bridge_host"
EXECUTION_MODE = "manual_read_only_single_recovery_review_only"
LOOKUP_MODE = "powershell"
APPROVED_BATCH_SIZE = 1

# This wrapper defines exactly one recovery generation. It never derives another
# numbered generation, and existing recovery artifacts block another lookup fail-closed.
RECOVERY_GENERATION = 1

LOOKUP_ERROR_REVIEW = gate4.LOOKUP_ERROR_REVIEW
EXISTING_MEMBER_REVIEW = gate4.EXISTING_MEMBER_REVIEW
MANUAL_REVIEW_REQUIRED = gate4.MANUAL_REVIEW_REQUIRED
READY_FOR_CREATE_REVIEW = gate4.READY_FOR_CREATE_REVIEW

# The four runtime-only process values the original failed attempt was missing. Their
# presence is checked as nonblank only; the values are never printed or persisted.
REQUIRED_AUTH_ENV_VARS = (
    "AC2_PROBE_SERVER_NAME",
    "AC2_PROBE_DATABASE_NAME",
    "AC2_PROBE_USER_ID",
    "AC2_PROBE_PASSWORD",
)

# Sanitized boolean flags the auth probe JSON must report true before any lookup.
REQUIRED_AUTH_PROBE_TRUE_FLAGS = (
    "session_probe_enabled",
    "authentication_success",
    "user_session_available",
    "instance_login_success",
)

# Root-login must never be requested or applied by the recovery preflight.
FORBIDDEN_AUTH_PROBE_TRUE_FLAGS = (
    "allow_root_login_requested",
    "allow_root_login_set",
)

# The exact sanitized signature of the one diagnosed original Gate 4 failure. Recovery
# is authorized only for an original durable result that matches every field exactly;
# any other failed attempt (including status = refused or a different error code) is
# out of scope for this reviewed recovery and must be re-diagnosed separately.
APPROVED_ORIGINAL_FAILURE_SIGNATURE = {
    "state": LOOKUP_ERROR_REVIEW,
    "status": "error",
    "authentication_success": False,
    "user_session_available": False,
    "member_command_found": False,
    "get_member_found": False,
    "submitted_member_no_status": "already_65_mobile",
    "normalized_member_no_length": 10,
    "member_exists": False,
    "member_found_by": None,
    "manual_review_required": False,
    "warning_count": 0,
    "error_code": "runtimeexception",
    "dry_run_only": True,
    "final_write_automation": False,
    "result_applied_at": None,
}

# Fixed sanitized non-PII content of the permanent recovery attempt claim. The claim
# carries no job identity, hash, timestamp, or member data; its existence alone is the
# consumed-attempt signal, and a claim file must equal this structure exactly.
ATTEMPT_CLAIM_STRUCTURE = {
    "claim_type": "gate4_recovery1_attempt_started",
    "recovery_generation": RECOVERY_GENERATION,
    "single_attempt_only": True,
    "dry_run_only": True,
    "final_write_automation": False,
}

# Test-only fault-injection hook, same discipline as Gate 4: it can only cause failures
# (never a false PASS) and proves an interrupted run stays detectable and blocking.
FAULT_ENV = "GATE4_RECOVERY_TEST_FAULT_INJECT"


def zero_counts():
    return {
        "queue_rows_read_count": 0,
        "lookup_attempt_count": 0,
        "lookup_success_count": 0,
        "lookup_existing_member_review_count": 0,
        "lookup_manual_review_count": 0,
        "lookup_ready_for_create_review_count": 0,
        "lookup_error_count": 0,
        "recovery_result_rows_written_count": 0,
        "recovery_processed_artifact_count": 0,
        "recovery_failed_artifact_count": 0,
    }


def zero_flags(*, powershell_lookup_enabled=False):
    return {
        "powershell_lookup_enabled": powershell_lookup_enabled,
        "original_failure_validated": False,
        "recovery_paths_isolated": False,
        "recovery_attempt_claim_present": False,
        "recovery_attempt_claim_created_by_this_run": False,
        "recovery_attempt_consumed": False,
        "auth_preflight_invoked": False,
        "auth_preflight_success": False,
        "ac2_lookup_invoked": False,
    }


def evidence_rows(status, counts, flags):
    bool_text = gate4.bool_text
    return [
        ("status", status),
        ("gate", GATE),
        ("runtime_location", RUNTIME_LOCATION),
        ("execution_mode", EXECUTION_MODE),
        ("lookup_mode", LOOKUP_MODE),
        ("powershell_lookup_enabled", bool_text(flags["powershell_lookup_enabled"])),
        ("recovery_generation", RECOVERY_GENERATION),
        ("original_failure_validated", bool_text(flags["original_failure_validated"])),
        ("original_artifacts_modified", "false"),
        ("recovery_paths_isolated", bool_text(flags["recovery_paths_isolated"])),
        ("recovery_attempt_claim_present", bool_text(flags["recovery_attempt_claim_present"])),
        (
            "recovery_attempt_claim_created_by_this_run",
            bool_text(flags["recovery_attempt_claim_created_by_this_run"]),
        ),
        ("recovery_attempt_consumed", bool_text(flags["recovery_attempt_consumed"])),
        ("auth_preflight_invoked", bool_text(flags["auth_preflight_invoked"])),
        ("auth_preflight_success", bool_text(flags["auth_preflight_success"])),
        ("allow_root_login_used", "false"),
        ("ac2_lookup_invoked", bool_text(flags["ac2_lookup_invoked"])),
        ("approved_batch_size", APPROVED_BATCH_SIZE),
        ("queue_rows_read_count", counts["queue_rows_read_count"]),
        ("lookup_attempt_count", counts["lookup_attempt_count"]),
        ("lookup_success_count", counts["lookup_success_count"]),
        ("lookup_existing_member_review_count", counts["lookup_existing_member_review_count"]),
        ("lookup_manual_review_count", counts["lookup_manual_review_count"]),
        ("lookup_ready_for_create_review_count", counts["lookup_ready_for_create_review_count"]),
        ("lookup_error_count", counts["lookup_error_count"]),
        ("recovery_result_rows_written_count", counts["recovery_result_rows_written_count"]),
        ("recovery_processed_artifact_count", counts["recovery_processed_artifact_count"]),
        ("recovery_failed_artifact_count", counts["recovery_failed_artifact_count"]),
        ("member_create_or_update_invoked", "false"),
        ("autocount_write_attempted", "false"),
        ("direct_sql_write_attempted", "false"),
        ("n8n_result_mapping_run", "false"),
        ("workflow_activation", "inactive"),
        ("scheduler_enabled", "false"),
        ("public_inbound_to_ac2_host", "false"),
        ("final_write_automation", "false"),
        ("no_row_values_printed", "true"),
    ]


def print_evidence(status, counts, flags):
    for key, value in evidence_rows(status, counts, flags):
        print(f"{key} = {value}")


def paths_conflict(first, second):
    """True when two configured paths resolve equal or one contains the other."""
    first = Path(first).resolve()
    second = Path(second).resolve()
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def recovery_paths_are_isolated(args):
    """Every recovery path must be disjoint from every original path and each other.

    This guarantees no recovery write can land inside (or on top of) the original queue,
    results file, processed directory, or failed directory.
    """
    original_paths = (
        args.queue_jsonl,
        args.original_results_jsonl,
        args.original_processed_dir,
        args.original_failed_dir,
    )
    recovery_paths = (
        args.recovery_attempt_claim_json,
        args.recovery_results_jsonl,
        args.recovery_processed_dir,
        args.recovery_failed_dir,
    )
    for recovery_path in recovery_paths:
        for original_path in original_paths:
            if paths_conflict(recovery_path, original_path):
                return False
    for index, recovery_path in enumerate(recovery_paths):
        for other_path in recovery_paths[index + 1 :]:
            if paths_conflict(recovery_path, other_path):
                return False
    return True


def signature_field_matches(result, field, expected):
    """Exact type-safe match: booleans by identity, ints excluding bools, else equality."""
    value = result.get(field)
    if expected is None or isinstance(expected, bool):
        return value is expected
    if isinstance(expected, int):
        return gate4.exact_int(value, expected)
    return value == expected


def original_failed_result_valid(result, job):
    """Strictly validate the single original ``LOOKUP_ERROR_REVIEW`` durable result.

    The result must carry the exact allowed sanitized envelope, match the approved queue
    job identity, and match every field of the exact diagnosed original failure
    signature. Any other failed attempt — including ``status = refused`` or a different
    error code — is out of scope for this reviewed recovery.
    """
    if not isinstance(result, dict) or set(result) != worker.ALLOWED_RESULT_FIELDS:
        return False
    job_id = worker.safe_job_id(job.get("job_id"))
    if job_id is None or worker.safe_job_id(result.get("job_id")) != job_id:
        return False
    for field in ("intake_source", "source_reference", "source_row_ref", "consent_status"):
        if result.get(field) != worker.safe_source_value(job.get(field)):
            return False
    if result.get("row_number") != worker.safe_row_number(job.get("row_number")):
        return False
    if result.get("pdpa_status") != "yes":
        return False
    if result.get("attempt") != worker.safe_attempt(job.get("attempt")):
        return False
    for field, expected in APPROVED_ORIGINAL_FAILURE_SIGNATURE.items():
        if not signature_field_matches(result, field, expected):
            return False
    return True


def original_failed_marker_valid(marker, job_id, payload_hash, original_result):
    """The single original failed marker must be the clean dead-letter for this job."""
    if not isinstance(marker, dict):
        return False
    if marker.get("marker_type") != "dead_letter":
        return False
    if worker.safe_job_id(marker.get("job_id")) != job_id:
        return False
    marker_hash = marker.get("payload_hash")
    if not isinstance(marker_hash, str) or not marker_hash or marker_hash != payload_hash:
        return False
    if marker.get("state") != LOOKUP_ERROR_REVIEW:
        return False
    if marker.get("status") != original_result.get("status"):
        return False
    if marker.get("error_code") != original_result.get("error_code"):
        return False
    if marker.get("dry_run_only") is not True:
        return False
    if marker.get("final_write_automation") is not False:
        return False
    return True


def original_failed_attempt_valid(args, job, job_id, payload_hash):
    """Aggregate-structure validation of the complete original failed attempt.

    Read-only: no original artifact is created, modified, or removed here, and no
    row, filename, hash, or field value is returned for printing.
    """
    original_results = gate4.inspect_results(args.original_results_jsonl, job_id)
    if not original_results["exists"]:
        return False
    if (
        original_results["total_nonblank"] != 1
        or original_results["malformed_lines"] != 0
        or original_results["non_object_lines"] != 0
        or original_results["unexpected_schema_rows"] != 0
        or original_results["other_job_rows"] != 0
        or original_results["expected_job_rows"] != 1
        or len(original_results["rows"]) != 1
    ):
        return False
    if not original_failed_result_valid(original_results["rows"][0], job):
        return False

    original_processed = gate4.inspect_markers(args.original_processed_dir, job_id)
    if not gate4.marker_dir_empty(original_processed):
        return False

    original_failed = gate4.inspect_markers(args.original_failed_dir, job_id)
    if not gate4.marker_dir_has_exactly_one_clean(original_failed):
        return False
    if not original_failed_marker_valid(
        original_failed["expected_marker"], job_id, payload_hash, original_results["rows"][0]
    ):
        return False
    return True


def required_auth_env_present():
    """All four AC2_PROBE_* process values must be present and nonblank.

    Presence only: the values themselves are never read into evidence, printed,
    or persisted.
    """
    return all(os.environ.get(name, "").strip() for name in REQUIRED_AUTH_ENV_VARS)


def run_auth_preflight(args):
    """Run the existing authentication-only probe; return an aggregate boolean only.

    The probe runs in the inherited process environment, never with the root-login
    switch, and writes sanitized JSON via ``-JsonOut`` to a local temporary file deleted
    after the booleans are read. Probe stdout/stderr are captured and discarded, never
    printed. Any process, parse, or flag failure is a preflight failure that stops the
    recovery before MemberCommand or GetMember is invoked.
    """
    with tempfile.TemporaryDirectory(prefix="gate4_recovery_auth_preflight_") as tmp_dir:
        json_out = Path(tmp_dir) / "auth_preflight.json"
        command = [
            args.powershell_exe,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(Path(args.auth_probe_script)),
            "-EnableSessionProbe",
            "-JsonOut",
            str(json_out),
        ]
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=args.timeout_seconds,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        if completed.returncode != 0:
            return False
        try:
            # Windows PowerShell 5.1 Set-Content -Encoding UTF8 writes a BOM.
            payload = json.loads(json_out.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return False
    if not isinstance(payload, dict):
        return False
    if payload.get("error") is not None:
        return False
    for flag in REQUIRED_AUTH_PROBE_TRUE_FLAGS:
        if payload.get(flag) is not True:
            return False
    for flag in FORBIDDEN_AUTH_PROBE_TRUE_FLAGS:
        if payload.get(flag) is not False:
            return False
    return True


def recovery_artifact_count(directory):
    """Count artifacts in a recovery marker directory without exposing names."""
    path = Path(directory)
    if not path.exists():
        return 0
    try:
        return sum(1 for _ in path.iterdir())
    except OSError:
        return 0


def recovery_results_path_absent(path):
    """True only when nothing exists at the recovery results path.

    Stricter than the Gate 4 empty-results rule on purpose: a zero-byte file,
    whitespace-only file, malformed file, partial file, directory, broken link, or any
    other filesystem object at this path blocks a fresh recovery attempt fail-closed.
    """
    return not os.path.lexists(path)


def attempt_claim_state(path):
    """Classify the permanent attempt claim as absent, valid, or invalid.

    Aggregate classification only: no claim content, filename, or path detail is ever
    returned for printing. Anything present that is not a regular file holding exactly
    the fixed sanitized claim structure is ``invalid`` (still a consumed/blocking claim,
    but never eligible for ``already_processed``).
    """
    if not os.path.lexists(path):
        return "absent"
    claim_path = Path(path)
    if not claim_path.is_file():
        return "invalid"
    try:
        payload = json.loads(claim_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "invalid"
    return "valid" if payload == ATTEMPT_CLAIM_STRUCTURE else "invalid"


def write_payload_fully(handle, payload, os_write):
    """Write every byte of ``payload`` to ``handle``, retrying short writes.

    A raw ``os.write`` may legally write fewer bytes than requested without raising, so
    the remainder is rewritten until the payload is complete. A zero-byte (or invalid)
    write result is treated as failure rather than looping forever.
    """
    view = memoryview(payload)
    while len(view) > 0:
        written = os_write(handle, view)
        if not isinstance(written, int) or written <= 0:
            raise OSError("claim payload write made no progress")
        view = view[written:]


def create_attempt_claim(path, *, os_write=os.write, os_fsync=os.fsync):
    """Atomically and exclusively create the permanent attempt claim, durably flushed.

    Uses ``O_CREAT | O_EXCL`` so exactly one process can ever win the claim; every other
    concurrent or later creation attempt raises ``FileExistsError``. Exclusive creation
    alone is insufficient: every byte of the fixed sanitized non-PII payload is written
    with short-write handling and durably flushed before creation counts as complete,
    and the caller must still revalidate the persisted claim before any lookup. Nothing
    ever overwrites, truncates, removes, renames, resets, or repairs an existing claim;
    if persistence fails partway, the partial claim object stays in place and
    permanently blocks the single attempt.

    ``os_write``/``os_fsync`` exist only for dependency injection in tests (simulating
    short writes and flush failures); production callers use the real defaults.
    """
    claim_path = Path(path)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(ATTEMPT_CLAIM_STRUCTURE, sort_keys=True) + "\n").encode("utf-8")
    handle = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        write_payload_fully(handle, payload, os_write)
        os_fsync(handle)
    finally:
        os.close(handle)


def maybe_fault(point):
    if os.environ.get(FAULT_ENV) == point:
        raise RuntimeError("gate4_recovery_test_fault_injected")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly one explicitly approved read-only recovery retry of the failed "
            "Gate 4 AC2 member lookup, writing only to isolated recovery result/marker "
            "paths and preserving every original Gate 4 artifact byte-for-byte. "
            "PowerShell-only; there is no mock route and no AllowRootLogin option."
        )
    )
    parser.add_argument("--enable-gate4-failed-attempt-recovery", action="store_true")
    parser.add_argument("--confirm-original-evidence-preserved", action="store_true")
    parser.add_argument("--enable-powershell-lookup", action="store_true")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--original-results-jsonl", required=True)
    parser.add_argument("--original-processed-dir", required=True)
    parser.add_argument("--original-failed-dir", required=True)
    parser.add_argument("--recovery-attempt-claim-json", required=True)
    parser.add_argument("--recovery-results-jsonl", required=True)
    parser.add_argument("--recovery-processed-dir", required=True)
    parser.add_argument("--recovery-failed-dir", required=True)
    parser.add_argument(
        "--auth-probe-script",
        default=str(Path("scripts") / "ac2_session_auth_probe.ps1"),
    )
    parser.add_argument(
        "--lookup-script",
        default=str(Path("scripts") / "ac2_member_lookup_review.ps1"),
    )
    parser.add_argument("--powershell-exe", default="powershell")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    ps_enabled = bool(args.enable_powershell_lookup)
    flags = zero_flags(powershell_lookup_enabled=ps_enabled)

    def emit(status, counts):
        print_evidence(status, counts, flags)

    # A. All three explicit opt-ins are required; refuse before reading anything.
    if not (
        args.enable_gate4_failed_attempt_recovery
        and args.confirm_original_evidence_preserved
        and args.enable_powershell_lookup
    ):
        emit("refused", zero_counts())
        return 2

    # B. Recovery paths must be fully isolated from every original path before any
    # filesystem inspection, so no later write can touch original evidence.
    if not recovery_paths_are_isolated(args):
        emit("needs_fix", zero_counts())
        return 2
    flags["recovery_paths_isolated"] = True

    # C. Revalidate the approved queue row with the existing Gate 4 controls (Gate 4A
    # precheck, canonical member contract, canonical payload identity, worker contract,
    # fixed retry-contract fields). Read-only; a rejection writes nothing.
    queue_path = Path(args.queue_jsonl)
    if not queue_path.exists():
        emit("needs_fix", zero_counts())
        return 2
    try:
        line_count, rows, has_shape_error = gate4a_precheck.read_queue_rows(queue_path)
    except OSError:
        emit("needs_fix", zero_counts())
        return 2

    counts = zero_counts()
    counts["queue_rows_read_count"] = line_count

    def stopped(status, exit_code, *, count_written=False):
        # ``recovery_result_rows_written_count`` reports rows written by THIS run,
        # verified against the durable file so an interrupted write is never hidden.
        # The artifact counts report the current recovery marker state.
        if count_written:
            counts["recovery_result_rows_written_count"] = gate4.nonblank_result_count(
                args.recovery_results_jsonl
            )
        counts["recovery_processed_artifact_count"] = recovery_artifact_count(args.recovery_processed_dir)
        counts["recovery_failed_artifact_count"] = recovery_artifact_count(args.recovery_failed_dir)
        emit(status, counts)
        return exit_code

    precheck = gate4a_precheck.summarize(rows, line_count, has_shape_error)
    if precheck["status"] != "ok":
        emit("needs_fix", counts)
        return 2

    job = rows[0]
    if not gate4.decoded_member_is_canonical_numeric(job):
        emit("needs_fix", counts)
        return 2
    if not gate4.canonical_identity_ok(job):
        emit("needs_fix", counts)
        return 2
    try:
        worker.validate_job(job)
    except worker.BridgeWorkerError:
        emit("needs_fix", counts)
        return 2
    if not gate4.exact_int(job.get("attempt"), gate4.CANONICAL_ATTEMPT):
        emit("needs_fix", counts)
        return 2
    if not gate4.exact_int(job.get("max_attempts"), gate4.CANONICAL_MAX_ATTEMPTS):
        emit("needs_fix", counts)
        return 2
    if job.get("consent_status") != gate4.CANONICAL_CONSENT_STATUS:
        emit("needs_fix", counts)
        return 2

    job_id = worker.safe_job_id(job.get("job_id"))
    payload_hash = job.get("payload_hash")

    # D. Strict aggregate validation of the complete original failed attempt. Any
    # mismatch stops here: no auth preflight, no lookup, no recovery artifact, and no
    # mutation of any original artifact.
    if not original_failed_attempt_valid(args, job, job_id, payload_hash):
        emit("needs_fix", counts)
        return 2
    flags["original_failure_validated"] = True

    # E. Claim-first recovery-state machine before authentication. Exactly one recovery
    # generation exists and the permanent attempt claim is its consumed-attempt signal:
    # any present claim is terminal (`already_processed` only for a valid claim plus a
    # fully valid successful result/processed pair, otherwise `needs_fix`), and with no
    # claim any recovery artifact at all — including an empty or zero-byte results path
    # or any other filesystem object at it — is terminal `needs_fix`. Nothing is
    # cleaned, reset, overwritten, or repaired, and no second lookup can ever start.
    claim_state = attempt_claim_state(args.recovery_attempt_claim_json)
    claim_present = claim_state != "absent"
    flags["recovery_attempt_claim_present"] = claim_present
    flags["recovery_attempt_consumed"] = claim_present

    recovery_processed = gate4.inspect_markers(args.recovery_processed_dir, job_id)
    recovery_failed = gate4.inspect_markers(args.recovery_failed_dir, job_id)
    recovery_results = gate4.inspect_results(args.recovery_results_jsonl, job_id)

    if claim_state == "valid" and (
        gate4.marker_dir_has_exactly_one_clean(recovery_processed)
        and gate4.processed_marker_valid(recovery_processed["expected_marker"], job_id, payload_hash)
        and gate4.marker_dir_empty(recovery_failed)
        and recovery_results["total_nonblank"] == 1
        and recovery_results["malformed_lines"] == 0
        and recovery_results["non_object_lines"] == 0
        and recovery_results["other_job_rows"] == 0
        and len(recovery_results["rows"]) == 1
        and gate4.durable_result_fully_valid(
            recovery_results["rows"][0],
            job,
            expected_state=recovery_processed["expected_marker"].get("state"),
        )
    ):
        return stopped("already_processed", 0)

    if claim_present:
        # The single approved attempt is consumed. Every non-perfect state (failed
        # dead-letter pair, missing/partial/malformed/inconsistent artifacts, or an
        # invalid claim) is terminal needs_fix with zero authentication and zero lookup.
        return stopped("needs_fix", 2)

    if not (
        recovery_results_path_absent(args.recovery_results_jsonl)
        and gate4.marker_dir_empty(recovery_processed)
        and gate4.marker_dir_empty(recovery_failed)
    ):
        # No claim, but recovery artifacts exist: inconsistent state, never rerun.
        return stopped("needs_fix", 2)

    # F. Invocation accuracy: both PowerShell scripts must exist as regular files before
    # any process is launched. A missing script is a precondition failure.
    if not Path(args.lookup_script).is_file() or not Path(args.auth_probe_script).is_file():
        emit("needs_fix", counts)
        return 2

    # G. All four AC2_PROBE_* process values must be present and nonblank before the
    # probe process is spawned. Presence only; values are never printed or persisted.
    if not required_auth_env_present():
        emit("needs_fix", counts)
        return 2

    # H. Authentication preflight via the existing probe. Failure stops before any
    # member lookup and writes no recovery artifact, so the single approved lookup
    # attempt is not consumed by an environment problem.
    flags["auth_preflight_invoked"] = True
    if not run_auth_preflight(args):
        emit("needs_fix", counts)
        return 2
    flags["auth_preflight_success"] = True

    # I. Atomic permanent attempt claim, immediately before the lookup. Only the process
    # that exclusively creates AND fully persists AND revalidates the claim may invoke
    # the lookup; a concurrent loser sees FileExistsError and performs zero lookups.
    # Claim creation consumes the single approved attempt, and the claim is never
    # removed, reset, or rewritten.
    try:
        create_attempt_claim(args.recovery_attempt_claim_json)
    except OSError:
        # Covers FileExistsError (concurrent/later loser) and every persistence failure
        # (short/zero write, flush failure, ...). os.open may already have created a
        # partial claim object, so recompute presence from the filesystem: any object
        # now occupying the claim path is a consumed attempt that permanently blocks
        # every future lookup. No cleanup, repair, or retry is allowed.
        claim_object_present = os.path.lexists(args.recovery_attempt_claim_json)
        flags["recovery_attempt_claim_present"] = claim_object_present
        flags["recovery_attempt_consumed"] = claim_object_present
        return stopped("needs_fix", 2)
    flags["recovery_attempt_claim_present"] = True
    flags["recovery_attempt_consumed"] = True

    # Exclusive creation alone is insufficient: re-read the persisted claim through the
    # same strict validator and require it to be exactly the fixed structure before the
    # lookup may run. A missing, truncated, malformed, or unreadable persisted claim is
    # needs_fix with zero lookups, stays permanently in place, and is never repaired.
    if attempt_claim_state(args.recovery_attempt_claim_json) != "valid":
        return stopped("needs_fix", 2)
    flags["recovery_attempt_claim_created_by_this_run"] = True

    # Test-only crash simulation between claim persistence and the lookup: the claim
    # already blocks every future lookup, which the rerun tests prove.
    maybe_fault("after_claim_before_lookup")

    # J. Exactly one real read-only PowerShell lookup through the proven worker path.
    args.lookup_mode = LOOKUP_MODE
    args.max_attempts = gate4.CANONICAL_MAX_ATTEMPTS
    args.allow_root_login = False
    result = worker.process_job(job, args)
    state = result.get("state")
    flags["ac2_lookup_invoked"] = True
    counts["lookup_attempt_count"] = 1

    # Test-only crash simulation after the lookup but before result persistence: the
    # rerun must perform zero lookups because the claim is already consumed.
    maybe_fault("after_lookup_before_result")

    if state == LOOKUP_ERROR_REVIEW or not gate4.is_valid_review_result(result):
        try:
            gate4.durable_append_result(args.recovery_results_jsonl, result)
            gate4.atomic_write_marker(
                args.recovery_failed_dir,
                job_id,
                gate4.build_marker(result, "dead_letter", payload_hash),
            )
        except OSError:
            pass
        counts["lookup_error_count"] = 1
        return stopped("needs_fix", 2, count_written=True)

    counts["lookup_success_count"] = 1

    # Success-shaped result: fully validate identity/routing BEFORE committing a
    # processed marker, exactly like Gate 4.
    if not gate4.durable_result_fully_valid(result, job):
        try:
            gate4.durable_append_result(args.recovery_results_jsonl, result)
        except OSError:
            pass
        return stopped("needs_fix", 2, count_written=True)

    # Durable result first, then commit the processed marker via atomic replace.
    try:
        gate4.durable_append_result(args.recovery_results_jsonl, result)
        maybe_fault("after_result_before_marker")
        gate4.atomic_write_marker(
            args.recovery_processed_dir,
            job_id,
            gate4.build_marker(result, "processed", payload_hash),
        )
    except Exception:
        return stopped("needs_fix", 2, count_written=True)

    # Post-success verification against the freshly persisted recovery artifacts.
    post_results = gate4.inspect_results(args.recovery_results_jsonl, job_id)
    post_processed = gate4.inspect_markers(args.recovery_processed_dir, job_id)
    post_failed = gate4.inspect_markers(args.recovery_failed_dir, job_id)
    consistent = (
        post_results["total_nonblank"] == 1
        and post_results["malformed_lines"] == 0
        and post_results["non_object_lines"] == 0
        and post_results["other_job_rows"] == 0
        and len(post_results["rows"]) == 1
        and gate4.durable_result_fully_valid(post_results["rows"][0], job)
        and gate4.marker_dir_has_exactly_one_clean(post_processed)
        and gate4.processed_marker_valid(post_processed["expected_marker"], job_id, payload_hash)
        and gate4.marker_dir_empty(post_failed)
    )
    routing = {
        "lookup_existing_member_review_count": 1 if state == EXISTING_MEMBER_REVIEW else 0,
        "lookup_manual_review_count": 1 if state == MANUAL_REVIEW_REQUIRED else 0,
        "lookup_ready_for_create_review_count": 1 if state == READY_FOR_CREATE_REVIEW else 0,
    }
    if not consistent or sum(routing.values()) != 1:
        return stopped("needs_fix", 2, count_written=True)

    counts.update(routing)
    return stopped("ok", 0, count_written=True)


if __name__ == "__main__":
    raise SystemExit(main())
