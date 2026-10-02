"""Gateway-side source and identity gates for v2 jobs.

These predicates were enforced by the removed ``eligibility.py`` on the v1
dispatch path. v2 applies them twice (W-G3-152 parent correction):

1. At ``VALIDATED`` during ingest: a failing job becomes
   ``REJECTED_VALIDATION`` with one bounded reason and never reaches
   ``QUEUED``.
2. At claim (defence in depth): a stored job that fails any source predicate
   is never returned by claim, and nothing is claimed unless dispatch is
   enabled, the environment matches and the gateway is ready.

Every reason here is gateway-side only; none can appear in a result body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .canonical import OPERATION, PAYLOAD_FIELDS
from .identity import REJECTION_REASONS as IDENTITY_REJECTION_REASONS
from .identity import IdentityRejected, MemberIdentity, derive_identity, validate_book_mode


_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
# The stored canonical payload carries the derived create_time as well.
STORED_PAYLOAD_FIELDS = PAYLOAD_FIELDS | {"create_time"}

# Closed set of source-gate reasons, in evaluation order.
SOURCE_GATE_REASONS = (
    "operation_not_member_create",
    "source_system_not_allowlisted",
    "form_alias_not_allowlisted",
    "mapping_version_not_allowlisted",
    "response_identity_invalid",
    "payload_hash_invalid",
    "payload_fields_invalid",
    "pdpa_not_acknowledged",
    "marketing_consent_unrecognized",
)
VALIDATION_REJECTION_REASONS = frozenset(SOURCE_GATE_REASONS) | IDENTITY_REJECTION_REASONS


class AdmissionRejected(ValueError):
    """The job must become ``REJECTED_VALIDATION`` with this bounded reason."""

    def __init__(self, reason: str):
        if reason not in VALIDATION_REJECTION_REASONS:
            raise ValueError("admission_reason_invalid")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """Configured inputs of the VALIDATED gate. There is no default book mode."""

    book_mode: str
    allowed_form_aliases: tuple[str, ...]
    allowed_mapping_versions: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_book_mode(self.book_mode)
        if not self.allowed_form_aliases or not self.allowed_mapping_versions:
            raise ValueError("admission_allowlist_required")

    @classmethod
    def from_config(cls, config: Any) -> "AdmissionPolicy":
        return cls(config.member_book_mode, tuple(config.allowed_form_aliases), tuple(config.allowed_mapping_versions))


@dataclass(frozen=True, slots=True)
class ClaimGate:
    """Service-side readiness evaluated before the claim transaction; the
    repository adds the durable control flags under the control-row lock."""

    policy: AdmissionPolicy
    environment_matches: bool
    ready: bool

    @property
    def open(self) -> bool:
        return self.environment_matches is True and self.ready is True


def source_gate_reason(job: Any, policy: AdmissionPolicy) -> str | None:
    """First failing source predicate for a stored job, or ``None``."""

    payload = job.member_payload if isinstance(job.member_payload, dict) else {}
    checks = (
        ("operation_not_member_create", job.operation == OPERATION),
        ("source_system_not_allowlisted", job.source_system == "google_forms"),
        ("form_alias_not_allowlisted", job.form_alias in policy.allowed_form_aliases),
        ("mapping_version_not_allowlisted", job.mapping_version in policy.allowed_mapping_versions),
        ("response_identity_invalid", isinstance(job.response_id, str) and _ID_RE.fullmatch(job.response_id) is not None),
        ("payload_hash_invalid", isinstance(job.payload_hash, str) and _HASH_RE.fullmatch(job.payload_hash) is not None),
        ("payload_fields_invalid", set(payload) == STORED_PAYLOAD_FIELDS),
        ("pdpa_not_acknowledged", payload.get("pdpa_acknowledged") is True),
        ("marketing_consent_unrecognized", payload.get("marketing_consent") in {"Yes", "No"}),
    )
    for reason, passed in checks:
        if not passed:
            return reason
    return None


def validate_for_queue(job: Any, policy: AdmissionPolicy) -> MemberIdentity:
    """The VALIDATED gate: source predicates first, then XB-MN-1 (2.1-2.3)."""

    reason = source_gate_reason(job, policy)
    if reason is not None:
        raise AdmissionRejected(reason)
    try:
        return derive_identity(job.member_payload, policy.book_mode)
    except IdentityRejected as exc:
        raise AdmissionRejected(exc.reason) from exc


def claimable(job: Any, policy: AdmissionPolicy) -> bool:
    """Claim-time defence in depth over a stored QUEUED/RETRY_WAIT job.

    The stored immutable identity must equal a fresh derivation under the
    current book mode, so a job admitted under another mode (or a tampered
    row) is never handed to the worker."""

    if source_gate_reason(job, policy) is not None:
        return False
    try:
        identity = derive_identity(job.member_payload, policy.book_mode)
    except IdentityRejected:
        return False
    return (
        job.member_no_rule == identity.rule
        and job.base_member_no == identity.base_member_no
        and job.name_component == identity.name_component
    )
