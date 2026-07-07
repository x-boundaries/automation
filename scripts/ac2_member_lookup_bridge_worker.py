"""Disabled dry-run polling worker skeleton for AC2 member lookup review.

The worker is intentionally fixture-first: it has no real queue endpoint, no
credentials, and no network transport. PowerShell lookup mode is separately
gated and calls only the existing read-only lookup script.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PENDING_LOOKUP = "PENDING_LOOKUP"
LOOKUP_ERROR_REVIEW = "LOOKUP_ERROR_REVIEW"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
EXISTING_MEMBER_REVIEW = "EXISTING_MEMBER_REVIEW"
READY_FOR_CREATE_REVIEW = "READY_FOR_CREATE_REVIEW"

BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
ALLOWED_MEMBER_STATUS_LABELS = {
    None,
    "canonical_65_mobile",
    "already_65_mobile",
    "manual_review",
    "invalid_too_long",
}
ALLOWED_PDPA_STATUS_LABELS = {"i_agree"}

ALLOWED_QUEUE_FIELDS = {
    "job_id",
    "intake_source",
    "source_reference",
    "source_row_ref",
    "row_number",
    "intake_id",
    "state",
    "submitted_member_no_base64_utf8",
    "consent_status",
    "pdpa_status",
    "payload_hash",
    "attempt",
    "max_attempts",
    "created_at",
    "updated_at",
    "lease_owner",
    "lease_expires_at",
    "timeout_at",
    "last_error_code",
}

FORBIDDEN_JOB_FIELDS = {
    "member_no",
    "member_number",
    "normalized_member_no",
    "name",
    "full_name",
    "email",
    "email_address",
    "phone",
    "mobile",
    "raw_phone_number",
    "dob",
    "address",
    "server",
    "database",
    "user",
    "password",
    "connection_string",
    "stderr",
    "stdout",
    "token",
    "secret",
}

LOOKUP_PUBLIC_FIELDS = [
    "status",
    "authentication_success",
    "user_session_available",
    "member_command_found",
    "get_member_found",
    "submitted_member_no_status",
    "normalized_member_no_length",
    "member_exists",
    "member_found_by",
    "manual_review_required",
    "warning_count",
]

ALLOWED_RESULT_FIELDS = {
    "job_id",
    "intake_source",
    "source_reference",
    "source_row_ref",
    "row_number",
    "state",
    "status",
    "authentication_success",
    "user_session_available",
    "member_command_found",
    "get_member_found",
    "submitted_member_no_status",
    "normalized_member_no_length",
    "member_exists",
    "member_found_by",
    "manual_review_required",
    "warning_count",
    "error_code",
    "consent_status",
    "pdpa_status",
    "attempt",
    "dry_run_only",
    "final_write_automation",
    "result_created_at",
    "result_applied_at",
}

ALLOWED_MOCK_RESULT_FIELDS = {"job_id", "error_code", *LOOKUP_PUBLIC_FIELDS}


class BridgeWorkerError(ValueError):
    """Raised when fixture jobs or runtime options violate the review contract."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def refusal_summary():
    return {
        "status": "refused",
        "error_code": "explicit_opt_in_required",
        "message": "Pass --enable-local-lookup-bridge-review to process fixture jobs.",
        "dry_run_only": True,
        "final_write_automation": False,
    }


def read_fixture_jobs(path):
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        jobs = json.loads(stripped)
        if not isinstance(jobs, list):
            raise BridgeWorkerError("Fixture JSON must contain a list of jobs.")
        return jobs

    jobs = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError as error:
            raise BridgeWorkerError(f"Fixture line {line_number} is not valid JSON.") from error
        jobs.append(job)
    return jobs


def read_json_or_jsonl(path):
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        rows = json.loads(stripped)
        if not isinstance(rows, list):
            raise BridgeWorkerError("Fixture JSON must contain a list of objects.")
        return rows

    rows = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise BridgeWorkerError(f"Fixture line {line_number} is not valid JSON.") from error
        rows.append(row)
    return rows


