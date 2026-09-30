"""Durable member-gateway state (v2) and an offline repository fake.

The fake is used by package tests. The PostgreSQL adapter commits every
state-changing action in one short transaction; AutoCount is never called
from this module. v2 job lifecycle (W-G2-149 section 3):

    RECEIVED -> VALIDATED -> QUEUED | REJECTED_VALIDATION      (ingest)
    QUEUED | RETRY_WAIT -> LEASED                               (claim)
    LEASED -> CREATED_VERIFIED | LINKED_EXISTING | REJECTED_VALIDATION
              | MANUAL_REVIEW | RETRY_WAIT                      (result / reaper)
    MANUAL_REVIEW -> RESOLVED | QUEUED                          (resolve)

MemberNo and member Guid are stored in ``member_outcomes`` and
``job_resolutions`` only; they are never logged, audited or returned.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Callable, Iterator, Mapping

from .admission import AdmissionPolicy, AdmissionRejected, ClaimGate, claimable, validate_for_queue
from .canonical import (
    CanonicalizationError, HASH_RE as PAYLOAD_HASH_RE, SAFE_ID_RE as SAFE_RESPONSE_ID_RE,
    canonical_json, create_time_utc_from_exact, exact_filter, parse_google_create_time_exact,
    parse_rfc3339, render_create_time_utc, validate_page_token,
)
from .crypto import hmac_reference
from .models import (
    AttemptRecord, HandlingOutcome, HandlingReceipt, IngestOutcome, JobRecord, JobResolution, JobState,
    LeaseRecord, MemberOutcomeRecord, RESTART_REASONS, RejectionOutcome, SOURCE_PAGE_ITEM_FIELDS,
    SOURCE_PAGE_SIZE, ScanEpoch, ScanEpochStatus, ScanPage, ScanPageState, SourceAdmissionMode,
    SourceCursor, SourceEvent, SourceRejection, WelcomeEmailOutbox, WelcomeEmailState,
)
from .notifications import (
    WELCOME_INITIAL_DELAY, WELCOME_LEASE_SECONDS, WELCOME_MAX_ATTEMPTS, WELCOME_TEMPLATE_ID,
    WelcomeEmailError, assert_transition, build_welcome_message, next_attempt_after,
    safe_failure_target, verify_message, welcome_message_hash,
)
from .results import (
    DEFAULT_WRITE_BUDGET, MAX_WRITE_BUDGET, REQUEUE_BUDGET_STEP, UNCERTAIN_OUTCOMES, Disposition,
    decide, lease_expiry_disposition, response_from_outcome_code, result_hash,
)
from .state_machine import LEGACY_STATES, next_state


class RepositoryError(RuntimeError):
    pass


class JobNotFound(RepositoryError):
    pass


class SourceConflict(RepositoryError):
    pass


class LeaseConflict(RepositoryError):
    pass


class ResultConflict(RepositoryError):
    """A different body for a lease whose result is already recorded (409)."""


class ResultStale(RepositoryError):
    """The body's lease expired, was replaced or does not match (409). No
    state is changed; the next attempt's probe settles what happened."""


class ResolutionConflict(RepositoryError):
    """Resolve is allowed only from MANUAL_REVIEW and within the budget cap."""


