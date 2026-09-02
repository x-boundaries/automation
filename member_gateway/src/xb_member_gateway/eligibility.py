"""Single explicit fail-closed predicate set for unattended dispatch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import GatewayConfig
from .models import AllocationRecord, JobRecord, JobState


_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


@dataclass(frozen=True, slots=True)
class EligibilityContext:
    config: GatewayConfig
    job: JobRecord
    allocation: AllocationRecord | None = None
    worker_id: str | None = None
    lease_owner: str | None = None
    positive_free_evidence: bool | None = None
    fresh_bound_member_no_recheck: bool | None = None
    gateway_ready: bool | None = None
    autocount_adapter_ready: bool | None = None
    worker_credential_valid: bool | None = None
    kill_switch_rechecked: bool | None = None
    save_invocation_count: int | None = None
    rate_allowed: bool | None = None
    source_conflict: bool | None = False
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    predicates: dict[str, bool]
    reasons: tuple[str, ...]
    unknown_predicates: tuple[str, ...]


def _known_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("eligibility_clock_must_be_timezone_aware")
    return result.astimezone(timezone.utc)


def _deadline_ok(job: JobRecord, config: GatewayConfig, now: datetime) -> bool:
    if job.next_attempt_at is not None:
        try:
            next_attempt = datetime.fromisoformat(job.next_attempt_at.replace("Z", "+00:00"))
            if next_attempt.astimezone(timezone.utc) > now:
                return False
        except (TypeError, ValueError):
            return False
    if job.attempt_started_at is None:
        return False
    try:
        started = datetime.fromisoformat(job.attempt_started_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    if started > now:
        return False
    return now <= started + timedelta(seconds=config.execution_deadline_seconds)


def evaluate_eligibility(context: EligibilityContext) -> EligibilityDecision:
    """Evaluate every pre-dispatch condition; unknown is always a block."""

    job = context.job
    config = context.config
    now = _now(context.now)
    allocation = context.allocation
    payload = job.member_payload
    required = {
        "name", "phone", "email", "birthday_month", "marketing_consent", "pdpa_acknowledged"
    }
    payload_shape_valid = set(payload).issubset(required | {"create_time"}) and required.issubset(payload)
    predicates: dict[str, bool | None] = {
        "production_activation_enabled": config.production_activation_enabled,
        "kill_switch_clear": config.kill_switch_clear,
        "correct_configured_environment": config.environment_matches,
        "source_system_allowlisted": job.source_system == "google_forms",
        "source_form_allowlisted": job.form_alias in config.allowed_form_aliases,
        "source_mapping_version_allowlisted": job.mapping_version in config.allowed_mapping_versions,
        "response_identity_valid": isinstance(job.response_id, str) and bool(_ID_RE.fullmatch(job.response_id)),
        "payload_hash_valid": isinstance(job.payload_hash, str) and bool(_HASH_RE.fullmatch(job.payload_hash)),
        "pdpa_acknowledged": payload.get("pdpa_acknowledged") is True,
        "marketing_consent_recognized": payload.get("marketing_consent") in {"Yes", "No"},
        "required_source_fields_valid": payload_shape_valid,
        "no_unknown_mapping_or_source_conflict": context.source_conflict is False,
        "operation_exact": job.operation == "member.create",
        "eligible_state": job.state in {JobState.ALLOCATION_BOUND, JobState.WRITE_INTENT_RECORDED},
        "current_lease_ownership": bool(context.worker_id)
        and context.lease_owner == context.worker_id
        and job.lease_owner == context.worker_id,
        "bound_allocation": allocation is not None and job.allocation_member_no == allocation.member_no,
        "positive_free_candidate_evidence": context.positive_free_evidence,
        "fresh_bound_member_no_recheck": context.fresh_bound_member_no_recheck,
        "production_member_no_constraint_valid": config.member_no_constraint_valid
        and allocation is not None
        and len(allocation.member_no) <= (config.member_no_max_length or 0),
        "no_prior_dispatch": job.dispatch_fence_id is None,
        "no_prior_result": job.result_status is None,
        "no_prior_uncertain_state": job.state != JobState.WRITE_OUTCOME_UNCERTAIN,
        "rate_attempt_deadline_eligible": context.rate_allowed
        and job.attempt > 0
        and job.attempt <= job.max_attempts
        and _deadline_ok(job, config, now),
        "gateway_ready": context.gateway_ready,
        "autocount_adapter_ready": context.autocount_adapter_ready,
        "worker_credential_valid": context.worker_credential_valid,
        "kill_switch_rechecked_immediately": context.kill_switch_rechecked,
        "save_invocation_count_zero": context.save_invocation_count == 0
        if context.save_invocation_count is not None
        else None,
    }

    normalized: dict[str, bool] = {}
    reasons: list[str] = []
    unknown: list[str] = []
    for name, value in predicates.items():
        known = _known_bool(value)
        if known is None:
            normalized[name] = False
            unknown.append(name)
            reasons.append(f"{name}_unknown")
        elif not known:
            normalized[name] = False
            reasons.append(name)
        else:
            normalized[name] = True
    return EligibilityDecision(
        eligible=not reasons,
        predicates=normalized,
        reasons=tuple(reasons),
        unknown_predicates=tuple(unknown),
    )