def read_mock_results(path):
    if not path:
        return {}
    rows = read_json_or_jsonl(path)
    mock_results = {}
    for row in rows:
        if not isinstance(row, dict):
            raise BridgeWorkerError("Mock result fixture rows must be JSON objects.")
        unknown = sorted(set(row) - ALLOWED_MOCK_RESULT_FIELDS)
        forbidden = sorted(set(row) & FORBIDDEN_JOB_FIELDS)
        if unknown or forbidden:
            raise BridgeWorkerError("Mock result fixture contains fields outside the sanitized contract.")
        job_id = safe_job_id(row.get("job_id"))
        if job_id is None:
            raise BridgeWorkerError("Mock result fixture row requires a safe non-PII job_id.")
        mock_results[job_id] = row
    return mock_results


def ensure_base64_shape(value):
    if not isinstance(value, str) or not value:
        return False
    if len(value) % 4 != 0:
        return False
    return BASE64_RE.fullmatch(value) is not None


def safe_job_id(value):
    if isinstance(value, str) and SAFE_ID_RE.fullmatch(value):
        return value
    return None


def safe_row_number(value):
    if value is None:
        return None
    if isinstance(value, int) and value >= 1:
        return value
    return None


def safe_attempt(value):
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def safe_source_value(value):
    if value is None:
        return None
    if isinstance(value, str) and SAFE_ID_RE.fullmatch(value):
        return value
    return None


def safe_max_attempts(job, fallback):
    value = job.get("max_attempts", fallback) if isinstance(job, dict) else fallback
    if isinstance(value, int) and value > 0:
        return value
    return fallback


def validate_job(job):
    if not isinstance(job, dict):
        raise BridgeWorkerError("Job must be a JSON object.")

    forbidden = sorted(set(job) & FORBIDDEN_JOB_FIELDS)
    if forbidden:
        raise BridgeWorkerError("Job contains forbidden sensitive fields.")
    unknown = sorted(set(job) - ALLOWED_QUEUE_FIELDS)
    if unknown:
        raise BridgeWorkerError("Job contains fields outside the bridge request contract.")

    job_id = job.get("job_id")
    if safe_job_id(job_id) is None:
        raise BridgeWorkerError("job_id is required and must use the safe non-PII id shape.")

    intake_source = job.get("intake_source")
    if safe_source_value(intake_source) is None:
        raise BridgeWorkerError("intake_source is required and must use the safe non-PII id shape.")

    source_reference = job.get("source_reference")
    source_row_ref = job.get("source_row_ref")
    row_number = job.get("row_number")
    if (
        safe_source_value(source_reference) is None
        and safe_source_value(source_row_ref) is None
        and safe_row_number(row_number) is None
    ):
        raise BridgeWorkerError("A safe source reference or spreadsheet row number is required.")

    pdpa_status = job.get("pdpa_status")
    consent_status = job.get("consent_status")
    if pdpa_status not in ALLOWED_PDPA_STATUS_LABELS:
        raise BridgeWorkerError("pdpa_status must be a valid new-form PDPA acknowledgement before lookup.")
    if consent_status is not None and safe_source_value(consent_status) is None:
        raise BridgeWorkerError("consent_status must be a sanitized category when supplied.")

    if job.get("state") != PENDING_LOOKUP:
        raise BridgeWorkerError("Only PENDING_LOOKUP fixture jobs are processed by this skeleton.")

    encoded = job.get("submitted_member_no_base64_utf8")
    if not ensure_base64_shape(encoded):
        raise BridgeWorkerError("submitted_member_no_base64_utf8 is missing or has invalid base64 shape.")

    attempt = job.get("attempt", 0)
    if not isinstance(attempt, int) or attempt < 0:
        raise BridgeWorkerError("attempt must be a nonnegative integer.")

    max_attempts = job.get("max_attempts")
    if max_attempts is not None and (not isinstance(max_attempts, int) or max_attempts <= 0):
        raise BridgeWorkerError("max_attempts must be a positive integer when supplied.")


def default_lookup_result():
    return {
        "status": "ok",
        "authentication_success": True,
        "user_session_available": True,
        "member_command_found": True,
        "get_member_found": True,
        "submitted_member_no_status": "canonical_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": False,
        "member_found_by": None,
        "manual_review_required": False,
        "warning_count": 0,
        "error": None,
    }


