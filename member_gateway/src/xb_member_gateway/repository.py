"""Durable member-gateway state and an offline repository fake.

The fake is used by package tests.  The PostgreSQL adapter commits every
state-changing action in a short transaction; AutoCount calls are never made
from this module or while a repository transaction is open.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from threading import RLock
from typing import Any, Callable, Iterator, Mapping, Protocol

from .canonical import (
    CanonicalizationError, HASH_RE as PAYLOAD_HASH_RE, SAFE_ID_RE as SAFE_RESPONSE_ID_RE,
    canonical_json, create_time_utc_from_exact, exact_filter, parse_google_create_time_exact,
    parse_rfc3339, render_create_time_utc, validate_page_token,
)
from .crypto import hmac_reference
from .models import (
    AllocationProbe, AllocationRecord, AllocationRecheck, DispatchFenceRecord, HandlingOutcome,
    HandlingReceipt, IngestOutcome, JobRecord, JobState, LeaseRecord, ProbeStatus, RESTART_REASONS,
    RejectionOutcome, ResultRecord, ResultStatus, ReconciliationCaseRecord, ReconciliationCaseState,
    ReconciliationCheckRecord, SOURCE_PAGE_ITEM_FIELDS, SOURCE_PAGE_SIZE, ScanEpoch, ScanEpochStatus,
    ScanPage, ScanPageState, SourceAdmissionMode, SourceCursor, SourceEvent, SourceRejection,
    WelcomeEmailOutbox, WelcomeEmailState, WriteIntentRecord, WriterExecutionHold, WriterHoldState,
)
from .notifications import (
    WELCOME_INITIAL_DELAY, WELCOME_LEASE_SECONDS, WELCOME_MAX_ATTEMPTS, WELCOME_TEMPLATE_ID,
    WelcomeEmailError, assert_transition, build_welcome_message, next_attempt_after,
    safe_failure_target, verify_message, welcome_message_hash,
)
from .results import make_result
from .state_machine import TERMINAL_STATES, next_state


class RepositoryError(RuntimeError):
    pass


class JobNotFound(RepositoryError):
    pass


class SourceConflict(RepositoryError):
    pass


class LeaseConflict(RepositoryError):
    pass


class AllocationConflict(RepositoryError):
    pass


class ResultConflict(RepositoryError):
    pass


class WriterTerminationConflict(RepositoryError):
    pass


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


def new_fence_id() -> str:
    return f"fence-{uuid.uuid4().hex}"


def public_fence_id(value: Any) -> str:
    text = str(value)
    raw = text[6:] if text.startswith("fence-") else text
    try:
        return f"fence-{uuid.UUID(raw).hex}"
    except (ValueError, AttributeError, TypeError) as exc:
        raise RepositoryError("dispatch_fence_id_invalid") from exc


def internal_fence_id(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("fence-"):
        raise RepositoryError("dispatch_fence_id_invalid")
    try:
        return str(uuid.UUID(value[6:]))
    except (ValueError, AttributeError) as exc:
        raise RepositoryError("dispatch_fence_id_invalid") from exc


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


class MemberProbe(Protocol):
    def __call__(self, candidate: str) -> ProbeStatus:
        ...


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
        self._probes: dict[str, list[AllocationProbe]] = {}
        self._allocations: dict[str, AllocationRecord] = {}
        self._allocations_by_member: dict[str, str] = {}
        self._write_intents: dict[str, WriteIntentRecord] = {}
        self._fences: dict[str, DispatchFenceRecord] = {}
        self._writer_holds: dict[str, WriterExecutionHold] = {}
        self._writer_gate_version = 0
        self._rechecks: dict[str, list[AllocationRecheck]] = {}
        self._reconciliation_cases: dict[str, ReconciliationCaseRecord] = {}
        self._reconciliation_checks: dict[str, list[ReconciliationCheckRecord]] = {}
        self._results: dict[str, ResultRecord] = {}
        self._result_history: dict[str, list[ResultRecord]] = {}
        self._result_conflicts: list[dict[str, str]] = []
        self._control = {"production_activation_enabled": False, "kill_switch_enabled": True}
        self._probe_status: dict[str, ProbeStatus] = {}
        self._member_records: dict[str, Mapping[str, Any]] = {}
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

    def _lease(self, job: JobRecord, worker_id: str, now: datetime) -> None:
        lease = self._leases.get(job.job_id)
        if lease is None or lease.worker_id != worker_id or lease.state_version != job.state_version or parse_timestamp(lease.expires_at) <= now:
            raise LeaseConflict("lease_not_owned_or_expired")

    def _assert_kill_switch_clear(self) -> None:
        """Must be called while _lock is held, at the mutation boundary."""
        if self._control.get("kill_switch_enabled", True):
            raise RepositoryError("kill_switch_enabled")

    def _active_writer_holds(self) -> list[WriterExecutionHold]:
        return [hold for hold in self._writer_holds.values() if hold.active]

    def _assert_no_active_writer_hold(self, *, allow_job_id: str | None = None) -> None:
        if any(hold.job_id != allow_job_id for hold in self._active_writer_holds()):
            raise WriterTerminationConflict("writer_termination_quarantine_active")

    def _hold(self, job_id: str) -> WriterExecutionHold | None:
        return self._writer_holds.get(job_id)

    def _set_hold(self, hold: WriterExecutionHold, job: JobRecord) -> None:
        self._writer_holds[job.job_id] = hold
        job.writer_termination_state = hold.state.value

    @staticmethod
    def _validate_process_identity(pid: int, process_start_time: str) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise WriterTerminationConflict("writer_process_identity_invalid")
        if not isinstance(process_start_time, str) or not process_start_time or len(process_start_time) > 80:
            raise WriterTerminationConflict("writer_process_identity_invalid")

    @staticmethod
    def _validate_evidence(evidence_type: str, evidence_reference: str, exit_code: int | None = None) -> None:
        if evidence_type != "process_exit" or not isinstance(evidence_reference, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", evidence_reference):
            raise WriterTerminationConflict("writer_termination_proof_required")
        if exit_code is None or isinstance(exit_code, bool) or not isinstance(exit_code, int) or not -(2**31) <= exit_code <= (2**31 - 1):
            raise WriterTerminationConflict("writer_termination_proof_required")

    def _active_leases(self, now: datetime) -> list[LeaseRecord]:
        return [
            lease for job_id, lease in self._leases.items()
            if parse_timestamp(lease.expires_at) > now
            and self._jobs.get(job_id) is not None
            and self._jobs[job_id].state not in TERMINAL_STATES
        ]

    def _fresh_recheck(self, job: JobRecord, worker_id: str, now: datetime) -> AllocationRecheck | None:
        allocation = self._allocations.get(job.job_id)
        records = self._rechecks.get(job.job_id, [])
        lease = self._leases.get(job.job_id)
        if allocation is None or lease is None or not records or job.state not in {JobState.ALLOCATION_BOUND, JobState.WRITE_INTENT_RECORDED}:
            return None
        latest = records[-1]
        if (
            job.state not in {JobState.ALLOCATION_BOUND, JobState.WRITE_INTENT_RECORDED}
            or latest.attempt != job.attempt
            or latest.worker_id != worker_id
            or latest.member_no != allocation.member_no
            or latest.status != ProbeStatus.FREE
            or latest.probe_reference == allocation.probe_reference
            or parse_timestamp(latest.observed_at) > now
        ):
            return None
        return latest

    def _move(self, job: JobRecord, target: JobState) -> None:
        job.state = next_state(job.state, target, dispatch_fenced=job.dispatch_fenced)
        job.state_version += 1
        lease = self._leases.get(job.job_id)
        if lease is not None:
            self._leases[job.job_id] = LeaseRecord(lease.job_id, lease.worker_id, lease.expires_at, job.state_version)

    def _audit(self, event_type: str, job: JobRecord | None = None, *, error_code: str | None = None, count: int | None = None) -> None:
        entry: dict[str, Any] = {"event_type": event_type, "recorded_at": timestamp()}
        if job is not None:
            entry.update({"job_id": job.job_id, "state": job.state.value, "operation": job.operation})
        if error_code is not None:
            entry["error_code"] = error_code
        if count is not None:
            entry["count"] = count
        self._audit_events.append(entry)

    def ingest_source_event(
        self,
        event: SourceEvent,
        *,
        now: datetime | None = None,
        initial_window_max: int | None = 1,
    ) -> IngestOutcome:
        """Receipt-driven admission. An unseen response is never compared with
        another response's position; only the fixed cutover excludes."""

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
                mapping_version=event.mapping_version,
            )
            self._move(job, JobState.VALIDATED)
            self._move(job, JobState.QUEUED)
            self._jobs[job.job_id] = job
            self._job_by_response[event.response_id] = job.job_id
            self._probes[job.job_id] = []
            self._receipts[event.response_id] = HandlingReceipt(
                event.response_id, source_ref, event.form_alias, cursor.form_id, event.mapping_version,
                event.create_time, event.payload_hash, HandlingOutcome.ACCEPTED, job.job_id, None, 1, timestamp(now),
            )
            self._audit("source_ingested", job)
            if initial_window_max is not None:
                self._source_cursors[cursor_key] = replace(
                    cursor,
                    state_version=cursor.state_version + 1,
                    initial_window_admission_count=cursor.initial_window_admission_count + 1,
                )
            return IngestOutcome(self._copy(job), replayed=False)

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

    def operator_status(self) -> dict[str, Any]:
        with self._lock:
            holds = list(self._writer_holds.values())
            active_holds = [hold for hold in holds if hold.active]
            return {
                "schema_version": "xb.member.gateway.operator_status.v1",
                "activation_enabled": self._control["production_activation_enabled"],
                "kill_switch_enabled": self._control["kill_switch_enabled"],
                "migrations_ready": True,
                "source_cursor_ready": bool(self._source_cursors),
                "active_lease_count": len([lease for lease in self._leases.values() if parse_timestamp(lease.expires_at) > utc_now()]),
                "writer_hold_active": bool(active_holds),
                "writer_quarantined": any(hold.state == WriterHoldState.QUARANTINED for hold in active_holds),
                "termination_proof_required": any(hold.state in {WriterHoldState.PENDING, WriterHoldState.REGISTERED, WriterHoldState.QUARANTINED} for hold in active_holds),
                "uncertain_job_count": len([job for job in self._jobs.values() if job.state == JobState.WRITE_OUTCOME_UNCERTAIN]),
            }

    def operator_reconciliation(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._job(job_id)
            result = self._results.get(job_id)
            case = next((item for item in self._reconciliation_cases.values() if item.job_id == job_id), None)
            checks = self._reconciliation_checks.get(case.case_id, []) if case else []
            return {
                "schema_version": "xb.member.gateway.operator_reconciliation.v1",
                "job": {"job_id": job.job_id, "operation": job.operation, "state": job.state.value, "state_version": job.state_version, "attempt": job.attempt, "created_at": job.created_at},
                "result": None if result is None else {"status": result.status.value, "result_hash": result.result_hash, "public_fence_reference": result.dispatch_fence_id, "acknowledged_at": result.acknowledged_at},
                "reconciliation": None if case is None else {"case_state": case.state.value, "opened_at": case.opened_at, "closed_at": case.closed_at, "check_state": checks[-1].lookup_status if checks else None, "checked_at": checks[-1].checked_at if checks else None},
            }

    def _quarantine_hold(self, job: JobRecord, current: datetime, *, error_code: str = "writer_termination_unconfirmed") -> WriterExecutionHold:
        fence = self._fences.get(job.job_id)
        if fence is None:
            raise WriterTerminationConflict("dispatch_fence_required")
        hold = self._writer_holds.get(job.job_id)
        if hold is None:
            hold = WriterExecutionHold(
                hold_id=f"hold-{uuid.uuid4().hex}", job_id=job.job_id, fence_id=fence.fence_id,
                attempt=max(job.attempt, 1), worker_session=None, host_binding=None,
                execution_id=fence.execution_id or f"legacy-execution-{uuid.uuid4().hex}",
                member_no=fence.member_no, state=WriterHoldState.QUARANTINED,
                state_version=1, pid=None, process_start_time=None,
                evidence_type="legacy_unproven", evidence_reference=f"legacy-{uuid.uuid4().hex}",
                created_at=timestamp(current), updated_at=timestamp(current),
                quarantined_at=timestamp(current),
            )
        elif hold.state == WriterHoldState.CLEARED:
            raise WriterTerminationConflict("writer_termination_clearance_rejected")
        elif hold.state != WriterHoldState.QUARANTINED:
            hold = replace(
                hold, state=WriterHoldState.QUARANTINED, state_version=hold.state_version + 1,
                evidence_type="quarantine", evidence_reference=f"quarantine-{uuid.uuid4().hex}",
                updated_at=timestamp(current), quarantined_at=timestamp(current),
            )
        self._set_hold(hold, job)
        if job.state != JobState.WRITER_TERMINATION_UNCONFIRMED:
            self._move(job, JobState.WRITER_TERMINATION_UNCONFIRMED)
        job.last_error_code = error_code
        return hold

    def _settle_confirmed_termination(self, job: JobRecord, current: datetime, *, error_code: str = "result_acknowledgement_lost") -> ResultRecord:
        hold = self._writer_holds.get(job.job_id)
        fence = self._fences.get(job.job_id)
        if hold is None or not hold.termination_confirmed or fence is None:
            raise WriterTerminationConflict("writer_termination_proof_required")
        existing = self._results.get(job.job_id)
        if existing is None:
            result = make_result(
                job=job, fence=fence, status=ResultStatus.WRITE_OUTCOME_UNCERTAIN,
                save_invocation_count=1, readback_found=False, readback_match=False,
                error_code=error_code, acknowledged_at=current,
            )
            self._result_history.setdefault(job.job_id, []).append(self._copy(result))
            self._results[job.job_id] = self._copy(result)
        elif existing.status == ResultStatus.WRITE_OUTCOME_UNCERTAIN:
            result = existing
        else:
            raise ResultConflict("result_payload_conflict")
        if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
            self._move(job, JobState.WRITE_OUTCOME_UNCERTAIN)
        job.result_status = ResultStatus.WRITE_OUTCOME_UNCERTAIN
        job.save_invocation_count = 1
        job.last_error_code = result.error_code
        cleared = replace(
            hold, state=WriterHoldState.CLEARED, state_version=hold.state_version + 1,
            updated_at=timestamp(current), cleared_at=timestamp(current),
        )
        self._set_hold(cleared, job)
        self._leases.pop(job.job_id, None)
        job.lease_owner = None
        job.lease_expires_at = None
        self._audit("confirmed_termination_uncertainty_settled", job, error_code=result.error_code)
        return self._copy(result)

    def reclaim_expired(self, *, now: datetime | None = None) -> int:
        current = utc_now(now)
        with self._lock:
            count = 0
            active_hold_jobs = {hold.job_id for hold in self._active_writer_holds()}
            for job in self._jobs.values():
                if job.state in TERMINAL_STATES:
                    self._leases.pop(job.job_id, None)
                    continue
                if active_hold_jobs and job.job_id not in active_hold_jobs:
                    continue
                lease = self._leases.get(job.job_id)
                if lease is None or parse_timestamp(lease.expires_at) > current:
                    continue
                hold = self._writer_holds.get(job.job_id)
                if hold is not None and hold.state == WriterHoldState.CLEARED:
                    self._leases.pop(job.job_id, None)
                    job.lease_owner = None
                    job.lease_expires_at = None
                    count += 1
                    self._audit("lease_reclaimed", job, count=count)
                    continue
                if hold is not None and hold.termination_confirmed:
                    self._settle_confirmed_termination(job, current, error_code="lease_expired_after_confirmed_termination")
                elif job.dispatch_fenced or job.state in {JobState.WRITING, JobState.READBACK, JobState.WRITER_TERMINATION_UNCONFIRMED}:
                    self._quarantine_hold(job, current, error_code="lease_expired_after_dispatch")
                elif job.state in {JobState.LEASED, JobState.PRECHECKING, JobState.ALLOCATION_BOUND, JobState.WRITE_INTENT_RECORDED}:
                    if job.attempt >= job.max_attempts:
                        self._move(job, JobState.DEAD_LETTER)
                        job.last_error_code = "lease_expired_attempt_limit"
                    else:
                        self._move(job, JobState.RETRY_WAIT)
                        job.next_attempt_at = timestamp(current)
                        job.attempt_started_at = None
                        job.last_error_code = "lease_expired_before_dispatch"
                else:
                    continue
                self._leases.pop(job.job_id, None)
                count += 1
                self._audit("lease_reclaimed", job, count=count)
            return count

    def claim_job(self, worker_id: str, *, lease_seconds: int = 600, now: datetime | None = None) -> JobRecord | None:
        if not worker_id or any(character.isspace() for character in worker_id):
            raise RepositoryError("worker_id_invalid")
        current = utc_now(now)
        with self._lock:
            self._assert_kill_switch_clear()
            self.reclaim_expired(now=current)
            if self._active_writer_holds():
                return None
            if self._active_leases(current):
                return None
            jobs = [job for job in self._jobs.values() if job.state in {JobState.QUEUED, JobState.RETRY_WAIT} and (not job.next_attempt_at or parse_timestamp(job.next_attempt_at) <= current) and job.attempt < job.max_attempts]
            if not jobs:
                return None
            job = sorted(jobs, key=lambda item: (item.created_at, item.job_id))[0]
            self._move(job, JobState.LEASED)
            job.attempt += 1
            job.attempt_started_at = timestamp(current)
            job.lease_owner = worker_id
            job.lease_expires_at = timestamp(current + timedelta(seconds=lease_seconds))
            job.next_attempt_at = None
            self._leases[job.job_id] = LeaseRecord(job.job_id, worker_id, job.lease_expires_at, job.state_version)
            self._audit("job_claimed", job)
            return self._copy(job)

    def heartbeat(self, job_id: str, worker_id: str, *, expected_state_version: int, lease_seconds: int = 600, now: datetime | None = None) -> LeaseRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            if job.state_version != expected_state_version:
                raise LeaseConflict("state_version_mismatch")
            self._lease(job, worker_id, current)
            job.state_version += 1
            expires = timestamp(current + timedelta(seconds=lease_seconds))
            job.lease_expires_at = expires
            lease = LeaseRecord(job_id, worker_id, expires, job.state_version)
            self._leases[job_id] = lease
            return lease

    def begin_prechecking(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> JobRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            self._lease(job, worker_id, current)
            if job.state == JobState.LEASED:
                self._move(job, JobState.PRECHECKING)
                if job.allocation_member_no is not None:
                    self._move(job, JobState.ALLOCATION_BOUND)
            elif job.state != JobState.PRECHECKING:
                raise RepositoryError("prechecking_state_invalid")
            return self._copy(job)

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._copy(self._job(job_id))

    def assert_lease(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> None:
        current = utc_now(now)
        with self._lock:
            self._lease(self._job(job_id), worker_id, current)

    def rate_allowed(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> bool:
        """Return true only when the durable singleton lease is current."""
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            try:
                self._lease(job, worker_id, current)
            except LeaseConflict:
                return False
            active = self._active_leases(current)
            return (
                len(active) == 1
                and job.state not in TERMINAL_STATES
                and 0 < job.attempt <= job.max_attempts
                and job.attempt_started_at is not None
            )

    def record_probe(self, job_id: str, candidate: str, status: ProbeStatus, probe_reference: str, worker_id: str, *, now: datetime | None = None) -> AllocationProbe:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            if job.dispatch_fenced or job.state not in {JobState.PRECHECKING, JobState.ALLOCATION_BOUND}:
                raise AllocationConflict("probe_state_invalid")
            self._assert_no_active_writer_hold()
            self._lease(job, worker_id, current)
            probe = AllocationProbe(job_id, candidate, ProbeStatus(status), probe_reference, timestamp(current))
            self._probes[job_id].append(probe)
            return probe

    def get_probes(self, job_id: str) -> tuple[AllocationProbe, ...]:
        with self._lock:
            self._job(job_id)
            return tuple(self._copy(self._probes[job_id]))

    def bind_allocation(self, job_id: str, member_no: str, probe_reference: str, worker_id: str, *, now: datetime | None = None) -> AllocationRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            if job.dispatch_fenced:
                raise AllocationConflict("allocation_after_dispatch_fence")
            self._assert_no_active_writer_hold()
            self._lease(job, worker_id, current)
            existing = self._allocations.get(job_id)
            if existing is not None:
                if existing.member_no != member_no or existing.probe_reference != probe_reference:
                    raise AllocationConflict("job_allocation_already_bound")
                return self._copy(existing)
            owner = self._allocations_by_member.get(member_no)
            if owner is not None and owner != job_id:
                raise AllocationConflict("member_no_allocation_race")
            if not any(p.candidate == member_no and p.status == ProbeStatus.FREE and p.probe_reference == probe_reference for p in self._probes[job_id]):
                raise AllocationConflict("positive_free_probe_required")
            if job.state != JobState.PRECHECKING:
                raise AllocationConflict("allocation_requires_prechecking")
            allocation = AllocationRecord(job_id, job.response_id, member_no, probe_reference, timestamp(current))
            self._allocations[job_id] = allocation
            self._allocations_by_member[member_no] = job_id
            job.allocation_member_no = member_no
            job.allocation_probe_reference = probe_reference
            self._move(job, JobState.ALLOCATION_BOUND)
            self._audit("allocation_bound", job)
            return self._copy(allocation)

    def recheck_bound_allocation(self, job_id: str, worker_id: str, status: ProbeStatus, probe_reference: str, *, now: datetime | None = None) -> JobRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            self._assert_no_active_writer_hold()
            self._lease(job, worker_id, current)
            allocation = self._allocations.get(job_id)
            if allocation is None or job.state != JobState.ALLOCATION_BOUND:
                raise AllocationConflict("bound_allocation_required")
            status = ProbeStatus(status)
            prior = self._rechecks.get(job_id, [])
            current_prior = next(
                (
                    item for item in reversed(prior)
                    if item.attempt == job.attempt
                    and item.worker_id == worker_id
                    and item.member_no == allocation.member_no
                ),
                None,
            )
            if prior:
                latest = prior[-1]
                if (
                    latest.attempt == job.attempt
                    and latest.worker_id == worker_id
                    and latest.member_no == allocation.member_no
                    and latest.status == status
                    and latest.probe_reference == probe_reference
                ):
                    return self._copy(job)
            recheck = AllocationRecheck(
                recheck_id=f"recheck-{uuid.uuid4().hex}",
                job_id=job_id,
                attempt=job.attempt,
                worker_id=worker_id,
                member_no=allocation.member_no,
                status=status,
                probe_reference=probe_reference,
                observed_at=timestamp(current),
            )
            self._rechecks.setdefault(job_id, []).append(recheck)
            if status == ProbeStatus.FREE and probe_reference != allocation.probe_reference and current_prior is None:
                return self._copy(job)
            self._probes[job_id].append(AllocationProbe(job_id, allocation.member_no, status, probe_reference, timestamp(current)))
            self._move(job, JobState.MANUAL_REVIEW)
            job.last_error_code = "bound_member_no_recheck_not_free"
            self._audit("allocation_recheck_failed", job, error_code=job.last_error_code)
            return self._copy(job)

    def get_allocation(self, job_id: str) -> AllocationRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._allocations.get(job_id))

    def get_fresh_recheck(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> AllocationRecheck | None:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            self._lease(job, worker_id, current)
            return self._copy(self._fresh_recheck(job, worker_id, current))

    def record_write_intent(self, job_id: str, member_no: str, operation: str, payload_hash_value: str, worker_id: str, *, now: datetime | None = None) -> WriteIntentRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            if job.dispatch_fenced:
                raise RepositoryError("write_intent_state_invalid")
            self._assert_no_active_writer_hold()
            self._lease(job, worker_id, current)
            if operation != "member.create" or job.operation != operation:
                raise RepositoryError("operation_invalid")
            allocation = self._allocations.get(job_id)
            if allocation is None or allocation.member_no != member_no:
                raise RepositoryError("allocation_binding_required")
            fresh = self._fresh_recheck(job, worker_id, current)
            if fresh is None:
                raise RepositoryError("fresh_bound_member_no_recheck_required")
            existing = self._write_intents.get(job_id)
            if existing is not None:
                if existing.member_no != member_no or existing.payload_hash != payload_hash_value:
                    raise SourceConflict("write_intent_payload_conflict")
                if existing.recheck_id != fresh.recheck_id:
                    existing = replace(existing, recheck_id=fresh.recheck_id)
                    self._write_intents[job_id] = existing
                if job.state == JobState.ALLOCATION_BOUND:
                    self._move(job, JobState.WRITE_INTENT_RECORDED)
                return self._copy(existing)
            if job.state != JobState.ALLOCATION_BOUND:
                raise RepositoryError("write_intent_state_invalid")
            intent = WriteIntentRecord(job_id, f"intent-{uuid.uuid4().hex}", member_no, payload_hash_value, timestamp(current), fresh.recheck_id)
            self._write_intents[job_id] = intent
            job.write_intent_id = intent.intent_id
            self._move(job, JobState.WRITE_INTENT_RECORDED)
            self._audit("write_intent_recorded", job)
            return self._copy(intent)

    def get_write_intent(self, job_id: str) -> WriteIntentRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._write_intents.get(job_id))

    def record_dispatch_fence(self, job_id: str, member_no: str, operation: str, worker_id: str, *, host_binding: str | None = None, now: datetime | None = None) -> DispatchFenceRecord:
        current = utc_now(now)
        with self._lock:
            self._assert_kill_switch_clear()
            job = self._job(job_id)
            self._lease(job, worker_id, current)
            existing = self._fences.get(job_id)
            if existing is not None:
                if existing.member_no != member_no or existing.operation != operation:
                    raise AllocationConflict("dispatch_fence_binding_invalid")
                return self._copy(existing)
            self._assert_no_active_writer_hold()
            intent = self._write_intents.get(job_id)
            fresh = self._fresh_recheck(job, worker_id, current)
            if (
                intent is None or intent.member_no != member_no or operation != "member.create"
                or job.state != JobState.WRITE_INTENT_RECORDED or job.save_invocation_count != 0
                or fresh is None or intent.recheck_id != fresh.recheck_id
            ):
                raise RepositoryError("write_intent_required")
            resolved_host = host_binding or f"host-{worker_id}"
            if not re.fullmatch(r"host-[A-Za-z0-9._:-]{1,120}", resolved_host):
                raise WriterTerminationConflict("worker_host_binding_invalid")
            fence = DispatchFenceRecord(job_id, new_fence_id(), member_no, operation, timestamp(current), fresh.recheck_id, f"exec-{uuid.uuid4().hex}")
            self._fences[job_id] = fence
            job.dispatch_fence_id = fence.fence_id
            self._move(job, JobState.WRITING)
            self._set_hold(WriterExecutionHold(
                hold_id=f"hold-{uuid.uuid4().hex}", job_id=job_id, fence_id=fence.fence_id,
                attempt=job.attempt, worker_session=worker_id, host_binding=resolved_host,
                execution_id=fence.execution_id or f"exec-{uuid.uuid4().hex}", member_no=member_no,
                state=WriterHoldState.PENDING, state_version=0, pid=None, process_start_time=None,
                evidence_type=None, evidence_reference=None, created_at=timestamp(current), updated_at=timestamp(current),
            ), job)
            self._audit("dispatch_fence_created", job)
            return self._copy(fence)

    def get_dispatch_fence(self, job_id: str) -> DispatchFenceRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._fences.get(job_id))

    def get_writer_execution_hold(self, job_id: str) -> WriterExecutionHold | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._writer_holds.get(job_id))

    def assert_no_active_writer_hold(self) -> None:
        with self._lock:
            self._assert_no_active_writer_hold()

    def _check_writer_binding(
        self,
        job: JobRecord,
        hold: WriterExecutionHold,
        *,
        fence_id: str,
        attempt: int,
        worker_session: str,
        host_binding: str,
        execution_id: str,
    ) -> None:
        if (
            hold.fence_id != fence_id or hold.attempt != attempt
            or hold.worker_session != worker_session or hold.host_binding != host_binding
            or hold.execution_id != execution_id or job.dispatch_fence_id != fence_id
        ):
            raise WriterTerminationConflict("writer_termination_binding_invalid")

    def register_writer_execution(
        self,
        job_id: str,
        *,
        fence_id: str,
        attempt: int,
        worker_session: str,
        host_binding: str,
        execution_id: str,
        pid: int,
        process_start_time: str,
        now: datetime | None = None,
    ) -> WriterExecutionHold:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            hold = self._writer_holds.get(job_id)
            if hold is None:
                raise WriterTerminationConflict("writer_termination_hold_missing")
            self._check_writer_binding(job, hold, fence_id=fence_id, attempt=attempt, worker_session=worker_session, host_binding=host_binding, execution_id=execution_id)
            self._validate_process_identity(pid, process_start_time)
            if hold.state == WriterHoldState.REGISTERED:
                if hold.pid != pid or hold.process_start_time != process_start_time:
                    raise WriterTerminationConflict("writer_process_identity_mismatch")
                return self._copy(hold)
            if hold.state != WriterHoldState.PENDING:
                raise WriterTerminationConflict("writer_termination_clearance_rejected")
            self._lease(job, worker_session, current)
            updated = replace(
                hold, state=WriterHoldState.REGISTERED, state_version=hold.state_version + 1,
                pid=pid, process_start_time=process_start_time, updated_at=timestamp(current),
                registered_at=timestamp(current),
            )
            self._set_hold(updated, job)
            self._audit("writer_execution_registered", job)
            return self._copy(updated)

    def confirm_writer_termination(
        self,
        job_id: str,
        *,
        fence_id: str,
        attempt: int,
        worker_session: str,
        host_binding: str,
        execution_id: str,
        pid: int,
        process_start_time: str,
        evidence_type: str,
        evidence_reference: str,
        exit_code: int,
        now: datetime | None = None,
    ) -> WriterExecutionHold:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            hold = self._writer_holds.get(job_id)
            if hold is None:
                raise WriterTerminationConflict("writer_termination_hold_missing")
            self._check_writer_binding(job, hold, fence_id=fence_id, attempt=attempt, worker_session=worker_session, host_binding=host_binding, execution_id=execution_id)
            self._validate_process_identity(pid, process_start_time)
            self._validate_evidence(evidence_type, evidence_reference, exit_code)
            if hold.state == WriterHoldState.TERMINATION_CONFIRMED:
                if hold.pid != pid or hold.process_start_time != process_start_time:
                    raise WriterTerminationConflict("writer_process_identity_mismatch")
                return self._copy(hold)
            if hold.state != WriterHoldState.REGISTERED:
                raise WriterTerminationConflict("writer_termination_proof_required")
            if hold.pid != pid or hold.process_start_time != process_start_time:
                raise WriterTerminationConflict("writer_process_identity_mismatch")
            self._lease(job, worker_session, current)
            updated = replace(
                hold, state=WriterHoldState.TERMINATION_CONFIRMED, state_version=hold.state_version + 1,
                evidence_type=evidence_type, evidence_reference=evidence_reference,
                updated_at=timestamp(current), termination_confirmed_at=timestamp(current),
            )
            self._set_hold(updated, job)
            self._audit("writer_termination_confirmed", job)
            return self._copy(updated)

    def quarantine_writer_execution(
        self,
        job_id: str,
        *,
        fence_id: str,
        attempt: int,
        worker_session: str,
        host_binding: str,
        execution_id: str,
        pid: int | None = None,
        process_start_time: str | None = None,
        evidence_reference: str,
        reason: str = "writer_termination_unconfirmed",
        now: datetime | None = None,
    ) -> WriterExecutionHold:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            hold = self._writer_holds.get(job_id)
            if hold is None:
                raise WriterTerminationConflict("writer_termination_hold_missing")
            self._check_writer_binding(job, hold, fence_id=fence_id, attempt=attempt, worker_session=worker_session, host_binding=host_binding, execution_id=execution_id)
            if hold.state == WriterHoldState.CLEARED or hold.state == WriterHoldState.TERMINATION_CONFIRMED:
                raise WriterTerminationConflict("writer_termination_clearance_rejected")
            if (pid is None) != (process_start_time is None):
                raise WriterTerminationConflict("writer_process_identity_invalid")
            if pid is not None and process_start_time is not None:
                self._validate_process_identity(pid, process_start_time)
            if not isinstance(evidence_reference, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", evidence_reference):
                raise WriterTerminationConflict("writer_quarantine_evidence_invalid")
            if hold.state in {WriterHoldState.REGISTERED, WriterHoldState.QUARANTINED}:
                if pid is not None and (hold.pid != pid or hold.process_start_time != process_start_time):
                    raise WriterTerminationConflict("writer_process_identity_mismatch")
                pid, process_start_time = hold.pid, hold.process_start_time
            if hold.state == WriterHoldState.QUARANTINED:
                return self._copy(hold)
            updated = replace(
                hold, state=WriterHoldState.QUARANTINED, state_version=hold.state_version + 1,
                pid=pid if pid is not None else hold.pid, process_start_time=process_start_time if process_start_time is not None else hold.process_start_time,
                evidence_type="quarantine", evidence_reference=evidence_reference,
                updated_at=timestamp(current), quarantined_at=timestamp(current),
            )
            self._set_hold(updated, job)
            if job.state != JobState.WRITER_TERMINATION_UNCONFIRMED:
                self._move(job, JobState.WRITER_TERMINATION_UNCONFIRMED)
            job.last_error_code = reason if re.fullmatch(r"[a-z0-9_.:-]{1,80}", reason) else "writer_termination_unconfirmed"
            self._audit("writer_termination_quarantined", job, error_code=job.last_error_code)
            return self._copy(updated)

    def recover_writer_termination(
        self,
        job_id: str,
        *,
        fence_id: str,
        attempt: int,
        recovery_session: str,
        host_binding: str,
        execution_id: str,
        pid: int,
        process_start_time: str,
        evidence_reference: str,
        exit_code: int,
        now: datetime | None = None,
    ) -> tuple[ResultRecord, bool]:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            existing = self._results.get(job_id)
            hold = self._writer_holds.get(job_id)
            if hold is not None and hold.state == WriterHoldState.CLEARED and existing is not None and existing.status == ResultStatus.WRITE_OUTCOME_UNCERTAIN:
                return self._copy(existing), True
            if hold is None or hold.state != WriterHoldState.QUARANTINED:
                raise WriterTerminationConflict("writer_termination_clearance_rejected")
            if hold.worker_session == recovery_session:
                raise WriterTerminationConflict("stale_worker_session")
            self._check_writer_binding(job, hold, fence_id=fence_id, attempt=attempt, worker_session=hold.worker_session or "", host_binding=host_binding, execution_id=execution_id)
            if hold.host_binding != host_binding or hold.execution_id != execution_id or hold.fence_id != fence_id or hold.attempt != attempt or hold.pid != pid or hold.process_start_time != process_start_time:
                raise WriterTerminationConflict("writer_termination_binding_invalid")
            self._validate_process_identity(pid, process_start_time)
            self._validate_evidence("process_exit", evidence_reference, exit_code)
            lease = self._leases.get(job_id)
            if lease is not None and parse_timestamp(lease.expires_at) > current:
                raise WriterTerminationConflict("writer_termination_clearance_rejected")
            confirmed = replace(
                hold, state=WriterHoldState.TERMINATION_CONFIRMED, state_version=hold.state_version + 1,
                pid=pid, process_start_time=process_start_time, evidence_type="termination_recovery",
                evidence_reference=evidence_reference, updated_at=timestamp(current),
                termination_confirmed_at=timestamp(current),
            )
            self._set_hold(confirmed, job)
            return self._copy(self._settle_confirmed_termination(job, current, error_code="termination_recovered_result_unknown")), False

    def open_reconciliation_case(self, job_id: str, member_no: str, *, now: datetime | None = None) -> ReconciliationCaseRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            hold = self._writer_holds.get(job_id)
            if any(item.active for item in self._writer_holds.values()):
                raise WriterTerminationConflict("writer_termination_quarantine_active")
            if hold is None or hold.state != WriterHoldState.CLEARED or hold.termination_confirmed_at is None:
                raise WriterTerminationConflict("writer_termination_proof_required")
            if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
                raise RepositoryError("reconciliation_requires_uncertain_state")
            allocation = self._allocations.get(job_id)
            fence = self._fences.get(job_id)
            if allocation is None or fence is None or allocation.member_no != member_no or fence.member_no != member_no:
                raise RepositoryError("reconciliation_member_binding_invalid")
            lease = self._leases.get(job_id)
            if lease is not None:
                if parse_timestamp(lease.expires_at) > current:
                    raise LeaseConflict("reconciliation_writer_lease_active")
                self._leases.pop(job_id, None)
                job.lease_owner = None
                job.lease_expires_at = None
            existing = next((case for case in self._reconciliation_cases.values() if case.job_id == job_id), None)
            if existing is not None:
                if existing.state != ReconciliationCaseState.OPEN:
                    raise RepositoryError("reconciliation_case_closed")
                return self._copy(existing)
            case = ReconciliationCaseRecord(
                case_id=f"case-{uuid.uuid4().hex}",
                job_id=job_id,
                member_no=member_no,
                state=ReconciliationCaseState.OPEN,
                opened_at=timestamp(current),
            )
            self._reconciliation_cases[case.case_id] = case
            self._reconciliation_checks[case.case_id] = []
            self._audit("reconciliation_case_opened", job)
            return self._copy(case)

    def record_reconciliation_check(
        self,
        case_id: str,
        lookup_status: str,
        readback_found: bool,
        readback_match: bool,
        *,
        now: datetime | None = None,
    ) -> ReconciliationCheckRecord:
        current = utc_now(now)
        with self._lock:
            case = self._reconciliation_cases.get(case_id)
            if case is None:
                raise RepositoryError("reconciliation_case_required")
            if case.state != ReconciliationCaseState.OPEN:
                raise RepositoryError("reconciliation_case_not_open")
            job = self._job(case.job_id)
            hold = self._writer_holds.get(case.job_id)
            if hold is None or hold.state != WriterHoldState.CLEARED or hold.termination_confirmed_at is None:
                raise WriterTerminationConflict("writer_termination_proof_required")
            if any(item.active for item in self._writer_holds.values()):
                raise WriterTerminationConflict("writer_termination_quarantine_active")
            if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
                raise RepositoryError("reconciliation_requires_uncertain_state")
            lease = self._leases.get(job.job_id)
            if lease is not None and parse_timestamp(lease.expires_at) > current:
                raise LeaseConflict("reconciliation_writer_lease_active")
            if lease is not None:
                self._leases.pop(job.job_id, None)
                job.lease_owner = None
                job.lease_expires_at = None
            if lookup_status not in {"exact_match", "absent", "mismatch", "ambiguous"}:
                raise RepositoryError("reconciliation_status_invalid")
            if not isinstance(readback_found, bool) or not isinstance(readback_match, bool):
                raise RepositoryError("reconciliation_flags_invalid")
            required_flags = {
                "exact_match": (True, True),
                "absent": (False, False),
                "mismatch": (True, False),
            }
            if lookup_status in required_flags and (readback_found, readback_match) != required_flags[lookup_status]:
                raise RepositoryError("reconciliation_flags_invalid")
            if lookup_status == "ambiguous" and readback_match:
                raise RepositoryError("reconciliation_flags_invalid")
            state = {
                "exact_match": ReconciliationCaseState.EXACT_MATCH,
                "absent": ReconciliationCaseState.ABSENT,
                "mismatch": ReconciliationCaseState.MISMATCH,
                "ambiguous": ReconciliationCaseState.AMBIGUOUS,
            }[lookup_status]
            check = ReconciliationCheckRecord(
                check_id=f"check-{uuid.uuid4().hex}",
                case_id=case_id,
                lookup_status=lookup_status,
                readback_found=readback_found,
                readback_match=readback_match,
                checked_at=timestamp(current),
            )
            self._reconciliation_checks.setdefault(case_id, []).append(check)
            self._reconciliation_cases[case_id] = replace(
                case,
                state=state,
                closed_at=timestamp(current) if state in {ReconciliationCaseState.EXACT_MATCH, ReconciliationCaseState.ABSENT} else None,
            )
            return self._copy(check)

    def get_reconciliation_case(self, case_id: str) -> ReconciliationCaseRecord:
        with self._lock:
            case = self._reconciliation_cases.get(case_id)
            if case is None:
                raise RepositoryError("reconciliation_case_required")
            return self._copy(case)

    def get_reconciliation_checks(self, case_id: str) -> tuple[ReconciliationCheckRecord, ...]:
        with self._lock:
            if case_id not in self._reconciliation_cases:
                raise RepositoryError("reconciliation_case_required")
            return tuple(self._copy(self._reconciliation_checks.get(case_id, [])))

    def _require_reconciliation_evidence(self, result: ResultRecord, case_id: str) -> None:
        case = self._reconciliation_cases.get(case_id)
        if case is None or case.job_id != result.job_id or case.member_no != result.member_no:
            raise RepositoryError("reconciliation_case_required")
        expected = {
            ResultStatus.CREATED_VERIFIED: (ReconciliationCaseState.EXACT_MATCH, "exact_match"),
            ResultStatus.CONFIRMED_NOT_CREATED: (ReconciliationCaseState.ABSENT, "absent"),
            ResultStatus.CREATED_READBACK_MISMATCH: (ReconciliationCaseState.MISMATCH, "mismatch"),
            ResultStatus.WRITE_OUTCOME_UNCERTAIN: (ReconciliationCaseState.AMBIGUOUS, "ambiguous"),
        }[result.status]
        checks = self._reconciliation_checks.get(case_id, [])
        if case.state != expected[0] or not checks or checks[-1].lookup_status != expected[1]:
            raise RepositoryError("reconciliation_check_required")
        check = checks[-1]
        if (check.readback_found, check.readback_match) != (result.readback_found, result.readback_match):
            raise RepositoryError("reconciliation_check_conflict")

    def acknowledge_result(self, result: ResultRecord, *, worker_id: str | None = None, require_lease: bool = True, reconciliation_case_id: str | None = None, now: datetime | None = None) -> tuple[ResultRecord, bool]:
        current = utc_now(now)
        with self._lock:
            job = self._job(result.job_id)
            fence = self._fences.get(result.job_id)
            if fence is None or fence.fence_id != result.dispatch_fence_id or fence.member_no != result.member_no:
                raise RepositoryError("dispatch_fence_binding_invalid")
            if result.save_invocation_count != 1:
                raise ResultConflict("save_invocation_count_must_be_one")
            existing = self._results.get(result.job_id)
            if existing is not None and existing.result_hash == result.result_hash:
                if not require_lease:
                    if reconciliation_case_id is None:
                        raise RepositoryError("reconciliation_case_required")
                    self._require_reconciliation_evidence(result, reconciliation_case_id)
                if existing.status == ResultStatus.CREATED_VERIFIED:
                    self._verify_welcome_outbox(job)
                return self._copy(existing), True
            reconciliation_projection = False
            if existing is not None:
                if require_lease or existing.status != ResultStatus.WRITE_OUTCOME_UNCERTAIN:
                    self._result_conflicts.append({"job_id": result.job_id, "code": "result_payload_conflict"})
                    raise ResultConflict("result_payload_conflict")
                reconciliation_projection = True
            hold = self._writer_holds.get(result.job_id)
            if require_lease and (hold is None or not hold.termination_confirmed):
                raise WriterTerminationConflict("writer_termination_proof_required")
            if not require_lease and (hold is None or hold.state != WriterHoldState.CLEARED or hold.termination_confirmed_at is None):
                raise WriterTerminationConflict("writer_termination_proof_required")
            if require_lease:
                if not worker_id:
                    raise LeaseConflict("worker_identity_required")
                self._lease(job, worker_id, current)
            else:
                if not reconciliation_projection:
                    raise ResultConflict("reconciliation_projection_missing")
                if reconciliation_case_id is None:
                    raise RepositoryError("reconciliation_case_required")
                self._require_reconciliation_evidence(result, reconciliation_case_id)
                current_projection = self._results.get(result.job_id)
                if (
                    current_projection is None
                    or current_projection.result_hash != existing.result_hash
                    or current_projection.status != ResultStatus.WRITE_OUTCOME_UNCERTAIN
                ):
                    self._result_conflicts.append({"job_id": result.job_id, "code": "reconciliation_projection_stale"})
                    raise ResultConflict("reconciliation_projection_stale")
            if reconciliation_projection and result.status == ResultStatus.WRITE_OUTCOME_UNCERTAIN:
                return self._copy(existing), True
            if result.status == ResultStatus.CREATED_VERIFIED and job.state == JobState.WRITING:
                self._move(job, JobState.READBACK)
            target = {
                ResultStatus.CREATED_VERIFIED: JobState.CREATED_VERIFIED,
                ResultStatus.WRITE_OUTCOME_UNCERTAIN: JobState.WRITE_OUTCOME_UNCERTAIN,
                ResultStatus.CONFIRMED_NOT_CREATED: JobState.CONFIRMED_NOT_CREATED,
                ResultStatus.CREATED_READBACK_MISMATCH: JobState.CREATED_READBACK_MISMATCH,
            }[result.status]
            if job.state != target:
                self._move(job, target)
            job.result_status = result.status
            job.save_invocation_count = result.save_invocation_count
            job.last_error_code = result.error_code
            self._result_history.setdefault(result.job_id, []).append(self._copy(result))
            self._results[result.job_id] = self._copy(result)
            if result.status == ResultStatus.CREATED_VERIFIED:
                # Atomic with the positive result, on both the leased worker
                # path and the exact-match reconciliation projection.
                self._create_welcome_outbox(job, current)
            if require_lease:
                confirmed = self._writer_holds.get(result.job_id)
                if confirmed is None or not confirmed.termination_confirmed:
                    raise WriterTerminationConflict("writer_termination_proof_required")
                self._set_hold(replace(
                    confirmed, state=WriterHoldState.CLEARED, state_version=confirmed.state_version + 1,
                    updated_at=timestamp(current), cleared_at=timestamp(current),
                ), job)
                self._leases.pop(result.job_id, None)
                job.lease_owner = None
                job.lease_expires_at = None
            self._audit("result_acknowledged", job, error_code=result.error_code)
            return self._copy(result), False

    def _create_welcome_outbox(self, job: JobRecord, current: datetime) -> WelcomeEmailOutbox:
        """Called only inside acknowledge_result for CREATED_VERIFIED."""

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

    def get_result(self, job_id: str) -> ResultRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._results.get(job_id))

    def mark_state(self, job_id: str, target: JobState, *, worker_id: str | None = None, require_lease: bool = False, error_code: str | None = None, now: datetime | None = None) -> JobRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            if require_lease:
                if worker_id is None:
                    raise LeaseConflict("worker_identity_required")
                self._lease(job, worker_id, current)
            if job.dispatch_fenced and JobState(target) == JobState.WRITER_TERMINATION_UNCONFIRMED:
                self._quarantine_hold(job, current, error_code=error_code or "writer_termination_unconfirmed")
                return self._copy(job)
            if job.dispatch_fenced and JobState(target) == JobState.WRITE_OUTCOME_UNCERTAIN:
                hold = self._writer_holds.get(job_id)
                if hold is None or not hold.termination_confirmed:
                    self._quarantine_hold(job, current, error_code=error_code or "writer_termination_unconfirmed")
                    return self._copy(job)
                self._settle_confirmed_termination(job, current, error_code=error_code or "writer_termination_unconfirmed")
                return self._copy(job)
            if job.dispatch_fenced and JobState(target) in {
                JobState.CREATED_VERIFIED,
                JobState.CONFIRMED_NOT_CREATED,
                JobState.CREATED_READBACK_MISMATCH,
            }:
                raise WriterTerminationConflict("writer_termination_proof_required")
            self._move(job, JobState(target))
            if error_code:
                job.last_error_code = error_code
            return self._copy(job)

    def probe_member_no(self, candidate: str) -> ProbeStatus:
        with self._lock:
            if candidate in self._member_records:
                return ProbeStatus.OCCUPIED
            return self._probe_status.get(candidate, ProbeStatus.AMBIGUOUS)

    def set_probe_status(self, candidate: str, status: ProbeStatus) -> None:
        with self._lock:
            self._probe_status[candidate] = ProbeStatus(status)

    def set_member_record(self, member_no: str, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._member_records[member_no] = self._copy(dict(record))

    def member_record(self, member_no: str) -> Mapping[str, Any] | None:
        with self._lock:
            return self._copy(self._member_records.get(member_no))

    def all_jobs(self) -> tuple[JobRecord, ...]:
        with self._lock:
            return tuple(self._copy(job) for job in self._jobs.values())

    def result_history(self, job_id: str) -> tuple[ResultRecord, ...]:
        with self._lock:
            self._job(job_id)
            return tuple(self._copy(item) for item in self._result_history.get(job_id, []))

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


class PostgresRepository:
    """PostgreSQL implementation of the same bounded repository contract."""

    _JOB_SELECT = """
        SELECT j.job_id,COALESCE(obs.request_id,''),sr.source_response_ref,j.response_id,
               j.payload_hash,j.operation,j.canonical_payload,j.created_at,j.state,
               j.state_version,j.attempt_count,j.max_attempts,j.next_attempt_at,
               j.lease_owner,j.lease_expires_at,j.allocation_member_no,
               j.allocation_probe_reference,j.write_intent_id,j.dispatch_fence_id,
               j.save_invocation_count,j.result_status,j.last_error_code,
               'google_forms',COALESCE(obs.form_alias,''),COALESCE(obs.mapping_version,''),j.attempt_started_at,
               hold.lifecycle
        FROM xb_member_gateway.jobs j
        JOIN xb_member_gateway.source_responses sr ON sr.response_id=j.response_id
        LEFT JOIN LATERAL (
            SELECT request_id,form_alias,mapping_version FROM xb_member_gateway.source_observations
            WHERE response_id=j.response_id ORDER BY observed_at DESC,observation_id DESC LIMIT 1
        ) obs ON TRUE
        LEFT JOIN LATERAL (
            SELECT lifecycle FROM xb_member_gateway.writer_execution_holds
            WHERE job_id=j.job_id ORDER BY hold_id DESC LIMIT 1
        ) hold ON TRUE
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
            row[0],row[1],row[2],row[3],row[4],row[5],dict(row[6]),cls._dt(row[7]) or "",
            state=JobState(row[8]),state_version=int(row[9]),attempt=int(row[10]),max_attempts=int(row[11]),
            next_attempt_at=cls._dt(row[12]),lease_owner=row[13],lease_expires_at=cls._dt(row[14]),
            allocation_member_no=row[15],allocation_probe_reference=row[16],write_intent_id=row[17],
            dispatch_fence_id=public_fence_id(row[18]) if row[18] is not None else None,save_invocation_count=int(row[19]),
            result_status=ResultStatus(row[20]) if row[20] else None,last_error_code=row[21],
            source_system=row[22],form_alias=row[23],mapping_version=row[24],
            attempt_started_at=cls._dt(row[25]),
            writer_termination_state=str(row[26]) if len(row) > 26 and row[26] else None,
        )

    def _select_job(self, cursor: Any, job_id: str, *, for_update: bool = False) -> JobRecord:
        cursor.execute(self._JOB_SELECT + (" FOR UPDATE OF j" if for_update else ""), (job_id,))
        row = cursor.fetchone()
        if row is None:
            raise JobNotFound("job_not_found")
        return self._from_row(row)

    def _require_lease(self, cursor: Any, job: JobRecord, worker_id: str, now: datetime) -> None:
        cursor.execute("SELECT worker_id,state_version,expires_at FROM xb_member_gateway.leases WHERE job_id=%s AND active=TRUE FOR UPDATE", (job.job_id,))
        row = cursor.fetchone()
        if row is None or row[0] != worker_id or int(row[1]) != job.state_version or parse_timestamp(row[2]) <= now:
            raise LeaseConflict("lease_not_owned_or_expired")

    def _lock_kill_switch(self, cursor: Any) -> None:
        """Lock the control row before any claim or irreversible fence mutation."""
        cursor.execute(
            "SELECT enabled FROM xb_member_gateway.control_flags "
            "WHERE flag_name='kill_switch_enabled' FOR UPDATE"
        )
        row = cursor.fetchone()
        if row is None:
            raise RepositoryError("kill_switch_state_unavailable")
        if bool(row[0]):
            raise RepositoryError("kill_switch_enabled")

    def _lock_writer_termination_gate(self, cursor: Any) -> None:
        cursor.execute(
            "SELECT state_version FROM xb_member_gateway.writer_termination_gate "
            "WHERE gate_id=1 FOR UPDATE"
        )
        if cursor.fetchone() is None:
            raise WriterTerminationConflict("writer_termination_gate_unavailable")

    def _bump_writer_termination_gate(self, cursor: Any) -> None:
        cursor.execute(
            "UPDATE xb_member_gateway.writer_termination_gate "
            "SET state_version=state_version+1,updated_at=now() WHERE gate_id=1"
        )

    def _assert_no_active_writer_hold_cursor(self, cursor: Any) -> None:
        cursor.execute(
            "SELECT 1 FROM xb_member_gateway.writer_execution_holds "
            "WHERE lifecycle <> 'CLEARED' LIMIT 1 FOR UPDATE"
        )
        if cursor.fetchone() is not None:
            raise WriterTerminationConflict("writer_termination_quarantine_active")

    @classmethod
    def _hold_from_row(cls, row: tuple[Any, ...]) -> WriterExecutionHold:
        return WriterExecutionHold(
            hold_id=str(row[0]), job_id=str(row[1]), fence_id=public_fence_id(row[2]),
            attempt=int(row[3]), worker_session=row[4], host_binding=row[5], execution_id=str(row[6]),
            member_no=row[7], state=WriterHoldState(row[8]), state_version=int(row[9]),
            pid=int(row[10]) if row[10] is not None else None, process_start_time=cls._dt(row[11]),
            evidence_type=row[12], evidence_reference=row[13], created_at=cls._dt(row[14]) or "",
            updated_at=cls._dt(row[15]) or "", registered_at=cls._dt(row[16]),
            termination_confirmed_at=cls._dt(row[17]), quarantined_at=cls._dt(row[18]),
            cleared_at=cls._dt(row[19]),
        )

    def _select_writer_hold(self, cursor: Any, job_id: str, *, for_update: bool = False) -> WriterExecutionHold | None:
        cursor.execute(
            "SELECT hold_id,job_id,fence_id,attempt_count,worker_session,host_binding,execution_id,member_no,"
            "lifecycle,state_version,process_pid,process_start_at,evidence_type,evidence_reference,created_at,"
            "updated_at,registered_at,termination_confirmed_at,quarantined_at,cleared_at "
            "FROM xb_member_gateway.writer_execution_holds WHERE job_id=%s" + (" FOR UPDATE" if for_update else ""),
            (job_id,),
        )
        row = cursor.fetchone()
        return None if row is None else self._hold_from_row(row)

    def _fresh_recheck_cursor(self, cursor: Any, job: JobRecord, worker_id: str, now: datetime) -> AllocationRecheck | None:
        latest = self._latest_recheck_cursor(cursor, job)
        if latest is None:
            return None
        if (
            job.state not in {JobState.ALLOCATION_BOUND, JobState.WRITE_INTENT_RECORDED}
            or latest.attempt != job.attempt
            or latest.worker_id != worker_id
            or latest.member_no != job.allocation_member_no
            or latest.status != ProbeStatus.FREE
            or latest.probe_reference == job.allocation_probe_reference
            or parse_timestamp(latest.observed_at) > now
        ):
            return None
        return latest

    def _latest_recheck_cursor(self, cursor: Any, job: JobRecord) -> AllocationRecheck | None:
        cursor.execute(
            "SELECT safe_reference,metadata,recorded_at FROM xb_member_gateway.audit_events "
            "WHERE job_id=%s AND event_type='allocation_recheck' "
            "ORDER BY audit_event_id DESC LIMIT 1",
            (job.job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        metadata = row[1]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                return None
        if not isinstance(metadata, Mapping):
            return None
        try:
            status = ProbeStatus(metadata["status"])
            attempt = int(metadata["attempt"])
            worker_id = str(metadata["worker_session"])
            probe_reference = str(metadata["probe_reference"])
        except (KeyError, TypeError, ValueError):
            return None
        return AllocationRecheck(
            str(row[0]), job.job_id, attempt, worker_id,
            job.allocation_member_no or "", status, probe_reference,
            self._dt(row[2]) or "",
        )

    def _advance(self, cursor: Any, job: JobRecord, target: JobState, now: datetime, values: Mapping[str, Any] | None = None) -> JobRecord:
        next_state(job.state, target, dispatch_fenced=job.dispatch_fenced)
        allowed = {"next_attempt_at","lease_owner","lease_expires_at","allocation_member_no","allocation_probe_reference","write_intent_id","dispatch_fence_id","last_error_code","result_status","save_invocation_count"}
        assignments = ["state=%s","state_version=state_version+1","updated_at=%s"]
        params: list[Any] = [target.value, now]
        for key, value in (values or {}).items():
            if key not in allowed:
                raise RepositoryError("job_update_field_invalid")
            assignments.append(f"{key}=%s")
            params.append(value)
        params.append(job.job_id)
        cursor.execute("UPDATE xb_member_gateway.jobs SET " + ",".join(assignments) + " WHERE job_id=%s", tuple(params))
        job.state = target
        job.state_version += 1
        for key, value in (values or {}).items():
            setattr(job, key, value)
        cursor.execute("UPDATE xb_member_gateway.leases SET state_version=%s WHERE job_id=%s AND active=TRUE", (job.state_version, job.job_id))
        return job

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
        "0004_forms_ingest_cursor", "0005_member_vertical_slice",
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
                # Historical success is never silently made email-eligible.
                cursor.execute(
                    "SELECT count(*) FROM xb_member_gateway.results r WHERE r.status='CREATED_VERIFIED' AND NOT EXISTS "
                    "(SELECT 1 FROM xb_member_gateway.welcome_email_outbox o WHERE o.job_id=r.job_id)"
                )
                if int(cursor.fetchone()[0]) != 0:
                    raise RepositoryError("created_verified_without_welcome_outbox")

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

    def operator_status(self) -> dict[str, Any]:
        current = utc_now()
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT flag_name,enabled FROM xb_member_gateway.control_flags WHERE flag_name IN ('production_activation_enabled','kill_switch_enabled')")
                controls = {str(row[0]): bool(row[1]) for row in cursor.fetchall()}
                cursor.execute("SELECT count(*) FROM xb_member_gateway.schema_migrations WHERE version IN ('0001_member_gateway','0002_result_event_history','0003_writer_termination_quarantine','0004_forms_ingest_cursor','0005_member_vertical_slice')")
                migrations_ready = int(cursor.fetchone()[0]) == 5
                cursor.execute("SELECT count(*) FROM xb_member_gateway.source_ingest_cursors")
                cursor_ready = int(cursor.fetchone()[0]) > 0
                cursor.execute("SELECT count(*) FROM xb_member_gateway.leases WHERE active=TRUE AND expires_at>%s", (current,))
                leases = int(cursor.fetchone()[0])
                cursor.execute("SELECT lifecycle FROM xb_member_gateway.writer_execution_holds WHERE lifecycle <> 'CLEARED'")
                holds = [str(row[0]) for row in cursor.fetchall()]
                cursor.execute("SELECT count(*) FROM xb_member_gateway.jobs WHERE state='WRITE_OUTCOME_UNCERTAIN'")
                uncertain = int(cursor.fetchone()[0])
                return {
                    "schema_version": "xb.member.gateway.operator_status.v1",
                    "activation_enabled": controls.get("production_activation_enabled", False),
                    "kill_switch_enabled": controls.get("kill_switch_enabled", True),
                    "migrations_ready": migrations_ready, "source_cursor_ready": cursor_ready,
                    "active_lease_count": leases, "writer_hold_active": bool(holds),
                    "writer_quarantined": "QUARANTINED" in holds,
                    "termination_proof_required": any(item in {"PENDING", "REGISTERED", "QUARANTINED"} for item in holds),
                    "uncertain_job_count": uncertain,
                }

    def operator_reconciliation(self, job_id: str) -> dict[str, Any]:
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
                    "result": None if result is None else {"status": str(result[1]), "result_hash": str(result[0]), "public_fence_reference": public_fence_id(result[2]), "acknowledged_at": self._dt(result[3])},
                    "reconciliation": None if case is None else {"case_state": str(case[1]), "opened_at": self._dt(case[2]), "closed_at": self._dt(case[3]), "check_state": None if check is None else str(check[0]), "checked_at": None if check is None else self._dt(check[1])},
                }

    def _reference_key_bytes(self) -> bytes:
        key = self._reference_key or os.environ.get("XB_MEMBER_GATEWAY_REFERENCE_HMAC_KEY", "").encode("utf-8")
        if not key:
            raise RepositoryError("source_reference_key_required")
        return key

    def ingest_source_event(
        self, event: SourceEvent, *, now: datetime | None = None,
        initial_window_max: int | None = 1,
    ) -> IngestOutcome:
        """Receipt-driven admission; no cross-response ordering authority."""

        source_ref = hmac_reference(event.response_id, self._reference_key_bytes())
        payload = dict(event.payload)
        create_time_utc = create_time_utc_from_exact(event.create_time)
        payload["create_time"] = render_create_time_utc(create_time_utc)
        created_at = utc_now(now)
        key = (event.source_system, event.form_alias, event.mapping_version)
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
                job_id = f"job-{uuid.uuid4().hex}"
                cursor.execute("INSERT INTO xb_member_gateway.ingest_receipts(request_id,response_id,payload_hash,replayed) VALUES(%s,%s,%s,FALSE)", (event.request_id,event.response_id,event.payload_hash))
                cursor.execute("INSERT INTO xb_member_gateway.jobs(job_id,response_id,operation,payload_hash,canonical_payload,state,state_version,attempt_count,max_attempts,created_at) VALUES(%s,%s,'member.create',%s,%s::jsonb,'QUEUED',2,0,3,%s)", (job_id,event.response_id,event.payload_hash,canonical_json(payload),created_at))
                cursor.execute(
                    "INSERT INTO xb_member_gateway.source_handling_receipts(response_id,source_response_ref,form_alias,form_id,mapping_version,create_time_exact,payload_fingerprint,outcome,job_id,rejection_id,receipt_version,recorded_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'ACCEPTED',%s,NULL,1,%s)",
                    (event.response_id, source_ref, event.form_alias, source_cursor.form_id, event.mapping_version, event.create_time, event.payload_hash, job_id, created_at),
                )
                return IngestOutcome(JobRecord(job_id,event.request_id,source_ref,event.response_id,event.payload_hash,event.operation,payload,timestamp(created_at),state=JobState.QUEUED,state_version=2,source_system=event.source_system,form_alias=event.form_alias,mapping_version=event.mapping_version), replayed=False)

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

    def get_job(self, job_id: str) -> JobRecord:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                return self._select_job(cursor, job_id)

    def assert_lease(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> None:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor, job_id, for_update=True)
                self._require_lease(cursor, job, worker_id, current)

    def rate_allowed(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> bool:
        """Use the current durable singleton lease as the only rate evidence."""
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor, job_id, for_update=True)
                try:
                    self._require_lease(cursor, job, worker_id, current)
                except LeaseConflict:
                    return False
                cursor.execute(
                    "SELECT count(*) FROM xb_member_gateway.leases "
                    "WHERE active=TRUE AND expires_at>%s",
                    (current,),
                )
                active_count = int(cursor.fetchone()[0])
                return (
                    active_count == 1
                    and job.state not in TERMINAL_STATES
                    and 0 < job.attempt <= job.max_attempts
                    and job.attempt_started_at is not None
                )

    @staticmethod
    def _result_from_row(job_id: str, row: tuple[Any, ...]) -> ResultRecord:
        return ResultRecord(
            job_id, row[0], ResultStatus(row[1]), row[2], public_fence_id(row[3]), int(row[4]),
            bool(row[5]), bool(row[6]), bool(row[7]), row[8], PostgresRepository._dt(row[9]) or "",
        )

    def _quarantine_hold_cursor(self, cursor: Any, job: JobRecord, current: datetime, error_code: str) -> WriterExecutionHold:
        hold = self._select_writer_hold(cursor, job.job_id, for_update=True)
        if hold is None:
            fence = self.get_dispatch_fence_cursor(cursor, job.job_id, job)
            if fence is None:
                raise WriterTerminationConflict("dispatch_fence_required")
            hold_id = str(uuid.uuid4())
            execution_id = fence.execution_id or f"legacy-execution-{uuid.uuid4().hex}"
            reference = f"legacy-{uuid.uuid4().hex}"
            cursor.execute(
                "INSERT INTO xb_member_gateway.writer_execution_holds(hold_id,job_id,fence_id,attempt_count,execution_id,member_no,lifecycle,state_version,evidence_type,evidence_reference,quarantined_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,'QUARANTINED',1,'legacy_unproven',%s,%s)",
                (hold_id, job.job_id, internal_fence_id(fence.fence_id), max(job.attempt, 1), execution_id, fence.member_no, reference, current),
            )
        elif hold.state == WriterHoldState.CLEARED:
            raise WriterTerminationConflict("writer_termination_clearance_rejected")
        elif hold.state != WriterHoldState.QUARANTINED:
            cursor.execute(
                "UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='QUARANTINED',state_version=state_version+1,evidence_type='quarantine',evidence_reference=%s,quarantined_at=%s,updated_at=%s WHERE job_id=%s",
                (f"quarantine-{uuid.uuid4().hex}", current, current, job.job_id),
            )
        if job.state != JobState.WRITER_TERMINATION_UNCONFIRMED:
            self._advance(cursor, job, JobState.WRITER_TERMINATION_UNCONFIRMED, current, {"last_error_code": error_code})
        else:
            cursor.execute("UPDATE xb_member_gateway.jobs SET last_error_code=%s,updated_at=%s WHERE job_id=%s", (error_code, current, job.job_id))
        job.writer_termination_state = WriterHoldState.QUARANTINED.value
        job.last_error_code = error_code
        self._bump_writer_termination_gate(cursor)
        return self._select_writer_hold(cursor, job.job_id, for_update=True)  # type: ignore[return-value]

    def get_dispatch_fence_cursor(self, cursor: Any, job_id: str, job: JobRecord | None = None) -> DispatchFenceRecord | None:
        cursor.execute("SELECT fence_id,member_no,operation,created_at FROM xb_member_gateway.dispatch_fences WHERE job_id=%s FOR UPDATE", (job_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        latest = self._latest_recheck_cursor(cursor, job or self._select_job(cursor, job_id))
        cursor.execute("SELECT execution_id FROM xb_member_gateway.writer_execution_holds WHERE job_id=%s", (job_id,))
        execution = cursor.fetchone()
        return DispatchFenceRecord(job_id, public_fence_id(row[0]), row[1], row[2], self._dt(row[3]) or "", latest.recheck_id if latest else None, execution[0] if execution else None)

    def _settle_confirmed_termination_cursor(self, cursor: Any, job: JobRecord, current: datetime, error_code: str) -> ResultRecord:
        hold = self._select_writer_hold(cursor, job.job_id, for_update=True)
        if hold is None or not hold.termination_confirmed:
            raise WriterTerminationConflict("writer_termination_proof_required")
        fence = self.get_dispatch_fence_cursor(cursor, job.job_id, job)
        if fence is None:
            raise WriterTerminationConflict("dispatch_fence_required")
        cursor.execute(
            "SELECT result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at FROM xb_member_gateway.results WHERE job_id=%s FOR UPDATE",
            (job.job_id,),
        )
        existing = cursor.fetchone()
        if existing is None:
            result = make_result(job=job, fence=fence, status=ResultStatus.WRITE_OUTCOME_UNCERTAIN, save_invocation_count=1, readback_found=False, readback_match=False, error_code=error_code, acknowledged_at=current)
            params = (result.job_id, result.result_hash, result.status.value, result.member_no, internal_fence_id(result.dispatch_fence_id), 1, False, False, True, result.error_code, current)
            cursor.execute("INSERT INTO xb_member_gateway.result_events(job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", params)
            cursor.execute(self._RESULTS_INSERT, params)
        else:
            result = self._result_from_row(job.job_id, existing)
            if result.status != ResultStatus.WRITE_OUTCOME_UNCERTAIN:
                raise ResultConflict("result_payload_conflict")
        if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
            self._advance(cursor, job, JobState.WRITE_OUTCOME_UNCERTAIN, current, {"result_status": ResultStatus.WRITE_OUTCOME_UNCERTAIN.value, "save_invocation_count": 1, "last_error_code": result.error_code})
        else:
            cursor.execute("UPDATE xb_member_gateway.jobs SET result_status='WRITE_OUTCOME_UNCERTAIN',save_invocation_count=1,last_error_code=%s,updated_at=%s WHERE job_id=%s", (result.error_code, current, job.job_id))
        cursor.execute("UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='CLEARED',state_version=state_version+1,cleared_at=%s,updated_at=%s WHERE job_id=%s AND lifecycle='TERMINATION_CONFIRMED'", (current, current, job.job_id))
        cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job.job_id,))
        cursor.execute("UPDATE xb_member_gateway.jobs SET lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE job_id=%s", (current, job.job_id))
        job.writer_termination_state = WriterHoldState.CLEARED.value
        job.result_status = ResultStatus.WRITE_OUTCOME_UNCERTAIN
        job.save_invocation_count = 1
        job.last_error_code = result.error_code
        self._bump_writer_termination_gate(cursor)
        return result

    def get_writer_execution_hold(self, job_id: str) -> WriterExecutionHold | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor, job_id)
                return self._select_writer_hold(cursor, job_id)

    def assert_no_active_writer_hold(self) -> None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                self._assert_no_active_writer_hold_cursor(cursor)

    def register_writer_execution(self, job_id: str, *, fence_id: str, attempt: int, worker_session: str, host_binding: str, execution_id: str, pid: int, process_start_time: str, now: datetime | None = None) -> WriterExecutionHold:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                job = self._select_job(cursor, job_id, for_update=True)
                hold = self._select_writer_hold(cursor, job_id, for_update=True)
                if hold is None or (hold.fence_id != fence_id or hold.attempt != attempt or hold.worker_session != worker_session or hold.host_binding != host_binding or hold.execution_id != execution_id or job.dispatch_fence_id != fence_id):
                    raise WriterTerminationConflict("writer_termination_binding_invalid")
                if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(process_start_time, str) or not process_start_time:
                    raise WriterTerminationConflict("writer_process_identity_invalid")
                if hold.state == WriterHoldState.REGISTERED:
                    if hold.pid != pid or hold.process_start_time != process_start_time:
                        raise WriterTerminationConflict("writer_process_identity_mismatch")
                    return hold
                if hold.state != WriterHoldState.PENDING:
                    raise WriterTerminationConflict("writer_termination_clearance_rejected")
                self._require_lease(cursor, job, worker_session, current)
                cursor.execute("UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='REGISTERED',process_pid=%s,process_start_at=%s,registered_at=%s,state_version=state_version+1,updated_at=%s WHERE job_id=%s AND lifecycle='PENDING'", (pid, process_start_time, current, current, job_id))
                self._bump_writer_termination_gate(cursor)
                return self._select_writer_hold(cursor, job_id, for_update=True)  # type: ignore[return-value]

    def confirm_writer_termination(self, job_id: str, *, fence_id: str, attempt: int, worker_session: str, host_binding: str, execution_id: str, pid: int, process_start_time: str, evidence_type: str, evidence_reference: str, exit_code: int, now: datetime | None = None) -> WriterExecutionHold:
        current = utc_now(now)
        if evidence_type != "process_exit" or not isinstance(evidence_reference, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", evidence_reference) or isinstance(exit_code, bool) or not isinstance(exit_code, int) or not -(2**31) <= exit_code <= (2**31 - 1):
            raise WriterTerminationConflict("writer_termination_proof_required")
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                job = self._select_job(cursor, job_id, for_update=True)
                hold = self._select_writer_hold(cursor, job_id, for_update=True)
                if hold is None or hold.fence_id != fence_id or hold.attempt != attempt or hold.worker_session != worker_session or hold.host_binding != host_binding or hold.execution_id != execution_id or job.dispatch_fence_id != fence_id:
                    raise WriterTerminationConflict("writer_termination_binding_invalid")
                if hold.state == WriterHoldState.TERMINATION_CONFIRMED:
                    if hold.pid != pid or hold.process_start_time != process_start_time:
                        raise WriterTerminationConflict("writer_process_identity_mismatch")
                    return hold
                if hold.state != WriterHoldState.REGISTERED or hold.pid != pid or hold.process_start_time != process_start_time:
                    raise WriterTerminationConflict("writer_process_identity_mismatch")
                self._require_lease(cursor, job, worker_session, current)
                cursor.execute("UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='TERMINATION_CONFIRMED',evidence_type=%s,evidence_reference=%s,termination_confirmed_at=%s,state_version=state_version+1,updated_at=%s WHERE job_id=%s AND lifecycle='REGISTERED'", (evidence_type, evidence_reference, current, current, job_id))
                self._bump_writer_termination_gate(cursor)
                return self._select_writer_hold(cursor, job_id, for_update=True)  # type: ignore[return-value]

    def quarantine_writer_execution(self, job_id: str, *, fence_id: str, attempt: int, worker_session: str, host_binding: str, execution_id: str, pid: int | None = None, process_start_time: str | None = None, evidence_reference: str, reason: str = "writer_termination_unconfirmed", now: datetime | None = None) -> WriterExecutionHold:
        current = utc_now(now)
        if (pid is None) != (process_start_time is None) or not isinstance(evidence_reference, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", evidence_reference):
            raise WriterTerminationConflict("writer_quarantine_evidence_invalid")
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                job = self._select_job(cursor, job_id, for_update=True)
                hold = self._select_writer_hold(cursor, job_id, for_update=True)
                if hold is None or hold.fence_id != fence_id or hold.attempt != attempt or hold.worker_session != worker_session or hold.host_binding != host_binding or hold.execution_id != execution_id or job.dispatch_fence_id != fence_id:
                    raise WriterTerminationConflict("writer_termination_binding_invalid")
                if hold.state in {WriterHoldState.CLEARED, WriterHoldState.TERMINATION_CONFIRMED}:
                    raise WriterTerminationConflict("writer_termination_clearance_rejected")
                if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(process_start_time, str) or not process_start_time or len(process_start_time) > 80):
                    raise WriterTerminationConflict("writer_process_identity_invalid")
                if hold.state in {WriterHoldState.REGISTERED, WriterHoldState.QUARANTINED}:
                    if pid is not None and (hold.pid != pid or hold.process_start_time != process_start_time):
                        raise WriterTerminationConflict("writer_process_identity_mismatch")
                    pid, process_start_time = hold.pid, hold.process_start_time
                if hold.state == WriterHoldState.QUARANTINED:
                    return hold
                if hold.state == WriterHoldState.REGISTERED:
                    cursor.execute("UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='QUARANTINED',evidence_type='quarantine',evidence_reference=%s,quarantined_at=%s,state_version=state_version+1,updated_at=%s WHERE job_id=%s", (evidence_reference, current, current, job_id))
                else:
                    cursor.execute("UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='QUARANTINED',process_pid=COALESCE(%s,process_pid),process_start_at=COALESCE(%s,process_start_at),evidence_type='quarantine',evidence_reference=%s,quarantined_at=%s,state_version=state_version+1,updated_at=%s WHERE job_id=%s", (pid, process_start_time, evidence_reference, current, current, job_id))
                safe_reason = reason if re.fullmatch(r"[a-z0-9_.:-]{1,80}", reason) else "writer_termination_unconfirmed"
                if job.state != JobState.WRITER_TERMINATION_UNCONFIRMED:
                    self._advance(cursor, job, JobState.WRITER_TERMINATION_UNCONFIRMED, current, {"last_error_code": safe_reason})
                else:
                    cursor.execute("UPDATE xb_member_gateway.jobs SET last_error_code=%s,updated_at=%s WHERE job_id=%s", (safe_reason, current, job_id))
                self._bump_writer_termination_gate(cursor)
                return self._select_writer_hold(cursor, job_id, for_update=True)  # type: ignore[return-value]

    def recover_writer_termination(self, job_id: str, *, fence_id: str, attempt: int, recovery_session: str, host_binding: str, execution_id: str, pid: int, process_start_time: str, evidence_reference: str, exit_code: int, now: datetime | None = None) -> tuple[ResultRecord, bool]:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                job = self._select_job(cursor, job_id, for_update=True)
                existing = self._select_writer_hold(cursor, job_id, for_update=True)
                cursor.execute("SELECT result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at FROM xb_member_gateway.results WHERE job_id=%s FOR UPDATE", (job_id,))
                result_row = cursor.fetchone()
                if existing is not None and existing.state == WriterHoldState.CLEARED and result_row is not None and result_row[1] == ResultStatus.WRITE_OUTCOME_UNCERTAIN.value:
                    return self._result_from_row(job_id, result_row), True
                if existing is None or existing.state != WriterHoldState.QUARANTINED or existing.worker_session is None:
                    raise WriterTerminationConflict("writer_termination_clearance_rejected")
                if existing.worker_session == recovery_session or existing.fence_id != fence_id or existing.attempt != attempt or existing.host_binding != host_binding or existing.execution_id != execution_id or job.dispatch_fence_id != fence_id or existing.pid != pid or existing.process_start_time != process_start_time:
                    raise WriterTerminationConflict("writer_termination_binding_invalid")
                if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(process_start_time, str) or not process_start_time or not isinstance(exit_code, int) or isinstance(exit_code, bool) or not -(2**31) <= exit_code <= (2**31 - 1) or not isinstance(evidence_reference, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", evidence_reference):
                    raise WriterTerminationConflict("writer_termination_proof_required")
                cursor.execute("SELECT expires_at FROM xb_member_gateway.leases WHERE job_id=%s AND active=TRUE FOR UPDATE", (job_id,))
                lease = cursor.fetchone()
                if lease is not None and parse_timestamp(lease[0]) > current:
                    raise WriterTerminationConflict("writer_termination_clearance_rejected")
                cursor.execute("UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='TERMINATION_CONFIRMED',evidence_type='termination_recovery',evidence_reference=%s,termination_confirmed_at=%s,state_version=state_version+1,updated_at=%s WHERE job_id=%s AND lifecycle='QUARANTINED'", (evidence_reference, current, current, job_id))
                return self._settle_confirmed_termination_cursor(cursor, job, current, "termination_recovered_result_unknown"), False

    def _reclaim_expired_cursor(self, cursor: Any, current: datetime) -> int:
        cursor.execute("SELECT j.job_id,j.state,j.dispatch_fence_id,j.attempt_count,j.max_attempts,h.lifecycle FROM xb_member_gateway.jobs j JOIN xb_member_gateway.leases l ON l.job_id=j.job_id LEFT JOIN xb_member_gateway.writer_execution_holds h ON h.job_id=j.job_id WHERE l.active=TRUE AND l.expires_at<=%s FOR UPDATE OF j,l", (current,))
        rows = cursor.fetchall()
        cursor.execute("SELECT job_id FROM xb_member_gateway.writer_execution_holds WHERE lifecycle <> 'CLEARED'")
        active_hold_jobs = {row[0] for row in cursor.fetchall()}
        changed = 0
        for job_id,state_value,fence_id,attempts,max_attempts,hold_lifecycle in rows:
            if state_value in {
                JobState.CREATED_VERIFIED.value, JobState.CONFIRMED_NOT_CREATED.value,
                JobState.CREATED_READBACK_MISMATCH.value, JobState.MANUAL_REVIEW.value,
                JobState.DEAD_LETTER.value, JobState.REJECTED_VALIDATION.value,
            }:
                cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job_id,))
                continue
            if active_hold_jobs and job_id not in active_hold_jobs:
                continue
            if hold_lifecycle == WriterHoldState.TERMINATION_CONFIRMED.value:
                job = self._select_job(cursor, job_id, for_update=True)
                self._settle_confirmed_termination_cursor(cursor, job, current, "lease_expired_after_confirmed_termination")
                changed += 1
                continue
            if hold_lifecycle == WriterHoldState.CLEARED.value:
                cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job_id,))
                changed += 1
                continue
            if fence_id is not None or state_value in {JobState.WRITING.value, JobState.READBACK.value, JobState.WRITER_TERMINATION_UNCONFIRMED.value}:
                job = self._select_job(cursor, job_id, for_update=True)
                self._quarantine_hold_cursor(cursor, job, current, "lease_expired_after_dispatch")
                cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job_id,))
                changed += 1
                continue
            elif attempts >= max_attempts:
                target,error = JobState.DEAD_LETTER,"lease_expired_attempt_limit"
            else:
                target,error = JobState.RETRY_WAIT,"lease_expired_before_dispatch"
            cursor.execute("UPDATE xb_member_gateway.jobs SET state=%s,state_version=state_version+1,next_attempt_at=%s,attempt_started_at=NULL,last_error_code=%s,updated_at=%s WHERE job_id=%s", (target.value,current,error,current,job_id))
            cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job_id,))
            if target == JobState.DEAD_LETTER:
                cursor.execute("INSERT INTO xb_member_gateway.dead_letters(job_id,error_code,attempt_count,lineage) VALUES(%s,%s,%s,%s::jsonb)", (job_id,error,attempts,'{"source":"lease_reclaim"}'))
            changed += 1
        return changed

    def reclaim_expired(self, *, now: datetime | None = None) -> int:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                return self._reclaim_expired_cursor(cursor, current)

    def claim_job(self, worker_id: str, *, lease_seconds: int = 600, now: datetime | None = None) -> JobRecord | None:
        if not worker_id or any(character.isspace() for character in worker_id):
            raise RepositoryError("worker_id_invalid")
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                self._lock_kill_switch(cursor)
                self._reclaim_expired_cursor(cursor, current)
                self._assert_no_active_writer_hold_cursor(cursor)
                cursor.execute(
                    "SELECT lease_id FROM xb_member_gateway.leases "
                    "WHERE active=TRUE AND expires_at>%s LIMIT 1 FOR UPDATE",
                    (current,),
                )
                if cursor.fetchone() is not None:
                    return None
                cursor.execute("SELECT job_id FROM xb_member_gateway.jobs WHERE state IN ('QUEUED','RETRY_WAIT') AND (next_attempt_at IS NULL OR next_attempt_at<=%s) AND attempt_count<max_attempts ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1", (current,))
                row = cursor.fetchone()
                if row is None:
                    return None
                job_id = row[0]
                expires = current + timedelta(seconds=lease_seconds)
                cursor.execute("UPDATE xb_member_gateway.jobs SET state='LEASED',state_version=state_version+1,attempt_count=attempt_count+1,lease_owner=%s,lease_expires_at=%s,next_attempt_at=NULL,attempt_started_at=%s,updated_at=%s WHERE job_id=%s", (worker_id,expires,current,current,job_id))
                job = self._select_job(cursor,job_id,for_update=True)
                cursor.execute("INSERT INTO xb_member_gateway.attempts(job_id,attempt_number,worker_id,started_at) VALUES(%s,%s,%s,%s)", (job_id,job.attempt,worker_id,current))
                cursor.execute("INSERT INTO xb_member_gateway.leases(job_id,worker_id,state_version,expires_at) VALUES(%s,%s,%s,%s)", (job_id,worker_id,job.state_version,expires))
                return job

    def heartbeat(self, job_id: str, worker_id: str, *, expected_state_version: int, lease_seconds: int = 600, now: datetime | None = None) -> LeaseRecord:
        current = utc_now(now)
        expires = current + timedelta(seconds=lease_seconds)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                job = self._select_job(cursor,job_id,for_update=True)
                if job.state_version != expected_state_version:
                    raise LeaseConflict("state_version_mismatch")
                self._require_lease(cursor,job,worker_id,current)
                cursor.execute("UPDATE xb_member_gateway.jobs SET state_version=state_version+1,lease_expires_at=%s,updated_at=%s WHERE job_id=%s", (expires,current,job_id))
                cursor.execute("UPDATE xb_member_gateway.leases SET state_version=%s,expires_at=%s WHERE job_id=%s AND active=TRUE", (expected_state_version+1,expires,job_id))
                return LeaseRecord(job_id,worker_id,timestamp(expires),expected_state_version+1)

    def begin_prechecking(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> JobRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor,job_id,for_update=True)
                self._require_lease(cursor,job,worker_id,current)
                if job.state == JobState.LEASED:
                    job = self._advance(cursor,job,JobState.PRECHECKING,current)
                    if job.allocation_member_no is not None:
                        job = self._advance(cursor,job,JobState.ALLOCATION_BOUND,current)
                elif job.state != JobState.PRECHECKING:
                    raise RepositoryError("prechecking_state_invalid")
                return job

    def get_probes(self, job_id: str) -> tuple[AllocationProbe, ...]:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor,job_id)
                cursor.execute("SELECT candidate,probe_status,probe_reference,observed_at FROM xb_member_gateway.allocation_probes WHERE job_id=%s ORDER BY probe_id", (job_id,))
                return tuple(AllocationProbe(job_id,row[0],ProbeStatus(row[1]),row[2],self._dt(row[3]) or "") for row in cursor.fetchall())

    def record_probe(self, job_id: str, candidate: str, status: ProbeStatus, probe_reference: str, worker_id: str, *, now: datetime | None = None) -> AllocationProbe:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                self._assert_no_active_writer_hold_cursor(cursor)
                job = self._select_job(cursor,job_id,for_update=True)
                self._require_lease(cursor,job,worker_id,current)
                if job.dispatch_fenced or job.state not in {JobState.PRECHECKING,JobState.ALLOCATION_BOUND}:
                    raise AllocationConflict("probe_state_invalid")
                cursor.execute("INSERT INTO xb_member_gateway.allocation_probes(job_id,candidate,probe_status,probe_reference,observed_at) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(job_id,candidate,probe_reference) DO UPDATE SET probe_status=EXCLUDED.probe_status,observed_at=EXCLUDED.observed_at RETURNING candidate,probe_status,probe_reference,observed_at", (job_id,candidate,ProbeStatus(status).value,probe_reference,current))
                row = cursor.fetchone()
                return AllocationProbe(job_id,row[0],ProbeStatus(row[1]),row[2],self._dt(row[3]) or timestamp(current))

    def get_allocation(self, job_id: str) -> AllocationRecord | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor,job_id)
                cursor.execute("SELECT response_id,member_no,probe_reference,bound_at FROM xb_member_gateway.member_allocations WHERE job_id=%s", (job_id,))
                row = cursor.fetchone()
                return None if row is None else AllocationRecord(job_id,row[0],row[1],row[2],self._dt(row[3]) or "")

    def bind_allocation(self, job_id: str, member_no: str, probe_reference: str, worker_id: str, *, now: datetime | None = None) -> AllocationRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                self._assert_no_active_writer_hold_cursor(cursor)
                job = self._select_job(cursor,job_id,for_update=True)
                self._require_lease(cursor,job,worker_id,current)
                if job.dispatch_fenced:
                    raise AllocationConflict("allocation_after_dispatch_fence")
                cursor.execute("SELECT response_id,member_no,probe_reference,bound_at FROM xb_member_gateway.member_allocations WHERE job_id=%s FOR UPDATE", (job_id,))
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[1] != member_no or existing[2] != probe_reference:
                        raise AllocationConflict("job_allocation_already_bound")
                    return AllocationRecord(job_id,existing[0],existing[1],existing[2],self._dt(existing[3]) or "")
                cursor.execute("SELECT 1 FROM xb_member_gateway.allocation_probes WHERE job_id=%s AND candidate=%s AND probe_reference=%s AND probe_status='FREE'", (job_id,member_no,probe_reference))
                if cursor.fetchone() is None:
                    raise AllocationConflict("positive_free_probe_required")
                cursor.execute("SELECT job_id FROM xb_member_gateway.member_allocations WHERE member_no=%s FOR UPDATE", (member_no,))
                owner = cursor.fetchone()
                if owner is not None and owner[0] != job_id:
                    raise AllocationConflict("member_no_allocation_race")
                if job.state != JobState.PRECHECKING:
                    raise AllocationConflict("allocation_requires_prechecking")
                cursor.execute("INSERT INTO xb_member_gateway.member_allocations(allocation_id,job_id,response_id,member_no,probe_reference,bound_at) VALUES(%s,%s,%s,%s,%s,%s)", (str(uuid.uuid4()),job_id,job.response_id,member_no,probe_reference,current))
                self._advance(cursor,job,JobState.ALLOCATION_BOUND,current,{"allocation_member_no":member_no,"allocation_probe_reference":probe_reference})
                return AllocationRecord(job_id,job.response_id,member_no,probe_reference,timestamp(current))

    def recheck_bound_allocation(self, job_id: str, worker_id: str, status: ProbeStatus, probe_reference: str, *, now: datetime | None = None) -> JobRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                self._assert_no_active_writer_hold_cursor(cursor)
                job = self._select_job(cursor,job_id,for_update=True)
                self._require_lease(cursor,job,worker_id,current)
                if job.state != JobState.ALLOCATION_BOUND or not job.allocation_member_no:
                    raise AllocationConflict("bound_allocation_required")
                status = ProbeStatus(status)
                prior = self._latest_recheck_cursor(cursor, job)
                current_prior = prior is not None and prior.attempt == job.attempt and prior.worker_id == worker_id and prior.member_no == job.allocation_member_no
                if current_prior and prior.status == status and prior.probe_reference == probe_reference:
                    return job
                recheck_id = f"recheck-{uuid.uuid4().hex}"
                cursor.execute(
                    "INSERT INTO xb_member_gateway.audit_events(event_type,job_id,operation,state,safe_reference,metadata,recorded_at) "
                    "VALUES('allocation_recheck',%s,'member.create','ALLOCATION_BOUND',%s,%s::jsonb,%s)",
                    (job_id,recheck_id,json.dumps({"attempt":job.attempt,"worker_session":worker_id,"status":status.value,"probe_reference":probe_reference},separators=(",",":")),current),
                )
                cursor.execute("INSERT INTO xb_member_gateway.allocation_probes(job_id,candidate,probe_status,probe_reference,observed_at) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(job_id,candidate,probe_reference) DO NOTHING", (job_id,job.allocation_member_no,status.value,probe_reference,current))
                if status == ProbeStatus.FREE and probe_reference != job.allocation_probe_reference and not current_prior:
                    return job
                return self._advance(cursor,job,JobState.MANUAL_REVIEW,current,{"last_error_code":"bound_member_no_recheck_not_free"})

    def get_fresh_recheck(self, job_id: str, worker_id: str, *, now: datetime | None = None) -> AllocationRecheck | None:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor, job_id, for_update=True)
                self._require_lease(cursor, job, worker_id, current)
                return self._fresh_recheck_cursor(cursor, job, worker_id, current)

    def record_write_intent(self, job_id: str, member_no: str, operation: str, payload_hash_value: str, worker_id: str, *, now: datetime | None = None) -> WriteIntentRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                self._assert_no_active_writer_hold_cursor(cursor)
                job = self._select_job(cursor,job_id,for_update=True)
                self._require_lease(cursor,job,worker_id,current)
                if operation != "member.create" or job.operation != operation:
                    raise RepositoryError("operation_invalid")
                cursor.execute("SELECT member_no FROM xb_member_gateway.member_allocations WHERE job_id=%s", (job_id,))
                allocation = cursor.fetchone()
                if allocation is None or allocation[0] != member_no:
                    raise RepositoryError("allocation_binding_required")
                fresh = self._fresh_recheck_cursor(cursor, job, worker_id, current)
                if fresh is None:
                    raise RepositoryError("fresh_bound_member_no_recheck_required")
                cursor.execute("SELECT intent_id,member_no,payload_hash,recorded_at,recheck_id FROM xb_member_gateway.write_intents WHERE job_id=%s FOR UPDATE", (job_id,))
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[1] != member_no or existing[2] != payload_hash_value:
                        raise SourceConflict("write_intent_payload_conflict")
                    if existing[4] != fresh.recheck_id:
                        cursor.execute("UPDATE xb_member_gateway.write_intents SET recheck_id=%s WHERE job_id=%s", (fresh.recheck_id, job_id))
                    if job.state == JobState.ALLOCATION_BOUND:
                        self._advance(cursor,job,JobState.WRITE_INTENT_RECORDED,current,{"write_intent_id":str(existing[0])})
                    return WriteIntentRecord(job_id,str(existing[0]),existing[1],existing[2],self._dt(existing[3]) or "",fresh.recheck_id)
                if job.state != JobState.ALLOCATION_BOUND:
                    raise RepositoryError("write_intent_state_invalid")
                intent_id = str(uuid.uuid4())
                cursor.execute("INSERT INTO xb_member_gateway.write_intents(intent_id,job_id,operation,member_no,payload_hash,recorded_at,recheck_id) VALUES(%s,%s,%s,%s,%s,%s,%s)", (intent_id,job_id,operation,member_no,payload_hash_value,current,fresh.recheck_id))
                self._advance(cursor,job,JobState.WRITE_INTENT_RECORDED,current,{"write_intent_id":intent_id})
                return WriteIntentRecord(job_id,intent_id,member_no,payload_hash_value,timestamp(current),fresh.recheck_id)

    def get_write_intent(self, job_id: str) -> WriteIntentRecord | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor,job_id)
                cursor.execute("SELECT intent_id,member_no,payload_hash,recorded_at,recheck_id FROM xb_member_gateway.write_intents WHERE job_id=%s", (job_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                return WriteIntentRecord(job_id,str(row[0]),row[1],row[2],self._dt(row[3]) or "",row[4])

    def record_dispatch_fence(self, job_id: str, member_no: str, operation: str, worker_id: str, *, host_binding: str | None = None, now: datetime | None = None) -> DispatchFenceRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                self._lock_kill_switch(cursor)
                job = self._select_job(cursor,job_id,for_update=True)
                self._require_lease(cursor,job,worker_id,current)
                cursor.execute("SELECT fence_id,member_no,operation,created_at FROM xb_member_gateway.dispatch_fences WHERE job_id=%s FOR UPDATE", (job_id,))
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[1] != member_no or existing[2] != operation:
                        raise AllocationConflict("dispatch_fence_binding_invalid")
                    return self.get_dispatch_fence_cursor(cursor, job_id, job)  # type: ignore[return-value]
                self._assert_no_active_writer_hold_cursor(cursor)
                fresh = self._fresh_recheck_cursor(cursor, job, worker_id, current)
                cursor.execute("SELECT member_no,recheck_id FROM xb_member_gateway.write_intents WHERE job_id=%s", (job_id,))
                intent = cursor.fetchone()
                if (
                    intent is None or intent[0] != member_no or operation != "member.create"
                    or job.state != JobState.WRITE_INTENT_RECORDED or job.save_invocation_count != 0
                    or fresh is None or intent[1] != fresh.recheck_id
                ):
                    raise RepositoryError("write_intent_required")
                fence_id = new_fence_id()
                cursor.execute("INSERT INTO xb_member_gateway.dispatch_fences(fence_id,job_id,operation,member_no,created_at,save_invocation_count) VALUES(%s,%s,%s,%s,%s,0)", (internal_fence_id(fence_id),job_id,operation,member_no,current))
                self._advance(cursor,job,JobState.WRITING,current,{"dispatch_fence_id":fence_id})
                resolved_host = host_binding or f"host-{worker_id}"
                if not re.fullmatch(r"host-[A-Za-z0-9._:-]{1,120}", resolved_host):
                    raise WriterTerminationConflict("worker_host_binding_invalid")
                execution_id = f"exec-{uuid.uuid4().hex}"
                cursor.execute("INSERT INTO xb_member_gateway.writer_execution_holds(hold_id,job_id,fence_id,attempt_count,worker_session,host_binding,execution_id,member_no,lifecycle,state_version,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',0,%s,%s)", (str(uuid.uuid4()),job_id,internal_fence_id(fence_id),job.attempt,worker_id,resolved_host,execution_id,member_no,current,current))
                self._bump_writer_termination_gate(cursor)
                return DispatchFenceRecord(job_id,fence_id,member_no,operation,timestamp(current),fresh.recheck_id,execution_id)

    def get_dispatch_fence(self, job_id: str) -> DispatchFenceRecord | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor,job_id)
                cursor.execute("SELECT fence_id,member_no,operation,created_at FROM xb_member_gateway.dispatch_fences WHERE job_id=%s", (job_id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                latest = self._latest_recheck_cursor(cursor, self._select_job(cursor, job_id))
                cursor.execute("SELECT execution_id FROM xb_member_gateway.writer_execution_holds WHERE job_id=%s", (job_id,))
                execution = cursor.fetchone()
                return DispatchFenceRecord(job_id,public_fence_id(row[0]),row[1],row[2],self._dt(row[3]) or "",latest.recheck_id if latest else None,execution[0] if execution else None)

    def open_reconciliation_case(self, job_id: str, member_no: str, *, now: datetime | None = None) -> ReconciliationCaseRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                job = self._select_job(cursor, job_id, for_update=True)
                hold = self._select_writer_hold(cursor, job_id, for_update=True)
                cursor.execute("SELECT 1 FROM xb_member_gateway.writer_execution_holds WHERE lifecycle <> 'CLEARED' LIMIT 1 FOR UPDATE")
                if cursor.fetchone() is not None:
                    raise WriterTerminationConflict("writer_termination_quarantine_active")
                if hold is None or hold.state != WriterHoldState.CLEARED or hold.termination_confirmed_at is None:
                    raise WriterTerminationConflict("writer_termination_proof_required")
                if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
                    raise RepositoryError("reconciliation_requires_uncertain_state")
                cursor.execute("SELECT member_no FROM xb_member_gateway.member_allocations WHERE job_id=%s FOR UPDATE", (job_id,))
                allocation = cursor.fetchone()
                cursor.execute("SELECT member_no FROM xb_member_gateway.dispatch_fences WHERE job_id=%s FOR UPDATE", (job_id,))
                fence = cursor.fetchone()
                if allocation is None or fence is None or allocation[0] != member_no or fence[0] != member_no:
                    raise RepositoryError("reconciliation_member_binding_invalid")
                cursor.execute("SELECT expires_at FROM xb_member_gateway.leases WHERE job_id=%s AND active=TRUE FOR UPDATE", (job_id,))
                lease = cursor.fetchone()
                if lease is not None:
                    if parse_timestamp(lease[0]) > current:
                        raise LeaseConflict("reconciliation_writer_lease_active")
                    cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job_id,))
                    cursor.execute("UPDATE xb_member_gateway.jobs SET lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE job_id=%s", (current,job_id))
                cursor.execute("SELECT case_id,job_id,member_no,case_state,opened_at,closed_at FROM xb_member_gateway.reconciliation_cases WHERE job_id=%s FOR UPDATE", (job_id,))
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[3] != ReconciliationCaseState.OPEN.value:
                        raise RepositoryError("reconciliation_case_closed")
                    return ReconciliationCaseRecord(str(existing[0]),existing[1],existing[2],ReconciliationCaseState(existing[3]),self._dt(existing[4]) or "",self._dt(existing[5]))
                case_id = str(uuid.uuid4())
                cursor.execute("INSERT INTO xb_member_gateway.reconciliation_cases(case_id,job_id,member_no,case_state,opened_at) VALUES(%s,%s,%s,'OPEN',%s)", (case_id,job_id,member_no,current))
                return ReconciliationCaseRecord(case_id,job_id,member_no,ReconciliationCaseState.OPEN,timestamp(current))

    def record_reconciliation_check(self, case_id: str, lookup_status: str, readback_found: bool, readback_match: bool, *, now: datetime | None = None) -> ReconciliationCheckRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                cursor.execute("SELECT job_id,member_no,case_state FROM xb_member_gateway.reconciliation_cases WHERE case_id=%s", (case_id,))
                case = cursor.fetchone()
                if case is None:
                    raise RepositoryError("reconciliation_case_required")
                job = self._select_job(cursor, case[0], for_update=True)
                cursor.execute("SELECT job_id,member_no,case_state FROM xb_member_gateway.reconciliation_cases WHERE case_id=%s FOR UPDATE", (case_id,))
                case = cursor.fetchone()
                if case is None:
                    raise RepositoryError("reconciliation_case_required")
                if case[2] != ReconciliationCaseState.OPEN.value:
                    raise RepositoryError("reconciliation_case_not_open")
                hold = self._select_writer_hold(cursor, job.job_id, for_update=True)
                cursor.execute("SELECT 1 FROM xb_member_gateway.writer_execution_holds WHERE lifecycle <> 'CLEARED' LIMIT 1 FOR UPDATE")
                if cursor.fetchone() is not None:
                    raise WriterTerminationConflict("writer_termination_quarantine_active")
                if hold is None or hold.state != WriterHoldState.CLEARED or hold.termination_confirmed_at is None:
                    raise WriterTerminationConflict("writer_termination_proof_required")
                if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
                    raise RepositoryError("reconciliation_requires_uncertain_state")
                cursor.execute("SELECT expires_at FROM xb_member_gateway.leases WHERE job_id=%s AND active=TRUE FOR UPDATE", (job.job_id,))
                lease = cursor.fetchone()
                if lease is not None and parse_timestamp(lease[0]) > current:
                    raise LeaseConflict("reconciliation_writer_lease_active")
                if lease is not None:
                    cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job.job_id,))
                    cursor.execute("UPDATE xb_member_gateway.jobs SET lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE job_id=%s", (current,job.job_id))
                if lookup_status not in {"exact_match", "absent", "mismatch", "ambiguous"}:
                    raise RepositoryError("reconciliation_status_invalid")
                if not isinstance(readback_found, bool) or not isinstance(readback_match, bool):
                    raise RepositoryError("reconciliation_flags_invalid")
                required_flags = {"exact_match":(True,True),"absent":(False,False),"mismatch":(True,False)}
                if lookup_status in required_flags and (readback_found,readback_match) != required_flags[lookup_status]:
                    raise RepositoryError("reconciliation_flags_invalid")
                if lookup_status == "ambiguous" and readback_match:
                    raise RepositoryError("reconciliation_flags_invalid")
                case_state = {
                    "exact_match": ReconciliationCaseState.EXACT_MATCH.value,
                    "absent": ReconciliationCaseState.ABSENT.value,
                    "mismatch": ReconciliationCaseState.MISMATCH.value,
                    "ambiguous": ReconciliationCaseState.AMBIGUOUS.value,
                }[lookup_status]
                closed_at = current if lookup_status in {"exact_match", "absent"} else None
                cursor.execute("UPDATE xb_member_gateway.reconciliation_cases SET case_state=%s,closed_at=%s WHERE case_id=%s", (case_state,closed_at,case_id))
                cursor.execute("INSERT INTO xb_member_gateway.reconciliation_checks(case_id,lookup_status,readback_found,readback_match,checked_at) VALUES(%s,%s,%s,%s,%s) RETURNING check_id,checked_at", (case_id,lookup_status,readback_found,readback_match,current))
                row = cursor.fetchone()
                return ReconciliationCheckRecord(str(row[0]),case_id,lookup_status,readback_found,readback_match,self._dt(row[1]) or timestamp(current))

    def _require_reconciliation_evidence(self, cursor: Any, result: ResultRecord, case_id: str) -> None:
        cursor.execute("SELECT job_id,member_no,case_state FROM xb_member_gateway.reconciliation_cases WHERE case_id=%s FOR UPDATE", (case_id,))
        case = cursor.fetchone()
        if case is None or case[0] != result.job_id or case[1] != result.member_no:
            raise RepositoryError("reconciliation_case_required")
        expected = {
            ResultStatus.CREATED_VERIFIED:(ReconciliationCaseState.EXACT_MATCH.value,"exact_match"),
            ResultStatus.CONFIRMED_NOT_CREATED:(ReconciliationCaseState.ABSENT.value,"absent"),
            ResultStatus.CREATED_READBACK_MISMATCH:(ReconciliationCaseState.MISMATCH.value,"mismatch"),
            ResultStatus.WRITE_OUTCOME_UNCERTAIN:(ReconciliationCaseState.AMBIGUOUS.value,"ambiguous"),
        }[result.status]
        cursor.execute("SELECT lookup_status,readback_found,readback_match FROM xb_member_gateway.reconciliation_checks WHERE case_id=%s ORDER BY check_id DESC LIMIT 1", (case_id,))
        check = cursor.fetchone()
        if case[2] != expected[0] or check is None or check[0] != expected[1] or (bool(check[1]),bool(check[2])) != (result.readback_found,result.readback_match):
            raise RepositoryError("reconciliation_check_required")

    def acknowledge_result(self, result: ResultRecord, *, worker_id: str | None = None, require_lease: bool = True, reconciliation_case_id: str | None = None, now: datetime | None = None) -> tuple[ResultRecord, bool]:
        current = utc_now(now)
        if result.save_invocation_count != 1:
            raise ResultConflict("save_invocation_count_must_be_one")
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                job = self._select_job(cursor,result.job_id,for_update=True)
                cursor.execute("SELECT fence_id,member_no FROM xb_member_gateway.dispatch_fences WHERE job_id=%s FOR UPDATE", (result.job_id,))
                fence = cursor.fetchone()
                if fence is None or public_fence_id(fence[0]) != result.dispatch_fence_id or fence[1] != result.member_no:
                    raise RepositoryError("dispatch_fence_binding_invalid")
                cursor.execute("SELECT result_hash,status FROM xb_member_gateway.results WHERE job_id=%s FOR UPDATE", (result.job_id,))
                existing = cursor.fetchone()
                if existing is not None and existing[0] == result.result_hash:
                    if not require_lease:
                        if reconciliation_case_id is None:
                            raise RepositoryError("reconciliation_case_required")
                        self._require_reconciliation_evidence(cursor, result, reconciliation_case_id)
                    if existing[1] == ResultStatus.CREATED_VERIFIED.value:
                        # Duplicate positive ack verifies, never creates/backfills.
                        self._verify_welcome_outbox_cursor(cursor, job)
                    return result, True
                reconciliation_projection = False
                if existing is not None:
                    if require_lease or existing[1] != ResultStatus.WRITE_OUTCOME_UNCERTAIN.value:
                        cursor.execute("INSERT INTO xb_member_gateway.result_conflicts(job_id,expected_hash,observed_hash) VALUES(%s,%s,%s)", (result.job_id,existing[0],result.result_hash))
                        raise ResultConflict("result_payload_conflict")
                    reconciliation_projection = True
                hold = self._select_writer_hold(cursor, result.job_id, for_update=True)
                if require_lease and (hold is None or hold.state != WriterHoldState.TERMINATION_CONFIRMED):
                    raise WriterTerminationConflict("writer_termination_proof_required")
                if not require_lease and (hold is None or hold.state != WriterHoldState.CLEARED or hold.termination_confirmed_at is None):
                    raise WriterTerminationConflict("writer_termination_proof_required")
                if require_lease:
                    if not worker_id:
                        raise LeaseConflict("worker_identity_required")
                    self._require_lease(cursor,job,worker_id,current)
                else:
                    if not reconciliation_projection:
                        raise ResultConflict("reconciliation_projection_missing")
                    if reconciliation_case_id is None:
                        raise RepositoryError("reconciliation_case_required")
                    self._require_reconciliation_evidence(cursor, result, reconciliation_case_id)
                if reconciliation_projection and result.status == ResultStatus.WRITE_OUTCOME_UNCERTAIN:
                    return result, True
                internal_fence = internal_fence_id(result.dispatch_fence_id)
                event_params = (result.job_id,result.result_hash,result.status.value,result.member_no,internal_fence,result.save_invocation_count,result.readback_found,result.readback_match,result.reconciliation_required,result.error_code,current)
                cursor.execute("INSERT INTO xb_member_gateway.result_events(job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", event_params)
                if reconciliation_projection:
                    cursor.execute("UPDATE xb_member_gateway.results SET result_hash=%s,status=%s,member_no=%s,dispatch_fence_id=%s,save_invocation_count=%s,readback_found=%s,readback_match=%s,reconciliation_required=%s,error_code=%s,acknowledged_at=%s WHERE job_id=%s AND result_hash=%s AND status=%s RETURNING result_id", (result.result_hash,result.status.value,result.member_no,internal_fence,result.save_invocation_count,result.readback_found,result.readback_match,result.reconciliation_required,result.error_code,current,result.job_id,existing[0],ResultStatus.WRITE_OUTCOME_UNCERTAIN.value))
                    if cursor.fetchone() is None:
                        raise ResultConflict("reconciliation_projection_stale")
                else:
                    cursor.execute(self._RESULTS_INSERT, event_params)
                if result.status == ResultStatus.CREATED_VERIFIED and job.state == JobState.WRITING:
                    self._advance(cursor,job,JobState.READBACK,current)
                target = {ResultStatus.CREATED_VERIFIED:JobState.CREATED_VERIFIED,ResultStatus.WRITE_OUTCOME_UNCERTAIN:JobState.WRITE_OUTCOME_UNCERTAIN,ResultStatus.CONFIRMED_NOT_CREATED:JobState.CONFIRMED_NOT_CREATED,ResultStatus.CREATED_READBACK_MISMATCH:JobState.CREATED_READBACK_MISMATCH}[result.status]
                if job.state != target:
                    self._advance(cursor,job,target,current,{"result_status":result.status.value,"save_invocation_count":1,"last_error_code":result.error_code})
                else:
                    cursor.execute("UPDATE xb_member_gateway.jobs SET result_status=%s,save_invocation_count=1,last_error_code=%s,updated_at=%s WHERE job_id=%s", (result.status.value,result.error_code,current,result.job_id))
                if result.status == ResultStatus.CREATED_VERIFIED:
                    # Atomic with the first positive result on both the leased
                    # worker path and the exact-match reconciliation projection.
                    self._insert_welcome_outbox_cursor(cursor, job, current)
                if require_lease:
                    cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (result.job_id,))
                    cursor.execute("UPDATE xb_member_gateway.jobs SET lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE job_id=%s", (current,result.job_id))
                    cursor.execute("UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='CLEARED',state_version=state_version+1,cleared_at=%s,updated_at=%s WHERE job_id=%s AND lifecycle='TERMINATION_CONFIRMED'", (current, current, result.job_id))
                    self._bump_writer_termination_gate(cursor)
                return result, False

    _OUTBOX_COLUMNS = (
        "outbox_id,job_id,response_id,source_response_ref,template_id,recipient,message_hash,state,state_version,"
        "attempt,max_attempts,lease_id,lease_expires_at,next_attempt_at,send_intent_at,last_error_code,created_at,updated_at"
    )
    _RESULTS_INSERT = (
        "INSERT INTO xb_member_gateway.results(job_id,result_hash,status,member_no,dispatch_fence_id,"
        "save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
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

    def get_result(self, job_id: str) -> ResultRecord | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor,job_id)
                cursor.execute("SELECT result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at FROM xb_member_gateway.results WHERE job_id=%s", (job_id,))
                row = cursor.fetchone()
                return None if row is None else ResultRecord(job_id,row[0],ResultStatus(row[1]),row[2],public_fence_id(row[3]),int(row[4]),bool(row[5]),bool(row[6]),bool(row[7]),row[8],self._dt(row[9]) or "")

    def mark_state(self, job_id: str, target: JobState, *, worker_id: str | None = None, require_lease: bool = False, error_code: str | None = None, now: datetime | None = None) -> JobRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._lock_writer_termination_gate(cursor)
                job = self._select_job(cursor,job_id,for_update=True)
                if require_lease:
                    if worker_id is None:
                        raise LeaseConflict("worker_identity_required")
                    self._require_lease(cursor,job,worker_id,current)
                desired = JobState(target)
                if job.dispatch_fenced and desired == JobState.WRITER_TERMINATION_UNCONFIRMED:
                    self._quarantine_hold_cursor(cursor, job, current, error_code or "writer_termination_unconfirmed")
                    return job
                if job.dispatch_fenced and desired == JobState.WRITE_OUTCOME_UNCERTAIN:
                    hold = self._select_writer_hold(cursor, job.job_id, for_update=True)
                    if hold is None or not hold.termination_confirmed:
                        self._quarantine_hold_cursor(cursor, job, current, error_code or "writer_termination_unconfirmed")
                        return job
                    self._settle_confirmed_termination_cursor(cursor, job, current, error_code or "writer_termination_unconfirmed")
                    return job
                if job.dispatch_fenced and desired in {
                    JobState.CREATED_VERIFIED,
                    JobState.CONFIRMED_NOT_CREATED,
                    JobState.CREATED_READBACK_MISMATCH,
                }:
                    raise WriterTerminationConflict("writer_termination_proof_required")
                return self._advance(cursor,job,desired,current,{"last_error_code":error_code} if error_code else None)
