"""Worker v2 wire contract: claim request, result v2 validation, the section
4.4 consistency matrix and the section 4.1 outcome disposition.

The two frozen schemas ``schemas/member_gateway_worker_claim.v2.schema.json``
and ``schemas/member_gateway_result.v2.schema.json`` are the authority; this
module implements exactly their closed shapes without a schema library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from .canonical import birthday_month_to_dob, canonical_json, derive_register_and_expiry
from .crypto import payload_hash
from .identity import NAME_COMPONENT_RE, RULE
from .models import JobRecord, JobState


CLAIM_SCHEMA_VERSION = "xb.member.gateway.worker_claim.v2"
RESULT_SCHEMA_VERSION = "xb.member.gateway.result.v2"

JOB_ID_RE = re.compile(r"^job-[A-Za-z0-9]{16,64}$")
LEASE_TOKEN_RE = re.compile(r"^lease-[0-9a-f]{32}$")
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ERROR_CODE_RE = re.compile(r"^[a-z0-9_.:-]{1,80}$")

OUTCOMES = frozenset(
    {
        "CREATED_VERIFIED", "CREATED_VERIFIED_PRIOR_ATTEMPT", "LINKED_EXISTING", "MANUAL_REVIEW",
        "REJECTED_VALIDATION", "FAILED_BEFORE_WRITE", "NOT_CREATED", "NOT_CREATED_CONFLICT",
        "OUTCOME_UNCERTAIN", "CREATED_READBACK_MISMATCH", "MUTEX_BUSY",
    }
)
RULES = frozenset({"R0", "R1", "R2a", "R2b", "R2c", "R3", "R3b", "R4", "NONE"})
BRANCHES = frozenset({"BASE", "NAME_APPENDED", "EXISTING", "NONE"})
REASON_CODES = frozenset(
    {
        "prior_attempt_ambiguous", "multiple_same_person", "inactive_match", "format_variant_other_person",
        "holder_identity_unknown", "name_component_empty", "name_candidate_collision",
        "request_contract_violation", "readback_foreign_row", "name_exceeds_autocount_limit",
        "email_exceeds_autocount_limit", "synthetic_in_production", "clock_skew", "mutex_unavailable",
        "session_unavailable", "book_binding_mismatch", "integration_user_mismatch", "probe_unavailable",
        "fault_injection_refused", "test_book_requires_synthetic", "primitive_config_invalid",
        "primitive_launch_failed", "readback_absent_after_save", "readback_unavailable", "unexpected_error",
        "child_deadline_exceeded", "child_termination_unconfirmed", "primitive_output_invalid",
    }
)
DQ_FLAGS = frozenset({"email_seen_on_other_member", "post_save_same_person_other_row", "malformed_member_no_excluded"})
RESULT_FIELDS = frozenset(
    {
        "schema_version", "job_id", "attempt_no", "lease_id", "state_version", "outcome", "rule", "branch",
        "member_no", "member_guid", "save_invoked", "save_invocation_count", "readback", "reason_code",
        "dq_flags", "primitive", "error_code",
    }
)
READBACK_FIELDS = frozenset({"found", "match", "created_by_integration_user"})
PRIMITIVE_FIELDS = frozenset({"release_sha256", "rule_version"})

# Gateway-only reasons; they never appear in a result body.
GATEWAY_REASONS = frozenset(
    {
        "result_contract_violation", "attempts_exhausted", "uncertain_exhausted", "mutex_busy_exhausted",
        "guid_conflict_on_fresh_create", "guid_already_verified", "lease_expired",
    }
)

# Per-outcome reason sets (section 2.6 / 2.7 / 4.2). A lease-matching result
# whose reason does not belong to its outcome is a contract violation.
REVIEW_RESULT_REASONS = frozenset(
    {
        "prior_attempt_ambiguous", "multiple_same_person", "inactive_match", "format_variant_other_person",
        "holder_identity_unknown", "name_component_empty", "name_candidate_collision",
        "request_contract_violation", "readback_foreign_row",
    }
)
REJECTION_RESULT_REASONS = frozenset({"name_exceeds_autocount_limit", "email_exceeds_autocount_limit", "synthetic_in_production"})
PRE_WRITE_RESULT_REASONS = frozenset(
    {
        "clock_skew", "mutex_unavailable", "session_unavailable", "book_binding_mismatch", "integration_user_mismatch",
        "probe_unavailable", "fault_injection_refused", "test_book_requires_synthetic", "primitive_config_invalid",
        "primitive_launch_failed", "unexpected_error",
    }
)
UNCERTAIN_RESULT_REASONS = frozenset(
    {
        "readback_absent_after_save", "readback_unavailable", "unexpected_error", "child_deadline_exceeded",
        "child_termination_unconfirmed", "primitive_output_invalid",
    }
)

WRITE_CLASS_OUTCOMES = frozenset({"FAILED_BEFORE_WRITE", "NOT_CREATED", "NOT_CREATED_CONFLICT", "OUTCOME_UNCERTAIN"})
UNCERTAIN_OUTCOMES = frozenset({"OUTCOME_UNCERTAIN", "LEASE_EXPIRED"})
MAX_WRITE_BUDGET = 12
REQUEUE_BUDGET_STEP = 3
DEFAULT_WRITE_BUDGET = 3
MAX_BUSY_ATTEMPTS = 12
FIRST_RETRY_DELAY = timedelta(minutes=5)
LATER_RETRY_DELAY = timedelta(minutes=30)
BUSY_RETRY_DELAY = timedelta(minutes=5)
LEASE_EXPIRY_RETRY_DELAY = timedelta(minutes=5)


class ResultValidationError(ValueError):
    """The body is not a closed result v2 object. No state is changed."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_result_v2(body: Any) -> dict[str, Any]:
    """Validate exactly the frozen ``xb.member.gateway.result.v2`` shape."""

    if not isinstance(body, Mapping) or set(body) != RESULT_FIELDS:
        raise ResultValidationError("result_fields_invalid")
    checks = (
        body["schema_version"] == RESULT_SCHEMA_VERSION,
        isinstance(body["job_id"], str) and JOB_ID_RE.fullmatch(body["job_id"]) is not None,
        _is_int(body["attempt_no"]) and body["attempt_no"] >= 1,
        isinstance(body["lease_id"], str) and LEASE_TOKEN_RE.fullmatch(body["lease_id"]) is not None,
        _is_int(body["state_version"]) and body["state_version"] >= 0,
        isinstance(body["outcome"], str) and body["outcome"] in OUTCOMES,
        isinstance(body["rule"], str) and body["rule"] in RULES,
        isinstance(body["branch"], str) and body["branch"] in BRANCHES,
        body["member_no"] is None or (isinstance(body["member_no"], str) and 1 <= len(body["member_no"]) <= 20),
        body["member_guid"] is None or (isinstance(body["member_guid"], str) and GUID_RE.fullmatch(body["member_guid"]) is not None),
        isinstance(body["save_invoked"], bool),
        _is_int(body["save_invocation_count"]) and body["save_invocation_count"] in (0, 1),
        body["reason_code"] is None or (isinstance(body["reason_code"], str) and body["reason_code"] in REASON_CODES),
        body["error_code"] is None or (isinstance(body["error_code"], str) and ERROR_CODE_RE.fullmatch(body["error_code"]) is not None),
    )
    if not all(checks):
        raise ResultValidationError("result_field_value_invalid")
    readback = body["readback"]
    if readback is not None and (
        not isinstance(readback, Mapping)
        or set(readback) != READBACK_FIELDS
        or not all(isinstance(readback[key], bool) for key in READBACK_FIELDS)
    ):
        raise ResultValidationError("result_readback_invalid")
    flags = body["dq_flags"]
    if (
        not isinstance(flags, list) or len(flags) > 3 or len(set(map(str, flags))) != len(flags)
        or any(not isinstance(flag, str) or flag not in DQ_FLAGS for flag in flags)
    ):
        raise ResultValidationError("result_dq_flags_invalid")
    primitive = body["primitive"]
    if (
        not isinstance(primitive, Mapping) or set(primitive) != PRIMITIVE_FIELDS
        or not isinstance(primitive["release_sha256"], str) or RELEASE_SHA_RE.fullmatch(primitive["release_sha256"]) is None
        or primitive["rule_version"] != RULE
    ):
        raise ResultValidationError("result_primitive_invalid")
    return dict(body)


