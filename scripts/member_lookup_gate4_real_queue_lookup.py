"""Gate 4 real-queue AC2 lookup-only handoff wrapper.

This is a thin, fail-closed wrapper around already-proven components. It takes the
single approved Gate 4A ``PENDING_LOOKUP`` queue row that an operator has securely
placed on the Windows AC2 lookup bridge host and runs exactly one real read-only
AutoCount member lookup, then prints aggregate-only Gate 4 evidence.

Gate 4 is the real AC2 lookup gate. It is PowerShell-only: there is no mock route,
and only a genuine PowerShell lookup can produce ``status = ok``. The wrapper adds no
new AC2 lookup logic:

- exact single-row schema, base64, blank, and dummy/rehearsal rejection reuse the
  existing ``member_lookup_gate4a_queue_precheck`` control (values are decoded only in
  memory for validation, never printed);
- the read-only lookup itself reuses ``ac2_member_lookup_bridge_worker`` (which calls
  ``scripts/ac2_member_lookup_review.ps1`` with ``-EnableMemberLookupReview`` and
  ``-MemberNoBase64Utf8`` only);
- marker/result helpers reuse ``member_lookup_gate3c_local_bridge_runtime``.

Gate 4 additionally, before invoking PowerShell:

- enforces the canonical decoded member-number contract ``^[0-9]{6,20}$`` in memory;
- verifies the canonical Gate 4A payload identity (FNV-1a ``payload_hash``, ``job_id``,
  and the ``source_row_ref``/``intake_id`` derived relationships).

It implements a Gate 4-specific state machine and durable persistence so that a failed
or incomplete run can never later report ``already_processed``: the sanitized result is
written durably before its processed marker is committed, a failed/dead-letter marker or
an incomplete result/marker pair is always ``needs_fix``, and ``already_processed`` is
only reported for a confirmed processed marker with a matching durable result.

This wrapper never decodes or echoes the encoded/decoded member value into evidence,
never writes to AutoCount, never performs direct SQL, never activates n8n, and never
maps results back to n8n. It processes exactly one approved job and installs no
scheduler, service, webhook, or tunnel.
"""

import argparse
import base64
import json
import os
import re
from pathlib import Path

import ac2_member_lookup_bridge_worker as worker
import member_lookup_gate3c_local_bridge_runtime as gate3c
import member_lookup_gate4a_queue_precheck as gate4a_precheck


GATE = "gate4_real_queue_uat_ac2_lookup_only"
RUNTIME_LOCATION = "windows_ac2_lookup_bridge_host"
EXECUTION_MODE = "manual_read_only_review_only"
LOOKUP_MODE = "powershell"
APPROVED_BATCH_SIZE = 1

EXISTING_MEMBER_REVIEW = worker.EXISTING_MEMBER_REVIEW
MANUAL_REVIEW_REQUIRED = worker.MANUAL_REVIEW_REQUIRED
READY_FOR_CREATE_REVIEW = worker.READY_FOR_CREATE_REVIEW
LOOKUP_ERROR_REVIEW = worker.LOOKUP_ERROR_REVIEW

SUCCESS_REVIEW_STATES = {EXISTING_MEMBER_REVIEW, MANUAL_REVIEW_REQUIRED, READY_FOR_CREATE_REVIEW}
ALL_REVIEW_STATES = SUCCESS_REVIEW_STATES | {LOOKUP_ERROR_REVIEW}

NUMERIC_MEMBER_RE = re.compile(r"^[0-9]{6,20}$")

# Canonical Gate 4A payload-hash field order, matching the FNV-1a input in
# n8n-workflows/member_intake_gate4a_container_queue_write.workflow.json.
CANONICAL_HASH_FIELDS = (
    "intake_source",
    "source_reference",
    "source_row_ref",
    "row_number",
    "intake_id",
    "state",
    "submitted_member_no_base64_utf8",
    "pdpa_status",
)

# Fixed Gate 4A queue contract fields (not part of the FNV payload hash). They are
# validated as exact canonical values so the underlying worker's retry-exhausted path can
# never be reached and mis-reported as an invoked AC2 lookup.
CANONICAL_ATTEMPT = 0
CANONICAL_MAX_ATTEMPTS = 1
CANONICAL_CONSENT_STATUS = "marketing_consent_not_queued"

# Fields a Gate 4 marker artifact may contain (from gate3c.marker_from_result).
ALLOWED_MARKER_FIELDS = {
    "marker_type",
    "job_id",
    "payload_hash",
    "state",
    "status",
    "error_code",
    "dry_run_only",
    "final_write_automation",
    "marked_at",
}