def mock_lookup(job, mock_results=None):
    result = default_lookup_result()
    mock_row = (mock_results or {}).get(job.get("job_id"), {})
    result["status"] = mock_row.get("status", result["status"])
    result["member_exists"] = bool(mock_row.get("member_exists", result["member_exists"]))
    result["manual_review_required"] = bool(
        mock_row.get("manual_review_required", result["manual_review_required"])
    )
    result["warning_count"] = int(mock_row.get("warning_count", result["warning_count"]))
    result["submitted_member_no_status"] = mock_row.get(
        "submitted_member_no_status",
        result["submitted_member_no_status"],
    )
    result["normalized_member_no_length"] = int(
        mock_row.get("normalized_member_no_length", result["normalized_member_no_length"])
    )
    result["authentication_success"] = bool(
        mock_row.get("authentication_success", result["authentication_success"])
    )
    result["user_session_available"] = bool(
        mock_row.get("user_session_available", result["user_session_available"])
    )
    result["member_command_found"] = bool(
        mock_row.get("member_command_found", result["member_command_found"])
    )
    result["get_member_found"] = bool(mock_row.get("get_member_found", result["get_member_found"]))
    if result["status"] != "ok":
        result["error"] = {"type": mock_row.get("error_code", "mock_lookup_error")}
    if result["member_exists"]:
        result["member_found_by"] = "MemberCommand.GetMember"
    return result


def load_single_json_object(text):
    stripped = text.strip()
    if not stripped:
        raise BridgeWorkerError("Lookup process returned empty stdout.")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise BridgeWorkerError("Lookup process returned invalid JSON.") from error
    if not isinstance(value, dict):
        raise BridgeWorkerError("Lookup process returned an unexpected JSON shape.")
    return value


def powershell_lookup(job, args):
    script_path = Path(args.lookup_script)
    if not script_path.exists():
        raise BridgeWorkerError("Lookup script path was not found.")

    command = [
        args.powershell_exe,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-EnableMemberLookupReview",
        "-MemberNoBase64Utf8",
        job["submitted_member_no_base64_utf8"],
    ]
    if args.allow_root_login:
        command.append("-AllowRootLogin")

    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=args.timeout_seconds,
    )
    if completed.stderr.strip():
        raise BridgeWorkerError("Lookup process wrote to stderr.")

    result = load_single_json_object(completed.stdout)
    if completed.returncode != 0 and result.get("status") != "error":
        raise BridgeWorkerError("Lookup process failed without a sanitized error object.")
    return result


def validate_lookup_result(result):
    for field in LOOKUP_PUBLIC_FIELDS:
        if field not in result:
            raise BridgeWorkerError("Lookup result is missing required sanitized fields.")
    if result.get("status") not in {"ok", "error", "refused"}:
        raise BridgeWorkerError("Lookup result status is outside the allowed set.")
    if not isinstance(result.get("member_exists"), bool):
        raise BridgeWorkerError("Lookup result member_exists must be boolean.")
    if not isinstance(result.get("manual_review_required"), bool):
        raise BridgeWorkerError("Lookup result manual_review_required must be boolean.")
    if not isinstance(result.get("warning_count"), int):
        raise BridgeWorkerError("Lookup result warning_count must be integer.")
    if not isinstance(result.get("normalized_member_no_length"), int):
        raise BridgeWorkerError("Lookup result normalized_member_no_length must be integer.")
    if result.get("submitted_member_no_status") not in ALLOWED_MEMBER_STATUS_LABELS:
        raise BridgeWorkerError("Lookup result submitted_member_no_status is outside allowed labels.")


def error_code_from_lookup(result):
    error = result.get("error")
    if isinstance(error, dict) and error.get("type"):
        raw = str(error["type"]).split(".")[-1]
        cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_").lower()
        return cleaned or "lookup_error"
    return "lookup_error"


def classify_lookup_result(result):
    validate_lookup_result(result)
    if result["status"] != "ok":
        return LOOKUP_ERROR_REVIEW, error_code_from_lookup(result)
    if result["manual_review_required"] or result["warning_count"] > 0:
        return MANUAL_REVIEW_REQUIRED, None
    if result["member_exists"]:
        return EXISTING_MEMBER_REVIEW, None
    return READY_FOR_CREATE_REVIEW, None


