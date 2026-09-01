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

from .canonical import canonical_json
from .crypto import hmac_reference
from .models import (
    AllocationProbe, AllocationRecord, AllocationRecheck, DispatchFenceRecord, IngestOutcome,
    JobRecord, JobState, LeaseRecord, ProbeStatus, ResultRecord, ResultStatus,
    ReconciliationCaseRecord, ReconciliationCaseState, ReconciliationCheckRecord,
    SourceEvent, WriteIntentRecord, WriterExecutionHold, WriterHoldState,
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


class MemberProbe(Protocol):
    def __call__(self, candidate: str) -> ProbeStatus:
        ...


class InMemoryRepository:
    """Thread-safe fake of all durable gateway records."""

    def __init__(self, *, reference_key: bytes = b"synthetic-member-gateway-key"):
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

    def ingest_source_event(self, event: SourceEvent, *, now: datetime | None = None) -> IngestOutcome:
        with self._lock:
            prior_hash = self._request_hashes.get(event.request_id)
            prior_response = self._request_responses.get(event.request_id)
            if prior_hash is not None and (prior_hash != event.payload_hash or prior_response != event.response_id):
                raise SourceConflict("request_identity_payload_conflict")
            existing = self._responses.get(event.response_id)
            if existing is not None:
                if existing.payload_hash != event.payload_hash:
                    self._audit("source_conflict", error_code="source_identity_conflict")
                    raise SourceConflict("source_identity_payload_conflict")
                self._request_hashes[event.request_id] = event.payload_hash
                self._request_responses[event.request_id] = event.response_id
                return IngestOutcome(self._copy(self._job(self._job_by_response[event.response_id])), replayed=True)
            self._responses[event.response_id] = self._copy(event)
            self._request_hashes[event.request_id] = event.payload_hash
            self._request_responses[event.request_id] = event.response_id
            payload = self._copy(event.payload)
            payload["create_time"] = event.create_time
            job = JobRecord(
                job_id=f"job-{uuid.uuid4().hex}", request_id=event.request_id,
                source_response_ref=hmac_reference(event.response_id, self._reference_key),
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
            self._audit("source_ingested", job)
            return IngestOutcome(self._copy(job), replayed=False)

    def set_control(self, name: str, enabled: bool) -> None:
        if name not in self._control or not isinstance(enabled, bool):
            raise RepositoryError("control_flag_invalid")
        with self._lock:
            self._control[name] = enabled

    def get_control(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._control)

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

    def ingest_source_event(self, event: SourceEvent, *, now: datetime | None = None) -> IngestOutcome:
        key = self._reference_key or os.environ.get("XB_MEMBER_GATEWAY_HMAC_KEY", "").encode("utf-8")
        if not key:
            raise RepositoryError("source_reference_key_required")
        source_ref = hmac_reference(event.response_id, key)
        payload = dict(event.payload)
        payload["create_time"] = event.create_time
        created_at = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT response_id,payload_hash FROM xb_member_gateway.ingest_receipts WHERE request_id=%s FOR UPDATE", (event.request_id,))
                receipt = cursor.fetchone()
                if receipt is not None and (receipt[0] != event.response_id or receipt[1] != event.payload_hash):
                    raise SourceConflict("request_identity_payload_conflict")
                cursor.execute("INSERT INTO xb_member_gateway.source_responses(response_id,source_response_ref,create_time,mapping_version,payload_hash,canonical_payload) VALUES(%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(response_id) DO NOTHING", (event.response_id,source_ref,event.create_time,event.mapping_version,event.payload_hash,canonical_json(payload)))
                cursor.execute("SELECT payload_hash FROM xb_member_gateway.source_responses WHERE response_id=%s FOR UPDATE", (event.response_id,))
                source = cursor.fetchone()
                if source is None:
                    raise RepositoryError("source_response_insert_failed")
                if source[0] != event.payload_hash:
                    raise SourceConflict("source_identity_payload_conflict")
                cursor.execute("INSERT INTO xb_member_gateway.source_observations(response_id,request_id,form_alias,create_time,mapping_version,payload_hash,canonical_payload) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)", (event.response_id,event.request_id,event.form_alias,event.create_time,event.mapping_version,event.payload_hash,canonical_json(payload)))
                cursor.execute("SELECT job_id FROM xb_member_gateway.jobs WHERE response_id=%s", (event.response_id,))
                job_row = cursor.fetchone()
                if receipt is not None or job_row is not None:
                    cursor.execute("INSERT INTO xb_member_gateway.ingest_receipts(request_id,response_id,payload_hash,replayed) VALUES(%s,%s,%s,TRUE) ON CONFLICT(request_id) DO UPDATE SET replayed=TRUE,received_at=now()", (event.request_id,event.response_id,event.payload_hash))
                    if job_row is None:
                        raise RepositoryError("job_missing_for_source_response")
                    return IngestOutcome(self._select_job(cursor,job_row[0]), replayed=True)
                job_id = f"job-{uuid.uuid4().hex}"
                cursor.execute("INSERT INTO xb_member_gateway.ingest_receipts(request_id,response_id,payload_hash,replayed) VALUES(%s,%s,%s,FALSE)", (event.request_id,event.response_id,event.payload_hash))
                cursor.execute("INSERT INTO xb_member_gateway.jobs(job_id,response_id,operation,payload_hash,canonical_payload,state,state_version,attempt_count,max_attempts,created_at) VALUES(%s,%s,'member.create',%s,%s::jsonb,'QUEUED',2,0,3,%s)", (job_id,event.response_id,event.payload_hash,canonical_json(payload),created_at))
                return IngestOutcome(JobRecord(job_id,event.request_id,source_ref,event.response_id,event.payload_hash,event.operation,payload,timestamp(created_at),state=JobState.QUEUED,state_version=2,source_system=event.source_system,form_alias=event.form_alias,mapping_version=event.mapping_version), replayed=False)

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
            cursor.execute("INSERT INTO xb_member_gateway.results(job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", params)
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
                if hold.state == WriterHoldState.QUARANTINED:
                    return hold
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
                    cursor.execute("INSERT INTO xb_member_gateway.results(job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", event_params)
                if result.status == ResultStatus.CREATED_VERIFIED and job.state == JobState.WRITING:
                    self._advance(cursor,job,JobState.READBACK,current)
                target = {ResultStatus.CREATED_VERIFIED:JobState.CREATED_VERIFIED,ResultStatus.WRITE_OUTCOME_UNCERTAIN:JobState.WRITE_OUTCOME_UNCERTAIN,ResultStatus.CONFIRMED_NOT_CREATED:JobState.CONFIRMED_NOT_CREATED,ResultStatus.CREATED_READBACK_MISMATCH:JobState.CREATED_READBACK_MISMATCH}[result.status]
                if job.state != target:
                    self._advance(cursor,job,target,current,{"result_status":result.status.value,"save_invocation_count":1,"last_error_code":result.error_code})
                else:
                    cursor.execute("UPDATE xb_member_gateway.jobs SET result_status=%s,save_invocation_count=1,last_error_code=%s,updated_at=%s WHERE job_id=%s", (result.status.value,result.error_code,current,result.job_id))
                if require_lease:
                    cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (result.job_id,))
                    cursor.execute("UPDATE xb_member_gateway.jobs SET lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE job_id=%s", (current,result.job_id))
                    cursor.execute("UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='CLEARED',state_version=state_version+1,cleared_at=%s,updated_at=%s WHERE job_id=%s AND lifecycle='TERMINATION_CONFIRMED'", (current, current, result.job_id))
                    self._bump_writer_termination_gate(cursor)
                return result, False

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