def utc_now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("repository_clock_must_be_timezone_aware")
    return result.astimezone(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return utc_now(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return utc_now(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


LEASE_SECONDS = 600
RESOLUTION_ACTIONS = frozenset({"CLOSE", "REQUEUE"})
_RESOLUTION_CODE_RE = re.compile(r"^[a-z0-9_.:-]{1,80}$")
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SUBJECT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_WORKER_SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
# Job states counted in the metadata-only operator status view.
V2_STATUS_STATES = (
    "RECEIVED", "VALIDATED", "QUEUED", "LEASED", "RETRY_WAIT", "CREATED_VERIFIED",
    "LINKED_EXISTING", "REJECTED_VALIDATION", "MANUAL_REVIEW", "RESOLVED",
)


def new_lease_token() -> str:
    return f"lease-{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """``claimed`` with job and lease, or a closed ``reason`` for no claim."""

    claimed: bool
    reason: str | None = None
    job: JobRecord | None = None
    lease: LeaseRecord | None = None


def next_write_budget(job: JobRecord) -> int:
    """REQUEUE grants +3 write attempts, capped at 12 in total."""

    budget = min(job.max_attempts + REQUEUE_BUDGET_STEP, MAX_WRITE_BUDGET)
    if budget <= job.write_attempts:
        raise ResolutionConflict("write_budget_cap_reached")
    return budget


def validate_resolution(action: Any, resolution_code: Any, member_no: Any, member_guid: Any, resolved_by: Any) -> None:
    if action not in RESOLUTION_ACTIONS:
        raise ResolutionConflict("resolution_action_invalid")
    if not isinstance(resolution_code, str) or not _RESOLUTION_CODE_RE.fullmatch(resolution_code):
        raise ResolutionConflict("resolution_code_invalid")
    if member_no is not None and (not isinstance(member_no, str) or not 1 <= len(member_no) <= 20):
        raise ResolutionConflict("resolution_member_no_invalid")
    if member_guid is not None and (not isinstance(member_guid, str) or not _GUID_RE.fullmatch(member_guid)):
        raise ResolutionConflict("resolution_member_guid_invalid")
    if not isinstance(resolved_by, str) or not _SUBJECT_RE.fullmatch(resolved_by):
        raise ResolutionConflict("resolution_actor_invalid")


def validate_worker_session(value: Any) -> str:
    if not isinstance(value, str) or not _WORKER_SESSION_RE.fullmatch(value):
        raise RepositoryError("worker_id_invalid")
    return value


def cas_matches(job: JobRecord, lease: LeaseRecord | None, result: Mapping[str, Any], worker_session: str, current: datetime) -> bool:
    """Section 3 compare-and-set: LEASED, same active token, same session,
    unexpired, same attempt_no and same state_version."""

    return (
        job.state == JobState.LEASED
        and lease is not None
        and lease.active
        and lease.lease_token == result["lease_id"]
        and lease.worker_id == worker_session
        and parse_timestamp(lease.expires_at) > current
        and result["attempt_no"] == job.attempt == lease.attempt_no
        and result["state_version"] == job.state_version == lease.state_version
    )

class SourceRestartReasonInvalid(SourceConflict):
    pass


def cutover_key(value: str | None) -> tuple[int, int]:
    """Lossless ordering key; the only comparator used against the cutover."""

    if value is None:
        raise SourceConflict("source_production_cutover_uninitialized")
    try:
        return parse_google_create_time_exact(value)
    except CanonicalizationError as exc:
        raise SourceConflict("source_create_time_exact_invalid") from exc


def receipt_matches(
    receipt: HandlingReceipt, form_alias: str, form_id: str | None, mapping_version: str,
    create_time_exact: str, fingerprint: str, outcome: HandlingOutcome,
) -> bool:
    """Byte equality of the exact createTime: a same-instant but differently
    formatted string is an immutable-source conflict."""

    return (
        receipt.outcome == outcome
        and receipt.create_time_exact == create_time_exact
        and receipt.payload_fingerprint == fingerprint
        and receipt.form_alias == form_alias
        and receipt.form_id == form_id
        and receipt.mapping_version == mapping_version
    )


def epoch_binding(cursor: SourceCursor, admission_mode: SourceAdmissionMode) -> tuple[Any, ...]:
    return (
        cursor.source_system, cursor.form_alias, cursor.form_id, cursor.mapping_version,
        admission_mode, exact_filter(cursor.production_cutover_exact or ""),
        cursor.production_cutover_exact, SOURCE_PAGE_SIZE,
    )


def new_epoch(
    cursor: SourceCursor, admission_mode: SourceAdmissionMode, predecessor: str | None,
    restart_reason: str | None, current: datetime,
) -> ScanEpoch:
    """Every epoch repeats the same inclusive fixed-cutover filter."""

    return ScanEpoch(
        epoch_id=f"epoch-{uuid.uuid4().hex}", source_system=cursor.source_system,
        form_alias=cursor.form_alias, form_id=cursor.form_id or "", mapping_version=cursor.mapping_version,
        admission_mode=admission_mode, filter_exact=exact_filter(cursor.production_cutover_exact or ""),
        production_cutover_exact=cursor.production_cutover_exact or "", page_size=SOURCE_PAGE_SIZE,
        status=ScanEpochStatus.ACTIVE, epoch_state_version=0, current_page_token=None, page_ordinal=0,
        predecessor_epoch_id=predecessor, restart_reason=restart_reason, abandon_reason=None,
        created_at=timestamp(current),
    )


def normalize_page_items(items: Any, epoch: ScanEpoch) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(items, list) or len(items) > epoch.page_size:
        raise SourceConflict("source_page_items_invalid")
    normalized: list[tuple[str, str, str]] = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != SOURCE_PAGE_ITEM_FIELDS:
            raise SourceConflict("source_page_items_invalid")
        response_id, create_time, digest = item["response_id"], item["create_time"], item["payload_hash"]
        if not isinstance(response_id, str) or not SAFE_RESPONSE_ID_RE.fullmatch(response_id):
            raise SourceConflict("source_page_items_invalid")
        if not isinstance(digest, str) or not PAYLOAD_HASH_RE.fullmatch(digest):
            raise SourceConflict("source_page_items_invalid")
        if cutover_key(create_time) < cutover_key(epoch.production_cutover_exact):
            raise SourceConflict("source_page_item_before_cutover")
        normalized.append((response_id, create_time, digest))
    if len({item[0] for item in normalized}) != len(normalized):
        raise SourceConflict("source_page_items_invalid")
    return tuple(normalized)


def verify_outbox_identity(outbox: WelcomeEmailOutbox, job: JobRecord) -> None:
    message = build_welcome_message(job.member_payload["email"])
    if (
        outbox.job_id != job.job_id
        or outbox.response_id != job.response_id
        or outbox.template_id != WELCOME_TEMPLATE_ID
        or outbox.recipient != message["to"]
        or outbox.message_hash != welcome_message_hash(message)
    ):
        raise ResultConflict("welcome_outbox_identity_mismatch")


def check_welcome_lease(
    outbox: WelcomeEmailOutbox, lease_id: str, expected_state_version: int,
    required: WelcomeEmailState, current: datetime, *, require_unexpired: bool,
) -> None:
    if outbox.state != required:
        raise WelcomeEmailError("welcome_email_state_conflict")
    if outbox.lease_id != lease_id:
        raise WelcomeEmailError("welcome_email_lease_conflict")
    if outbox.state_version != expected_state_version:
        raise WelcomeEmailError("welcome_email_state_version_mismatch")
    if require_unexpired and (outbox.lease_expires_at is None or parse_timestamp(outbox.lease_expires_at) <= current):
        raise WelcomeEmailError("welcome_email_lease_expired")


def welcome_result_target(outbox: WelcomeEmailOutbox, outcome: str) -> WelcomeEmailState:
    if outcome == "smtp_accepted":
        return WelcomeEmailState.SENT
    if outcome == "delivery_outcome_uncertain":
        return WelcomeEmailState.DELIVERY_OUTCOME_UNCERTAIN
    if outcome == "failed_before_send_intent":
        return safe_failure_target(outbox.attempt, outbox.max_attempts)
    raise WelcomeEmailError("welcome_email_outcome_invalid")


class InMemoryRepository:
    """Thread-safe fake of all durable gateway records."""

    def __init__(
        self,
        *,
        reference_key: bytes = b"synthetic-member-gateway-key",
        source_cutover_watermark: str | None = "1970-01-01T00:00:00Z",
        source_production_cutover_exact: str | None = None,
        source_form_id: str | None = "synthetic-form",
    ):
        self._lock = RLock()
        self._reference_key = reference_key
        self._responses: dict[str, SourceEvent] = {}
        self._request_hashes: dict[str, str] = {}
        self._request_responses: dict[str, str] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._job_by_response: dict[str, str] = {}
        self._leases: dict[str, LeaseRecord] = {}
        self._attempts: dict[str, list[AttemptRecord]] = {}
        self._member_outcomes: dict[str, MemberOutcomeRecord] = {}
        self._resolutions: list[JobResolution] = []
        self._result_conflicts: list[dict[str, str]] = []
        self._control = {"production_activation_enabled": False, "kill_switch_enabled": True}
        self._audit_events: list[dict[str, Any]] = []
        self._source_cursors: dict[tuple[str, str, str], SourceCursor] = {}
        self._receipts: dict[str, HandlingReceipt] = {}
        self._rejections: dict[str, dict[str, Any]] = {}
        self._epochs: dict[str, ScanEpoch] = {}
        self._pages: dict[str, ScanPage] = {}
        self._page_items: list[dict[str, Any]] = []
        self._outbox: dict[str, WelcomeEmailOutbox] = {}
        self._outbox_by_job: dict[str, str] = {}
        self._welcome_events: list[dict[str, Any]] = []
        if source_cutover_watermark is not None:
            self.initialize_source_cursor(
                "google_forms", "member_registration", "member-intake.v1",
                source_cutover_watermark,
                production_cutover_exact=source_production_cutover_exact or source_cutover_watermark,
                form_id=source_form_id,
            )

    def initialize_source_cursor(
        self, source_system: str, form_alias: str, mapping_version: str, watermark: str,
        *, production_cutover_exact: str, form_id: str | None,
    ) -> SourceCursor:
        """Synthetic-test initialization seam; production bootstrap never calls it."""

        normalized = timestamp(parse_rfc3339(watermark, field="source_cutover_watermark"))
        cutover_key(production_cutover_exact)
        key = (source_system, form_alias, mapping_version)
        with self._lock:
            if key in self._source_cursors:
                raise SourceConflict("source_cursor_already_initialized")
            cursor = SourceCursor(source_system, form_alias, mapping_version, normalized, production_cutover_exact, form_id, 0, 0)
            self._source_cursors[key] = cursor
            return self._copy(cursor)

    @staticmethod
    def _copy(value: Any) -> Any:
        return copy.deepcopy(value)

    def _job(self, job_id: str) -> JobRecord:
        if job_id not in self._jobs:
            raise JobNotFound("job_not_found")
        return self._jobs[job_id]

    def _move(self, job: JobRecord, target: JobState) -> None:
        job.state = next_state(job.state, target)
        job.state_version += 1

    def _audit(self, event_type: str, job: JobRecord | None = None, *, error_code: str | None = None, count: int | None = None) -> None:
        """Metadata-only audit: never MemberNo, Guid, response id or payload."""

        entry: dict[str, Any] = {"event_type": event_type, "recorded_at": timestamp()}
        if job is not None:
            entry.update({"job_id": job.job_id, "state": job.state.value, "operation": job.operation})
        if error_code is not None:
            entry["error_code"] = error_code
        if count is not None:
            entry["count"] = count
        self._audit_events.append(entry)

    @staticmethod
    def _admit(job: JobRecord, policy: AdmissionPolicy) -> None:
        """RECEIVED -> VALIDATED -> QUEUED | REJECTED_VALIDATION, computing
        the immutable XB-MN-1 identity exactly once."""

        job.state = next_state(job.state, JobState.VALIDATED)
        job.state_version += 1
        try:
            identity = validate_for_queue(job, policy)
        except AdmissionRejected as exc:
            job.state = next_state(job.state, JobState.REJECTED_VALIDATION)
            job.state_version += 1
            job.outcome_reason = exc.reason
            return
        job.member_no_rule = identity.rule
        job.base_member_no = identity.base_member_no
        job.name_component = identity.name_component
        job.state = next_state(job.state, JobState.QUEUED)
        job.state_version += 1

    def ingest_source_event(
        self,
        event: SourceEvent,
        *,
        policy: AdmissionPolicy,
        now: datetime | None = None,
        initial_window_max: int | None = 1,
    ) -> IngestOutcome:
        """Receipt-driven admission. An unseen response is never compared with
        another response's position; only the fixed cutover excludes. The
        source receipt, payload hash and first-member guard are unchanged."""

        if not isinstance(policy, AdmissionPolicy):
            raise RepositoryError("admission_policy_required")
        with self._lock:
            cursor_key = (event.source_system, event.form_alias, event.mapping_version)
            cursor = self._source_cursor_for_admission(cursor_key)
            if cutover_key(event.create_time) < cutover_key(cursor.production_cutover_exact):
                raise SourceConflict("source_event_before_cutover")
            prior_hash = self._request_hashes.get(event.request_id)
            prior_response = self._request_responses.get(event.request_id)
            if prior_hash is not None and (prior_hash != event.payload_hash or prior_response != event.response_id):
                raise SourceConflict("request_identity_payload_conflict")
            receipt = self._receipts.get(event.response_id)
            if receipt is not None:
                if not receipt_matches(receipt, event.form_alias, cursor.form_id, event.mapping_version, event.create_time, event.payload_hash, HandlingOutcome.ACCEPTED):
                    self._audit("source_conflict", error_code="source_identity_conflict")
                    raise SourceConflict("source_identity_payload_conflict")
                self._request_hashes[event.request_id] = event.payload_hash
                self._request_responses[event.request_id] = event.response_id
                return IngestOutcome(self._copy(self._job(self._job_by_response[event.response_id])), replayed=True)
            if event.response_id in self._responses:
                raise RepositoryError("source_handling_receipt_missing")
            if initial_window_max is not None and cursor.initial_window_admission_count >= initial_window_max:
                # The losing admission gets no receipt and cannot checkpoint; it
                # stays discoverable for a later continuous epoch.
                raise SourceConflict("initial_source_window_exhausted")
            self._responses[event.response_id] = self._copy(event)
            self._request_hashes[event.request_id] = event.payload_hash
            self._request_responses[event.request_id] = event.response_id
            payload = self._copy(event.payload)
            payload["create_time"] = render_create_time_utc(create_time_utc_from_exact(event.create_time))
            source_ref = hmac_reference(event.response_id, self._reference_key)
            job = JobRecord(
                job_id=f"job-{uuid.uuid4().hex}", request_id=event.request_id,
                source_response_ref=source_ref,
                response_id=event.response_id, payload_hash=event.payload_hash,
                operation=event.operation, member_payload=payload, created_at=timestamp(now),
                source_system=event.source_system, form_alias=event.form_alias,
                mapping_version=event.mapping_version, max_attempts=DEFAULT_WRITE_BUDGET,
            )
            self._admit(job, policy)
            self._jobs[job.job_id] = job
            self._job_by_response[event.response_id] = job.job_id
            self._attempts[job.job_id] = []
            self._receipts[event.response_id] = HandlingReceipt(
                event.response_id, source_ref, event.form_alias, cursor.form_id, event.mapping_version,
                event.create_time, event.payload_hash, HandlingOutcome.ACCEPTED, job.job_id, None, 1, timestamp(now),
            )
            self._audit("source_ingested", job, error_code=job.outcome_reason)
            if initial_window_max is not None:
                self._source_cursors[cursor_key] = replace(
                    cursor,
                    state_version=cursor.state_version + 1,
                    initial_window_admission_count=cursor.initial_window_admission_count + 1,
                )
            return IngestOutcome(self._copy(job), replayed=False)

    def _dispatch_enabled(self) -> bool:
        return bool(self._control.get("production_activation_enabled", False)) and not self._control.get("kill_switch_enabled", True)

    def _assert_kill_switch_clear(self) -> None:
        """Mailer claims only; must be called while _lock is held."""
        if self._control.get("kill_switch_enabled", True):
            raise RepositoryError("kill_switch_enabled")

    def _active_lease(self, job_id: str) -> LeaseRecord | None:
        lease = self._leases.get(job_id)
        return lease if lease is not None and lease.active else None

    def _finish_attempt(self, job: JobRecord, current: datetime, **values: Any) -> None:
        attempts = self._attempts.setdefault(job.job_id, [])
        for index, attempt in enumerate(attempts):
            if attempt.attempt_no == job.attempt:
                attempts[index] = replace(attempt, finished_at=timestamp(current), **values)
                return
        raise RepositoryError("attempt_record_missing")

    def _apply(self, job: JobRecord, disposition: Disposition) -> None:
        self._move(job, disposition.state)
        job.write_attempts = disposition.write_attempts
        job.busy_attempts = disposition.busy_attempts
        job.next_attempt_at = None if disposition.next_attempt_at is None else timestamp(disposition.next_attempt_at)
        job.outcome_reason = disposition.reason
        job.lease_owner = None
        job.lease_expires_at = None
        job.lease_token = None
        lease = self._leases.get(job.job_id)
        if lease is not None:
            self._leases[job.job_id] = replace(lease, active=False, state_version=job.state_version)

    def _reap_expired(self, current: datetime) -> int:
        """Expired lease -> uncertain write-class attempt (section 3 step 3)."""

        count = 0
        for job_id, lease in list(self._leases.items()):
            if not lease.active or parse_timestamp(lease.expires_at) > current:
                continue
            job = self._jobs[job_id]
            if job.state != JobState.LEASED:
                self._leases[job_id] = replace(lease, active=False)
                continue
            disposition = lease_expiry_disposition(job, expired_at=parse_timestamp(lease.expires_at))
            self._apply(job, disposition)
            self._finish_attempt(job, current, outcome="LEASE_EXPIRED", outcome_code=disposition.outcome_code)
            count += 1
            self._audit("lease_expired", job, error_code=disposition.reason)
        return count

    def claim_job(
        self, worker_session: str, *, gate: ClaimGate, lease_seconds: int = LEASE_SECONDS,
        now: datetime | None = None,
    ) -> ClaimOutcome:
        """Section 3 claim under the control lock: dispatch_enabled and
        readiness, reap, single active lease globally, oldest eligible job."""

        validate_worker_session(worker_session)
        if not isinstance(gate, ClaimGate):
            raise RepositoryError("claim_gate_required")
        if lease_seconds != LEASE_SECONDS:
            raise RepositoryError("lease_seconds_must_be_600")
        current = utc_now(now)
        with self._lock:
            if not self._dispatch_enabled() or not gate.open:
                return ClaimOutcome(False, "dispatch_disabled")
            self._reap_expired(current)
            if any(lease.active for lease in self._leases.values()):
                return ClaimOutcome(False, "singleton_busy")
            candidates = sorted(
                (
                    job for job in self._jobs.values()
                    if job.state in {JobState.QUEUED, JobState.RETRY_WAIT}
                    and (job.next_attempt_at is None or parse_timestamp(job.next_attempt_at) <= current)
                ),
                key=lambda item: (item.created_at, item.job_id),
            )
            job = next((item for item in candidates if claimable(item, gate.policy)), None)
            if job is None:
                return ClaimOutcome(False, "no_eligible_job")
            self._move(job, JobState.LEASED)
            job.attempt += 1
            token = new_lease_token()
            expires = timestamp(current + timedelta(seconds=lease_seconds))
            job.attempt_started_at = timestamp(current)
            if job.first_claimed_at is None:
                job.first_claimed_at = timestamp(current)
            job.lease_owner = worker_session
            job.lease_expires_at = expires
            job.lease_token = token
            job.next_attempt_at = None
            lease = LeaseRecord(job.job_id, worker_session, token, job.attempt, job.state_version, expires)
            self._leases[job.job_id] = lease
            self._attempts.setdefault(job.job_id, []).append(
                AttemptRecord(job.job_id, job.attempt, worker_session, token, timestamp(current))
            )
            self._audit("job_claimed", job)
            return ClaimOutcome(True, None, self._copy(job), lease)

    def submit_result(
        self, job_id: str, result: Mapping[str, Any], *, worker_session: str, now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Accept one schema-valid result v2 (section 3 CAS, section 4.1).

        Returns ``(response, replayed)``. An identical body replays the stored
        response; a different body for a recorded lease is ``ResultConflict``;
        a body for an expired, replaced or foreign lease is ``ResultStale``.
        The kill switch never blocks a result."""

        current = utc_now(now)
        if result.get("job_id") != job_id:
            raise ResultStale("result_job_mismatch")
        digest = result_hash(result)
        with self._lock:
            job = self._job(job_id)
            recorded = next(
                (item for item in self._attempts.get(job_id, []) if item.attempt_no == result["attempt_no"]),
                None,
            )
            if recorded is not None and recorded.lease_token == result["lease_id"] and recorded.result_hash is not None:
                if recorded.result_hash == digest:
                    return response_from_outcome_code(job_id, recorded.attempt_no, recorded.outcome_code or "", replayed=True), True
                self._result_conflicts.append({"job_id": job_id, "code": "result_payload_conflict"})
                self._audit("result_conflict", job, error_code="result_payload_conflict")
                raise ResultConflict("result_payload_conflict")
            if not cas_matches(job, self._active_lease(job_id), result, worker_session, current):
                raise ResultStale("result_lease_stale")
            guid = result["member_guid"]
            guid_elsewhere = guid is not None and any(
                item.member_guid == guid and item.outcome == "CREATED_VERIFIED" and item.job_id != job_id
                for item in self._member_outcomes.values()
            )
            prior_uncertain = any(
                item.outcome in UNCERTAIN_OUTCOMES
                for item in self._attempts.get(job_id, []) if item.attempt_no < job.attempt
            )
            disposition = decide(job, result, now=current, guid_credited_elsewhere=guid_elsewhere, prior_uncertain=prior_uncertain)
            attempt_no = job.attempt
            self._apply(job, disposition)
            self._finish_attempt(
                job, current, outcome=result["outcome"], rule=result["rule"],
                save_invoked=result["save_invoked"], result_hash=digest, outcome_code=disposition.outcome_code,
            )
            if disposition.outcome_row is not None:
                if job_id in self._member_outcomes:
                    raise RepositoryError("member_outcome_already_recorded")
                if disposition.outcome_row == "CREATED_VERIFIED" and guid_elsewhere:
                    raise RepositoryError("member_guid_already_credited")
                self._member_outcomes[job_id] = MemberOutcomeRecord(
                    job_id, disposition.outcome_row, result["rule"], result["member_no"], guid,
                    attempt_no, digest, timestamp(current),
                )
            if disposition.welcome:
                # Same critical section as the CREATED_VERIFIED outcome.
                self._create_welcome_outbox(job, current)
            self._audit("result_recorded", job, error_code=disposition.reason)
            for flag in (result["dq_flags"] if disposition.reason != "result_contract_violation" else ()):
                # Closed data-quality codes for the operator report (2.6, 2.7 step 8);
                # a contract-violating result reports nothing.
                self._audit("member_dq_flag", job, error_code=flag)
            return response_from_outcome_code(job_id, attempt_no, disposition.outcome_code, replayed=False), False

    def resolve_job(
        self, job_id: str, *, action: str, resolution_code: str, member_no: str | None = None,
        member_guid: str | None = None, resolved_by: str, now: datetime | None = None,
    ) -> JobResolution:
        """Operator decision on a MANUAL_REVIEW job: CLOSE -> RESOLVED, or
        REQUEUE -> QUEUED with write budget +3 (cap 12)."""

        validate_resolution(action, resolution_code, member_no, member_guid, resolved_by)
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            if job.state != JobState.MANUAL_REVIEW:
                raise ResolutionConflict("resolution_state_invalid")
            if action == "REQUEUE" and (job.member_no_rule != "XB-MN-1" or job.base_member_no is None or job.name_component is None):
                # A legacy (pre-v2) job has no immutable identity; requeueing
                # it would strand it unclaimable in QUEUED.
                raise ResolutionConflict("resolution_legacy_job_not_requeueable")
            prior_version = job.state_version
            if action == "CLOSE":
                self._move(job, JobState.RESOLVED)
            else:
                job.max_attempts = next_write_budget(job)
                self._move(job, JobState.QUEUED)
                job.busy_attempts = 0
                job.next_attempt_at = None
            job.outcome_reason = resolution_code if action == "CLOSE" else None
            resolution = JobResolution(
                f"resolution-{uuid.uuid4().hex}", job_id, action, resolution_code, member_no, member_guid,
                prior_version, job.state.value, job.max_attempts, timestamp(current),
            )
            self._resolutions.append(resolution)
            self._audit("job_resolved", job, error_code=resolution_code)
            return self._copy(resolution)

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._copy(self._job(job_id))

    def all_jobs(self) -> tuple[JobRecord, ...]:
        with self._lock:
            return tuple(self._copy(job) for job in self._jobs.values())

    def attempts(self, job_id: str) -> tuple[AttemptRecord, ...]:
        with self._lock:
            self._job(job_id)
            return tuple(self._copy(self._attempts.get(job_id, [])))

    def lease(self, job_id: str) -> LeaseRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._leases.get(job_id))

    def member_outcome(self, job_id: str) -> MemberOutcomeRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._member_outcomes.get(job_id))

    def job_resolutions(self, job_id: str) -> tuple[JobResolution, ...]:
        with self._lock:
            self._job(job_id)
            return tuple(self._copy(item) for item in self._resolutions if item.job_id == job_id)

    def operator_status(self) -> dict[str, Any]:
        """``xb.member.gateway.operator_status.v2``: counts and codes only."""

        current = utc_now()
        with self._lock:
            counts = {state: 0 for state in V2_STATUS_STATES}
            legacy = 0
            reasons: dict[str, int] = {}
            for job in self._jobs.values():
                if job.state in LEGACY_STATES:
                    legacy += 1
                else:
                    counts[job.state.value] += 1
                if job.state == JobState.MANUAL_REVIEW:
                    key = job.outcome_reason or "unspecified"
                    reasons[key] = reasons.get(key, 0) + 1
            return {
                "schema_version": "xb.member.gateway.operator_status.v2",
                "activation_enabled": self._control["production_activation_enabled"],
                "kill_switch_enabled": self._control["kill_switch_enabled"],
                "dispatch_enabled": self._dispatch_enabled(),
                "migrations_ready": True,
                "source_cursor_ready": bool(self._source_cursors),
                "active_lease_count": len([lease for lease in self._leases.values() if lease.active and parse_timestamp(lease.expires_at) > current]),
                "job_state_counts": counts,
                "legacy_state_count": legacy,
                "manual_review_reason_counts": dict(sorted(reasons.items())),
                "dq_flag_counts": self._dq_flag_counts(),
            }

    def _dq_flag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._audit_events:
            if entry.get("event_type") == "member_dq_flag":
                counts[entry["error_code"]] = counts.get(entry["error_code"], 0) + 1
        return dict(sorted(counts.items()))

    def operator_reconciliation(self, job_id: str) -> dict[str, Any]:
        """Legacy v1 view. v2 jobs carry no v1 result or reconciliation case."""

        with self._lock:
            job = self._job(job_id)
            return {
                "schema_version": "xb.member.gateway.operator_reconciliation.v1",
                "job": {"job_id": job.job_id, "operation": job.operation, "state": job.state.value, "state_version": job.state_version, "attempt": job.attempt, "created_at": job.created_at},
                "result": None,
                "reconciliation": None,
            }

    def snapshot_state(self) -> dict[str, Any]:
        """Return a private restart fixture without exposing it through the API."""
        with self._lock:
            return self._copy({
                key: value for key, value in self.__dict__.items()
                if key not in {"_lock", "_reference_key"}
            })

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any], *, reference_key: bytes = b"synthetic-member-gateway-key") -> "InMemoryRepository":
        repository = cls(reference_key=reference_key)
        with repository._lock:
            for key, value in snapshot.items():
                if key == "_lock":
                    continue
                setattr(repository, key, repository._copy(value))
        return repository

    @property
    def audit_events(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(self._copy(self._audit_events))

    @property
    def result_conflicts(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            return tuple(self._result_conflicts)

    def reject_source_response(self, rejection: SourceRejection, *, now: datetime | None = None) -> RejectionOutcome:
        """Record a PII-free customer rejection. It never consumes the
        first-member allowance and creates no job."""

        with self._lock:
            cursor_key = (rejection.source_system, rejection.form_alias, rejection.mapping_version)
            cursor = self._source_cursor_for_admission(cursor_key)
            if cutover_key(rejection.create_time) < cutover_key(cursor.production_cutover_exact):
                raise SourceConflict("source_event_before_cutover")
            prior_response = self._request_responses.get(rejection.request_id)
            if prior_response is not None and prior_response != rejection.response_id:
                raise SourceConflict("request_identity_payload_conflict")
            receipt = self._receipts.get(rejection.response_id)
            if receipt is not None:
                stored = self._rejections.get(rejection.response_id)
                if (
                    stored is None
                    or stored["error_code"] != rejection.error_code
                    or not receipt_matches(receipt, rejection.form_alias, cursor.form_id, rejection.mapping_version, rejection.create_time, rejection.payload_hash, HandlingOutcome.REJECTED)
                ):
                    self._audit("source_conflict", error_code="source_identity_conflict")
                    raise SourceConflict("source_identity_payload_conflict")
                return RejectionOutcome(stored["rejection_id"], receipt.source_response_ref, stored["error_code"], True)
            if rejection.response_id in self._responses:
                raise SourceConflict("source_identity_payload_conflict")
            source_ref = hmac_reference(rejection.response_id, self._reference_key)
            rejection_id = f"rejection-{uuid.uuid4().hex}"
            self._rejections[rejection.response_id] = {
                "rejection_id": rejection_id, "source_response_ref": source_ref,
                "form_alias": rejection.form_alias, "form_id": cursor.form_id,
                "mapping_version": rejection.mapping_version, "create_time_exact": rejection.create_time,
                "payload_hash": rejection.payload_hash, "error_code": rejection.error_code,
                "request_id": rejection.request_id, "recorded_at": timestamp(now),
            }
            self._request_responses[rejection.request_id] = rejection.response_id
            self._receipts[rejection.response_id] = HandlingReceipt(
                rejection.response_id, source_ref, rejection.form_alias, cursor.form_id, rejection.mapping_version,
                rejection.create_time, rejection.payload_hash, HandlingOutcome.REJECTED, None, rejection_id, 1, timestamp(now),
            )
            self._audit("source_rejected", error_code=rejection.error_code)
            return RejectionOutcome(rejection_id, source_ref, rejection.error_code, False)

    def _source_cursor_for_admission(self, key: tuple[str, str, str]) -> SourceCursor:
        cursor = self._source_cursors.get(key)
        if cursor is None:
            raise SourceConflict("source_cursor_not_initialized")
        if cursor.production_cutover_exact is None or cursor.form_id is None:
            raise SourceConflict("source_production_cutover_uninitialized")
        return cursor

    def _active_epoch(self, key: tuple[str, str, str]) -> ScanEpoch | None:
        active = [epoch for epoch in self._epochs.values() if (epoch.source_system, epoch.form_alias, epoch.mapping_version) == key and epoch.status == ScanEpochStatus.ACTIVE]
        if len(active) > 1:
            raise RepositoryError("source_scan_epoch_active_duplicate")
        return active[0] if active else None

    def _open_page(self, epoch_id: str) -> ScanPage | None:
        pages = [page for page in self._pages.values() if page.epoch_id == epoch_id and page.page_state == ScanPageState.OPEN]
        return pages[0] if pages else None

    def _abandon_epoch(self, epoch: ScanEpoch, reason: str, current: datetime) -> None:
        open_page = self._open_page(epoch.epoch_id)
        if open_page is not None:
            self._pages[open_page.page_id] = replace(open_page, page_state=ScanPageState.ABANDONED)
        self._epochs[epoch.epoch_id] = replace(
            epoch, status=ScanEpochStatus.ABANDONED, abandon_reason=reason,
            epoch_state_version=epoch.epoch_state_version + 1,
        )

    def get_source_cursor(self, form_alias: str, mapping_version: str) -> tuple[SourceCursor, ScanEpoch | None, ScanPage | None]:
        with self._lock:
            key = ("google_forms", form_alias, mapping_version)
            cursor = self._source_cursors.get(key)
            if cursor is None:
                raise SourceConflict("source_cursor_not_initialized")
            epoch = self._active_epoch(key)
            page = self._open_page(epoch.epoch_id) if epoch else None
            return self._copy(cursor), self._copy(epoch), self._copy(page)

    def begin_source_epoch(
        self, form_alias: str, mapping_version: str, *, admission_mode: SourceAdmissionMode,
        form_id: str | None, now: datetime | None = None,
    ) -> tuple[ScanEpoch, bool]:
        """Resume the one ACTIVE epoch for this binding or begin a new one from
        the same fixed cutover. Returns ``(epoch, resumed)``."""

        current = utc_now(now)
        with self._lock:
            key = ("google_forms", form_alias, mapping_version)
            cursor = self._source_cursor_for_admission(key)
            if form_id is None or cursor.form_id != form_id:
                raise SourceConflict("source_form_binding_mismatch")
            binding = epoch_binding(cursor, SourceAdmissionMode(admission_mode))
            active = self._active_epoch(key)
            if active is not None:
                if active.binding == binding:
                    return self._copy(active), True
                if active.binding[:4] + active.binding[5:] != binding[:4] + binding[5:]:
                    raise SourceConflict("source_epoch_binding_mismatch")
                self._abandon_epoch(active, "admission_mode_changed", current)
            predecessor = self._latest_epoch_id(key)
            epoch = new_epoch(cursor, SourceAdmissionMode(admission_mode), predecessor, None, current)
            self._epochs[epoch.epoch_id] = epoch
            return self._copy(epoch), False

    def _latest_epoch_id(self, key: tuple[str, str, str]) -> str | None:
        epochs = [epoch for epoch in self._epochs.values() if (epoch.source_system, epoch.form_alias, epoch.mapping_version) == key]
        return epochs[-1].epoch_id if epochs else None

    def restart_source_epoch(
        self, epoch_id: str, *, expected_epoch_state_version: int, restart_reason: str,
        now: datetime | None = None,
    ) -> ScanEpoch:
        if restart_reason not in RESTART_REASONS:
            raise SourceRestartReasonInvalid("source_restart_reason_invalid")
        current = utc_now(now)
        with self._lock:
            epoch = self._epochs.get(epoch_id)
            if epoch is None:
                raise SourceConflict("source_epoch_not_found")
            if epoch.status != ScanEpochStatus.ACTIVE:
                raise SourceConflict("source_epoch_not_active")
            if epoch.epoch_state_version != expected_epoch_state_version:
                raise SourceConflict("source_epoch_state_version_mismatch")
            key = (epoch.source_system, epoch.form_alias, epoch.mapping_version)
            cursor = self._source_cursor_for_admission(key)
            self._abandon_epoch(epoch, restart_reason, current)
            successor = new_epoch(cursor, epoch.admission_mode, epoch.epoch_id, restart_reason, current)
            self._epochs[successor.epoch_id] = successor
            return self._copy(successor)

    def open_source_page(
        self, epoch_id: str, *, expected_epoch_state_version: int, request_page_token: str | None,
        next_page_token: str | None, terminal: bool, items: list[Mapping[str, Any]],
        now: datetime | None = None,
    ) -> tuple[ScanPage, ScanEpoch, bool]:
        request_page_token = validate_page_token(request_page_token)
        next_page_token = validate_page_token(next_page_token)
        with self._lock:
            epoch = self._epochs.get(epoch_id)
            if epoch is None:
                raise SourceConflict("source_epoch_not_found")
            if epoch.status != ScanEpochStatus.ACTIVE:
                raise SourceConflict("source_epoch_not_active")
            normalized = normalize_page_items(items, epoch)
            existing = self._open_page(epoch_id)
            if existing is not None:
                if (
                    existing.request_page_token == request_page_token
                    and existing.next_page_token == next_page_token
                    and existing.terminal == terminal
                    and existing.items == normalized
                    and expected_epoch_state_version == existing.opened_state_version - 1
                ):
                    return self._copy(existing), self._copy(epoch), True
                raise SourceConflict("source_page_already_open")
            if epoch.epoch_state_version != expected_epoch_state_version:
                raise SourceConflict("source_epoch_state_version_mismatch")
            if request_page_token != epoch.current_page_token:
                raise SourceConflict("source_page_token_unexpected")
            if terminal != (next_page_token is None):
                raise SourceConflict("source_page_terminal_invalid")
            seen = {
                token for page in self._pages.values() if page.epoch_id == epoch_id
                for token in (page.request_page_token, page.next_page_token) if token is not None
            }
            if next_page_token is not None and (next_page_token == request_page_token or next_page_token in seen):
                raise SourceConflict("source_page_token_repeated_in_epoch")
            page = ScanPage(
                f"page-{uuid.uuid4().hex}", epoch_id, epoch.page_ordinal, ScanPageState.OPEN,
                request_page_token, next_page_token, terminal, normalized, epoch.epoch_state_version + 1,
            )
            self._pages[page.page_id] = page
            epoch = replace(epoch, epoch_state_version=epoch.epoch_state_version + 1)
            self._epochs[epoch_id] = epoch
            return self._copy(page), self._copy(epoch), False

    def commit_source_page(
        self, page_id: str, *, expected_epoch_state_version: int, now: datetime | None = None,
    ) -> tuple[ScanPage, ScanEpoch, bool]:
        current = utc_now(now)
        with self._lock:
            page = self._pages.get(page_id)
            if page is None:
                raise SourceConflict("source_page_not_found")
            epoch = self._epochs[page.epoch_id]
            if page.page_state == ScanPageState.COMMITTED:
                if expected_epoch_state_version == page.opened_state_version:
                    return self._copy(page), self._copy(epoch), True
                raise SourceConflict("source_epoch_state_version_mismatch")
            if page.page_state == ScanPageState.ABANDONED or epoch.status != ScanEpochStatus.ACTIVE:
                raise SourceConflict("source_page_abandoned")
            if epoch.epoch_state_version != expected_epoch_state_version:
                raise SourceConflict("source_epoch_state_version_mismatch")
            for response_id, create_time, fingerprint in page.items:
                receipt = self._receipts.get(response_id)
                if receipt is None:
                    raise SourceConflict("source_page_item_unreceipted")
                if (
                    receipt.create_time_exact != create_time
                    or receipt.payload_fingerprint != fingerprint
                    or receipt.form_alias != epoch.form_alias
                    or receipt.form_id != epoch.form_id
                    or receipt.mapping_version != epoch.mapping_version
                ):
                    raise SourceConflict("source_page_item_receipt_mismatch")
            for response_id, create_time, fingerprint in page.items:
                self._page_items.append({
                    "page_id": page_id, "response_id": response_id,
                    "create_time_exact": create_time, "payload_fingerprint": fingerprint,
                })
            page = replace(page, page_state=ScanPageState.COMMITTED, committed_state_version=expected_epoch_state_version + 1)
            self._pages[page_id] = page
            epoch = replace(
                epoch, epoch_state_version=expected_epoch_state_version + 1,
                current_page_token=page.next_page_token, page_ordinal=epoch.page_ordinal + 1,
                status=ScanEpochStatus.COMPLETED if page.terminal else ScanEpochStatus.ACTIVE,
                completed_at=timestamp(current) if page.terminal else None,
            )
            self._epochs[epoch.epoch_id] = epoch
            return self._copy(page), self._copy(epoch), False

    def handling_receipt(self, response_id: str) -> HandlingReceipt | None:
        with self._lock:
            return self._copy(self._receipts.get(response_id))

    def page_items(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(self._copy(self._page_items))

    def set_control(self, name: str, enabled: bool) -> None:
        if name not in self._control or not isinstance(enabled, bool):
            raise RepositoryError("control_flag_invalid")
        with self._lock:
            self._control[name] = enabled

    def get_control(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._control)

    def _create_welcome_outbox(self, job: JobRecord, current: datetime) -> WelcomeEmailOutbox:
        """Called only inside submit_result for a CREATED_VERIFIED outcome."""

        existing = self._outbox_by_job.get(job.job_id)
        if existing is not None:
            return self._outbox[existing]
        message = build_welcome_message(job.member_payload["email"])
        outbox = WelcomeEmailOutbox(
            outbox_id=f"welcome-{uuid.uuid4().hex}", job_id=job.job_id, response_id=job.response_id,
            source_response_ref=job.source_response_ref, template_id=WELCOME_TEMPLATE_ID,
            recipient=message["to"], message_hash=welcome_message_hash(message),
            state=WelcomeEmailState.PENDING, state_version=0, attempt=0, max_attempts=WELCOME_MAX_ATTEMPTS,
            lease_id=None, lease_expires_at=None, next_attempt_at=timestamp(current + WELCOME_INITIAL_DELAY),
            send_intent_at=None, last_error_code=None, created_at=timestamp(current), updated_at=timestamp(current),
        )
        if any(item.response_id == job.response_id and item.template_id == WELCOME_TEMPLATE_ID for item in self._outbox.values()):
            raise ResultConflict("welcome_outbox_identity_conflict")
        self._outbox[outbox.outbox_id] = outbox
        self._outbox_by_job[job.job_id] = outbox.outbox_id
        self._welcome_event(outbox, "created", None)
        return outbox

    def _verify_welcome_outbox(self, job: JobRecord) -> None:
        """A duplicate CREATED_VERIFIED ack verifies, never backfills."""

        outbox_id = self._outbox_by_job.get(job.job_id)
        if outbox_id is None:
            raise ResultConflict("welcome_outbox_identity_missing")
        verify_outbox_identity(self._outbox[outbox_id], job)

    def _welcome_event(self, outbox: WelcomeEmailOutbox, event_type: str, from_state: WelcomeEmailState | None) -> None:
        self._welcome_events.append({
            "outbox_id": outbox.outbox_id, "event_type": event_type,
            "from_state": None if from_state is None else from_state.value, "to_state": outbox.state.value,
            "state_version": outbox.state_version, "attempt": outbox.attempt,
            "error_code": outbox.last_error_code, "recorded_at": outbox.updated_at,
        })

    def _welcome_move(self, outbox: WelcomeEmailOutbox, target: WelcomeEmailState, current: datetime, event_type: str, **values: Any) -> WelcomeEmailOutbox:
        assert_transition(outbox.state, target)
        updated = replace(outbox, state=target, state_version=outbox.state_version + 1, updated_at=timestamp(current), **values)
        self._outbox[outbox.outbox_id] = updated
        self._welcome_event(updated, event_type, outbox.state)
        return updated

    def _sweep_welcome_leases(self, current: datetime) -> None:
        for outbox in list(self._outbox.values()):
            if outbox.lease_expires_at is None or parse_timestamp(outbox.lease_expires_at) > current:
                continue
            if outbox.state == WelcomeEmailState.SEND_INTENT_RECORDED:
                # A crash or lost acknowledgement after send intent is never
                # resent: the SMTP server may already have accepted it.
                self._welcome_move(outbox, WelcomeEmailState.DELIVERY_OUTCOME_UNCERTAIN, current, "delivery_outcome_uncertain", last_error_code="send_intent_lease_expired")
            elif outbox.state == WelcomeEmailState.LEASED:
                self._welcome_safe_failure(outbox, current, "lease_expired_before_send_intent")

    def _welcome_safe_failure(self, outbox: WelcomeEmailOutbox, current: datetime, error_code: str) -> WelcomeEmailOutbox:
        target = safe_failure_target(outbox.attempt, outbox.max_attempts)
        if target == WelcomeEmailState.DEAD_LETTER:
            return self._welcome_move(outbox, target, current, "dead_lettered", last_error_code=error_code, next_attempt_at=None)
        return self._welcome_move(
            outbox, target, current, "retry_scheduled", last_error_code=error_code,
            next_attempt_at=timestamp(next_attempt_after(outbox.attempt, current)),
        )

    def claim_welcome_email(self, *, now: datetime | None = None) -> tuple[WelcomeEmailOutbox, dict[str, Any]] | None:
        current = utc_now(now)
        with self._lock:
            self._assert_kill_switch_clear()
            self._sweep_welcome_leases(current)
            if any(item.state in {WelcomeEmailState.LEASED, WelcomeEmailState.SEND_INTENT_RECORDED} for item in self._outbox.values()):
                return None
            candidates = sorted(
                (
                    item for item in self._outbox.values()
                    if item.state in {WelcomeEmailState.PENDING, WelcomeEmailState.RETRY_WAIT}
                    and item.next_attempt_at is not None and parse_timestamp(item.next_attempt_at) <= current
                ),
                key=lambda item: (item.next_attempt_at, item.created_at, item.outbox_id),
            )
            if not candidates:
                return None
            outbox = candidates[0]
            message = verify_message(outbox)
            outbox = self._welcome_move(
                outbox, WelcomeEmailState.LEASED, current, "claimed", attempt=outbox.attempt + 1,
                lease_id=f"lease-{uuid.uuid4().hex}", lease_expires_at=timestamp(current + timedelta(seconds=WELCOME_LEASE_SECONDS)),
                next_attempt_at=None, last_error_code=None,
            )
            return self._copy(outbox), message

    def record_welcome_send_intent(self, outbox_id: str, *, lease_id: str, expected_state_version: int, now: datetime | None = None) -> tuple[WelcomeEmailOutbox, bool]:
        current = utc_now(now)
        with self._lock:
            outbox = self._outbox.get(outbox_id)
            if outbox is None:
                raise WelcomeEmailError("welcome_email_not_found")
            if outbox.state == WelcomeEmailState.SEND_INTENT_RECORDED and outbox.lease_id == lease_id and outbox.state_version == expected_state_version + 1:
                return self._copy(outbox), True
            check_welcome_lease(outbox, lease_id, expected_state_version, WelcomeEmailState.LEASED, current, require_unexpired=True)
            outbox = self._welcome_move(outbox, WelcomeEmailState.SEND_INTENT_RECORDED, current, "send_intent_recorded", send_intent_at=timestamp(current))
            return self._copy(outbox), False

    def record_welcome_result(
        self, outbox_id: str, *, lease_id: str, expected_state_version: int, outcome: str,
        error_code: str | None, now: datetime | None = None,
    ) -> tuple[WelcomeEmailOutbox, bool]:
        current = utc_now(now)
        with self._lock:
            outbox = self._outbox.get(outbox_id)
            if outbox is None:
                raise WelcomeEmailError("welcome_email_not_found")
            target = welcome_result_target(outbox, outcome)
            if outbox.state == target and outbox.lease_id == lease_id and outbox.state_version == expected_state_version + 1:
                return self._copy(outbox), True
            if outcome == "failed_before_send_intent":
                check_welcome_lease(outbox, lease_id, expected_state_version, WelcomeEmailState.LEASED, current, require_unexpired=False)
                outbox = self._welcome_safe_failure(outbox, current, error_code or "failed_before_send_intent")
            else:
                # A positive or uncertain post-intent result is accepted even
                # after lease expiry while the row still awaits its outcome.
                check_welcome_lease(outbox, lease_id, expected_state_version, WelcomeEmailState.SEND_INTENT_RECORDED, current, require_unexpired=False)
                event = "sent" if target == WelcomeEmailState.SENT else "delivery_outcome_uncertain"
                outbox = self._welcome_move(outbox, target, current, event, last_error_code=error_code)
            return self._copy(outbox), False

    def welcome_outbox_for_job(self, job_id: str) -> WelcomeEmailOutbox | None:
        with self._lock:
            outbox_id = self._outbox_by_job.get(job_id)
            return None if outbox_id is None else self._copy(self._outbox[outbox_id])

    def welcome_outboxes(self) -> tuple[WelcomeEmailOutbox, ...]:
        with self._lock:
            return tuple(self._copy(item) for item in self._outbox.values())

    @property
    def welcome_events(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(self._copy(self._welcome_events))


class PostgresRepository:
    """PostgreSQL implementation of the same bounded repository contract."""

    _JOB_SELECT = """
        SELECT j.job_id,COALESCE(obs.request_id,''),sr.source_response_ref,j.response_id,
               j.payload_hash,j.operation,j.canonical_payload,j.created_at,j.state,
               j.state_version,j.attempt_count,j.max_attempts,j.next_attempt_at,
               j.lease_owner,j.lease_expires_at,'google_forms',COALESCE(obs.form_alias,''),
               COALESCE(obs.mapping_version,''),j.attempt_started_at,j.member_no_rule,
               j.base_member_no,j.name_component,j.first_claimed_at,j.write_attempts,
               j.busy_attempts,j.outcome_reason,lease.lease_token
        FROM xb_member_gateway.jobs j
        JOIN xb_member_gateway.source_responses sr ON sr.response_id=j.response_id
        LEFT JOIN LATERAL (
            SELECT request_id,form_alias,mapping_version FROM xb_member_gateway.source_observations
            WHERE response_id=j.response_id ORDER BY observed_at DESC,observation_id DESC LIMIT 1
        ) obs ON TRUE
        LEFT JOIN LATERAL (
            SELECT lease_token FROM xb_member_gateway.leases
            WHERE job_id=j.job_id AND active=TRUE LIMIT 1
        ) lease ON TRUE
        WHERE j.job_id=%s
    """

    def __init__(self, dsn: str | None = None, *, connection_factory: Callable[[], Any] | None = None, reference_key: bytes | None = None):
        self._dsn = dsn or os.environ.get("XB_MEMBER_GATEWAY_DATABASE_URL")
        self._connection_factory = connection_factory
        self._reference_key = reference_key
        if not self._dsn and connection_factory is None:
            raise RepositoryError("postgres_dsn_required")

    def _connect(self) -> Any:
        if self._connection_factory:
            return self._connection_factory()
        try:
            import psycopg
        except ImportError as exc:
            raise RepositoryError("psycopg_optional_dependency_missing") from exc
        return psycopg.connect(self._dsn)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _dt(value: Any) -> str | None:
        return None if value is None else timestamp(value) if isinstance(value, datetime) else str(value)

    @classmethod
    def _from_row(cls, row: tuple[Any, ...]) -> JobRecord:
        return JobRecord(
            row[0], row[1], row[2], row[3], row[4], row[5], dict(row[6]), cls._dt(row[7]) or "",
            state=JobState(row[8]), state_version=int(row[9]), attempt=int(row[10]), max_attempts=int(row[11]),
            next_attempt_at=cls._dt(row[12]), lease_owner=row[13], lease_expires_at=cls._dt(row[14]),
            source_system=row[15], form_alias=row[16], mapping_version=row[17],
            attempt_started_at=cls._dt(row[18]), member_no_rule=row[19], base_member_no=row[20],
            name_component=row[21], first_claimed_at=cls._dt(row[22]), write_attempts=int(row[23]),
            busy_attempts=int(row[24]), outcome_reason=row[25], lease_token=row[26],
        )

    def _select_job(self, cursor: Any, job_id: str, *, for_update: bool = False) -> JobRecord:
        cursor.execute(self._JOB_SELECT + (" FOR UPDATE OF j" if for_update else ""), (job_id,))
        row = cursor.fetchone()
        if row is None:
            raise JobNotFound("job_not_found")
        return self._from_row(row)

    def _lock_controls(self, cursor: Any) -> dict[str, bool]:
        """Lock both control rows (the section 3 claim lock) in a fixed order."""

        cursor.execute(
            "SELECT flag_name,enabled FROM xb_member_gateway.control_flags "
            "WHERE flag_name IN ('kill_switch_enabled','production_activation_enabled') ORDER BY flag_name FOR UPDATE"
        )
        controls = {str(row[0]): bool(row[1]) for row in cursor.fetchall()}
        if set(controls) != {"kill_switch_enabled", "production_activation_enabled"}:
            raise RepositoryError("kill_switch_state_unavailable")
        return controls

    def _lock_kill_switch(self, cursor: Any) -> None:
        """Mailer claims stay blocked while the kill switch is engaged."""

        cursor.execute(
            "SELECT enabled FROM xb_member_gateway.control_flags "
            "WHERE flag_name='kill_switch_enabled' FOR UPDATE"
        )
        row = cursor.fetchone()
        if row is None:
            raise RepositoryError("kill_switch_state_unavailable")
        if bool(row[0]):
            raise RepositoryError("kill_switch_enabled")

    def _select_lease(self, cursor: Any, job_id: str) -> LeaseRecord | None:
        cursor.execute(
            "SELECT l.worker_id,l.lease_token,j.attempt_count,l.state_version,l.expires_at,l.active "
            "FROM xb_member_gateway.leases l JOIN xb_member_gateway.jobs j ON j.job_id=l.job_id "
            "WHERE l.job_id=%s AND l.active=TRUE FOR UPDATE OF l",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None or row[1] is None:
            return None
        return LeaseRecord(job_id, str(row[0]), str(row[1]), int(row[2]), int(row[3]), self._dt(row[4]) or "", bool(row[5]))

    def _audit_cursor(self, cursor: Any, event_type: str, job: JobRecord, reason: str | None = None) -> None:
        """Metadata-only audit row: a closed reason code, never private data."""

        cursor.execute(
            "INSERT INTO xb_member_gateway.audit_events(event_type,job_id,operation,state,metadata) VALUES(%s,%s,%s,%s,%s::jsonb)",
            (event_type, job.job_id, job.operation, job.state.value, canonical_json({} if reason is None else {"reason": reason})),
        )

    def _apply_cursor(self, cursor: Any, job: JobRecord, disposition: Disposition, current: datetime) -> None:
        next_state(job.state, disposition.state)
        cursor.execute(
            "UPDATE xb_member_gateway.jobs SET state=%s,state_version=state_version+1,write_attempts=%s,busy_attempts=%s,"
            "next_attempt_at=%s,outcome_reason=%s,lease_owner=NULL,lease_expires_at=NULL,updated_at=%s "
            "WHERE job_id=%s AND state='LEASED' AND state_version=%s",
            (disposition.state.value, disposition.write_attempts, disposition.busy_attempts, disposition.next_attempt_at,
             disposition.reason, current, job.job_id, job.state_version),
        )
        if cursor.rowcount != 1:
            raise ResultStale("result_lease_stale")
        job.state = disposition.state
        job.state_version += 1
        job.write_attempts, job.busy_attempts = disposition.write_attempts, disposition.busy_attempts
        job.outcome_reason = disposition.reason
        cursor.execute(
            "UPDATE xb_member_gateway.leases SET active=FALSE,state_version=%s WHERE job_id=%s AND active=TRUE",
            (job.state_version, job.job_id),
        )

    def _reap_expired_cursor(self, cursor: Any, current: datetime) -> int:
        # Lock order is job then lease, the same order submit_result uses, so
        # a result racing the reap of its own lease cannot deadlock.
        cursor.execute(
            "SELECT l.job_id FROM xb_member_gateway.leases l "
            "WHERE l.active=TRUE AND l.expires_at<=%s ORDER BY l.job_id",
            (current,),
        )
        count = 0
        for (job_id,) in cursor.fetchall():
            job = self._select_job(cursor, str(job_id), for_update=True)
            cursor.execute(
                "SELECT expires_at FROM xb_member_gateway.leases WHERE job_id=%s AND active=TRUE AND expires_at<=%s FOR UPDATE",
                (job.job_id, current),
            )
            row = cursor.fetchone()
            if row is None:
                continue  # a result settled the lease after the scan
            expires_at = row[0]
            if job.state != JobState.LEASED:
                cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job.job_id,))
                continue
            disposition = lease_expiry_disposition(job, expired_at=utc_now(expires_at))
            attempt_no = job.attempt
            self._apply_cursor(cursor, job, disposition, current)
            cursor.execute(
                "UPDATE xb_member_gateway.attempts SET finished_at=%s,outcome='LEASE_EXPIRED',outcome_code=%s "
                "WHERE job_id=%s AND attempt_number=%s",
                (current, disposition.outcome_code, job.job_id, attempt_no),
            )
            self._audit_cursor(cursor, "lease_expired", job, disposition.reason)
            count += 1
        return count

    def claim_job(
        self, worker_session: str, *, gate: ClaimGate, lease_seconds: int = LEASE_SECONDS,
        now: datetime | None = None,
    ) -> ClaimOutcome:
        validate_worker_session(worker_session)
        if not isinstance(gate, ClaimGate):
            raise RepositoryError("claim_gate_required")
        if lease_seconds != LEASE_SECONDS:
            raise RepositoryError("lease_seconds_must_be_600")
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                controls = self._lock_controls(cursor)
                if not controls["production_activation_enabled"] or controls["kill_switch_enabled"] or not gate.open:
                    return ClaimOutcome(False, "dispatch_disabled")
                self._reap_expired_cursor(cursor, current)
                cursor.execute("SELECT 1 FROM xb_member_gateway.leases WHERE active=TRUE LIMIT 1")
                if cursor.fetchone() is not None:
                    return ClaimOutcome(False, "singleton_busy")
                cursor.execute(
                    "SELECT job_id FROM xb_member_gateway.jobs WHERE state IN ('QUEUED','RETRY_WAIT') "
                    "AND (next_attempt_at IS NULL OR next_attempt_at<=%s) ORDER BY created_at,job_id FOR UPDATE",
                    (current,),
                )
                job = None
                for (candidate_id,) in cursor.fetchall():
                    candidate = self._select_job(cursor, str(candidate_id), for_update=True)
                    if claimable(candidate, gate.policy):
                        job = candidate
                        break
                if job is None:
                    return ClaimOutcome(False, "no_eligible_job")
                next_state(job.state, JobState.LEASED)
                token = new_lease_token()
                expires = current + timedelta(seconds=lease_seconds)
                cursor.execute(
                    "UPDATE xb_member_gateway.jobs SET state='LEASED',state_version=state_version+1,attempt_count=attempt_count+1,"
                    "lease_owner=%s,lease_expires_at=%s,next_attempt_at=NULL,attempt_started_at=%s,"
                    "first_claimed_at=COALESCE(first_claimed_at,%s),updated_at=%s WHERE job_id=%s AND state_version=%s",
                    (worker_session, expires, current, current, current, job.job_id, job.state_version),
                )
                if cursor.rowcount != 1:
                    raise LeaseConflict("claim_state_version_mismatch")
                cursor.execute(
                    "INSERT INTO xb_member_gateway.attempts(job_id,attempt_number,worker_id,started_at,lease_token) VALUES(%s,%s,%s,%s,%s)",
                    (job.job_id, job.attempt + 1, worker_session, current, token),
                )
                cursor.execute(
                    "INSERT INTO xb_member_gateway.leases(job_id,worker_id,state_version,expires_at,lease_token) VALUES(%s,%s,%s,%s,%s)",
                    (job.job_id, worker_session, job.state_version + 1, expires, token),
                )
                claimed = self._select_job(cursor, job.job_id)
                self._audit_cursor(cursor, "job_claimed", claimed)
                lease = LeaseRecord(claimed.job_id, worker_session, token, claimed.attempt, claimed.state_version, timestamp(expires))
                return ClaimOutcome(True, None, claimed, lease)

    def submit_result(
        self, job_id: str, result: Mapping[str, Any], *, worker_session: str, now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        current = utc_now(now)
        if result.get("job_id") != job_id:
            raise ResultStale("result_job_mismatch")
        digest = result_hash(result)
        conflict = False
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor, job_id, for_update=True)
                cursor.execute(
                    "SELECT lease_token,result_hash,outcome_code FROM xb_member_gateway.attempts "
                    "WHERE job_id=%s AND attempt_number=%s FOR UPDATE",
                    (job_id, result["attempt_no"]),
                )
                recorded = cursor.fetchone()
                if recorded is not None and recorded[0] == result["lease_id"] and recorded[1] is not None:
                    if recorded[1] == digest:
                        return response_from_outcome_code(job_id, result["attempt_no"], str(recorded[2] or ""), replayed=True), True
                    # Recorded, then reported after commit: a conflict is evidence.
                    cursor.execute(
                        "INSERT INTO xb_member_gateway.result_conflicts(job_id,expected_hash,observed_hash) VALUES(%s,%s,%s)",
                        (job_id, recorded[1], digest),
                    )
                    self._audit_cursor(cursor, "result_conflict", job, "result_payload_conflict")
                    conflict = True
                else:
                    if not cas_matches(job, self._select_lease(cursor, job_id), result, worker_session, current):
                        raise ResultStale("result_lease_stale")
                    guid = result["member_guid"]
                    guid_elsewhere = False
                    if guid is not None:
                        cursor.execute(
                            "SELECT 1 FROM xb_member_gateway.member_outcomes WHERE member_guid=%s AND outcome='CREATED_VERIFIED' AND job_id<>%s",
                            (guid, job_id),
                        )
                        guid_elsewhere = cursor.fetchone() is not None
                    cursor.execute(
                        "SELECT 1 FROM xb_member_gateway.attempts WHERE job_id=%s AND attempt_number<%s "
                        "AND outcome IN ('OUTCOME_UNCERTAIN','LEASE_EXPIRED') LIMIT 1",
                        (job_id, job.attempt),
                    )
                    prior_uncertain = cursor.fetchone() is not None
                    disposition = decide(job, result, now=current, guid_credited_elsewhere=guid_elsewhere, prior_uncertain=prior_uncertain)
                    attempt_no = job.attempt
                    self._apply_cursor(cursor, job, disposition, current)
                    cursor.execute(
                        "UPDATE xb_member_gateway.attempts SET finished_at=%s,outcome=%s,rule=%s,save_invoked=%s,result_hash=%s,outcome_code=%s "
                        "WHERE job_id=%s AND attempt_number=%s",
                        (current, result["outcome"], result["rule"], result["save_invoked"], digest, disposition.outcome_code, job_id, attempt_no),
                    )
                    if disposition.outcome_row is not None:
                        cursor.execute(
                            "INSERT INTO xb_member_gateway.member_outcomes(job_id,outcome,rule,member_no,member_guid,attempt_number,result_hash,recorded_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                            (job_id, disposition.outcome_row, result["rule"], result["member_no"], guid, attempt_no, digest, current),
                        )
                    if disposition.welcome:
                        # Same transaction as the CREATED_VERIFIED outcome row.
                        self._insert_welcome_outbox_cursor(cursor, job, current)
                    self._audit_cursor(cursor, "result_recorded", job, disposition.reason)
                    for flag in (result["dq_flags"] if disposition.reason != "result_contract_violation" else ()):
                        # Closed data-quality codes for the operator report (2.6, 2.7 step 8);
                        # a contract-violating result reports nothing.
                        self._audit_cursor(cursor, "member_dq_flag", job, flag)
                    return response_from_outcome_code(job_id, attempt_no, disposition.outcome_code, replayed=False), False
        if conflict:
            raise ResultConflict("result_payload_conflict")
        raise RepositoryError("result_state_unreachable")

    def resolve_job(
        self, job_id: str, *, action: str, resolution_code: str, member_no: str | None = None,
        member_guid: str | None = None, resolved_by: str, now: datetime | None = None,
    ) -> JobResolution:
        validate_resolution(action, resolution_code, member_no, member_guid, resolved_by)
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor, job_id, for_update=True)
                if job.state != JobState.MANUAL_REVIEW:
                    raise ResolutionConflict("resolution_state_invalid")
                if action == "REQUEUE" and (job.member_no_rule != "XB-MN-1" or job.base_member_no is None or job.name_component is None):
                    raise ResolutionConflict("resolution_legacy_job_not_requeueable")
                if action == "CLOSE":
                    target, budget, busy, reason = JobState.RESOLVED, job.max_attempts, job.busy_attempts, resolution_code
                else:
                    target, budget, busy, reason = JobState.QUEUED, next_write_budget(job), 0, None
                next_state(job.state, target)
                cursor.execute(
                    "UPDATE xb_member_gateway.jobs SET state=%s,state_version=state_version+1,max_attempts=%s,busy_attempts=%s,"
                    "next_attempt_at=NULL,outcome_reason=%s,updated_at=%s WHERE job_id=%s AND state='MANUAL_REVIEW' AND state_version=%s",
                    (target.value, budget, busy, reason, current, job_id, job.state_version),
                )
                if cursor.rowcount != 1:
                    raise ResolutionConflict("resolution_state_invalid")
                resolution = JobResolution(
                    f"resolution-{uuid.uuid4().hex}", job_id, action, resolution_code, member_no, member_guid,
                    job.state_version, target.value, budget, timestamp(current),
                )
                cursor.execute(
                    "INSERT INTO xb_member_gateway.job_resolutions(resolution_id,job_id,action,resolution_code,member_no,member_guid,prior_state_version,resulting_state,write_budget,resolved_by,resolved_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (resolution.resolution_id, job_id, action, resolution_code, member_no, member_guid,
                     job.state_version, target.value, budget, resolved_by, current),
                )
                job.state = target
                job.state_version += 1
                self._audit_cursor(cursor, "job_resolved", job, resolution_code)
                return resolution

    def get_job(self, job_id: str) -> JobRecord:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                return self._select_job(cursor, job_id)

    def get_control(self) -> dict[str, bool]:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT flag_name,enabled FROM xb_member_gateway.control_flags WHERE flag_name IN ('production_activation_enabled','kill_switch_enabled')")
                return {str(row[0]): bool(row[1]) for row in cursor.fetchall()}

    def set_control(self, name: str, enabled: bool, *, updated_by: str = "operator") -> None:
        if name not in {"production_activation_enabled","kill_switch_enabled"} or not isinstance(enabled, bool):
            raise RepositoryError("control_flag_invalid")
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE xb_member_gateway.control_flags SET enabled=%s,updated_at=now(),updated_by=%s WHERE flag_name=%s", (enabled,updated_by,name))
                if cursor.rowcount != 1:
                    raise RepositoryError("control_flag_missing")

    _CURSOR_COLUMNS = "source_system,form_alias,mapping_version,watermark,production_cutover_exact,form_id,state_version,initial_window_admission_count"
    _EPOCH_COLUMNS = (
        "epoch_id,source_system,form_alias,form_id,mapping_version,admission_mode,filter_exact,"
        "production_cutover_exact,page_size,status,epoch_state_version,current_page_token,page_ordinal,"
        "predecessor_epoch_id,restart_reason,abandon_reason,created_at,completed_at"
    )
    _PAGE_COLUMNS = (
        "page_id,epoch_id,page_ordinal,page_state,request_page_token,next_page_token,terminal,items,"
        "opened_state_version,committed_state_version"
    )
    _RECEIPT_COLUMNS = (
        "response_id,source_response_ref,form_alias,form_id,mapping_version,create_time_exact,"
        "payload_fingerprint,outcome,job_id,rejection_id,receipt_version,recorded_at"
    )
    _REQUIRED_MIGRATIONS = (
        "0001_member_gateway", "0002_result_event_history", "0003_writer_termination_quarantine",
        "0004_forms_ingest_cursor", "0005_member_vertical_slice", "0006_member_write_v2",
    )

    @classmethod
    def _source_cursor_from_row(cls, row: tuple[Any, ...]) -> SourceCursor:
        return SourceCursor(
            str(row[0]), str(row[1]), str(row[2]), cls._dt(row[3]) or "",
            None if row[4] is None else str(row[4]), None if row[5] is None else str(row[5]),
            int(row[6]), int(row[7]),
        )

    @classmethod
    def _epoch_from_row(cls, row: tuple[Any, ...]) -> ScanEpoch:
        return ScanEpoch(
            epoch_id=str(row[0]), source_system=str(row[1]), form_alias=str(row[2]), form_id=str(row[3]),
            mapping_version=str(row[4]), admission_mode=SourceAdmissionMode(row[5]), filter_exact=str(row[6]),
            production_cutover_exact=str(row[7]), page_size=int(row[8]), status=ScanEpochStatus(row[9]),
            epoch_state_version=int(row[10]), current_page_token=row[11], page_ordinal=int(row[12]),
            predecessor_epoch_id=row[13], restart_reason=row[14], abandon_reason=row[15],
            created_at=cls._dt(row[16]) or "", completed_at=cls._dt(row[17]),
        )

    @staticmethod
    def _page_from_row(row: tuple[Any, ...]) -> ScanPage:
        raw = row[7]
        items = json.loads(raw) if isinstance(raw, str) else list(raw)
        return ScanPage(
            page_id=str(row[0]), epoch_id=str(row[1]), page_ordinal=int(row[2]), page_state=ScanPageState(row[3]),
            request_page_token=row[4], next_page_token=row[5], terminal=bool(row[6]),
            items=tuple((str(item["response_id"]), str(item["create_time"]), str(item["payload_hash"])) for item in items),
            opened_state_version=int(row[8]), committed_state_version=None if row[9] is None else int(row[9]),
        )

    @classmethod
    def _receipt_from_row(cls, row: tuple[Any, ...]) -> HandlingReceipt:
        return HandlingReceipt(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6]),
            HandlingOutcome(row[7]), row[8], row[9], int(row[10]), cls._dt(row[11]) or "",
        )

    @staticmethod
    def _page_items_json(items: tuple[tuple[str, str, str], ...]) -> str:
        return canonical_json([{"response_id": rid, "create_time": created, "payload_hash": digest} for rid, created, digest in items])

    def _select_cursor(self, cursor: Any, key: tuple[str, str, str], *, for_update: bool = False) -> SourceCursor:
        cursor.execute(
            f"SELECT {self._CURSOR_COLUMNS} FROM xb_member_gateway.source_ingest_cursors "
            "WHERE source_system=%s AND form_alias=%s AND mapping_version=%s" + (" FOR UPDATE" if for_update else ""),
            key,
        )
        row = cursor.fetchone()
        if row is None:
            raise SourceConflict("source_cursor_not_initialized")
        return self._source_cursor_from_row(row)

    def _admission_cursor(self, cursor: Any, key: tuple[str, str, str]) -> SourceCursor:
        source_cursor = self._select_cursor(cursor, key, for_update=True)
        if source_cursor.production_cutover_exact is None or source_cursor.form_id is None:
            raise SourceConflict("source_production_cutover_uninitialized")
        return source_cursor

    def _select_active_epoch(self, cursor: Any, key: tuple[str, str, str], *, for_update: bool = False) -> ScanEpoch | None:
        cursor.execute(
            f"SELECT {self._EPOCH_COLUMNS} FROM xb_member_gateway.source_scan_epochs "
            "WHERE source_system=%s AND form_alias=%s AND mapping_version=%s AND status='ACTIVE'" + (" FOR UPDATE" if for_update else ""),
            key,
        )
        row = cursor.fetchone()
        return None if row is None else self._epoch_from_row(row)

    def _select_epoch(self, cursor: Any, epoch_id: str, *, for_update: bool = False) -> ScanEpoch:
        cursor.execute(
            f"SELECT {self._EPOCH_COLUMNS} FROM xb_member_gateway.source_scan_epochs WHERE epoch_id=%s" + (" FOR UPDATE" if for_update else ""),
            (epoch_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SourceConflict("source_epoch_not_found")
        return self._epoch_from_row(row)

    def _select_open_page(self, cursor: Any, epoch_id: str, *, for_update: bool = False) -> ScanPage | None:
        cursor.execute(
            f"SELECT {self._PAGE_COLUMNS} FROM xb_member_gateway.source_scan_pages WHERE epoch_id=%s AND page_state='OPEN'" + (" FOR UPDATE" if for_update else ""),
            (epoch_id,),
        )
        row = cursor.fetchone()
        return None if row is None else self._page_from_row(row)

    def _select_receipt(self, cursor: Any, response_id: str, *, for_update: bool = False) -> HandlingReceipt | None:
        cursor.execute(
            f"SELECT {self._RECEIPT_COLUMNS} FROM xb_member_gateway.source_handling_receipts WHERE response_id=%s" + (" FOR UPDATE" if for_update else ""),
            (response_id,),
        )
        row = cursor.fetchone()
        return None if row is None else self._receipt_from_row(row)

    def _abandon_epoch_cursor(self, cursor: Any, epoch: ScanEpoch, reason: str, current: datetime) -> None:
        cursor.execute(
            "UPDATE xb_member_gateway.source_scan_pages SET page_state='ABANDONED',abandoned_at=%s WHERE epoch_id=%s AND page_state='OPEN'",
            (current, epoch.epoch_id),
        )
        cursor.execute(
            "UPDATE xb_member_gateway.source_scan_epochs SET status='ABANDONED',abandon_reason=%s,abandoned_at=%s,"
            "epoch_state_version=epoch_state_version+1,updated_at=%s WHERE epoch_id=%s AND status='ACTIVE' AND epoch_state_version=%s",
            (reason, current, current, epoch.epoch_id, epoch.epoch_state_version),
        )
        if cursor.rowcount != 1:
            raise SourceConflict("source_epoch_state_version_mismatch")

    def _insert_epoch(self, cursor: Any, epoch: ScanEpoch, current: datetime) -> None:
        cursor.execute(
            "INSERT INTO xb_member_gateway.source_scan_epochs(epoch_id,source_system,form_alias,form_id,mapping_version,"
            "admission_mode,production_cutover_exact,filter_exact,page_size,status,epoch_state_version,current_page_token,"
            "page_ordinal,predecessor_epoch_id,restart_reason,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',0,NULL,0,%s,%s,%s,%s)",
            (
                epoch.epoch_id, epoch.source_system, epoch.form_alias, epoch.form_id, epoch.mapping_version,
                epoch.admission_mode.value, epoch.production_cutover_exact, epoch.filter_exact, epoch.page_size,
                epoch.predecessor_epoch_id, epoch.restart_reason, current, current,
            ),
        )

    def get_source_cursor(self, form_alias: str, mapping_version: str) -> tuple[SourceCursor, ScanEpoch | None, ScanPage | None]:
        key = ("google_forms", form_alias, mapping_version)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                source_cursor = self._select_cursor(cursor, key)
                epoch = self._select_active_epoch(cursor, key)
                page = self._select_open_page(cursor, epoch.epoch_id) if epoch else None
                return source_cursor, epoch, page

    def verify_bootstrap_readiness(self, config: Any) -> None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version FROM xb_member_gateway.schema_migrations")
                versions = {str(row[0]) for row in cursor.fetchall()}
                if not set(self._REQUIRED_MIGRATIONS).issubset(versions):
                    raise RepositoryError("required_migrations_missing")
                cursor.execute("SELECT flag_name,enabled FROM xb_member_gateway.control_flags WHERE flag_name IN ('production_activation_enabled','kill_switch_enabled')")
                controls = {str(row[0]): bool(row[1]) for row in cursor.fetchall()}
                if set(controls) != {"production_activation_enabled", "kill_switch_enabled"}:
                    raise RepositoryError("required_control_rows_missing")
                if controls["production_activation_enabled"] or not controls["kill_switch_enabled"]:
                    raise RepositoryError("unsafe_control_state")
                cursor.execute(
                    f"SELECT {self._CURSOR_COLUMNS} FROM xb_member_gateway.source_ingest_cursors WHERE source_system='google_forms' AND form_alias=%s AND mapping_version=%s",
                    (config.allowed_form_aliases[0], config.allowed_mapping_versions[0]),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RepositoryError("source_cursor_not_initialized")
                source_cursor = self._source_cursor_from_row(row)
                expected = timestamp(parse_rfc3339(config.source_cutover_watermark, field="source_cutover_watermark"))
                if source_cursor.watermark != expected:
                    raise RepositoryError("source_cursor_watermark_mismatch")
                if source_cursor.production_cutover_exact is None or source_cursor.production_cutover_exact != config.source_production_cutover_exact:
                    raise RepositoryError("source_production_cutover_mismatch")
                if source_cursor.form_id is None or source_cursor.form_id != config.source_form_id:
                    raise RepositoryError("source_form_binding_mismatch")
                # Pre-0005 rows cannot prove their exact Google createTime and
                # are never guessed: fail closed until a reviewed backfill.
                cursor.execute("SELECT count(*) FROM xb_member_gateway.source_responses WHERE create_time_exact IS NULL")
                if int(cursor.fetchone()[0]) != 0:
                    raise RepositoryError("source_exact_time_backfill_missing")
                cursor.execute(
                    "SELECT count(*) FROM xb_member_gateway.source_responses sr WHERE NOT EXISTS "
                    "(SELECT 1 FROM xb_member_gateway.source_handling_receipts r WHERE r.response_id=sr.response_id)"
                )
                if int(cursor.fetchone()[0]) != 0:
                    raise RepositoryError("source_handling_receipt_missing")
                # Historical success is never silently made email-eligible, in
                # the legacy results table or in member_outcomes.
                cursor.execute(
                    "SELECT count(*) FROM ("
                    "SELECT job_id FROM xb_member_gateway.results WHERE status='CREATED_VERIFIED' "
                    "UNION SELECT job_id FROM xb_member_gateway.member_outcomes WHERE outcome='CREATED_VERIFIED'"
                    ") verified WHERE NOT EXISTS "
                    "(SELECT 1 FROM xb_member_gateway.welcome_email_outbox o WHERE o.job_id=verified.job_id)"
                )
                if int(cursor.fetchone()[0]) != 0:
                    raise RepositoryError("created_verified_without_welcome_outbox")

    def operator_status(self) -> dict[str, Any]:
        """``xb.member.gateway.operator_status.v2``: counts and codes only."""

        current = utc_now()
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT flag_name,enabled FROM xb_member_gateway.control_flags WHERE flag_name IN ('production_activation_enabled','kill_switch_enabled')")
                controls = {str(row[0]): bool(row[1]) for row in cursor.fetchall()}
                cursor.execute("SELECT count(*) FROM xb_member_gateway.schema_migrations WHERE version = ANY(%s)", (list(self._REQUIRED_MIGRATIONS),))
                migrations_ready = int(cursor.fetchone()[0]) == len(self._REQUIRED_MIGRATIONS)
                cursor.execute("SELECT count(*) FROM xb_member_gateway.source_ingest_cursors")
                cursor_ready = int(cursor.fetchone()[0]) > 0
                cursor.execute("SELECT count(*) FROM xb_member_gateway.leases WHERE active=TRUE AND expires_at>%s", (current,))
                leases = int(cursor.fetchone()[0])
                cursor.execute("SELECT state,count(*) FROM xb_member_gateway.jobs GROUP BY state")
                counts = {state: 0 for state in V2_STATUS_STATES}
                legacy = 0
                for state, count in cursor.fetchall():
                    if str(state) in counts:
                        counts[str(state)] = int(count)
                    else:
                        legacy += int(count)
                cursor.execute(
                    "SELECT COALESCE(outcome_reason,'unspecified'),count(*) FROM xb_member_gateway.jobs "
                    "WHERE state='MANUAL_REVIEW' GROUP BY 1 ORDER BY 1"
                )
                reasons = {str(reason): int(count) for reason, count in cursor.fetchall()}
                cursor.execute(
                    "SELECT metadata->>'reason',count(*) FROM xb_member_gateway.audit_events "
                    "WHERE event_type='member_dq_flag' GROUP BY 1 ORDER BY 1"
                )
                dq_counts = {str(flag): int(count) for flag, count in cursor.fetchall() if flag is not None}
                activation = controls.get("production_activation_enabled", False)
                kill = controls.get("kill_switch_enabled", True)
                return {
                    "schema_version": "xb.member.gateway.operator_status.v2",
                    "activation_enabled": activation,
                    "kill_switch_enabled": kill,
                    "dispatch_enabled": activation and not kill,
                    "migrations_ready": migrations_ready, "source_cursor_ready": cursor_ready,
                    "active_lease_count": leases,
                    "job_state_counts": counts,
                    "legacy_state_count": legacy,
                    "manual_review_reason_counts": reasons,
                    "dq_flag_counts": dq_counts,
                }

    def operator_reconciliation(self, job_id: str) -> dict[str, Any]:
        """Legacy v1 view over the read-only v1 result and case tables."""

        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor, job_id)
                cursor.execute("SELECT result_hash,status,dispatch_fence_id,acknowledged_at FROM xb_member_gateway.results WHERE job_id=%s", (job_id,))
                result = cursor.fetchone()
                cursor.execute("SELECT case_id,case_state,opened_at,closed_at FROM xb_member_gateway.reconciliation_cases WHERE job_id=%s", (job_id,))
                case = cursor.fetchone()
                check = None
                if case is not None:
                    cursor.execute("SELECT lookup_status,checked_at FROM xb_member_gateway.reconciliation_checks WHERE case_id=%s ORDER BY check_id DESC LIMIT 1", (case[0],))
                    check = cursor.fetchone()
                return {
                    "schema_version": "xb.member.gateway.operator_reconciliation.v1",
                    "job": {"job_id": job.job_id, "operation": job.operation, "state": job.state.value, "state_version": job.state_version, "attempt": job.attempt, "created_at": job.created_at},
                    "result": None if result is None else {"status": str(result[1]), "result_hash": str(result[0]), "public_fence_reference": f"fence-{uuid.UUID(str(result[2])).hex}", "acknowledged_at": self._dt(result[3])},
                    "reconciliation": None if case is None else {"case_state": str(case[1]), "opened_at": self._dt(case[2]), "closed_at": self._dt(case[3]), "check_state": None if check is None else str(check[0]), "checked_at": None if check is None else self._dt(check[1])},
                }

    def begin_source_epoch(
        self, form_alias: str, mapping_version: str, *, admission_mode: SourceAdmissionMode,
        form_id: str | None, now: datetime | None = None,
    ) -> tuple[ScanEpoch, bool]:
        current = utc_now(now)
        key = ("google_forms", form_alias, mapping_version)
        mode = SourceAdmissionMode(admission_mode)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                source_cursor = self._admission_cursor(cursor, key)
                if form_id is None or source_cursor.form_id != form_id:
                    raise SourceConflict("source_form_binding_mismatch")
                binding = epoch_binding(source_cursor, mode)
                active = self._select_active_epoch(cursor, key, for_update=True)
                if active is not None:
                    if active.binding == binding:
                        return active, True
                    if active.binding[:4] + active.binding[5:] != binding[:4] + binding[5:]:
                        raise SourceConflict("source_epoch_binding_mismatch")
                    self._abandon_epoch_cursor(cursor, active, "admission_mode_changed", current)
                cursor.execute(
                    "SELECT epoch_id FROM xb_member_gateway.source_scan_epochs WHERE source_system=%s AND form_alias=%s AND mapping_version=%s ORDER BY created_at DESC,epoch_id DESC LIMIT 1",
                    key,
                )
                latest = cursor.fetchone()
                epoch = new_epoch(source_cursor, mode, None if latest is None else str(latest[0]), None, current)
                self._insert_epoch(cursor, epoch, current)
                return epoch, False

    def restart_source_epoch(
        self, epoch_id: str, *, expected_epoch_state_version: int, restart_reason: str,
        now: datetime | None = None,
    ) -> ScanEpoch:
        if restart_reason not in RESTART_REASONS:
            raise SourceRestartReasonInvalid("source_restart_reason_invalid")
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                epoch = self._select_epoch(cursor, epoch_id, for_update=True)
                if epoch.status != ScanEpochStatus.ACTIVE:
                    raise SourceConflict("source_epoch_not_active")
                if epoch.epoch_state_version != expected_epoch_state_version:
                    raise SourceConflict("source_epoch_state_version_mismatch")
                source_cursor = self._admission_cursor(cursor, (epoch.source_system, epoch.form_alias, epoch.mapping_version))
                self._abandon_epoch_cursor(cursor, epoch, restart_reason, current)
                successor = new_epoch(source_cursor, epoch.admission_mode, epoch.epoch_id, restart_reason, current)
                self._insert_epoch(cursor, successor, current)
                return successor

    def open_source_page(
        self, epoch_id: str, *, expected_epoch_state_version: int, request_page_token: str | None,
        next_page_token: str | None, terminal: bool, items: list[Mapping[str, Any]],
        now: datetime | None = None,
    ) -> tuple[ScanPage, ScanEpoch, bool]:
        current = utc_now(now)
        request_page_token = validate_page_token(request_page_token)
        next_page_token = validate_page_token(next_page_token)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                epoch = self._select_epoch(cursor, epoch_id, for_update=True)
                if epoch.status != ScanEpochStatus.ACTIVE:
                    raise SourceConflict("source_epoch_not_active")
                normalized = normalize_page_items(items, epoch)
                existing = self._select_open_page(cursor, epoch_id, for_update=True)
                if existing is not None:
                    if (
                        existing.request_page_token == request_page_token
                        and existing.next_page_token == next_page_token
                        and existing.terminal == terminal
                        and existing.items == normalized
                        and expected_epoch_state_version == existing.opened_state_version - 1
                    ):
                        return existing, epoch, True
                    raise SourceConflict("source_page_already_open")
                if epoch.epoch_state_version != expected_epoch_state_version:
                    raise SourceConflict("source_epoch_state_version_mismatch")
                if request_page_token != epoch.current_page_token:
                    raise SourceConflict("source_page_token_unexpected")
                if terminal != (next_page_token is None):
                    raise SourceConflict("source_page_terminal_invalid")
                if next_page_token is not None:
                    cursor.execute(
                        "SELECT 1 FROM xb_member_gateway.source_scan_pages WHERE epoch_id=%s AND (request_page_token=%s OR next_page_token=%s) LIMIT 1",
                        (epoch_id, next_page_token, next_page_token),
                    )
                    if next_page_token == request_page_token or cursor.fetchone() is not None:
                        raise SourceConflict("source_page_token_repeated_in_epoch")
                page = ScanPage(
                    f"page-{uuid.uuid4().hex}", epoch_id, epoch.page_ordinal, ScanPageState.OPEN,
                    request_page_token, next_page_token, terminal, normalized, expected_epoch_state_version + 1,
                )
                cursor.execute(
                    "UPDATE xb_member_gateway.source_scan_epochs SET epoch_state_version=epoch_state_version+1,updated_at=%s "
                    "WHERE epoch_id=%s AND status='ACTIVE' AND epoch_state_version=%s",
                    (current, epoch_id, expected_epoch_state_version),
                )
                if cursor.rowcount != 1:
                    raise SourceConflict("source_epoch_state_version_mismatch")
                cursor.execute(
                    "INSERT INTO xb_member_gateway.source_scan_pages(page_id,epoch_id,page_ordinal,page_state,request_page_token,"
                    "next_page_token,terminal,items,opened_state_version,opened_at) VALUES(%s,%s,%s,'OPEN',%s,%s,%s,%s::jsonb,%s,%s)",
                    (page.page_id, epoch_id, page.page_ordinal, request_page_token, next_page_token, terminal,
                     self._page_items_json(normalized), page.opened_state_version, current),
                )
                return page, replace(epoch, epoch_state_version=expected_epoch_state_version + 1), False

    def commit_source_page(
        self, page_id: str, *, expected_epoch_state_version: int, now: datetime | None = None,
    ) -> tuple[ScanPage, ScanEpoch, bool]:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT {self._PAGE_COLUMNS} FROM xb_member_gateway.source_scan_pages WHERE page_id=%s FOR UPDATE", (page_id,))
                row = cursor.fetchone()
                if row is None:
                    raise SourceConflict("source_page_not_found")
                page = self._page_from_row(row)
                epoch = self._select_epoch(cursor, page.epoch_id, for_update=True)
                if page.page_state == ScanPageState.COMMITTED:
                    if expected_epoch_state_version == page.opened_state_version:
                        return page, epoch, True
                    raise SourceConflict("source_epoch_state_version_mismatch")
                if page.page_state == ScanPageState.ABANDONED or epoch.status != ScanEpochStatus.ACTIVE:
                    raise SourceConflict("source_page_abandoned")
                if epoch.epoch_state_version != expected_epoch_state_version:
                    raise SourceConflict("source_epoch_state_version_mismatch")
                for response_id, create_time, fingerprint in page.items:
                    receipt = self._select_receipt(cursor, response_id)
                    if receipt is None:
                        raise SourceConflict("source_page_item_unreceipted")
                    if (
                        receipt.create_time_exact != create_time
                        or receipt.payload_fingerprint != fingerprint
                        or receipt.form_alias != epoch.form_alias
                        or receipt.form_id != epoch.form_id
                        or receipt.mapping_version != epoch.mapping_version
                    ):
                        raise SourceConflict("source_page_item_receipt_mismatch")
                    cursor.execute(
                        "INSERT INTO xb_member_gateway.source_scan_page_items(page_id,response_id,create_time_exact,payload_fingerprint,committed_at) VALUES(%s,%s,%s,%s,%s)",
                        (page_id, response_id, create_time, fingerprint, current),
                    )
                committed_version = expected_epoch_state_version + 1
                cursor.execute(
                    "UPDATE xb_member_gateway.source_scan_pages SET page_state='COMMITTED',committed_state_version=%s,committed_at=%s WHERE page_id=%s AND page_state='OPEN'",
                    (committed_version, current, page_id),
                )
                cursor.execute(
                    "UPDATE xb_member_gateway.source_scan_epochs SET epoch_state_version=epoch_state_version+1,current_page_token=%s,"
                    "page_ordinal=page_ordinal+1,status=%s,completed_at=%s,updated_at=%s WHERE epoch_id=%s AND status='ACTIVE' AND epoch_state_version=%s",
                    (page.next_page_token, "COMPLETED" if page.terminal else "ACTIVE", current if page.terminal else None,
                     current, epoch.epoch_id, expected_epoch_state_version),
                )
                if cursor.rowcount != 1:
                    raise SourceConflict("source_epoch_state_version_mismatch")
                committed = replace(page, page_state=ScanPageState.COMMITTED, committed_state_version=committed_version)
                updated = replace(
                    epoch, epoch_state_version=committed_version, current_page_token=page.next_page_token,
                    page_ordinal=epoch.page_ordinal + 1,
                    status=ScanEpochStatus.COMPLETED if page.terminal else ScanEpochStatus.ACTIVE,
                    completed_at=timestamp(current) if page.terminal else None,
                )
                return committed, updated, False

    def _reference_key_bytes(self) -> bytes:
        key = self._reference_key or os.environ.get("XB_MEMBER_GATEWAY_REFERENCE_HMAC_KEY", "").encode("utf-8")
        if not key:
            raise RepositoryError("source_reference_key_required")
        return key


    def ingest_source_event(
        self, event: SourceEvent, *, policy: AdmissionPolicy, now: datetime | None = None,
        initial_window_max: int | None = 1,
    ) -> IngestOutcome:
        """Receipt-driven admission; no cross-response ordering authority.
        The VALIDATED gate runs once, on the first admission only."""

        if not isinstance(policy, AdmissionPolicy):
            raise RepositoryError("admission_policy_required")
        source_ref = hmac_reference(event.response_id, self._reference_key_bytes())
        payload = dict(event.payload)
        create_time_utc = create_time_utc_from_exact(event.create_time)
        payload["create_time"] = render_create_time_utc(create_time_utc)
        created_at = utc_now(now)
        key = (event.source_system, event.form_alias, event.mapping_version)
        job = JobRecord(
            f"job-{uuid.uuid4().hex}", event.request_id, source_ref, event.response_id, event.payload_hash,
            event.operation, payload, timestamp(created_at), source_system=event.source_system,
            form_alias=event.form_alias, mapping_version=event.mapping_version, max_attempts=DEFAULT_WRITE_BUDGET,
        )
        InMemoryRepository._admit(job, policy)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                source_cursor = self._admission_cursor(cursor, key)
                if cutover_key(event.create_time) < cutover_key(source_cursor.production_cutover_exact):
                    raise SourceConflict("source_event_before_cutover")
                cursor.execute("SELECT response_id,payload_hash FROM xb_member_gateway.ingest_receipts WHERE request_id=%s FOR UPDATE", (event.request_id,))
                request = cursor.fetchone()
                if request is not None and (request[0] != event.response_id or request[1] != event.payload_hash):
                    raise SourceConflict("request_identity_payload_conflict")
                cursor.execute("SELECT response_id FROM xb_member_gateway.source_rejections WHERE request_id=%s", (event.request_id,))
                rejected_request = cursor.fetchone()
                if rejected_request is not None and rejected_request[0] != event.response_id:
                    raise SourceConflict("request_identity_payload_conflict")
                receipt = self._select_receipt(cursor, event.response_id, for_update=True)
                if receipt is not None:
                    if not receipt_matches(receipt, event.form_alias, source_cursor.form_id, event.mapping_version, event.create_time, event.payload_hash, HandlingOutcome.ACCEPTED):
                        raise SourceConflict("source_identity_payload_conflict")
                    cursor.execute("INSERT INTO xb_member_gateway.source_observations(response_id,request_id,form_alias,create_time,create_time_exact,mapping_version,payload_hash,canonical_payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)", (event.response_id,event.request_id,event.form_alias,create_time_utc,event.create_time,event.mapping_version,event.payload_hash,canonical_json(payload)))
                    cursor.execute("INSERT INTO xb_member_gateway.ingest_receipts(request_id,response_id,payload_hash,replayed) VALUES(%s,%s,%s,TRUE) ON CONFLICT(request_id) DO UPDATE SET replayed=TRUE,received_at=now()", (event.request_id,event.response_id,event.payload_hash))
                    return IngestOutcome(self._select_job(cursor, str(receipt.job_id)), replayed=True)
                cursor.execute("SELECT 1 FROM xb_member_gateway.source_responses WHERE response_id=%s", (event.response_id,))
                if cursor.fetchone() is not None:
                    raise RepositoryError("source_handling_receipt_missing")
                if initial_window_max is not None:
                    # Atomic 0->1 accepted-member guard. A losing concurrent
                    # admission receives no receipt and cannot checkpoint.
                    cursor.execute(
                        "UPDATE xb_member_gateway.source_ingest_cursors SET initial_window_admission_count=initial_window_admission_count+1,"
                        "state_version=state_version+1,updated_at=now() WHERE source_system=%s AND form_alias=%s AND mapping_version=%s "
                        "AND initial_window_admission_count < %s",
                        (*key, initial_window_max),
                    )
                    if cursor.rowcount != 1:
                        raise SourceConflict("initial_source_window_exhausted")
                cursor.execute("INSERT INTO xb_member_gateway.source_responses(response_id,source_response_ref,create_time,create_time_exact,mapping_version,payload_hash,canonical_payload) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)", (event.response_id,source_ref,create_time_utc,event.create_time,event.mapping_version,event.payload_hash,canonical_json(payload)))
                cursor.execute("INSERT INTO xb_member_gateway.source_observations(response_id,request_id,form_alias,create_time,create_time_exact,mapping_version,payload_hash,canonical_payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)", (event.response_id,event.request_id,event.form_alias,create_time_utc,event.create_time,event.mapping_version,event.payload_hash,canonical_json(payload)))
                cursor.execute("INSERT INTO xb_member_gateway.ingest_receipts(request_id,response_id,payload_hash,replayed) VALUES(%s,%s,%s,FALSE)", (event.request_id,event.response_id,event.payload_hash))
                cursor.execute(
                    "INSERT INTO xb_member_gateway.jobs(job_id,response_id,operation,payload_hash,canonical_payload,state,state_version,attempt_count,max_attempts,"
                    "member_no_rule,base_member_no,name_component,outcome_reason,created_at) VALUES(%s,%s,'member.create',%s,%s::jsonb,%s,%s,0,%s,%s,%s,%s,%s,%s)",
                    (job.job_id, event.response_id, event.payload_hash, canonical_json(payload), job.state.value, job.state_version,
                     job.max_attempts, job.member_no_rule, job.base_member_no, job.name_component, job.outcome_reason, created_at),
                )
                cursor.execute(
                    "INSERT INTO xb_member_gateway.source_handling_receipts(response_id,source_response_ref,form_alias,form_id,mapping_version,create_time_exact,payload_fingerprint,outcome,job_id,rejection_id,receipt_version,recorded_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'ACCEPTED',%s,NULL,1,%s)",
                    (event.response_id, source_ref, event.form_alias, source_cursor.form_id, event.mapping_version, event.create_time, event.payload_hash, job.job_id, created_at),
                )
                return IngestOutcome(job, replayed=False)

    def reject_source_response(self, rejection: SourceRejection, *, now: datetime | None = None) -> RejectionOutcome:
        source_ref = hmac_reference(rejection.response_id, self._reference_key_bytes())
        current = utc_now(now)
        key = (rejection.source_system, rejection.form_alias, rejection.mapping_version)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                source_cursor = self._admission_cursor(cursor, key)
                if cutover_key(rejection.create_time) < cutover_key(source_cursor.production_cutover_exact):
                    raise SourceConflict("source_event_before_cutover")
                cursor.execute("SELECT response_id FROM xb_member_gateway.ingest_receipts WHERE request_id=%s", (rejection.request_id,))
                request = cursor.fetchone()
                cursor.execute("SELECT response_id FROM xb_member_gateway.source_rejections WHERE request_id=%s", (rejection.request_id,))
                rejected_request = cursor.fetchone()
                if any(item is not None and item[0] != rejection.response_id for item in (request, rejected_request)):
                    raise SourceConflict("request_identity_payload_conflict")
                receipt = self._select_receipt(cursor, rejection.response_id, for_update=True)
                if receipt is not None:
                    cursor.execute("SELECT rejection_id,error_code FROM xb_member_gateway.source_rejections WHERE response_id=%s", (rejection.response_id,))
                    stored = cursor.fetchone()
                    if (
                        stored is None or stored[1] != rejection.error_code
                        or not receipt_matches(receipt, rejection.form_alias, source_cursor.form_id, rejection.mapping_version, rejection.create_time, rejection.payload_hash, HandlingOutcome.REJECTED)
                    ):
                        raise SourceConflict("source_identity_payload_conflict")
                    return RejectionOutcome(str(stored[0]), receipt.source_response_ref, str(stored[1]), True)
                cursor.execute("SELECT 1 FROM xb_member_gateway.source_responses WHERE response_id=%s", (rejection.response_id,))
                if cursor.fetchone() is not None:
                    raise SourceConflict("source_identity_payload_conflict")
                rejection_id = f"rejection-{uuid.uuid4().hex}"
                cursor.execute(
                    "INSERT INTO xb_member_gateway.source_rejections(response_id,rejection_id,source_response_ref,form_alias,form_id,mapping_version,create_time_exact,payload_hash,error_code,request_id,recorded_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (rejection.response_id, rejection_id, source_ref, rejection.form_alias, source_cursor.form_id, rejection.mapping_version, rejection.create_time, rejection.payload_hash, rejection.error_code, rejection.request_id, current),
                )
                cursor.execute(
                    "INSERT INTO xb_member_gateway.source_handling_receipts(response_id,source_response_ref,form_alias,form_id,mapping_version,create_time_exact,payload_fingerprint,outcome,job_id,rejection_id,receipt_version,recorded_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'REJECTED',NULL,%s,1,%s)",
                    (rejection.response_id, source_ref, rejection.form_alias, source_cursor.form_id, rejection.mapping_version, rejection.create_time, rejection.payload_hash, rejection_id, current),
                )
                return RejectionOutcome(rejection_id, source_ref, rejection.error_code, False)

    _OUTBOX_COLUMNS = (
        "outbox_id,job_id,response_id,source_response_ref,template_id,recipient,message_hash,state,state_version,"
        "attempt,max_attempts,lease_id,lease_expires_at,next_attempt_at,send_intent_at,last_error_code,created_at,updated_at"
    )

    @classmethod
    def _outbox_from_row(cls, row: tuple[Any, ...]) -> WelcomeEmailOutbox:
        return WelcomeEmailOutbox(
            outbox_id=str(row[0]), job_id=str(row[1]), response_id=str(row[2]), source_response_ref=str(row[3]),
            template_id=str(row[4]), recipient=str(row[5]), message_hash=str(row[6]), state=WelcomeEmailState(row[7]),
            state_version=int(row[8]), attempt=int(row[9]), max_attempts=int(row[10]), lease_id=row[11],
            lease_expires_at=cls._dt(row[12]), next_attempt_at=cls._dt(row[13]), send_intent_at=cls._dt(row[14]),
            last_error_code=row[15], created_at=cls._dt(row[16]) or "", updated_at=cls._dt(row[17]) or "",
        )

    def _select_outbox(self, cursor: Any, where: str, params: tuple[Any, ...], *, for_update: bool = False) -> WelcomeEmailOutbox | None:
        cursor.execute(
            f"SELECT {self._OUTBOX_COLUMNS} FROM xb_member_gateway.welcome_email_outbox WHERE {where}" + (" FOR UPDATE" if for_update else ""),
            params,
        )
        row = cursor.fetchone()
        return None if row is None else self._outbox_from_row(row)

    def _welcome_event_cursor(self, cursor: Any, outbox: WelcomeEmailOutbox, event_type: str, from_state: WelcomeEmailState | None, current: datetime) -> None:
        cursor.execute(
            "INSERT INTO xb_member_gateway.welcome_email_events(outbox_id,event_type,from_state,to_state,state_version,attempt,error_code,recorded_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (outbox.outbox_id, event_type, None if from_state is None else from_state.value, outbox.state.value,
             outbox.state_version, outbox.attempt, outbox.last_error_code, current),
        )

    _WELCOME_UPDATABLE = frozenset({"attempt", "lease_id", "lease_expires_at", "next_attempt_at", "send_intent_at", "last_error_code"})

    def _welcome_move_cursor(self, cursor: Any, outbox: WelcomeEmailOutbox, target: WelcomeEmailState, current: datetime, event_type: str, **values: Any) -> WelcomeEmailOutbox:
        assert_transition(outbox.state, target)
        assignments = ["state=%s", "state_version=state_version+1", "updated_at=%s"]
        params: list[Any] = [target.value, current]
        for name, value in values.items():
            if name not in self._WELCOME_UPDATABLE:
                raise RepositoryError("welcome_email_update_field_invalid")
            assignments.append(f"{name}=%s")
            params.append(parse_timestamp(value) if name in {"lease_expires_at", "next_attempt_at", "send_intent_at"} and value is not None else value)
        params.extend([outbox.outbox_id, outbox.state_version])
        cursor.execute(
            "UPDATE xb_member_gateway.welcome_email_outbox SET " + ",".join(assignments) + " WHERE outbox_id=%s AND state_version=%s",
            tuple(params),
        )
        if cursor.rowcount != 1:
            raise WelcomeEmailError("welcome_email_state_version_mismatch")
        updated = replace(outbox, state=target, state_version=outbox.state_version + 1, updated_at=timestamp(current), **values)
        self._welcome_event_cursor(cursor, updated, event_type, outbox.state, current)
        return updated

    def _insert_welcome_outbox_cursor(self, cursor: Any, job: JobRecord, current: datetime) -> None:
        """Inserted inside the same transaction that records CREATED_VERIFIED."""

        message = build_welcome_message(job.member_payload["email"])
        outbox_id = f"welcome-{uuid.uuid4().hex}"
        cursor.execute(
            "INSERT INTO xb_member_gateway.welcome_email_outbox(outbox_id,job_id,response_id,source_response_ref,template_id,recipient,message_hash,state,state_version,attempt,max_attempts,next_attempt_at,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'PENDING',0,0,%s,%s,%s,%s)",
            (outbox_id, job.job_id, job.response_id, job.source_response_ref, WELCOME_TEMPLATE_ID, message["to"],
             welcome_message_hash(message), WELCOME_MAX_ATTEMPTS, current + WELCOME_INITIAL_DELAY, current, current),
        )
        cursor.execute(
            "INSERT INTO xb_member_gateway.welcome_email_events(outbox_id,event_type,from_state,to_state,state_version,attempt,error_code,recorded_at) VALUES(%s,'created',NULL,'PENDING',0,0,NULL,%s)",
            (outbox_id, current),
        )

    def _verify_welcome_outbox_cursor(self, cursor: Any, job: JobRecord) -> None:
        outbox = self._select_outbox(cursor, "job_id=%s", (job.job_id,))
        if outbox is None:
            raise ResultConflict("welcome_outbox_identity_missing")
        verify_outbox_identity(outbox, job)

    def _sweep_welcome_leases_cursor(self, cursor: Any, current: datetime) -> None:
        cursor.execute(
            f"SELECT {self._OUTBOX_COLUMNS} FROM xb_member_gateway.welcome_email_outbox "
            "WHERE state IN ('LEASED','SEND_INTENT_RECORDED') AND lease_expires_at<=%s ORDER BY outbox_id FOR UPDATE",
            (current,),
        )
        for row in cursor.fetchall():
            outbox = self._outbox_from_row(row)
            if outbox.state == WelcomeEmailState.SEND_INTENT_RECORDED:
                self._welcome_move_cursor(cursor, outbox, WelcomeEmailState.DELIVERY_OUTCOME_UNCERTAIN, current, "delivery_outcome_uncertain", last_error_code="send_intent_lease_expired")
            else:
                self._welcome_safe_failure_cursor(cursor, outbox, current, "lease_expired_before_send_intent")

    def _welcome_safe_failure_cursor(self, cursor: Any, outbox: WelcomeEmailOutbox, current: datetime, error_code: str) -> WelcomeEmailOutbox:
        target = safe_failure_target(outbox.attempt, outbox.max_attempts)
        if target == WelcomeEmailState.DEAD_LETTER:
            return self._welcome_move_cursor(cursor, outbox, target, current, "dead_lettered", last_error_code=error_code, next_attempt_at=None)
        return self._welcome_move_cursor(
            cursor, outbox, target, current, "retry_scheduled", last_error_code=error_code,
            next_attempt_at=timestamp(next_attempt_after(outbox.attempt, current)),
        )

    def claim_welcome_email(self, *, now: datetime | None = None) -> tuple[WelcomeEmailOutbox, dict[str, Any]] | None:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_kill_switch(cursor)
                self._sweep_welcome_leases_cursor(cursor, current)
                cursor.execute("SELECT 1 FROM xb_member_gateway.welcome_email_outbox WHERE state IN ('LEASED','SEND_INTENT_RECORDED') LIMIT 1")
                if cursor.fetchone() is not None:
                    return None
                outbox = self._select_outbox(
                    cursor,
                    "state IN ('PENDING','RETRY_WAIT') AND next_attempt_at<=%s ORDER BY next_attempt_at,created_at,outbox_id LIMIT 1",
                    (current,), for_update=True,
                )
                if outbox is None:
                    return None
                message = verify_message(outbox)
                outbox = self._welcome_move_cursor(
                    cursor, outbox, WelcomeEmailState.LEASED, current, "claimed", attempt=outbox.attempt + 1,
                    lease_id=f"lease-{uuid.uuid4().hex}", lease_expires_at=timestamp(current + timedelta(seconds=WELCOME_LEASE_SECONDS)),
                    next_attempt_at=None, last_error_code=None,
                )
                return outbox, message

    def record_welcome_send_intent(self, outbox_id: str, *, lease_id: str, expected_state_version: int, now: datetime | None = None) -> tuple[WelcomeEmailOutbox, bool]:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                outbox = self._select_outbox(cursor, "outbox_id=%s", (outbox_id,), for_update=True)
                if outbox is None:
                    raise WelcomeEmailError("welcome_email_not_found")
                if outbox.state == WelcomeEmailState.SEND_INTENT_RECORDED and outbox.lease_id == lease_id and outbox.state_version == expected_state_version + 1:
                    return outbox, True
                check_welcome_lease(outbox, lease_id, expected_state_version, WelcomeEmailState.LEASED, current, require_unexpired=True)
                return self._welcome_move_cursor(cursor, outbox, WelcomeEmailState.SEND_INTENT_RECORDED, current, "send_intent_recorded", send_intent_at=timestamp(current)), False

    def record_welcome_result(
        self, outbox_id: str, *, lease_id: str, expected_state_version: int, outcome: str,
        error_code: str | None, now: datetime | None = None,
    ) -> tuple[WelcomeEmailOutbox, bool]:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                outbox = self._select_outbox(cursor, "outbox_id=%s", (outbox_id,), for_update=True)
                if outbox is None:
                    raise WelcomeEmailError("welcome_email_not_found")
                target = welcome_result_target(outbox, outcome)
                if outbox.state == target and outbox.lease_id == lease_id and outbox.state_version == expected_state_version + 1:
                    return outbox, True
                if outcome == "failed_before_send_intent":
                    check_welcome_lease(outbox, lease_id, expected_state_version, WelcomeEmailState.LEASED, current, require_unexpired=False)
                    return self._welcome_safe_failure_cursor(cursor, outbox, current, error_code or "failed_before_send_intent"), False
                check_welcome_lease(outbox, lease_id, expected_state_version, WelcomeEmailState.SEND_INTENT_RECORDED, current, require_unexpired=False)
                event = "sent" if target == WelcomeEmailState.SENT else "delivery_outcome_uncertain"
                return self._welcome_move_cursor(cursor, outbox, target, current, event, last_error_code=error_code), False

    def welcome_outbox_for_job(self, job_id: str) -> WelcomeEmailOutbox | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                return self._select_outbox(cursor, "job_id=%s", (job_id,))