def result_envelope(job, state, lookup_result=None, error_code=None):
    envelope = {
        "job_id": safe_job_id(job.get("job_id")),
        "intake_source": safe_source_value(job.get("intake_source")),
        "source_reference": safe_source_value(job.get("source_reference")),
        "source_row_ref": safe_source_value(job.get("source_row_ref")),
        "row_number": safe_row_number(job.get("row_number")),
        "state": state,
        "consent_status": safe_source_value(job.get("consent_status")),
        "pdpa_status": safe_source_value(job.get("pdpa_status")),
        "attempt": safe_attempt(job.get("attempt")),
        "result_created_at": utc_now(),
        "result_applied_at": None,
        "dry_run_only": True,
        "final_write_automation": False,
    }
    if lookup_result:
        for field in LOOKUP_PUBLIC_FIELDS:
            envelope[field] = lookup_result.get(field)
    else:
        envelope.update(
            {
                "status": "error",
                "authentication_success": False,
                "user_session_available": False,
                "member_command_found": False,
                "get_member_found": False,
                "submitted_member_no_status": None,
                "normalized_member_no_length": 0,
                "member_exists": False,
                "member_found_by": None,
                "manual_review_required": False,
                "warning_count": 1,
            }
        )
    envelope["error_code"] = error_code
    envelope = {field: envelope.get(field) for field in ALLOWED_RESULT_FIELDS}
    return envelope


def process_job(job, args, mock_results=None):
    try:
        validate_job(job)
        if job.get("attempt", 0) >= safe_max_attempts(job, args.max_attempts):
            return result_envelope(job, LOOKUP_ERROR_REVIEW, error_code="retry_exhausted")
        if args.lookup_mode == "mock":
            lookup_result = mock_lookup(job, mock_results=mock_results)
        else:
            lookup_result = powershell_lookup(job, args)
        state, error_code = classify_lookup_result(lookup_result)
        return result_envelope(job, state, lookup_result=lookup_result, error_code=error_code)
    except subprocess.TimeoutExpired:
        return result_envelope(
            job if isinstance(job, dict) else {},
            LOOKUP_ERROR_REVIEW,
            error_code="lookup_timeout",
        )
    except (BridgeWorkerError, OSError, ValueError):
        return result_envelope(
            job if isinstance(job, dict) else {},
            LOOKUP_ERROR_REVIEW,
            error_code="request_or_lookup_contract_error",
        )


def write_results_jsonl(path, results):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for result in results:
            handle.write(json.dumps(result, sort_keys=True) + "\n")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Disabled-by-default AC2 member lookup bridge worker skeleton."
    )
    parser.add_argument("--enable-local-lookup-bridge-review", action="store_true")
    parser.add_argument("--queue-mode", choices=["fixture"], default=None)
    parser.add_argument("--fixture-jobs", default=None)
    parser.add_argument("--fixture-mock-results", default=None)
    parser.add_argument("--results-jsonl", default=None)
    parser.add_argument("--lookup-mode", choices=["mock", "powershell"], default="mock")
    parser.add_argument("--enable-powershell-lookup", action="store_true")
    parser.add_argument("--lookup-script", default=str(Path("scripts") / "ac2_member_lookup_review.ps1"))
    parser.add_argument("--powershell-exe", default="powershell")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--allow-root-login", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.enable_local_lookup_bridge_review:
        print(json.dumps(refusal_summary(), indent=2, sort_keys=True))
        return 2

    if args.queue_mode != "fixture" or not args.fixture_jobs or not args.results_jsonl:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": "fixture_queue_required",
                    "dry_run_only": True,
                    "final_write_automation": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    if args.lookup_mode == "powershell" and not args.enable_powershell_lookup:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": "powershell_lookup_opt_in_required",
                    "dry_run_only": True,
                    "final_write_automation": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    try:
        jobs = read_fixture_jobs(args.fixture_jobs)
        mock_results = read_mock_results(args.fixture_mock_results)
    except (BridgeWorkerError, OSError, json.JSONDecodeError):
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": "fixture_load_error",
                    "dry_run_only": True,
                    "final_write_automation": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    results = [process_job(job, args, mock_results=mock_results) for job in jobs]
    write_results_jsonl(args.results_jsonl, results)
    summary = {
        "status": "ok",
        "queue_mode": args.queue_mode,
        "lookup_mode": args.lookup_mode,
        "processed_count": len(results),
        "result_state_counts": {},
        "results_jsonl_name": Path(args.results_jsonl).name,
        "dry_run_only": True,
        "final_write_automation": False,
    }
    for result in results:
        summary["result_state_counts"][result["state"]] = (
            summary["result_state_counts"].get(result["state"], 0) + 1
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
