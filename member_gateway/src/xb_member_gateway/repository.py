"""Durable member-gateway state and an offline repository fake.

The fake is used by package tests.  The PostgreSQL adapter commits every
state-changing action in a short transaction; AutoCount calls are never made
from this module or while a repository transaction is open.
"""

from __future__ import annotations

import copy
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Iterator, Mapping, Protocol

from .canonical import canonical_json
from .crypto import hmac_reference
from .models import (
    AllocationProbe, AllocationRecord, DispatchFenceRecord, IngestOutcome,
    JobRecord, JobState, LeaseRecord, ProbeStatus, ResultRecord, ResultStatus,
    SourceEvent, WriteIntentRecord,
)
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

    def reclaim_expired(self, *, now: datetime | None = None) -> int:
        current = utc_now(now)
        with self._lock:
            count = 0
            for job in self._jobs.values():
                if job.state in TERMINAL_STATES:
                    self._leases.pop(job.job_id, None)
                    continue
                lease = self._leases.get(job.job_id)
                if lease is None or parse_timestamp(lease.expires_at) > current:
                    continue
                if job.dispatch_fenced or job.state == JobState.WRITING:
                    if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
                        self._move(job, JobState.WRITE_OUTCOME_UNCERTAIN)
                    job.last_error_code = "lease_expired_after_dispatch"
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
            self.reclaim_expired(now=current)
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

    def record_probe(self, job_id: str, candidate: str, status: ProbeStatus, probe_reference: str, worker_id: str, *, now: datetime | None = None) -> AllocationProbe:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            self._lease(job, worker_id, current)
            if job.dispatch_fenced or job.state not in {JobState.PRECHECKING, JobState.ALLOCATION_BOUND}:
                raise AllocationConflict("probe_state_invalid")
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
            self._lease(job, worker_id, current)
            if job.dispatch_fenced:
                raise AllocationConflict("allocation_after_dispatch_fence")
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
            self._lease(job, worker_id, current)
            allocation = self._allocations.get(job_id)
            if allocation is None or job.state != JobState.ALLOCATION_BOUND:
                raise AllocationConflict("bound_allocation_required")
            if ProbeStatus(status) == ProbeStatus.FREE and probe_reference == allocation.probe_reference:
                return self._copy(job)
            self._probes[job_id].append(AllocationProbe(job_id, allocation.member_no, ProbeStatus(status), probe_reference, timestamp(current)))
            self._move(job, JobState.MANUAL_REVIEW)
            job.last_error_code = "bound_member_no_recheck_not_free"
            self._audit("allocation_recheck_failed", job, error_code=job.last_error_code)
            return self._copy(job)

    def get_allocation(self, job_id: str) -> AllocationRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._allocations.get(job_id))

    def record_write_intent(self, job_id: str, member_no: str, operation: str, payload_hash_value: str, worker_id: str, *, now: datetime | None = None) -> WriteIntentRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            self._lease(job, worker_id, current)
            if operation != "member.create" or job.operation != operation:
                raise RepositoryError("operation_invalid")
            allocation = self._allocations.get(job_id)
            if allocation is None or allocation.member_no != member_no:
                raise RepositoryError("allocation_binding_required")
            existing = self._write_intents.get(job_id)
            if existing is not None:
                if existing.member_no != member_no or existing.payload_hash != payload_hash_value:
                    raise SourceConflict("write_intent_payload_conflict")
                if job.state == JobState.ALLOCATION_BOUND:
                    self._move(job, JobState.WRITE_INTENT_RECORDED)
                return self._copy(existing)
            if job.state != JobState.ALLOCATION_BOUND:
                raise RepositoryError("write_intent_state_invalid")
            intent = WriteIntentRecord(job_id, f"intent-{uuid.uuid4().hex}", member_no, payload_hash_value, timestamp(current))
            self._write_intents[job_id] = intent
            job.write_intent_id = intent.intent_id
            self._move(job, JobState.WRITE_INTENT_RECORDED)
            self._audit("write_intent_recorded", job)
            return self._copy(intent)

    def get_write_intent(self, job_id: str) -> WriteIntentRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._write_intents.get(job_id))

    def record_dispatch_fence(self, job_id: str, member_no: str, operation: str, worker_id: str, *, now: datetime | None = None) -> DispatchFenceRecord:
        current = utc_now(now)
        with self._lock:
            job = self._job(job_id)
            existing = self._fences.get(job_id)
            if existing is not None:
                if existing.member_no != member_no or existing.operation != operation:
                    raise AllocationConflict("dispatch_fence_binding_invalid")
                return self._copy(existing)
            self._lease(job, worker_id, current)
            intent = self._write_intents.get(job_id)
            if intent is None or intent.member_no != member_no or operation != "member.create" or job.state != JobState.WRITE_INTENT_RECORDED or job.save_invocation_count != 0:
                raise RepositoryError("write_intent_required")
            fence = DispatchFenceRecord(job_id, f"fence-{uuid.uuid4().hex}", member_no, operation, timestamp(current))
            self._fences[job_id] = fence
            job.dispatch_fence_id = fence.fence_id
            self._move(job, JobState.WRITING)
            self._audit("dispatch_fence_created", job)
            return self._copy(fence)

    def get_dispatch_fence(self, job_id: str) -> DispatchFenceRecord | None:
        with self._lock:
            self._job(job_id)
            return self._copy(self._fences.get(job_id))

    def acknowledge_result(self, result: ResultRecord, *, worker_id: str | None = None, require_lease: bool = True, now: datetime | None = None) -> tuple[ResultRecord, bool]:
        current = utc_now(now)
        with self._lock:
            job = self._job(result.job_id)
            fence = self._fences.get(result.job_id)
            if fence is None or fence.fence_id != result.dispatch_fence_id or fence.member_no != result.member_no:
                raise RepositoryError("dispatch_fence_binding_invalid")
            if result.save_invocation_count != 1:
                raise ResultConflict("save_invocation_count_must_be_one")
            if require_lease:
                if not worker_id:
                    raise LeaseConflict("worker_identity_required")
                self._lease(job, worker_id, current)
            existing = self._results.get(result.job_id)
            if existing is not None:
                if existing.result_hash == result.result_hash:
                    return self._copy(existing), True
                if existing.status != ResultStatus.WRITE_OUTCOME_UNCERTAIN or result.status == ResultStatus.WRITE_OUTCOME_UNCERTAIN:
                    self._result_conflicts.append({"job_id": result.job_id, "code": "result_payload_conflict"})
                    raise ResultConflict("result_payload_conflict")
                self._result_history.setdefault(result.job_id, []).append(self._copy(existing))
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
            self._results[result.job_id] = self._copy(result)
            self._result_history.setdefault(result.job_id, []).append(self._copy(result))
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
               'google_forms',COALESCE(obs.form_alias,''),COALESCE(obs.mapping_version,''),j.attempt_started_at
        FROM xb_member_gateway.jobs j
        JOIN xb_member_gateway.source_responses sr ON sr.response_id=j.response_id
        LEFT JOIN LATERAL (
            SELECT request_id,form_alias,mapping_version FROM xb_member_gateway.source_observations
            WHERE response_id=j.response_id ORDER BY observed_at DESC,observation_id DESC LIMIT 1
        ) obs ON TRUE
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
            dispatch_fence_id=str(row[18]) if row[18] is not None else None,save_invocation_count=int(row[19]),
            result_status=ResultStatus(row[20]) if row[20] else None,last_error_code=row[21],
            source_system=row[22],form_alias=row[23],mapping_version=row[24],
            attempt_started_at=cls._dt(row[25]),
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

    def _reclaim_expired_cursor(self, cursor: Any, current: datetime) -> int:
        cursor.execute("SELECT j.job_id,j.state,j.dispatch_fence_id,j.attempt_count,j.max_attempts FROM xb_member_gateway.jobs j JOIN xb_member_gateway.leases l ON l.job_id=j.job_id WHERE l.active=TRUE AND l.expires_at<=%s FOR UPDATE OF j,l", (current,))
        rows = cursor.fetchall()
        changed = 0
        for job_id,state_value,fence_id,attempts,max_attempts in rows:
            if state_value in {
                JobState.CREATED_VERIFIED.value, JobState.CONFIRMED_NOT_CREATED.value,
                JobState.CREATED_READBACK_MISMATCH.value, JobState.MANUAL_REVIEW.value,
                JobState.DEAD_LETTER.value, JobState.REJECTED_VALIDATION.value,
            }:
                cursor.execute("UPDATE xb_member_gateway.leases SET active=FALSE WHERE job_id=%s AND active=TRUE", (job_id,))
                continue
            if fence_id is not None or state_value == JobState.WRITING.value:
                target,error = JobState.WRITE_OUTCOME_UNCERTAIN,"lease_expired_after_dispatch"
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
                return self._reclaim_expired_cursor(cursor, current)

    def claim_job(self, worker_id: str, *, lease_seconds: int = 600, now: datetime | None = None) -> JobRecord | None:
        if not worker_id or any(character.isspace() for character in worker_id):
            raise RepositoryError("worker_id_invalid")
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._reclaim_expired_cursor(cursor, current)
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
                job = self._select_job(cursor,job_id,for_update=True)
                self._require_lease(cursor,job,worker_id,current)
                if job.state != JobState.ALLOCATION_BOUND or not job.allocation_member_no:
                    raise AllocationConflict("bound_allocation_required")
                if ProbeStatus(status) == ProbeStatus.FREE and probe_reference == job.allocation_probe_reference:
                    return job
                cursor.execute("INSERT INTO xb_member_gateway.allocation_probes(job_id,candidate,probe_status,probe_reference,observed_at) VALUES(%s,%s,%s,%s,%s)", (job_id,job.allocation_member_no,ProbeStatus(status).value,probe_reference,current))
                return self._advance(cursor,job,JobState.MANUAL_REVIEW,current,{"last_error_code":"bound_member_no_recheck_not_free"})

    def record_write_intent(self, job_id: str, member_no: str, operation: str, payload_hash_value: str, worker_id: str, *, now: datetime | None = None) -> WriteIntentRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor,job_id,for_update=True)
                self._require_lease(cursor,job,worker_id,current)
                if operation != "member.create" or job.operation != operation:
                    raise RepositoryError("operation_invalid")
                cursor.execute("SELECT member_no FROM xb_member_gateway.member_allocations WHERE job_id=%s", (job_id,))
                allocation = cursor.fetchone()
                if allocation is None or allocation[0] != member_no:
                    raise RepositoryError("allocation_binding_required")
                cursor.execute("SELECT intent_id,member_no,payload_hash,recorded_at FROM xb_member_gateway.write_intents WHERE job_id=%s FOR UPDATE", (job_id,))
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[1] != member_no or existing[2] != payload_hash_value:
                        raise SourceConflict("write_intent_payload_conflict")
                    if job.state == JobState.ALLOCATION_BOUND:
                        self._advance(cursor,job,JobState.WRITE_INTENT_RECORDED,current,{"write_intent_id":str(existing[0])})
                    return WriteIntentRecord(job_id,str(existing[0]),existing[1],existing[2],self._dt(existing[3]) or "")
                if job.state != JobState.ALLOCATION_BOUND:
                    raise RepositoryError("write_intent_state_invalid")
                intent_id = str(uuid.uuid4())
                cursor.execute("INSERT INTO xb_member_gateway.write_intents(intent_id,job_id,operation,member_no,payload_hash,recorded_at) VALUES(%s,%s,%s,%s,%s,%s)", (intent_id,job_id,operation,member_no,payload_hash_value,current))
                self._advance(cursor,job,JobState.WRITE_INTENT_RECORDED,current,{"write_intent_id":intent_id})
                return WriteIntentRecord(job_id,intent_id,member_no,payload_hash_value,timestamp(current))

    def get_write_intent(self, job_id: str) -> WriteIntentRecord | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor,job_id)
                cursor.execute("SELECT intent_id,member_no,payload_hash,recorded_at FROM xb_member_gateway.write_intents WHERE job_id=%s", (job_id,))
                row = cursor.fetchone()
                return None if row is None else WriteIntentRecord(job_id,str(row[0]),row[1],row[2],self._dt(row[3]) or "")

    def record_dispatch_fence(self, job_id: str, member_no: str, operation: str, worker_id: str, *, now: datetime | None = None) -> DispatchFenceRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor,job_id,for_update=True)
                cursor.execute("SELECT fence_id,member_no,operation,created_at FROM xb_member_gateway.dispatch_fences WHERE job_id=%s FOR UPDATE", (job_id,))
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[1] != member_no or existing[2] != operation:
                        raise AllocationConflict("dispatch_fence_binding_invalid")
                    return DispatchFenceRecord(job_id,str(existing[0]),existing[1],existing[2],self._dt(existing[3]) or "")
                self._require_lease(cursor,job,worker_id,current)
                cursor.execute("SELECT member_no FROM xb_member_gateway.write_intents WHERE job_id=%s", (job_id,))
                intent = cursor.fetchone()
                if intent is None or intent[0] != member_no or operation != "member.create" or job.state != JobState.WRITE_INTENT_RECORDED or job.save_invocation_count != 0:
                    raise RepositoryError("write_intent_required")
                fence_id = str(uuid.uuid4())
                cursor.execute("INSERT INTO xb_member_gateway.dispatch_fences(fence_id,job_id,operation,member_no,created_at,save_invocation_count) VALUES(%s,%s,%s,%s,%s,0)", (fence_id,job_id,operation,member_no,current))
                self._advance(cursor,job,JobState.WRITING,current,{"dispatch_fence_id":fence_id})
                return DispatchFenceRecord(job_id,fence_id,member_no,operation,timestamp(current))

    def get_dispatch_fence(self, job_id: str) -> DispatchFenceRecord | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor,job_id)
                cursor.execute("SELECT fence_id,member_no,operation,created_at FROM xb_member_gateway.dispatch_fences WHERE job_id=%s", (job_id,))
                row = cursor.fetchone()
                return None if row is None else DispatchFenceRecord(job_id,str(row[0]),row[1],row[2],self._dt(row[3]) or "")

    def acknowledge_result(self, result: ResultRecord, *, worker_id: str | None = None, require_lease: bool = True, now: datetime | None = None) -> tuple[ResultRecord, bool]:
        current = utc_now(now)
        if result.save_invocation_count != 1:
            raise ResultConflict("save_invocation_count_must_be_one")
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor,result.job_id,for_update=True)
                cursor.execute("SELECT fence_id,member_no FROM xb_member_gateway.dispatch_fences WHERE job_id=%s FOR UPDATE", (result.job_id,))
                fence = cursor.fetchone()
                if fence is None or str(fence[0]) != result.dispatch_fence_id or fence[1] != result.member_no:
                    raise RepositoryError("dispatch_fence_binding_invalid")
                if require_lease:
                    if not worker_id:
                        raise LeaseConflict("worker_identity_required")
                    self._require_lease(cursor,job,worker_id,current)
                cursor.execute("SELECT result_hash,status FROM xb_member_gateway.results WHERE job_id=%s FOR UPDATE", (result.job_id,))
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[0] == result.result_hash:
                        return result, True
                    if existing[1] != ResultStatus.WRITE_OUTCOME_UNCERTAIN.value or result.status == ResultStatus.WRITE_OUTCOME_UNCERTAIN:
                        cursor.execute("INSERT INTO xb_member_gateway.result_conflicts(job_id,expected_hash,observed_hash) VALUES(%s,%s,%s)", (result.job_id,existing[0],result.result_hash))
                        raise ResultConflict("result_payload_conflict")
                    cursor.execute("INSERT INTO xb_member_gateway.result_events(job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) SELECT job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at FROM xb_member_gateway.results WHERE job_id=%s", (result.job_id,))
                    cursor.execute("DELETE FROM xb_member_gateway.results WHERE job_id=%s", (result.job_id,))
                cursor.execute("INSERT INTO xb_member_gateway.result_events(job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (result.job_id,result.result_hash,result.status.value,result.member_no,result.dispatch_fence_id,result.save_invocation_count,result.readback_found,result.readback_match,result.reconciliation_required,result.error_code,current))
                cursor.execute("INSERT INTO xb_member_gateway.results(job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (result.job_id,result.result_hash,result.status.value,result.member_no,result.dispatch_fence_id,result.save_invocation_count,result.readback_found,result.readback_match,result.reconciliation_required,result.error_code,current))
                if result.status == ResultStatus.CREATED_VERIFIED and job.state == JobState.WRITING:
                    self._advance(cursor,job,JobState.READBACK,current)
                target = {ResultStatus.CREATED_VERIFIED:JobState.CREATED_VERIFIED,ResultStatus.WRITE_OUTCOME_UNCERTAIN:JobState.WRITE_OUTCOME_UNCERTAIN,ResultStatus.CONFIRMED_NOT_CREATED:JobState.CONFIRMED_NOT_CREATED,ResultStatus.CREATED_READBACK_MISMATCH:JobState.CREATED_READBACK_MISMATCH}[result.status]
                if job.state != target:
                    self._advance(cursor,job,target,current,{"result_status":result.status.value,"save_invocation_count":1,"last_error_code":result.error_code})
                else:
                    cursor.execute("UPDATE xb_member_gateway.jobs SET result_status=%s,save_invocation_count=1,last_error_code=%s,updated_at=%s WHERE job_id=%s", (result.status.value,result.error_code,current,result.job_id))
                return result, False

    def get_result(self, job_id: str) -> ResultRecord | None:
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                self._select_job(cursor,job_id)
                cursor.execute("SELECT result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,error_code,acknowledged_at FROM xb_member_gateway.results WHERE job_id=%s", (job_id,))
                row = cursor.fetchone()
                return None if row is None else ResultRecord(job_id,row[0],ResultStatus(row[1]),row[2],str(row[3]),int(row[4]),bool(row[5]),bool(row[6]),bool(row[7]),row[8],self._dt(row[9]) or "")

    def mark_state(self, job_id: str, target: JobState, *, worker_id: str | None = None, require_lease: bool = False, error_code: str | None = None, now: datetime | None = None) -> JobRecord:
        current = utc_now(now)
        with self._transaction() as connection:
            with connection.cursor() as cursor:
                job = self._select_job(cursor,job_id,for_update=True)
                if require_lease:
                    if worker_id is None:
                        raise LeaseConflict("worker_identity_required")
                    self._require_lease(cursor,job,worker_id,current)
                return self._advance(cursor,job,JobState(target),current,{"last_error_code":error_code} if error_code else None)