def result_hash(body: Mapping[str, Any]) -> str:
    return payload_hash(canonical_json(dict(body)))


def build_claim_request(job: JobRecord) -> dict[str, Any]:
    """The ``request`` object of claim v2: ``build_member_record`` inputs
    computed from the immutable VALIDATED identity and canonical payload."""

    if job.member_no_rule != RULE or job.base_member_no is None or job.name_component is None:
        raise ResultValidationError("job_identity_missing")
    payload = job.member_payload
    register_date, expiry_date = derive_register_and_expiry(payload["create_time"])
    return {
        "rule": RULE,
        "base_member_no": job.base_member_no,
        "name_component": job.name_component,
        "phone": payload["phone"],
        "name": payload["name"],
        "email": payload["email"],
        "MemberType": "Default",
        "DOB": birthday_month_to_dob(payload["birthday_month"]),
        "RegisterDate": register_date,
        "ExpiryDate": expiry_date,
        "OpeningPoints": 0,
        "IsActive": True,
        "Individual": True,
    }


def consistency_violations(job: JobRecord, result: Mapping[str, Any]) -> tuple[str, ...]:
    """Section 4.4. Any entry sends a lease-matching result to
    ``MANUAL_REVIEW(result_contract_violation)``. MemberNo equality is exact
    against the gateway's own stored base and name component."""

    base = job.base_member_no or ""
    component = job.name_component or ""
    appended = base + component
    outcome = result["outcome"]
    rule, branch = result["rule"], result["branch"]
    member_no, guid = result["member_no"], result["member_guid"]
    invoked, count = result["save_invoked"], result["save_invocation_count"]
    readback = result["readback"]
    problems: list[str] = []
    if count > 1:
        problems.append("save_invocation_count_exceeds_one")
    # Conservative coherence: "invoked" and "count" describe the same call.
    if count != (1 if invoked else 0):
        problems.append("save_invoked_count_incoherent")
    if not base:
        problems.append("job_identity_missing")
    if outcome in {"CREATED_VERIFIED", "CREATED_VERIFIED_PRIOR_ATTEMPT", "LINKED_EXISTING"}:
        if result["reason_code"] is not None:
            problems.append("success_reason_present")
        if outcome != "CREATED_VERIFIED" and readback is not None:
            problems.append("no_save_outcome_has_readback")
    if outcome == "CREATED_VERIFIED":
        base_ok = rule == "R1" and branch == "BASE" and member_no == base
        appended_ok = rule == "R4" and branch == "NAME_APPENDED" and component != "" and member_no == appended
        if not (base_ok or appended_ok):
            problems.append("created_member_no_or_rule_mismatch")
        if count != 1:
            problems.append("created_save_count_not_one")
        if not (isinstance(readback, Mapping) and readback["found"] and readback["match"] and readback["created_by_integration_user"]):
            problems.append("created_readback_not_verified")
        if guid is None:
            problems.append("created_guid_missing")
    elif outcome == "CREATED_VERIFIED_PRIOR_ATTEMPT":
        if rule != "R0":
            problems.append("prior_attempt_rule_not_r0")
        if result["attempt_no"] < 2:
            problems.append("prior_attempt_on_first_attempt")
        if invoked or count != 0:
            problems.append("prior_attempt_saved")
        if member_no not in {base, appended} or member_no is None:
            problems.append("prior_attempt_member_no_mismatch")
        base_branch = branch == "BASE" and member_no == base
        appended_branch = branch == "NAME_APPENDED" and component != "" and member_no == appended
        if not (base_branch or appended_branch):
            problems.append("prior_attempt_branch_mismatch")
        if guid is None:
            problems.append("prior_attempt_guid_missing")
    elif outcome == "MANUAL_REVIEW":
        reason = result["reason_code"]
        if reason not in REVIEW_RESULT_REASONS:
            problems.append("review_reason_invalid")
        elif reason == "readback_foreign_row":
            if not invoked:
                problems.append("foreign_row_without_save")
        elif invoked or count != 0:
            problems.append("review_outcome_saved")
    elif outcome == "LINKED_EXISTING":
        if rule != "R2c" or branch != "EXISTING":
            problems.append("linked_rule_or_branch_mismatch")
        if invoked or count != 0:
            problems.append("linked_saved")
        if not (isinstance(member_no, str) and 1 <= len(member_no) <= 20):
            problems.append("linked_member_no_invalid")
        if guid is None:
            problems.append("linked_guid_missing")
    elif outcome in {"FAILED_BEFORE_WRITE", "MUTEX_BUSY", "REJECTED_VALIDATION"}:
        # The primitive rejects or fails before any write on these paths.
        if invoked or count != 0:
            problems.append("pre_write_outcome_saved")
        if outcome == "REJECTED_VALIDATION" and result["reason_code"] not in REJECTION_RESULT_REASONS:
            problems.append("rejection_reason_invalid")
        if outcome == "FAILED_BEFORE_WRITE" and result["reason_code"] not in PRE_WRITE_RESULT_REASONS:
            problems.append("pre_write_reason_invalid")
        if outcome == "MUTEX_BUSY" and result["reason_code"] is not None:
            problems.append("mutex_busy_reason_invalid")
    elif outcome in {"NOT_CREATED", "NOT_CREATED_CONFLICT"}:
        if result["reason_code"] is not None:
            problems.append("not_created_reason_invalid")
        if not invoked:
            problems.append("not_created_without_save")
        if not (isinstance(readback, Mapping) and (not readback["found"] or not readback["created_by_integration_user"])):
            problems.append("not_created_readback_inconsistent")
    elif outcome == "OUTCOME_UNCERTAIN":
        if not invoked:
            problems.append("uncertain_without_save")
        if result["reason_code"] not in UNCERTAIN_RESULT_REASONS:
            problems.append("uncertain_reason_invalid")
    elif outcome == "CREATED_READBACK_MISMATCH":
        # Section 4.2: our own row was found (created by the integration user)
        # after a save, but its fields differ or it has no Guid.
        if not invoked or count != 1:
            problems.append("mismatch_without_save")
        if not (isinstance(readback, Mapping) and readback["found"] and readback["created_by_integration_user"]
                and (not readback["match"] or guid is None)):
            problems.append("mismatch_readback_inconsistent")
        if result["reason_code"] is not None:
            problems.append("mismatch_reason_invalid")
    if not NAME_COMPONENT_RE.fullmatch(component):
        problems.append("job_identity_missing")
    return tuple(dict.fromkeys(problems))


