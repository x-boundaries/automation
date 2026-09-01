"""Small protected HTTP boundary for the member-first gateway."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from .allocation import AllocationError, MemberNoAllocator
from .auth import (
    Authenticator,
    AuthenticationError,
    DenyAllAuthenticator,
    Principal,
    WORKER_SESSION_HEADER,
    require_scope,
    worker_session,
)
from .canonical import CanonicalizationError, canonicalize_source_event
from .config import GatewayConfig
from .eligibility import EligibilityContext, evaluate_eligibility
from .models import JobState, ProbeStatus, ResultStatus
from .repository import (
    AllocationConflict,
    InMemoryRepository,
    JobNotFound,
    LeaseConflict,
    RepositoryError,
    ResultConflict,
    SourceConflict,
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


class GatewayService:
    """Application service shared by the standard-library HTTP adapter and tests."""

    def __init__(
        self,
        config: GatewayConfig,
        repository: Any,
        *,
        adapter_ready: bool = False,
        clock=None,
    ):
        self.config = config
        self.repository = repository
        self.adapter_ready = adapter_ready
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
        config = self._runtime_config()
        if event.form_alias not in config.allowed_form_aliases:
            raise ApiError(422, "form_alias_not_allowlisted")
        if event.mapping_version not in config.allowed_mapping_versions:
            raise ApiError(422, "mapping_version_not_allowlisted")
        outcome = self.repository.ingest_source_event(event, now=self.clock)
        return {
            "schema_version": "xb.member.gateway.job.v1",
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
            return {"schema_version": "xb.member.gateway.job.v1", "claimed": False, "job": None}
        return {"schema_version": "xb.member.gateway.job.v1", "claimed": True, "job": job.worker_dict()}

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
                gateway_ready=config.gateway_ready,
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
        _exact_fields(body, {"operation", "member_no"})
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
        fence = self.repository.record_dispatch_fence(
            job_id, body["member_no"], body["operation"], worker_id, now=self.clock
        )
        return {"job_id": job_id, "state": JobState.WRITING.value, "dispatch_fence_id": fence.fence_id, "member_no": fence.member_no}

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
        if fence is None or allocation is None or body["member_no"] != fence.member_no or job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
            raise ApiError(409, "reconciliation_member_binding_invalid")
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
        elif isinstance(error, SourceConflict):
            status, code = 409, "source_identity_conflict"
        elif isinstance(error, ResultConflict):
            status, code = 409, "result_conflict"
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
        route = urlsplit(path).path
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
