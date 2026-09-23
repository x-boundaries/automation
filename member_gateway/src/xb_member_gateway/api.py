"""Small protected HTTP boundary for the member-first gateway."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .allocation import AllocationError, MemberNoAllocator
from .auth import (
    Authenticator,
    AuthenticationError,
    DenyAllAuthenticator,
    Principal,
    WORKER_SESSION_HEADER,
    require_scope,
    worker_host_binding,
    worker_session,
)
from .canonical import (
    CanonicalizationError,
    canonicalize_source_event,
    canonicalize_source_rejection,
    validate_google_create_time_exact,
)
from .config import GatewayConfig
from .eligibility import EligibilityContext, evaluate_eligibility
from .models import JobState, ProbeStatus, ResultStatus, source_cursor_v2
from .notifications import (
    WELCOME_JOB_SCHEMA_VERSION,
    WelcomeEmailError,
    claim_envelope,
    validate_lease_request,
    validate_outbox_id,
    validate_result_request,
)
from .repository import (
    AllocationConflict,
    InMemoryRepository,
    JobNotFound,
    LeaseConflict,
    RepositoryError,
    ResultConflict,
    SourceConflict,
    SourceRestartReasonInvalid,
    WriterTerminationConflict,
)
from .results import ResultValidationError, make_result, result_contract
from .state_machine import InvalidTransition


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    body: dict[str, Any]


_SAFE_CODE_RE = re.compile(r"^[a-z0-9_.:-]{1,80}$")
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
_APPROVAL_RE = re.compile(r"^[A-Za-z0-9._:/#-]{1,160}$")
_EXECUTION_ID_RE = re.compile(r"^exec-[A-Za-z0-9]{16,80}$")
_EVIDENCE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_PROCESS_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?Z$")


def _safe_code(value: Any, default: str = "request_rejected") -> str:
    candidate = str(value)
    return candidate if _SAFE_CODE_RE.fullmatch(candidate) else default


def _json_body(body: bytes | str | Mapping[str, Any] | None) -> Mapping[str, Any]:
    if isinstance(body, Mapping):
        return body
    if body is None or body == b"" or body == "":
        return {}
    try:
        value = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "invalid_json") from exc
    if not isinstance(value, Mapping):
        raise ApiError(400, "json_object_required")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str]) -> None:
    if set(value) != fields:
        raise ApiError(400, "request_fields_invalid")


def _strict_bool(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise ApiError(400, code)
    return value


def _state_version(value: Any, code: str = "source_epoch_state_version_invalid") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiError(400, code)
    return value


class GatewayService:
    """Application service shared by the standard-library HTTP adapter and tests."""

    def __init__(
        self,
        config: GatewayConfig,
        repository: Any,
        *,
        adapter_ready: bool | None = None,
        clock=None,
    ):
        self.config = config
        self.repository = repository
        self.adapter_ready = config.autocount_adapter_ready if adapter_ready is None else adapter_ready
        self.clock = clock

    def _runtime_config(self) -> GatewayConfig:
        control = self.repository.get_control()
        return replace(
            self.config,
            production_activation_enabled=control.get("production_activation_enabled", False),
            kill_switch_enabled=control.get("kill_switch_enabled", True),
        )

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "service": "xb-member-gateway", "schema_version": "xb.member.gateway.error.v1"}

    def readiness(self) -> dict[str, Any]:
        config = self._runtime_config()
        reasons = list(config.readiness_reasons())
        if not self.adapter_ready:
            reasons.append("autocount_adapter_not_ready")
        return {
            "status": "ready" if not reasons else "not_ready",
            "ready": not reasons,
            "reasons": sorted(set(reasons)),
            "worker_concurrency": config.worker_concurrency,
            "claim_size": config.claim_size,
            "lease_seconds": config.lease_seconds,
            "heartbeat_seconds": config.heartbeat_seconds,
            "execution_deadline_seconds": config.execution_deadline_seconds,
        }

    def ingest(self, body: Mapping[str, Any]) -> dict[str, Any]:
        event = canonicalize_source_event(body)
        validate_google_create_time_exact(event.create_time, field="create_time")
        config = self._runtime_config()
        if event.form_alias not in config.allowed_form_aliases:
            raise ApiError(422, "form_alias_not_allowlisted")
        if event.mapping_version not in config.allowed_mapping_versions:
            raise ApiError(422, "mapping_version_not_allowlisted")
        # The accepted-member cap comes only from the closed admission mode;
        # production activation no longer changes source admission semantics.
        outcome = self.repository.ingest_source_event(
            event,
            now=self.clock,
            initial_window_max=config.initial_window_max,
        )
        return {
            "schema_version": "xb.member.gateway.job.v2",
            "job_id": outcome.job.job_id,
            "operation": "member.create",
            "state": outcome.job.state.value,
            "replayed": outcome.replayed,
            "source_response_ref": outcome.job.source_response_ref,
            "payload_hash": outcome.job.payload_hash,
        }

    def claim(self, worker_id: str) -> dict[str, Any]:
        if not _WORKER_ID_RE.fullmatch(worker_id):
            raise ApiError(400, "worker_id_invalid")
        config = self._runtime_config()
        if config.kill_switch_enabled:
            raise ApiError(423, "kill_switch_enabled")
        job = self.repository.claim_job(worker_id, lease_seconds=config.lease_seconds, now=self.clock)
        if job is None:
            return {"schema_version": "xb.member.gateway.job.v2", "claimed": False, "job": None}
        return {"schema_version": "xb.member.gateway.job.v2", "claimed": True, "job": job.worker_dict()}

    def precheck(self, job_id: str, worker_id: str) -> dict[str, Any]:
        job = self.repository.begin_prechecking(job_id, worker_id, now=self.clock)
        return {"job_id": job.job_id, "state": job.state.value, "state_version": job.state_version}

    def heartbeat(self, job_id: str, worker_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"state_version"})
        if isinstance(body["state_version"], bool) or not isinstance(body["state_version"], int):
            raise ApiError(400, "state_version_invalid")
        config = self._runtime_config()
        lease = self.repository.heartbeat(
            job_id,
            worker_id,
            expected_state_version=body["state_version"],
            lease_seconds=config.lease_seconds,
            now=self.clock,
        )
        return {"job_id": job_id, "state_version": lease.state_version, "lease_expires_at": lease.expires_at}

    def _allocation_context(self, job: Any) -> tuple[MemberNoAllocator, str]:
        config = self._runtime_config()
        if not config.member_no_constraint_valid:
            raise ApiError(409, "member_no_max_length_required")
        base = job.member_payload.get("phone")
        if not isinstance(base, str):
            raise ApiError(422, "canonical_phone_missing")
        try:
            allocator = MemberNoAllocator(config.member_no_max_length)
        except AllocationError as exc:
            raise ApiError(422, "canonical_phone_missing") from exc
        if not allocator.validate_candidate(base, base):
            raise ApiError(422, "canonical_phone_missing")
        return allocator, base

    def _validate_candidate(self, job: Any, candidate: Any) -> str:
        if not isinstance(candidate, str) or not candidate:
            raise ApiError(400, "allocation_candidate_invalid")
        allocator, base = self._allocation_context(job)
        if not allocator.validate_candidate(base, candidate):
            raise ApiError(400, "allocation_candidate_invalid")
        return candidate

    def allocation_candidate(self, job_id: str, worker_id: str | None = None) -> dict[str, Any]:
        self.repository.assert_no_active_writer_hold()
        job = self.repository.get_job(job_id)
        if worker_id is not None:
            self.repository.assert_lease(job_id, worker_id, now=self.clock)
        allocation = self.repository.get_allocation(job_id)
        if allocation is not None:
            return {"job_id": job_id, "bound": True, "member_no": allocation.member_no}
        allocator, base = self._allocation_context(job)
        if job.state != JobState.PRECHECKING:
            raise ApiError(409, "allocation_candidate_state_invalid")
        attempted = {probe.candidate for probe in self.repository.get_probes(job_id)}
        for candidate in allocator.candidates(base):
            if candidate not in attempted:
                return {"job_id": job_id, "bound": False, "candidate": candidate}
        raise ApiError(409, "member_no_exhausted")

    def allocation_probe(self, job_id: str, worker_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"candidate", "status", "probe_reference"})
        candidate = body["candidate"]
        probe_reference = body["probe_reference"]
        job = self.repository.get_job(job_id)
        candidate = self._validate_candidate(job, candidate)
        if not isinstance(probe_reference, str) or not probe_reference or len(probe_reference) > 200:
            raise ApiError(400, "allocation_probe_identity_invalid")
        try:
            status = ProbeStatus(body["status"])
        except (ValueError, TypeError) as exc:
            raise ApiError(400, "allocation_probe_status_invalid") from exc
        self.repository.record_probe(job_id, candidate, status, probe_reference, worker_id, now=self.clock)
        if status == ProbeStatus.FREE:
            try:
                allocation = self.repository.bind_allocation(job_id, candidate, probe_reference, worker_id, now=self.clock)
            except AllocationConflict as exc:
                if str(exc) == "member_no_allocation_race":
                    # The candidate was free when probed but was durably claimed
                    # by another pre-fence job. The next suffix is deterministic
                    # and still pre-fence; a bound candidate is never reallocated.
                    return self.allocation_candidate(job_id, worker_id)
                raise
            return {"job_id": job_id, "state": JobState.ALLOCATION_BOUND.value, "bound": True, "member_no": allocation.member_no}
        if status == ProbeStatus.OCCUPIED:
            return self.allocation_candidate(job_id, worker_id)
        self.repository.mark_state(
            job_id,
            JobState.AMBIGUOUS_LOOKUP,
            worker_id=worker_id,
            require_lease=True,
            error_code="allocation_probe_not_conclusive",
            now=self.clock,
        )
        raise ApiError(409, "allocation_probe_not_positive_free")

    def allocation_recheck(self, job_id: str, worker_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"status", "probe_reference"})
        if not isinstance(body["probe_reference"], str) or not body["probe_reference"] or len(body["probe_reference"]) > 200:
            raise ApiError(400, "allocation_probe_identity_invalid")
        try:
            status = ProbeStatus(body["status"])
        except (ValueError, TypeError) as exc:
            raise ApiError(400, "allocation_probe_status_invalid") from exc
        job = self.repository.recheck_bound_allocation(
            job_id, worker_id, status, body["probe_reference"], now=self.clock
        )
        return {"job_id": job_id, "state": job.state.value, "member_no_recheck": status.value}

    def bind_allocation(self, job_id: str, worker_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"member_no", "probe_reference"})
        if not isinstance(body["probe_reference"], str):
            raise ApiError(400, "allocation_binding_invalid")
        job = self.repository.get_job(job_id)
        member_no = self._validate_candidate(job, body["member_no"])
        allocation = self.repository.bind_allocation(
            job_id, member_no, body["probe_reference"], worker_id, now=self.clock
        )
        return {"job_id": job_id, "state": JobState.ALLOCATION_BOUND.value, "bound": True, "member_no": allocation.member_no}

    def _eligibility(self, job_id: str, worker_id: str, *, principal_valid: bool) -> Any:
        job = self.repository.get_job(job_id)
        allocation = self.repository.get_allocation(job_id)
        config = self._runtime_config()
        positive_free = bool(
            allocation
            and any(
                probe.candidate == allocation.member_no and probe.status == ProbeStatus.FREE
                for probe in self.repository.get_probes(job_id)
            )
        )
        try:
            rate_allowed = self.repository.rate_allowed(job_id, worker_id, now=self.clock)
        except (LeaseConflict, RepositoryError):
            rate_allowed = None
        try:
            fresh_recheck = self.repository.get_fresh_recheck(job_id, worker_id, now=self.clock) is not None
        except (LeaseConflict, RepositoryError):
            fresh_recheck = None
        return evaluate_eligibility(
            EligibilityContext(
                config=config,
                job=job,
                allocation=allocation,
                worker_id=worker_id,
                lease_owner=job.lease_owner,
                positive_free_evidence=positive_free,
                fresh_bound_member_no_recheck=fresh_recheck,
                gateway_ready=config.worker_gateway_ready,
                autocount_adapter_ready=self.adapter_ready,
                worker_credential_valid=principal_valid,
                kill_switch_rechecked=not config.kill_switch_enabled,
                save_invocation_count=job.save_invocation_count,
                rate_allowed=rate_allowed,
                now=self.clock,
            )
        )

    def write_intent(self, job_id: str, worker_id: str, body: Mapping[str, Any], *, principal_valid: bool) -> dict[str, Any]:
        _exact_fields(body, {"operation", "member_no", "payload_hash"})
        if body["operation"] != "member.create":
            raise ApiError(400, "operation_invalid")
        job = self.repository.get_job(job_id)
        if body["member_no"] != job.allocation_member_no or body["payload_hash"] != job.payload_hash:
            raise ApiError(409, "write_intent_binding_invalid")
        decision = self._eligibility(job_id, worker_id, principal_valid=principal_valid)
        if not decision.eligible:
            raise ApiError(409, "dispatch_ineligible")
        intent = self.repository.record_write_intent(
            job_id, body["member_no"], body["operation"], body["payload_hash"], worker_id, now=self.clock
        )
        return {"job_id": job_id, "state": JobState.WRITE_INTENT_RECORDED.value, "intent_id": intent.intent_id}

    def dispatch_fence(self, job_id: str, worker_id: str, body: Mapping[str, Any], *, principal_valid: bool) -> dict[str, Any]:
        if set(body) not in ({"operation", "member_no"}, {"operation", "member_no", "host_binding"}):
            raise ApiError(400, "request_fields_invalid")
        if body["operation"] != "member.create":
            raise ApiError(400, "operation_invalid")
        job = self.repository.get_job(job_id)
        if body["member_no"] != job.allocation_member_no:
            raise ApiError(409, "dispatch_fence_binding_invalid")
        decision = self._eligibility(job_id, worker_id, principal_valid=principal_valid)
        if not decision.eligible:
            raise ApiError(409, "dispatch_ineligible")
        # Re-read control immediately before the irreversible fence transaction.
        if self._runtime_config().kill_switch_enabled:
            raise ApiError(423, "kill_switch_enabled")
        resolved_host = body.get("host_binding", f"host-{worker_id}")
        try:
            resolved_host = worker_host_binding(resolved_host)
        except AuthenticationError as exc:
            raise ApiError(400, exc.code) from exc
        fence = self.repository.record_dispatch_fence(
            job_id, body["member_no"], body["operation"], worker_id, host_binding=resolved_host, now=self.clock
        )
        return {"job_id": job_id, "state": JobState.WRITING.value, "dispatch_fence_id": fence.fence_id, "member_no": fence.member_no, "execution_id": fence.execution_id}

    @staticmethod
    def _writer_request(body: Mapping[str, Any], *, include_pid: bool = True, include_reason: bool = False, include_proof: bool = True) -> tuple[str, int, str, str, str | None, int | None, str | None, str | None]:
        fields = {"fence_id", "attempt", "execution_id", "host_binding"}
        if include_pid:
            fields |= {"pid", "process_start_time"}
        if include_reason:
            fields |= {"evidence_reference", "reason"}
        elif include_proof:
            fields |= {"evidence_type", "evidence_reference", "exit_code"}
        _exact_fields(body, fields)
        fence_id = body.get("fence_id")
        execution_id = body.get("execution_id")
        host = body.get("host_binding")
        if not isinstance(fence_id, str) or not re.fullmatch(r"^fence-[A-Za-z0-9]{16,64}$", fence_id):
            raise ApiError(400, "dispatch_fence_id_invalid")
        if not isinstance(execution_id, str) or not _EXECUTION_ID_RE.fullmatch(execution_id):
            raise ApiError(400, "writer_execution_identity_invalid")
        try:
            host = worker_host_binding(host)
        except AuthenticationError as exc:
            raise ApiError(400, exc.code) from exc
        attempt = body.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
            raise ApiError(400, "writer_attempt_invalid")
        pid = body.get("pid") if include_pid else None
        start = body.get("process_start_time") if include_pid else None
        if include_pid and pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise ApiError(400, "writer_process_identity_invalid")
        if include_pid and pid is not None and (not isinstance(start, str) or not _PROCESS_START_RE.fullmatch(start)):
            raise ApiError(400, "writer_process_identity_invalid")
        if include_reason:
            reference = body.get("evidence_reference")
            if not isinstance(reference, str) or not _EVIDENCE_REFERENCE_RE.fullmatch(reference):
                raise ApiError(400, "writer_quarantine_evidence_invalid")
            return fence_id, attempt, execution_id, host, "", pid, start, reference
        if not include_proof:
            return fence_id, attempt, execution_id, host, None, pid, start, None
        evidence_type = body.get("evidence_type")
        reference = body.get("evidence_reference")
        exit_code = body.get("exit_code")
        if evidence_type != "process_exit" or not isinstance(reference, str) or not _EVIDENCE_REFERENCE_RE.fullmatch(reference):
            raise ApiError(400, "writer_termination_proof_required")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not -(2**31) <= exit_code <= (2**31 - 1):
            raise ApiError(400, "writer_termination_proof_required")
        return fence_id, attempt, execution_id, host, evidence_type, pid, start, reference

    def register_writer_execution(self, job_id: str, worker_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        fence_id, attempt, execution_id, host, _, pid, start, _ = self._writer_request(body, include_proof=False)
        if pid is None or start is None:
            raise ApiError(400, "writer_process_identity_invalid")
        hold = self.repository.register_writer_execution(
            job_id, fence_id=fence_id, attempt=attempt, worker_session=worker_id,
            host_binding=host, execution_id=execution_id, pid=pid,
            process_start_time=start, now=self.clock,
        )
        return {"job_id": job_id, "lifecycle": hold.state.value, "registered": True, "execution_id": hold.execution_id}

    def confirm_writer_termination(self, job_id: str, worker_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        fence_id, attempt, execution_id, host, evidence_type, pid, start, reference = self._writer_request(body)
        if pid is None or start is None:
            raise ApiError(400, "writer_process_identity_invalid")
        hold = self.repository.confirm_writer_termination(
            job_id, fence_id=fence_id, attempt=attempt, worker_session=worker_id,
            host_binding=host, execution_id=execution_id, pid=pid,
            process_start_time=start, evidence_type=evidence_type,
            evidence_reference=reference, exit_code=body["exit_code"], now=self.clock,
        )
        return {"job_id": job_id, "lifecycle": hold.state.value, "termination_confirmed": True}

    def quarantine_writer_execution(self, job_id: str, worker_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        fence_id, attempt, execution_id, host, _, pid, start, reference = self._writer_request(body, include_reason=True)
        hold = self.repository.quarantine_writer_execution(
            job_id, fence_id=fence_id, attempt=attempt, worker_session=worker_id,
            host_binding=host, execution_id=execution_id, pid=pid,
            process_start_time=start, evidence_reference=reference,
            reason=str(body["reason"]), now=self.clock,
        )
        return {"job_id": job_id, "state": JobState.WRITER_TERMINATION_UNCONFIRMED.value, "lifecycle": hold.state.value, "quarantined": True}

    def recover_writer_termination(self, job_id: str, recovery_session: str, body: Mapping[str, Any]) -> dict[str, Any]:
        fence_id, attempt, execution_id, host, _, pid, start, reference = self._writer_request(body)
        if pid is None or start is None:
            raise ApiError(400, "writer_process_identity_invalid")
        result, duplicate = self.repository.recover_writer_termination(
            job_id, fence_id=fence_id, attempt=attempt, recovery_session=recovery_session,
            host_binding=host, execution_id=execution_id, pid=pid,
            process_start_time=start, evidence_reference=reference,
            exit_code=body["exit_code"], now=self.clock,
        )
        return {"job_id": job_id, "state": JobState.WRITE_OUTCOME_UNCERTAIN.value, "result_status": result.status.value, "result_hash": result.result_hash, "duplicate": duplicate}

    def acknowledge_result(self, job_id: str, worker_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        result_body = dict(body)
        result_body["job_id"] = job_id
        try:
            contract = result_contract(result_body)
            job = self.repository.get_job(job_id)
            fence = self.repository.get_dispatch_fence(job_id)
            if fence is None:
                raise ApiError(409, "dispatch_fence_missing")
            if contract["member_no"] != fence.member_no or contract["dispatch_fence_id"] != fence.fence_id:
                raise ApiError(409, "result_binding_invalid")
            result = make_result(
                job=job,
                fence=fence,
                status=contract["status"],
                save_invocation_count=contract["save_invocation_count"],
                readback_found=contract["readback_found"],
                readback_match=contract["readback_match"],
                error_code=contract["error_code"],
                acknowledged_at=self.clock,
            )
        except ApiError:
            raise
        except (ResultValidationError, ValueError, KeyError) as exc:
            raise ApiError(400, "result_invalid") from exc
        stored, duplicate = self.repository.acknowledge_result(result, worker_id=worker_id, require_lease=True, now=self.clock)
        return {"job_id": job_id, "state": stored.status.value, "duplicate": duplicate, "result_hash": stored.result_hash}

    def reconcile(self, job_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"member_no", "lookup_status", "readback_found", "readback_match", "error_code"})
        if not isinstance(body["member_no"], str):
            raise ApiError(400, "reconciliation_member_no_invalid")
        readback_found = _strict_bool(body["readback_found"], "readback_found_invalid")
        readback_match = _strict_bool(body["readback_match"], "readback_match_invalid")
        job = self.repository.get_job(job_id)
        fence = self.repository.get_dispatch_fence(job_id)
        allocation = self.repository.get_allocation(job_id)
        if fence is None or allocation is None or body["member_no"] != fence.member_no:
            raise ApiError(409, "reconciliation_member_binding_invalid")
        if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
            if job.state == JobState.WRITER_TERMINATION_UNCONFIRMED:
                raise ApiError(423, "writer_termination_quarantine_active")
            raise ApiError(409, "reconciliation_requires_uncertain_state")
        try:
            status = {
                "exact_match": ResultStatus.CREATED_VERIFIED,
                "absent": ResultStatus.CONFIRMED_NOT_CREATED,
                "mismatch": ResultStatus.CREATED_READBACK_MISMATCH,
                "ambiguous": ResultStatus.WRITE_OUTCOME_UNCERTAIN,
            }[body["lookup_status"]]
        except (KeyError, TypeError) as exc:
            raise ApiError(400, "reconciliation_status_invalid") from exc
        case = self.repository.open_reconciliation_case(job_id, body["member_no"], now=self.clock)
        try:
            self.repository.record_reconciliation_check(
                case.case_id,
                body["lookup_status"],
                readback_found,
                readback_match,
                now=self.clock,
            )
        except RepositoryError as exc:
            raise ApiError(409, _safe_code(str(exc))) from exc
        result = make_result(
            job=job,
            fence=fence,
            status=status,
            save_invocation_count=1,
            readback_found=readback_found,
            readback_match=readback_match,
            error_code=body["error_code"],
            acknowledged_at=self.clock,
        )
        stored, duplicate = self.repository.acknowledge_result(
            result,
            require_lease=False,
            reconciliation_case_id=case.case_id,
            now=self.clock,
        )
        return {"job_id": job_id, "state": stored.status.value, "duplicate": duplicate, "case_id": case.case_id}

    def status(self, job_id: str) -> dict[str, Any]:
        return self.repository.get_job(job_id).safe_dict()

    def reject_source(self, body: Mapping[str, Any]) -> dict[str, Any]:
        rejection = canonicalize_source_rejection(body)
        if rejection.form_alias not in self.config.allowed_form_aliases:
            raise ApiError(422, "form_alias_not_allowlisted")
        if rejection.mapping_version not in self.config.allowed_mapping_versions:
            raise ApiError(422, "mapping_version_not_allowlisted")
        outcome = self.repository.reject_source_response(rejection, now=self.clock)
        return {
            "schema_version": "xb.member.gateway.source_rejection_receipt.v1",
            "rejection_id": outcome.rejection_id,
            "outcome": "REJECTED",
            "error_code": outcome.error_code,
            "replayed": outcome.replayed,
            "source_response_ref": outcome.source_response_ref,
        }

    def _cursor_binding(self, form_alias: Any, mapping_version: Any) -> None:
        if form_alias not in self.config.allowed_form_aliases or mapping_version not in self.config.allowed_mapping_versions:
            raise ApiError(422, "source_cursor_binding_invalid")

    def _cursor_v2(self, form_alias: str, mapping_version: str) -> dict[str, Any]:
        cursor, epoch, page = self.repository.get_source_cursor(form_alias, mapping_version)
        if cursor.production_cutover_exact is None:
            raise SourceConflict("source_production_cutover_uninitialized")
        return source_cursor_v2(cursor, admission_mode=self.config.admission_mode, epoch=epoch, open_page=page)

    def source_cursor(self, form_alias: str, mapping_version: str) -> dict[str, Any]:
        self._cursor_binding(form_alias, mapping_version)
        return self._cursor_v2(form_alias, mapping_version)

    def begin_source_epoch(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"form_alias", "mapping_version"})
        self._cursor_binding(body["form_alias"], body["mapping_version"])
        _, resumed = self.repository.begin_source_epoch(
            body["form_alias"], body["mapping_version"], admission_mode=self.config.admission_mode,
            form_id=self.config.source_form_id, now=self.clock,
        )
        value = self._cursor_v2(body["form_alias"], body["mapping_version"])
        value["resumed"] = resumed
        return value

    def restart_source_epoch(self, epoch_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"expected_epoch_state_version", "restart_reason"})
        if not isinstance(body["restart_reason"], str):
            raise ApiError(422, "source_restart_reason_invalid")
        successor = self.repository.restart_source_epoch(
            epoch_id, expected_epoch_state_version=_state_version(body["expected_epoch_state_version"]),
            restart_reason=body["restart_reason"], now=self.clock,
        )
        value = self._cursor_v2(successor.form_alias, successor.mapping_version)
        value["resumed"] = False
        return value

    def open_source_page(self, epoch_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"expected_epoch_state_version", "request_page_token", "next_page_token", "terminal", "items"})
        if not isinstance(body["terminal"], bool) or not isinstance(body["items"], list):
            raise ApiError(400, "source_page_request_invalid")
        page, epoch, replayed = self.repository.open_source_page(
            epoch_id, expected_epoch_state_version=_state_version(body["expected_epoch_state_version"]),
            request_page_token=body["request_page_token"], next_page_token=body["next_page_token"],
            terminal=body["terminal"], items=body["items"], now=self.clock,
        )
        return {
            "schema_version": "xb.member.gateway.source_page.v1", "page_id": page.page_id,
            "epoch_id": epoch.epoch_id, "page_state": page.page_state.value, "page_ordinal": page.page_ordinal,
            "item_count": len(page.items), "terminal": page.terminal,
            "epoch_state_version": page.opened_state_version, "replayed": replayed,
        }

    def commit_source_page(self, page_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"expected_epoch_state_version"})
        page, epoch, replayed = self.repository.commit_source_page(
            page_id, expected_epoch_state_version=_state_version(body["expected_epoch_state_version"]), now=self.clock,
        )
        return {
            "schema_version": "xb.member.gateway.source_page.v1", "page_id": page.page_id,
            "epoch_id": epoch.epoch_id, "page_state": page.page_state.value, "page_ordinal": page.page_ordinal,
            "item_count": len(page.items), "terminal": page.terminal,
            "epoch_state_version": page.committed_state_version, "epoch_status": epoch.status.value,
            "current_page_token": epoch.current_page_token,
            "replayed": replayed,
        }

    def claim_welcome_email(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if body:
            _exact_fields(body, set())
        if self._runtime_config().kill_switch_enabled:
            raise ApiError(423, "kill_switch_enabled")
        claimed = self.repository.claim_welcome_email(now=self.clock)
        if claimed is None:
            return {"schema_version": WELCOME_JOB_SCHEMA_VERSION, "claimed": False, "job": None}
        outbox, message = claimed
        return {"schema_version": WELCOME_JOB_SCHEMA_VERSION, "claimed": True, "job": claim_envelope(outbox, message)}

    def welcome_send_intent(self, outbox_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        lease_id, version = validate_lease_request(body)
        outbox, replayed = self.repository.record_welcome_send_intent(
            validate_outbox_id(outbox_id), lease_id=lease_id, expected_state_version=version, now=self.clock,
        )
        return {"outbox_id": outbox.outbox_id, "state": outbox.state.value, "state_version": outbox.state_version, "replayed": replayed}

    def welcome_result(self, outbox_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        lease_id, version, outcome, error_code = validate_result_request(body)
        outbox, duplicate = self.repository.record_welcome_result(
            validate_outbox_id(outbox_id), lease_id=lease_id, expected_state_version=version,
            outcome=outcome, error_code=error_code, now=self.clock,
        )
        return {"outbox_id": outbox.outbox_id, "state": outbox.state.value, "state_version": outbox.state_version, "duplicate": duplicate}

    def operator_status(self) -> dict[str, Any]:
        return self.repository.operator_status()

    def operator_reconciliation(self, job_id: str) -> dict[str, Any]:
        return self.repository.operator_reconciliation(job_id)

    def disable_kill_switch(self) -> dict[str, Any]:
        self.repository.set_control("kill_switch_enabled", False)
        return {"status": "disabled", "kill_switch_enabled": False}

    def enable_kill_switch(self) -> dict[str, Any]:
        self.repository.set_control("kill_switch_enabled", True)
        return {"status": "enabled", "kill_switch_enabled": True}

    def enable_activation(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"enabled", "environment", "approval_reference"})
        if body["enabled"] is not True or body["environment"] != self.config.expected_environment:
            raise ApiError(422, "activation_request_invalid")
        if not self.config.member_no_constraint_valid:
            raise ApiError(422, "member_no_max_length_required")
        if not isinstance(body["approval_reference"], str) or not _APPROVAL_RE.fullmatch(body["approval_reference"]):
            raise ApiError(422, "activation_reference_required")
        self.repository.set_control("production_activation_enabled", True)
        return {"status": "activation_enabled", "production_activation_enabled": True}


class GatewayApp:
    """Testable JSON router and standard-library HTTPS-fronted HTTP adapter."""

    def __init__(self, service: GatewayService, authenticator: Authenticator | None = None):
        self.service = service
        self.authenticator = authenticator or DenyAllAuthenticator()

    def _principal(self, headers: Mapping[str, str], scope: str) -> Principal:
        try:
            current = self.authenticator.authenticate(headers)
            require_scope(current, scope)
            return current
        except AuthenticationError as exc:
            raise ApiError(403 if exc.code == "scope_denied" else 401, exc.code) from exc

    def _worker_session(self, headers: Mapping[str, str]) -> str:
        value = next(
            (str(candidate) for key, candidate in headers.items() if str(key).lower() == WORKER_SESSION_HEADER.lower()),
            None,
        )
        try:
            return worker_session(value)  # type: ignore[arg-type]
        except AuthenticationError as exc:
            raise ApiError(400, exc.code) from exc

    def _error(self, error: Exception) -> ApiResponse:
        code = _safe_code(getattr(error, "code", None), "request_rejected")
        status = getattr(error, "status", 500)
        if isinstance(error, JobNotFound):
            status, code = 404, "job_not_found"
        elif isinstance(error, SourceRestartReasonInvalid):
            status, code = 422, "source_restart_reason_invalid"
        elif isinstance(error, SourceConflict):
            status, code = 409, _safe_code(str(error), "source_identity_conflict")
        elif isinstance(error, WelcomeEmailError):
            status = 404 if error.code == "welcome_email_not_found" else 400 if error.code.endswith("_invalid") else 409
            code = _safe_code(error.code)
        elif isinstance(error, ResultConflict):
            status, code = 409, "result_conflict"
        elif isinstance(error, WriterTerminationConflict):
            status, code = 423 if str(error) == "writer_termination_quarantine_active" else 409, _safe_code(str(error))
        elif isinstance(error, RepositoryError) and str(error) == "kill_switch_enabled":
            status, code = 423, "kill_switch_enabled"
        elif isinstance(error, (LeaseConflict, AllocationConflict, InvalidTransition, RepositoryError)):
            status, code = 409, _safe_code(str(error))
        elif isinstance(error, CanonicalizationError):
            status, code = 400, _safe_code(error.code)
        elif isinstance(error, ValueError) and status == 500:
            status, code = 400, "request_invalid"
        return ApiResponse(
            status,
            {
                "schema_version": "xb.member.gateway.error.v1",
                "trace_id": f"trace-{uuid.uuid4().hex}",
                "error_code": code,
                "message": "Request rejected; use trace_id for support.",
            },
        )

    def handle(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | str | Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        headers = headers or {}
        split = urlsplit(path)
        route = split.path
        try:
            if method == "GET" and route in {"/livez", "/v1/health"}:
                return ApiResponse(200, self.service.health())
            if method == "GET" and route in {"/readyz", "/v1/readiness"}:
                value = self.service.readiness()
                return ApiResponse(200 if value["ready"] else 503, value)
            value = _json_body(body)
            if method == "POST" and route == "/v1/source-events":
                self._principal(headers, "source.ingest")
                return ApiResponse(202, self.service.ingest(value))
            if method == "GET" and route == "/v1/source/cursor":
                self._principal(headers, "source.ingest")
                query = parse_qs(split.query, keep_blank_values=True, strict_parsing=True)
                if set(query) != {"form_alias", "mapping_version"} or any(len(item) != 1 for item in query.values()):
                    raise ApiError(400, "source_cursor_query_invalid")
                return ApiResponse(200, self.service.source_cursor(query["form_alias"][0], query["mapping_version"][0]))
            if method == "POST" and route == "/v1/source-rejections":
                self._principal(headers, "source.ingest")
                return ApiResponse(202, self.service.reject_source(value))
            if method == "POST" and route == "/v1/source/epochs/begin":
                self._principal(headers, "source.ingest")
                return ApiResponse(200, self.service.begin_source_epoch(value))
            match = re.fullmatch(r"/v1/source/epochs/(epoch-[0-9a-f]{32})/restart", route)
            if method == "POST" and match:
                self._principal(headers, "source.ingest")
                return ApiResponse(200, self.service.restart_source_epoch(match.group(1), value))
            match = re.fullmatch(r"/v1/source/epochs/(epoch-[0-9a-f]{32})/pages/open", route)
            if method == "POST" and match:
                self._principal(headers, "source.ingest")
                return ApiResponse(200, self.service.open_source_page(match.group(1), value))
            match = re.fullmatch(r"/v1/source/pages/(page-[0-9a-f]{32})/commit", route)
            if method == "POST" and match:
                self._principal(headers, "source.ingest")
                return ApiResponse(200, self.service.commit_source_page(match.group(1), value))
            if method == "POST" and route == "/v1/welcome-emails/claim":
                self._principal(headers, "welcome_email.claim")
                return ApiResponse(200, self.service.claim_welcome_email(value))
            match = re.fullmatch(r"/v1/welcome-emails/([^/]+)/send-intent", route)
            if method == "POST" and match:
                self._principal(headers, "welcome_email.send_intent")
                return ApiResponse(200, self.service.welcome_send_intent(unquote(match.group(1)), value))
            match = re.fullmatch(r"/v1/welcome-emails/([^/]+)/result", route)
            if method == "POST" and match:
                self._principal(headers, "welcome_email.result")
                return ApiResponse(200, self.service.welcome_result(unquote(match.group(1)), value))
            if method == "GET" and route == "/v1/operator/status":
                self._principal(headers, "operator.status.read")
                return ApiResponse(200, self.service.operator_status())
            match = re.fullmatch(r"/v1/operator/reconciliation/([^/]+)", route)
            if method == "GET" and match:
                self._principal(headers, "operator.reconciliation.read")
                return ApiResponse(200, self.service.operator_reconciliation(unquote(match.group(1))))
            if method == "POST" and route == "/v1/worker/claim":
                self._principal(headers, "worker.claim")
                return ApiResponse(200, self.service.claim(self._worker_session(headers)))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/precheck", route)
            if method == "POST" and match:
                self._principal(headers, "worker.claim")
                return ApiResponse(200, self.service.precheck(unquote(match.group(1)), self._worker_session(headers)))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/lease", route)
            if method == "POST" and match:
                self._principal(headers, "worker.heartbeat")
                return ApiResponse(200, self.service.heartbeat(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/allocation/candidate", route)
            if method == "POST" and match:
                self._principal(headers, "worker.allocation")
                session = self._worker_session(headers)
                return ApiResponse(200, self.service.allocation_candidate(unquote(match.group(1)), session))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/allocation/probe", route)
            if method == "POST" and match:
                self._principal(headers, "worker.allocation")
                return ApiResponse(200, self.service.allocation_probe(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/allocation/recheck", route)
            if method == "POST" and match:
                self._principal(headers, "worker.allocation")
                return ApiResponse(200, self.service.allocation_recheck(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/allocation", route)
            if method == "POST" and match:
                self._principal(headers, "worker.allocation")
                return ApiResponse(200, self.service.bind_allocation(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/write-intent", route)
            if method == "POST" and match:
                self._principal(headers, "worker.write_intent")
                return ApiResponse(200, self.service.write_intent(unquote(match.group(1)), self._worker_session(headers), value, principal_valid=True))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/dispatch-fence", route)
            if method == "POST" and match:
                self._principal(headers, "worker.dispatch")
                return ApiResponse(200, self.service.dispatch_fence(unquote(match.group(1)), self._worker_session(headers), value, principal_valid=True))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/writer/register", route)
            if method == "POST" and match:
                self._principal(headers, "worker.writer_register")
                return ApiResponse(200, self.service.register_writer_execution(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/writer/termination", route)
            if method == "POST" and match:
                self._principal(headers, "worker.writer_termination")
                return ApiResponse(200, self.service.confirm_writer_termination(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/writer/quarantine", route)
            if method == "POST" and match:
                self._principal(headers, "worker.writer_quarantine")
                return ApiResponse(200, self.service.quarantine_writer_execution(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/writer/recover", route)
            if method == "POST" and match:
                self._principal(headers, "worker.writer_termination_recovery")
                return ApiResponse(200, self.service.recover_writer_termination(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/result", route)
            if method == "POST" and match:
                self._principal(headers, "worker.result")
                return ApiResponse(200, self.service.acknowledge_result(unquote(match.group(1)), self._worker_session(headers), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)/reconcile", route)
            if method == "POST" and match:
                self._principal(headers, "worker.reconcile")
                return ApiResponse(200, self.service.reconcile(unquote(match.group(1)), value))
            match = re.fullmatch(r"/v1/jobs/([^/]+)(?:/status)?", route)
            if method == "GET" and match:
                self._principal(headers, "job.read")
                return ApiResponse(200, self.service.status(unquote(match.group(1))))
            if method == "POST" and route == "/v1/control/kill-switch/disable":
                self._principal(headers, "control.kill_switch")
                if value:
                    _exact_fields(value, set())
                return ApiResponse(200, self.service.disable_kill_switch())
            if method == "POST" and route == "/v1/control/kill-switch/enable":
                self._principal(headers, "control.kill_switch")
                if value:
                    _exact_fields(value, set())
                return ApiResponse(200, self.service.enable_kill_switch())
            if method == "POST" and route == "/v1/control/activation":
                self._principal(headers, "control.activate")
                return ApiResponse(200, self.service.enable_activation(value))
            raise ApiError(404, "route_not_found")
        except Exception as exc:
            return self._error(exc)


def create_app(service: GatewayService, authenticator: Authenticator | None = None) -> GatewayApp:
    return GatewayApp(service, authenticator)


class _RequestHandler(BaseHTTPRequestHandler):
    app: GatewayApp
    max_body_bytes = 1_048_576

    def _serve(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > self.max_body_bytes:
            response = ApiResponse(413, {"error_code": "request_body_too_large"})
        else:
            raw = self.rfile.read(length) if length else b""
            response = self.app.handle(self.command, self.path, headers=dict(self.headers), body=raw)
        encoded = json.dumps(response.body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(app: GatewayApp, host: str = "127.0.0.1", port: int = 8080) -> None:
    handler = type("GatewayRequestHandler", (_RequestHandler,), {"app": app})
    with ThreadingHTTPServer((host, port), handler) as server:
        server.serve_forever()
