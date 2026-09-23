"""Data contracts shared by the gateway, worker seam, and tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobState(str, Enum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    PRECHECKING = "PRECHECKING"
    ALLOCATION_BOUND = "ALLOCATION_BOUND"
    WRITE_INTENT_RECORDED = "WRITE_INTENT_RECORDED"
    WRITING = "WRITING"
    READBACK = "READBACK"
    CREATED_VERIFIED = "CREATED_VERIFIED"
    REJECTED_VALIDATION = "REJECTED_VALIDATION"
    RETRY_WAIT = "RETRY_WAIT"
    AMBIGUOUS_LOOKUP = "AMBIGUOUS_LOOKUP"
    WRITE_OUTCOME_UNCERTAIN = "WRITE_OUTCOME_UNCERTAIN"
    WRITER_TERMINATION_UNCONFIRMED = "WRITER_TERMINATION_UNCONFIRMED"
    CONFIRMED_NOT_CREATED = "CONFIRMED_NOT_CREATED"
    CREATED_READBACK_MISMATCH = "CREATED_READBACK_MISMATCH"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DEAD_LETTER = "DEAD_LETTER"


class ProbeStatus(str, Enum):
    FREE = "FREE"
    OCCUPIED = "OCCUPIED"
    AMBIGUOUS = "AMBIGUOUS"
    UNAVAILABLE = "UNAVAILABLE"


class ResultStatus(str, Enum):
    CREATED_VERIFIED = "CREATED_VERIFIED"
    WRITE_OUTCOME_UNCERTAIN = "WRITE_OUTCOME_UNCERTAIN"
    CONFIRMED_NOT_CREATED = "CONFIRMED_NOT_CREATED"
    CREATED_READBACK_MISMATCH = "CREATED_READBACK_MISMATCH"


class WriterHoldState(str, Enum):
    PENDING = "PENDING"
    REGISTERED = "REGISTERED"
    TERMINATION_CONFIRMED = "TERMINATION_CONFIRMED"
    QUARANTINED = "QUARANTINED"
    CLEARED = "CLEARED"


class ReconciliationCaseState(str, Enum):
    OPEN = "OPEN"
    EXACT_MATCH = "EXACT_MATCH"
    ABSENT = "ABSENT"
    MISMATCH = "MISMATCH"
    AMBIGUOUS = "AMBIGUOUS"
    MANUAL_REVIEW = "MANUAL_REVIEW"


def iso_utc(value: Any) -> str:
    if hasattr(value, "tzinfo") and hasattr(value, "isoformat"):
        if value.tzinfo is None:
            raise ValueError("timestamp_must_be_timezone_aware")
        rendered = value.astimezone(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
        return rendered.replace("+00:00", "Z")
    return str(value)


@dataclass(frozen=True, slots=True)
class SourceEvent:
    schema_version: str
    source_system: str
    form_alias: str
    response_id: str
    create_time: str
    mapping_version: str
    request_id: str
    payload: dict[str, Any]
    payload_hash: str
    operation: str = "member.create"


@dataclass(frozen=True, slots=True)
class MemberRecord:
    MemberNo: str
    MemberType: str
    Name: str
    MobilePhone: str
    EmailAddress: str
    DOB: str
    RegisterDate: str
    ExpiryDate: str
    OpeningPoints: int
    IsActive: bool
    Individual: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "MemberNo": self.MemberNo,
            "MemberType": self.MemberType,
            "Name": self.Name,
            "MobilePhone": self.MobilePhone,
            "EmailAddress": self.EmailAddress,
            "DOB": self.DOB,
            "RegisterDate": self.RegisterDate,
            "ExpiryDate": self.ExpiryDate,
            "OpeningPoints": self.OpeningPoints,
            "IsActive": self.IsActive,
            "Individual": self.Individual,
        }


@dataclass(slots=True)
class JobRecord:
    job_id: str
    request_id: str
    source_response_ref: str
    response_id: str
    payload_hash: str
    operation: str
    member_payload: dict[str, Any]
    created_at: str
    state: JobState = JobState.RECEIVED
    state_version: int = 0
    attempt: int = 0
    max_attempts: int = 3
    next_attempt_at: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    allocation_member_no: str | None = None
    allocation_probe_reference: str | None = None
    write_intent_id: str | None = None
    dispatch_fence_id: str | None = None
    save_invocation_count: int = 0
    result_status: ResultStatus | None = None
    last_error_code: str | None = None
    source_system: str = "google_forms"
    form_alias: str = "member_registration"
    mapping_version: str = "member-intake.v1"
    attempt_started_at: str | None = None
    writer_termination_state: str | None = None

    @property
    def dispatch_fenced(self) -> bool:
        return self.dispatch_fence_id is not None

    def safe_dict(self) -> dict[str, Any]:
        writer_state = self.writer_termination_state or (
            WriterHoldState.QUARANTINED.value
            if self.state == JobState.WRITER_TERMINATION_UNCONFIRMED
            else None
        )
        return {
            "schema_version": "xb.member.gateway.job.v2",
            "job_id": self.job_id,
            "request_id": self.request_id,
            "source_response_ref": self.source_response_ref,
            "payload_hash": self.payload_hash,
            "operation": self.operation,
            "source_system": self.source_system,
            "form_alias": self.form_alias,
            "mapping_version": self.mapping_version,
            "state": self.state.value,
            "state_version": self.state_version,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "attempt_started_at": self.attempt_started_at,
            "next_attempt_at": self.next_attempt_at,
            "lease_expires_at": self.lease_expires_at,
            "has_allocation": self.allocation_member_no is not None,
            "has_write_intent": self.write_intent_id is not None,
            "has_dispatch_fence": self.dispatch_fenced,
            "result_status": self.result_status.value if self.result_status else None,
            "last_error_code": self.last_error_code,
            "writer_termination_state": writer_state,
            "writer_termination_hold_active": writer_state not in (None, WriterHoldState.CLEARED.value),
            "writer_termination_proof_required": writer_state in {
                WriterHoldState.PENDING.value,
                WriterHoldState.REGISTERED.value,
                WriterHoldState.QUARANTINED.value,
            },
        }

    def worker_dict(self) -> dict[str, Any]:
        value = self.safe_dict()
        value["member_payload"] = dict(self.member_payload)
        if self.allocation_member_no is not None:
            value["allocation"] = {"member_no": self.allocation_member_no}
        return value


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    job_id: str
    worker_id: str
    expires_at: str
    state_version: int


@dataclass(frozen=True, slots=True)
class AllocationRecord:
    job_id: str
    source_response_id: str
    member_no: str
    probe_reference: str
    bound_at: str


@dataclass(frozen=True, slots=True)
class AllocationProbe:
    job_id: str
    candidate: str
    status: ProbeStatus
    probe_reference: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class AllocationRecheck:
    """Durable positive evidence immediately before dispatch."""

    recheck_id: str
    job_id: str
    attempt: int
    worker_id: str
    member_no: str
    status: ProbeStatus
    probe_reference: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class WriteIntentRecord:
    job_id: str
    intent_id: str
    member_no: str
    payload_hash: str
    recorded_at: str
    recheck_id: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchFenceRecord:
    job_id: str
    fence_id: str
    member_no: str
    operation: str
    created_at: str
    recheck_id: str | None = None
    execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class WriterExecutionHold:
    """Private durable binding for one fenced writer execution."""

    hold_id: str
    job_id: str
    fence_id: str
    attempt: int
    worker_session: str | None
    host_binding: str | None
    execution_id: str
    member_no: str
    state: WriterHoldState
    state_version: int
    pid: int | None
    process_start_time: str | None
    evidence_type: str | None
    evidence_reference: str | None
    created_at: str
    updated_at: str
    registered_at: str | None = None
    termination_confirmed_at: str | None = None
    quarantined_at: str | None = None
    cleared_at: str | None = None

    @property
    def active(self) -> bool:
        return self.state != WriterHoldState.CLEARED

    @property
    def termination_confirmed(self) -> bool:
        return self.state == WriterHoldState.TERMINATION_CONFIRMED


@dataclass(frozen=True, slots=True)
class ReconciliationCaseRecord:
    case_id: str
    job_id: str
    member_no: str
    state: ReconciliationCaseState
    opened_at: str
    closed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationCheckRecord:
    check_id: str
    case_id: str
    lookup_status: str
    readback_found: bool
    readback_match: bool
    checked_at: str


@dataclass(frozen=True, slots=True)
class ResultRecord:
    job_id: str
    result_hash: str
    status: ResultStatus
    member_no: str
    dispatch_fence_id: str
    save_invocation_count: int
    readback_found: bool
    readback_match: bool
    reconciliation_required: bool
    error_code: str | None
    acknowledged_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "xb.member.gateway.result.v1",
            "job_id": self.job_id,
            "operation": "member.create",
            "result_hash": self.result_hash,
            "status": self.status.value,
            "member_no": self.member_no,
            "dispatch_fence_id": self.dispatch_fence_id,
            "save_invocation_count": self.save_invocation_count,
            "readback_found": self.readback_found,
            "readback_match": self.readback_match,
            "reconciliation_required": self.reconciliation_required,
            "error_code": self.error_code,
            "acknowledged_at": self.acknowledged_at,
        }


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    job: JobRecord
    replayed: bool
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class SourceRejection:
    """PII-free customer-validation rejection; it carries no customer field."""

    schema_version: str
    source_system: str
    form_alias: str
    response_id: str
    create_time: str
    mapping_version: str
    request_id: str
    payload_hash: str
    error_code: str
    operation: str = "member.create"


@dataclass(frozen=True, slots=True)
class RejectionOutcome:
    rejection_id: str
    source_response_ref: str
    error_code: str
    replayed: bool


class SourceAdmissionMode(str, Enum):
    FIRST_MEMBER = "first_member"
    CONTINUOUS = "continuous"


class HandlingOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ScanEpochStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class ScanPageState(str, Enum):
    OPEN = "OPEN"
    COMMITTED = "COMMITTED"
    ABANDONED = "ABANDONED"


# The only reasons a scan epoch may be abandoned and restarted from the same
# fixed cutover. General OAuth/API/schema/mapping errors halt instead.
RESTART_REASONS = frozenset({"token_invalidated", "ambiguous_crashed_attempt"})
ABANDON_REASONS = RESTART_REASONS | {"admission_mode_changed"}
SOURCE_PAGE_SIZE = 1
SOURCE_PAGE_ITEM_FIELDS = frozenset({"response_id", "create_time", "payload_hash"})


@dataclass(frozen=True, slots=True)
class SourceCursor:
    """Durable cursor/config row. ``watermark`` is deprecated audit-only."""

    source_system: str
    form_alias: str
    mapping_version: str
    watermark: str
    production_cutover_exact: str | None
    form_id: str | None
    state_version: int
    initial_window_admission_count: int


@dataclass(frozen=True, slots=True)
class HandlingReceipt:
    response_id: str
    source_response_ref: str
    form_alias: str
    form_id: str
    mapping_version: str
    create_time_exact: str
    payload_fingerprint: str
    outcome: HandlingOutcome
    job_id: str | None
    rejection_id: str | None
    receipt_version: int
    recorded_at: str


@dataclass(frozen=True, slots=True)
class ScanPage:
    page_id: str
    epoch_id: str
    page_ordinal: int
    page_state: ScanPageState
    request_page_token: str | None
    next_page_token: str | None
    terminal: bool
    items: tuple[tuple[str, str, str], ...]
    opened_state_version: int
    committed_state_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "page_ordinal": self.page_ordinal,
            "page_state": self.page_state.value,
            "request_page_token": self.request_page_token,
            "next_page_token": self.next_page_token,
            "terminal": self.terminal,
            "item_count": len(self.items),
            "items": [
                {"response_id": rid, "create_time": created, "payload_hash": digest}
                for rid, created, digest in self.items
            ],
        }


@dataclass(frozen=True, slots=True)
class ScanEpoch:
    epoch_id: str
    source_system: str
    form_alias: str
    form_id: str
    mapping_version: str
    admission_mode: SourceAdmissionMode
    filter_exact: str
    production_cutover_exact: str
    page_size: int
    status: ScanEpochStatus
    epoch_state_version: int
    current_page_token: str | None
    page_ordinal: int
    predecessor_epoch_id: str | None
    restart_reason: str | None
    abandon_reason: str | None
    created_at: str
    completed_at: str | None = None

    @property
    def binding(self) -> tuple[Any, ...]:
        return (
            self.source_system, self.form_alias, self.form_id, self.mapping_version,
            self.admission_mode, self.filter_exact, self.production_cutover_exact, self.page_size,
        )

    def to_dict(self, open_page: ScanPage | None = None) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "status": self.status.value,
            "admission_mode": self.admission_mode.value,
            "epoch_state_version": self.epoch_state_version,
            "current_page_token": self.current_page_token,
            "page_ordinal": self.page_ordinal,
            "predecessor_epoch_id": self.predecessor_epoch_id,
            "restart_reason": self.restart_reason,
            "open_page": None if open_page is None else open_page.to_dict(),
        }


def source_cursor_v2(
    cursor: SourceCursor,
    *,
    admission_mode: SourceAdmissionMode,
    epoch: ScanEpoch | None,
    open_page: ScanPage | None,
) -> dict[str, Any]:
    """Cursor-v2 projection. The deprecated admitted tuple and resume token
    are intentionally absent: they carry no admission or skip authority."""

    if cursor.production_cutover_exact is None:
        raise ValueError("source_production_cutover_uninitialized")
    remaining = (
        max(0, 1 - cursor.initial_window_admission_count)
        if admission_mode == SourceAdmissionMode.FIRST_MEMBER
        else None
    )
    return {
        "schema_version": "xb.member.gateway.source_cursor.v2",
        "source_system": cursor.source_system,
        "form_alias": cursor.form_alias,
        "mapping_version": cursor.mapping_version,
        "production_cutover_exact": cursor.production_cutover_exact,
        "filter_exact": f"timestamp >= {cursor.production_cutover_exact}",
        "admission_mode": admission_mode.value,
        "page_size": SOURCE_PAGE_SIZE,
        "cursor_state_version": cursor.state_version,
        "accepted_member_count": cursor.initial_window_admission_count,
        "accepted_member_allowance_remaining": remaining,
        "active_epoch": None if epoch is None else epoch.to_dict(open_page),
    }


class WelcomeEmailState(str, Enum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    RETRY_WAIT = "RETRY_WAIT"
    SEND_INTENT_RECORDED = "SEND_INTENT_RECORDED"
    SENT = "SENT"
    DELIVERY_OUTCOME_UNCERTAIN = "DELIVERY_OUTCOME_UNCERTAIN"
    DEAD_LETTER = "DEAD_LETTER"


WELCOME_TERMINAL_STATES = frozenset(
    {WelcomeEmailState.SENT, WelcomeEmailState.DELIVERY_OUTCOME_UNCERTAIN, WelcomeEmailState.DEAD_LETTER}
)


@dataclass(frozen=True, slots=True)
class WelcomeEmailOutbox:
    """Private outbox row. ``recipient`` and ``response_id`` never leave the
    private mailer claim envelope."""

    outbox_id: str
    job_id: str
    response_id: str
    source_response_ref: str
    template_id: str
    recipient: str
    message_hash: str
    state: WelcomeEmailState
    state_version: int
    attempt: int
    max_attempts: int
    lease_id: str | None
    lease_expires_at: str | None
    next_attempt_at: str | None
    send_intent_at: str | None
    last_error_code: str | None
    created_at: str
    updated_at: str

    def safe_dict(self) -> dict[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "job_id": self.job_id,
            "source_response_ref": self.source_response_ref,
            "template_id": self.template_id,
            "message_hash": self.message_hash,
            "state": self.state.value,
            "state_version": self.state_version,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "last_error_code": self.last_error_code,
        }


@dataclass(frozen=True, slots=True)
class ReadbackCheck:
    match: bool
    mismatches: tuple[str, ...] = field(default_factory=tuple)