@dataclass(frozen=True, slots=True)
class Disposition:
    state: JobState
    reason: str | None
    write_attempts: int
    busy_attempts: int
    next_attempt_at: datetime | None
    outcome_row: str | None = None  # CREATED_VERIFIED | LINKED_EXISTING
    welcome: bool = False

    @property
    def outcome_code(self) -> str:
        return self.state.value if self.reason is None else f"{self.state.value}:{self.reason}"


def response_from_outcome_code(job_id: str, attempt_no: int, outcome_code: str, *, replayed: bool) -> dict[str, Any]:
    state, _, reason = outcome_code.partition(":")
    return {"job_id": job_id, "attempt_no": attempt_no, "state": state, "outcome_reason": reason or None, "replayed": replayed}


def write_retry_delay(write_attempts: int) -> timedelta:
    return FIRST_RETRY_DELAY if write_attempts <= 1 else LATER_RETRY_DELAY


def decide(
    job: JobRecord,
    result: Mapping[str, Any],
    *,
    now: datetime,
    guid_credited_elsewhere: bool,
    prior_uncertain: bool,
) -> Disposition:
    """Section 4.1 disposition of a lease-matching, schema-valid result."""

    writes, busy = job.write_attempts, job.busy_attempts
    if consistency_violations(job, result):
        return Disposition(JobState.MANUAL_REVIEW, "result_contract_violation", writes, busy, None)
    outcome = result["outcome"]
    reason = result["reason_code"]
    if outcome == "CREATED_VERIFIED":
        if guid_credited_elsewhere:
            return Disposition(JobState.MANUAL_REVIEW, "guid_conflict_on_fresh_create", writes, busy, None)
        return Disposition(JobState.CREATED_VERIFIED, None, writes, busy, None, "CREATED_VERIFIED", True)
    if outcome == "CREATED_VERIFIED_PRIOR_ATTEMPT":
        if guid_credited_elsewhere:
            return Disposition(JobState.LINKED_EXISTING, "guid_already_verified", writes, busy, None, "LINKED_EXISTING")
        return Disposition(JobState.CREATED_VERIFIED, None, writes, busy, None, "CREATED_VERIFIED", True)
    if outcome == "LINKED_EXISTING":
        return Disposition(JobState.LINKED_EXISTING, None, writes, busy, None, "LINKED_EXISTING")
    if outcome == "MANUAL_REVIEW":
        return Disposition(JobState.MANUAL_REVIEW, reason or "primitive_manual_review", writes, busy, None)
    if outcome == "CREATED_READBACK_MISMATCH":
        return Disposition(JobState.MANUAL_REVIEW, reason or "created_readback_mismatch", writes, busy, None)
    if outcome == "REJECTED_VALIDATION":
        return Disposition(JobState.REJECTED_VALIDATION, reason or "primitive_rejected_validation", writes, busy, None)
    if outcome in WRITE_CLASS_OUTCOMES:
        writes += 1
        if writes >= job.max_attempts:
            exhausted = "uncertain_exhausted" if (prior_uncertain or outcome in UNCERTAIN_OUTCOMES) else "attempts_exhausted"
            return Disposition(JobState.MANUAL_REVIEW, exhausted, writes, busy, None)
        return Disposition(JobState.RETRY_WAIT, reason, writes, busy, now + write_retry_delay(writes))
    if outcome == "MUTEX_BUSY":
        busy += 1
        if busy >= MAX_BUSY_ATTEMPTS:
            return Disposition(JobState.MANUAL_REVIEW, "mutex_busy_exhausted", writes, busy, None)
        return Disposition(JobState.RETRY_WAIT, reason, writes, busy, now + BUSY_RETRY_DELAY)
    return Disposition(JobState.MANUAL_REVIEW, "result_contract_violation", writes, busy, None)


def lease_expiry_disposition(job: JobRecord, *, expired_at: datetime) -> Disposition:
    """Reaped expired lease: an uncertain write-class attempt."""

    writes = job.write_attempts + 1
    if writes >= job.max_attempts:
        return Disposition(JobState.MANUAL_REVIEW, "uncertain_exhausted", writes, job.busy_attempts, None)
    return Disposition(JobState.RETRY_WAIT, "lease_expired", writes, job.busy_attempts, expired_at + LEASE_EXPIRY_RETRY_DELAY)