# Test-only fault-injection hook. It can only cause failures (never a false PASS): it is
# used to prove that an interrupted persistence run stays detectable and never becomes
# an ``already_processed`` result.
FAULT_ENV = "GATE4_TEST_FAULT_INJECT"


def bool_text(value):
    return "true" if value else "false"


def exact_int(value, expected):
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def zero_counts():
    return {
        "queue_rows_read_count": 0,
        "lookup_attempt_count": 0,
        "lookup_success_count": 0,
        "lookup_existing_member_review_count": 0,
        "lookup_manual_review_count": 0,
        "lookup_ready_for_create_review_count": 0,
        "lookup_error_count": 0,
        "review_rows_written_count": 0,
    }


def evidence_rows(status, counts, *, powershell_lookup_enabled, ac2_lookup_invoked):
    return [
        ("status", status),
        ("gate", GATE),
        ("runtime_location", RUNTIME_LOCATION),
        ("execution_mode", EXECUTION_MODE),
        ("lookup_mode", LOOKUP_MODE),
        ("powershell_lookup_enabled", bool_text(powershell_lookup_enabled)),
        ("ac2_lookup_invoked", bool_text(ac2_lookup_invoked)),
        ("approved_batch_size", APPROVED_BATCH_SIZE),
        ("queue_rows_read_count", counts["queue_rows_read_count"]),
        ("lookup_attempt_count", counts["lookup_attempt_count"]),
        ("lookup_success_count", counts["lookup_success_count"]),
        ("lookup_existing_member_review_count", counts["lookup_existing_member_review_count"]),
        ("lookup_manual_review_count", counts["lookup_manual_review_count"]),
        ("lookup_ready_for_create_review_count", counts["lookup_ready_for_create_review_count"]),
        ("lookup_error_count", counts["lookup_error_count"]),
        ("review_rows_written_count", counts["review_rows_written_count"]),
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


def print_evidence(status, counts, *, powershell_lookup_enabled, ac2_lookup_invoked):
    for key, value in evidence_rows(
        status,
        counts,
        powershell_lookup_enabled=powershell_lookup_enabled,
        ac2_lookup_invoked=ac2_lookup_invoked,
    ):
        print(f"{key} = {value}")


def fnv1a_hex(text):
    """32-bit FNV-1a over UTF-16 code units, matching the canonical Gate 4A workflow."""
    hash_value = 0x811C9DC5
    for char in text:
        hash_value ^= ord(char)
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return format(hash_value, "08x")


def canonical_payload_hash(row):
    canonical = {field: row.get(field) for field in CANONICAL_HASH_FIELDS}
    serialized = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return "fnv1a_" + fnv1a_hex(serialized)


def decoded_member_is_canonical_numeric(row):
    """Decode only in memory and require the canonical numeric member contract.

    The decoded value is never returned, printed, logged, or included in evidence.
    """
    encoded = row.get("submitted_member_no_base64_utf8")
    if not isinstance(encoded, str) or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return NUMERIC_MEMBER_RE.fullmatch(decoded) is not None


def canonical_identity_ok(row):
    """Verify the row was not altered after canonical Gate 4A queue-row derivation."""
    payload_hash = row.get("payload_hash")
    if not isinstance(payload_hash, str) or not payload_hash:
        return False
    if payload_hash != canonical_payload_hash(row):
        return False
    if row.get("job_id") != f"gate4a_{payload_hash}":
        return False
    row_number = row.get("row_number")
    if not isinstance(row_number, int) or isinstance(row_number, bool) or row_number < 1:
        return False
    if row.get("source_row_ref") != f"row_{row_number}":
        return False
    if row.get("intake_id") != f"gate4a_row_{row_number}":
        return False
    return True


def inspect_results(results_path, expected_job_id):
    """Strictly inspect the dedicated Gate 4 results file for state decisions.

    Reports aggregate structure only (no row or field values are returned to the
    caller for printing). ``rows`` holds parsed dict objects for further validation.
    An unreadable file is treated as blocking (a malformed line).
    """
    path = Path(results_path)
    info = {
        "exists": path.exists(),
        "total_nonblank": 0,
        "parsed_objects": 0,
        "malformed_lines": 0,
        "non_object_lines": 0,
        "unexpected_schema_rows": 0,
        "expected_job_rows": 0,
        "other_job_rows": 0,
        "rows": [],
    }
    if not info["exists"]:
        return info
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        info["total_nonblank"] = 1
        info["malformed_lines"] = 1
        return info
    for line in text.splitlines():
        if not line.strip():
            continue
        info["total_nonblank"] += 1
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            info["malformed_lines"] += 1
            continue
        if not isinstance(value, dict):
            info["non_object_lines"] += 1
            continue
        info["parsed_objects"] += 1
        info["rows"].append(value)
        if set(value) != worker.ALLOWED_RESULT_FIELDS:
            info["unexpected_schema_rows"] += 1
        row_job_id = worker.safe_job_id(value.get("job_id"))
        if row_job_id == expected_job_id:
            info["expected_job_rows"] += 1
        elif row_job_id is not None:
            info["other_job_rows"] += 1
    return info


def results_block_fresh_lookup(info):
    """A fresh lookup is allowed only when results are absent or fully empty."""
    return not ((not info["exists"]) or info["total_nonblank"] == 0)


def nonblank_result_count(results_path):
    """Count complete nonblank result lines without returning any content."""
    path = Path(results_path)
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def inspect_markers(directory, expected_job_id):
    """Strictly inspect a dedicated Gate 4 marker directory for state decisions.

    Reports aggregate structure only. It never returns or prints filenames, job IDs,
    payload hashes, marker content, or row values. ``expected_marker`` holds the parsed
    dict for the artifact named exactly ``<expected_job_id>.json`` (when present and a
    JSON object), for downstream field validation. Unreadable or malformed artifacts are
    counted (and therefore blocking); nothing is skipped silently.
    """
    path = Path(directory)
    info = {
        "exists": path.exists(),
        "total_artifacts": 0,
        "json_files": 0,
        "tmp_files": 0,
        "malformed_files": 0,
        "unreadable_files": 0,
        "non_object_files": 0,
        "unexpected_filenames": 0,
        "content_filename_mismatch": 0,
        "unexpected_field_markers": 0,
        "expected_files": 0,
        "other_valid_markers": 0,
        "expected_marker": None,
    }
    if not info["exists"]:
        return info
    try:
        entries = sorted(path.iterdir())
    except OSError:
        info["total_artifacts"] = 1
        info["unreadable_files"] = 1
        return info
    for entry in entries:
        info["total_artifacts"] += 1
        name = entry.name
        if not entry.is_file():
            info["unexpected_filenames"] += 1
            continue
        if name.endswith(".json.tmp"):
            info["tmp_files"] += 1
            continue
        if not name.endswith(".json"):
            info["unexpected_filenames"] += 1
            continue
        info["json_files"] += 1
        stem = name[: -len(".json")]
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            info["unreadable_files"] += 1
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            info["malformed_files"] += 1
            continue
        if not isinstance(value, dict):
            info["non_object_files"] += 1
            continue
        if set(value) - ALLOWED_MARKER_FIELDS:
            info["unexpected_field_markers"] += 1
        content_job_id = worker.safe_job_id(value.get("job_id"))
        if content_job_id is None or content_job_id != stem:
            info["content_filename_mismatch"] += 1
        if stem == expected_job_id:
            info["expected_files"] += 1
            info["expected_marker"] = value
        elif content_job_id is not None:
            info["other_valid_markers"] += 1
        else:
            info["unexpected_filenames"] += 1
    return info


def marker_dir_empty(info):
    """A marker directory is clean only when it is absent or contains no artifacts."""
    return (not info["exists"]) or info["total_artifacts"] == 0


def marker_dir_has_exactly_one_clean(info):
    """True only when the directory holds exactly one well-formed JSON marker file."""
    return (
        info["total_artifacts"] == 1
        and info["json_files"] == 1
        and info["tmp_files"] == 0
        and info["malformed_files"] == 0
        and info["unreadable_files"] == 0
        and info["non_object_files"] == 0
        and info["unexpected_filenames"] == 0
        and info["content_filename_mismatch"] == 0
        and info["unexpected_field_markers"] == 0
        and info["other_valid_markers"] == 0
        and info["expected_files"] == 1
        and isinstance(info["expected_marker"], dict)
    )


def is_valid_review_result(row, expected_state=None):
    if set(row) != worker.ALLOWED_RESULT_FIELDS:
        return False
    if row.get("dry_run_only") is not True:
        return False
    if row.get("final_write_automation") is not False:
        return False
    state = row.get("state")
    if state not in ALL_REVIEW_STATES:
        return False
    if expected_state is not None and state != expected_state:
        return False
    return True


def processed_marker_valid(marker, job_id, payload_hash):
    """A processed marker is accepted only when every required field is exact."""
    if not isinstance(marker, dict):
        return False
    if marker.get("marker_type") != "processed":
        return False
    if worker.safe_job_id(marker.get("job_id")) != job_id:
        return False
    marker_hash = marker.get("payload_hash")
    if not isinstance(marker_hash, str) or not marker_hash or marker_hash != payload_hash:
        return False
    if marker.get("state") not in SUCCESS_REVIEW_STATES:
        return False
    if marker.get("dry_run_only") is not True:
        return False
    if marker.get("final_write_automation") is not False:
        return False
    return True


def durable_result_fully_valid(result, job, expected_state=None):
    """Verify a stored result matches the queue row and the recomputed lookup routing.

    A result is accepted only when its envelope, its successful-lookup evidence, and its
    routing state (recomputed via the worker classifier) are all internally and
    queue-consistent. A result is never trusted merely because its stored ``state`` string
    is in the allowed set.
    """
    if not isinstance(result, dict) or set(result) != worker.ALLOWED_RESULT_FIELDS:
        return False

    # Exact envelope / queue identity.
    if worker.safe_job_id(result.get("job_id")) != worker.safe_job_id(job.get("job_id")):
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
    if result.get("dry_run_only") is not True:
        return False
    if result.get("final_write_automation") is not False:
        return False
    if result.get("result_applied_at") is not None:
        return False

    # Successful-lookup evidence.
    if result.get("status") != "ok":
        return False
    for flag in ("authentication_success", "user_session_available", "member_command_found", "get_member_found"):
        if result.get(flag) is not True:
            return False
    if result.get("error_code") is not None:
        return False

    # Routing consistency: recompute the review state from the stored lookup fields.
    try:
        lookup_like = {field: result.get(field) for field in worker.LOOKUP_PUBLIC_FIELDS}
        computed_state, _ = worker.classify_lookup_result(lookup_like)
    except worker.BridgeWorkerError:
        return False
    if computed_state not in SUCCESS_REVIEW_STATES:
        return False
    if result.get("state") != computed_state:
        return False
    if expected_state is not None and computed_state != expected_state:
        return False
    return True


def maybe_fault(point):
    if os.environ.get(FAULT_ENV) == point:
        raise RuntimeError("gate4_test_fault_injected")


def durable_append_result(results_path, result):
    """Append one sanitized result row and flush it to disk before returning."""
    path = Path(results_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_write_marker(directory, marker_id, marker):
    """Write a marker via a durable temp file plus atomic replace."""
    marker_dir = Path(directory)
    marker_dir.mkdir(parents=True, exist_ok=True)
    target = marker_dir / f"{marker_id}.json"
    tmp = marker_dir / f"{marker_id}.json.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(marker, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def build_marker(result, marker_type, payload_hash):
    marker = gate3c.marker_from_result(result, marker_type)
    marker["payload_hash"] = worker.safe_source_value(payload_hash)
    return marker


def record_input_rejection(args, error_code):
    """Record a pre-lookup input rejection via the existing failed/dead-letter discipline.

    A fixed marker name is used because the queue identity may itself be untrusted at this
    point; the marker carries no row-level values and is not keyed to any job_id.
    """
    try:
        result = gate3c.build_failed_result({}, error_code)
        marker = gate3c.marker_from_result(result, "dead_letter")
        atomic_write_marker(args.failed_dir, "gate4-input-rejected", marker)
    except OSError:
        pass


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly one real read-only PowerShell AC2 member lookup for the approved "
            "Gate 4A queue row on the Windows AC2 lookup bridge host, printing "
            "aggregate-only Gate 4 evidence. PowerShell-only; there is no mock route."
        )
    )
    parser.add_argument("--enable-gate4-real-queue-lookup", action="store_true")
    parser.add_argument("--enable-powershell-lookup", action="store_true")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--results-jsonl", required=True)
    parser.add_argument("--processed-dir", required=True)
    parser.add_argument("--failed-dir", required=True)
    parser.add_argument(
        "--lookup-script",
        default=str(Path("scripts") / "ac2_member_lookup_review.ps1"),
    )
    parser.add_argument("--powershell-exe", default="powershell")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--allow-root-login", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    ps_enabled = bool(args.enable_powershell_lookup)

    def emit(status, counts, *, ac2_invoked=False):
        print_evidence(
            status,
            counts,
            powershell_lookup_enabled=ps_enabled,
            ac2_lookup_invoked=ac2_invoked,
        )

    # A. Both explicit opt-ins are required; refuse before reading or processing the queue.
    if not args.enable_gate4_real_queue_lookup or not args.enable_powershell_lookup:
        emit("refused", zero_counts())
        return 2

    queue_path = Path(args.queue_jsonl)
    if not queue_path.exists():
        emit("needs_fix", zero_counts())
        return 2

    # Phase 1: existing Gate 4A precheck (single-row schema, base64 shape, blank,
    # dummy/rehearsal, state, PDPA). Fail closed before any lookup.
    try:
        line_count, rows, has_shape_error = gate4a_precheck.read_queue_rows(queue_path)
    except OSError:
        emit("needs_fix", zero_counts())
        return 2

    precheck = gate4a_precheck.summarize(rows, line_count, has_shape_error)
    if precheck["status"] != "ok":
        record_input_rejection(args, "gate4_queue_precheck_rejected")
        counts = zero_counts()
        counts["queue_rows_read_count"] = precheck["queue_row_count"]
        emit("needs_fix", counts)
        return 2

    job = rows[0]

    def reject_before_lookup(error_code):
        record_input_rejection(args, error_code)
        counts = zero_counts()
        counts["queue_rows_read_count"] = line_count
        emit("needs_fix", counts)
        return 2

    # B. Canonical decoded member-number contract (in memory only, never printed).
    if not decoded_member_is_canonical_numeric(job):
        return reject_before_lookup("gate4_member_no_not_canonical_numeric")

    # C. Canonical Gate 4A payload identity (hash, job_id, derived relationships).
    if not canonical_identity_ok(job):
        return reject_before_lookup("gate4_payload_identity_mismatch")

    # Belt-and-suspenders worker contract validation before touching the lookup path.
    try:
        worker.validate_job(job)
    except worker.BridgeWorkerError:
        return reject_before_lookup("gate4_worker_contract_error")

    # Fixed Gate 4A queue retry-contract fields (validated exactly, not FNV-hashed). This
    # keeps the worker's retry-exhausted branch unreachable so it can never be mis-reported
    # as an invoked AC2 lookup when no PowerShell process was launched.
    if not exact_int(job.get("attempt"), CANONICAL_ATTEMPT):
        return reject_before_lookup("gate4_attempt_must_be_zero")
    if not exact_int(job.get("max_attempts"), CANONICAL_MAX_ATTEMPTS):
        return reject_before_lookup("gate4_max_attempts_must_be_one")
    if job.get("consent_status") != CANONICAL_CONSENT_STATUS:
        return reject_before_lookup("gate4_consent_status_not_canonical")

    job_id = worker.safe_job_id(job.get("job_id"))
    payload_hash = job.get("payload_hash")

    def stopped(status):
        counts = zero_counts()
        counts["queue_rows_read_count"] = line_count
        emit(status, counts)
        return 0 if status in {"ok", "already_processed"} else 2

    # D. Strict marker/result state machine before any lookup. Both dedicated marker
    # directories are inspected strictly (nothing skipped): any marker artifact at all --
    # malformed, unreadable, non-object, wrong filename, wrong/missing content job_id,
    # unrelated, duplicate, temporary .json.tmp, failed/dead-letter, or incomplete -- blocks
    # an automatic lookup. A fresh lookup is permitted only when both marker directories are
    # absent/empty and the results file is absent/empty.
    processed = inspect_markers(args.processed_dir, job_id)
    failed = inspect_markers(args.failed_dir, job_id)
    info = inspect_results(args.results_jsonl, job_id)

    # Valid idempotent rerun: exactly one clean processed marker for this job, zero failed
    # artifacts, and exactly one fully-valid matching durable result.
    if (
        marker_dir_has_exactly_one_clean(processed)
        and processed_marker_valid(processed["expected_marker"], job_id, payload_hash)
        and marker_dir_empty(failed)
        and info["total_nonblank"] == 1
        and info["malformed_lines"] == 0
        and info["non_object_lines"] == 0
        and info["other_job_rows"] == 0
        and len(info["rows"]) == 1
        and durable_result_fully_valid(
            info["rows"][0], job, expected_state=processed["expected_marker"].get("state")
        )
    ):
        return stopped("already_processed")

    # Otherwise a fresh lookup requires both marker directories and the results file to be
    # absent or empty. Any artifact of any kind is needs_fix and never already_processed.
    if not (marker_dir_empty(processed) and marker_dir_empty(failed) and not results_block_fresh_lookup(info)):
        return stopped("needs_fix")

    # Invocation accuracy: the read-only lookup script must exist as a regular file before
    # we can attempt a lookup. A missing script is a precondition failure, not an invoked
    # lookup, so ac2_lookup_invoked stays false.
    if not Path(args.lookup_script).is_file():
        return stopped("needs_fix")

    # E/F. Clean state -> real PowerShell lookup, durable result first, then processed marker.
    args.lookup_mode = LOOKUP_MODE
    result = worker.process_job(job, args)
    state = result.get("state")

    if state == LOOKUP_ERROR_REVIEW or not is_valid_review_result(result):
        try:
            durable_append_result(args.results_jsonl, result)
            atomic_write_marker(args.failed_dir, job_id, build_marker(result, "dead_letter", payload_hash))
        except OSError:
            pass
        counts = zero_counts()
        counts.update(
            queue_rows_read_count=line_count,
            lookup_attempt_count=1,
            lookup_error_count=1,
            review_rows_written_count=nonblank_result_count(args.results_jsonl),
        )
        emit("needs_fix", counts, ac2_invoked=True)
        return 2

    # Success-shaped result: fully validate identity/routing BEFORE committing a processed
    # marker, so a processed marker never certifies an inconsistent result.
    if not durable_result_fully_valid(result, job):
        try:
            durable_append_result(args.results_jsonl, result)
        except OSError:
            pass
        counts = zero_counts()
        counts.update(
            queue_rows_read_count=line_count,
            lookup_attempt_count=1,
            lookup_success_count=1,
            review_rows_written_count=nonblank_result_count(args.results_jsonl),
        )
        emit("needs_fix", counts, ac2_invoked=True)
        return 2

    # Durable result first, then commit the processed marker via atomic replace.
    try:
        durable_append_result(args.results_jsonl, result)
        maybe_fault("after_result_before_marker")
        atomic_write_marker(args.processed_dir, job_id, build_marker(result, "processed", payload_hash))
    except Exception:
        # Persistence is incomplete; never claim a successful lookup. Report the actual
        # count of complete result rows written so an interrupted write is not hidden.
        counts = zero_counts()
        counts.update(
            queue_rows_read_count=line_count,
            lookup_attempt_count=1,
            lookup_success_count=1,
            review_rows_written_count=nonblank_result_count(args.results_jsonl),
        )
        emit("needs_fix", counts, ac2_invoked=True)
        return 2

    # Post-success verification against the freshly persisted artifacts (strict, not just
    # dictionary membership): exactly one valid expected result, exactly one clean processed
    # marker that re-validates, and zero failed marker artifacts.
    post = inspect_results(args.results_jsonl, job_id)
    post_processed = inspect_markers(args.processed_dir, job_id)
    post_failed = inspect_markers(args.failed_dir, job_id)
    consistent = (
        post["total_nonblank"] == 1
        and post["malformed_lines"] == 0
        and post["non_object_lines"] == 0
        and post["other_job_rows"] == 0
        and len(post["rows"]) == 1
        and durable_result_fully_valid(post["rows"][0], job)
        and marker_dir_has_exactly_one_clean(post_processed)
        and processed_marker_valid(post_processed["expected_marker"], job_id, payload_hash)
        and marker_dir_empty(post_failed)
    )
    routing = {
        "lookup_existing_member_review_count": 1 if state == EXISTING_MEMBER_REVIEW else 0,
        "lookup_manual_review_count": 1 if state == MANUAL_REVIEW_REQUIRED else 0,
        "lookup_ready_for_create_review_count": 1 if state == READY_FOR_CREATE_REVIEW else 0,
    }
    if not consistent or sum(routing.values()) != 1:
        counts = zero_counts()
        counts.update(
            queue_rows_read_count=line_count,
            lookup_attempt_count=1,
            lookup_success_count=1,
            review_rows_written_count=post["total_nonblank"],
        )
        emit("needs_fix", counts, ac2_invoked=True)
        return 2

    counts = {
        "queue_rows_read_count": line_count,
        "lookup_attempt_count": 1,
        "lookup_success_count": 1,
        "lookup_error_count": 0,
        "review_rows_written_count": 1,
        **routing,
    }
    emit("ok", counts, ac2_invoked=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
