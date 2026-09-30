"""Small protected HTTP boundary for the member-first gateway (v2 worker).

Worker v2 surface (W-G2-149 section 2.8): ``GET /readyz``,
``POST /v2/worker/claim`` and ``POST /v2/jobs/{job_id}/result``; the
control principal resolves review jobs with
``POST /v2/control/jobs/{job_id}/resolve``. Every v1 worker route
(claim, precheck, lease, allocation, write-intent, dispatch-fence, writer
lifecycle, result, reconcile) was removed from code and answers
``404 route_not_found``.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .admission import AdmissionPolicy, ClaimGate
from .auth import (
    Authenticator,
    AuthenticationError,
    DenyAllAuthenticator,
    Principal,
    WORKER_SESSION_HEADER,
    require_scope,
    worker_session,
)
from .canonical import (
    CanonicalizationError,
    canonicalize_source_event,
    canonicalize_source_rejection,
    validate_google_create_time_exact,
)
from .config import GatewayConfig
from .identity import BOOK_MODES
from .models import source_cursor_v2
from .notifications import (
    WELCOME_JOB_SCHEMA_VERSION,
    WelcomeEmailError,
    claim_envelope,
    validate_lease_request,
    validate_outbox_id,
    validate_result_request,
)
from .repository import (
    JobNotFound,
    LeaseConflict,
    RepositoryError,
    ResolutionConflict,
    ResultConflict,
    ResultStale,
    SourceConflict,
    SourceRestartReasonInvalid,
    timestamp,
)
from .results import CLAIM_SCHEMA_VERSION, ResultValidationError, build_claim_request, validate_result_v2
from .state_machine import InvalidTransition


RESOLUTION_SCHEMA_VERSION = "xb.member.gateway.resolution.v1"


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
_APPROVAL_RE = re.compile(r"^[A-Za-z0-9._:/#-]{1,160}$")
_JOB_ID_RE = re.compile(r"^job-[A-Za-z0-9]{16,64}$")


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


def _state_version(value: Any, code: str = "source_epoch_state_version_invalid") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiError(400, code)
    return value


def _job_id(value: str) -> str:
    if not _JOB_ID_RE.fullmatch(value):
        raise ApiError(404, "job_not_found")
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

    def _server_time(self) -> str:
        return timestamp(self.clock)

    def _readiness_reasons(self, config: GatewayConfig) -> list[str]:
        reasons = list(config.readiness_reasons())
        if not self.adapter_ready:
            reasons.append("autocount_adapter_not_ready")
        return sorted(set(reasons))

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "service": "xb-member-gateway", "schema_version": "xb.member.gateway.error.v1"}

    def readiness(self) -> dict[str, Any]:
        """``{ready, reasons[], dispatch_enabled, server_time_utc}`` where
        ``dispatch_enabled = activation AND NOT kill_switch``."""

        config = self._runtime_config()
        reasons = self._readiness_reasons(config)
        return {
            "ready": not reasons,
            "reasons": reasons,
            "dispatch_enabled": bool(config.production_activation_enabled and not config.kill_switch_enabled),
            "server_time_utc": self._server_time(),
        }

    def _admission_policy(self) -> AdmissionPolicy:
        try:
            return AdmissionPolicy.from_config(self.config)
        except ValueError as exc:
            raise ApiError(503, "member_book_mode_invalid") from exc

    def ingest(self, body: Mapping[str, Any]) -> dict[str, Any]:
        event = canonicalize_source_event(body)
        validate_google_create_time_exact(event.create_time, field="create_time")
        config = self._runtime_config()
        if event.form_alias not in config.allowed_form_aliases:
            raise ApiError(422, "form_alias_not_allowlisted")
        if event.mapping_version not in config.allowed_mapping_versions:
            raise ApiError(422, "mapping_version_not_allowlisted")
        # The accepted-member cap comes only from the closed admission mode;
        # production activation does not change source admission semantics.
        outcome = self.repository.ingest_source_event(
            event,
            policy=self._admission_policy(),
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

    def claim(self, session: str) -> dict[str, Any]:
        """``POST /v2/worker/claim`` -> ``xb.member.gateway.worker_claim.v2``."""

        config = self._runtime_config()
        server_time = self._server_time()
        if self._readiness_reasons(config) or not config.environment_matches:
            # Readiness is part of the dispatch gate; nothing is claimed.
            return {"schema_version": CLAIM_SCHEMA_VERSION, "claimed": False, "reason": "dispatch_disabled", "server_time_utc": server_time}
        gate = ClaimGate(self._admission_policy(), config.environment_matches, True)
        outcome = self.repository.claim_job(session, gate=gate, lease_seconds=config.lease_seconds, now=self.clock)
        if not outcome.claimed:
            return {"schema_version": CLAIM_SCHEMA_VERSION, "claimed": False, "reason": outcome.reason, "server_time_utc": server_time}
        job, lease = outcome.job, outcome.lease
        return {
            "schema_version": CLAIM_SCHEMA_VERSION,
            "claimed": True,
            "job_id": job.job_id,
            "attempt_no": job.attempt,
            "lease_id": lease.lease_token,
            "state_version": job.state_version,
            "lease_expires_at": lease.expires_at,
            "first_claimed_at": job.first_claimed_at,
            "server_time_utc": server_time,
            "request": build_claim_request(job),
        }

    def submit_result(self, job_id: str, session: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /v2/jobs/{job_id}/result``. The kill switch never blocks it."""

        try:
            result = validate_result_v2(body)
        except ResultValidationError as exc:
            raise ApiError(400, "result_invalid") from exc
        if result["job_id"] != job_id:
            raise ApiError(400, "result_job_mismatch")
        response, _ = self.repository.submit_result(job_id, result, worker_session=session, now=self.clock)
        return response

    def resolve(self, job_id: str, body: Mapping[str, Any], principal: Principal) -> dict[str, Any]:
        """``POST /v2/control/jobs/{job_id}/resolve`` (CLOSE | REQUEUE)."""

        keys = set(body)
        if not {"action", "resolution_code"} <= keys <= {"action", "resolution_code", "member_no", "member_guid"}:
            raise ApiError(400, "request_fields_invalid")
        resolution = self.repository.resolve_job(
            job_id, action=body["action"], resolution_code=body["resolution_code"],
            member_no=body.get("member_no"), member_guid=body.get("member_guid"),
            resolved_by=principal.subject, now=self.clock,
        )
        # MemberNo and Guid are stored privately and never echoed.
        return {
            "schema_version": RESOLUTION_SCHEMA_VERSION,
            "resolution_id": resolution.resolution_id,
            "job_id": resolution.job_id,
            "action": resolution.action,
            "resolution_code": resolution.resolution_code,
            "state": resolution.resulting_state,
            "state_version": resolution.prior_state_version + 1,
            "write_budget": resolution.write_budget,
            "resolved_at": resolution.resolved_at,
        }

    def status(self, job_id: str) -> dict[str, Any]:
        """Operator job view ``xb.member.gateway.job.v3`` (metadata only)."""

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
        if self.config.member_book_mode not in BOOK_MODES:
            raise ApiError(422, "member_book_mode_invalid")
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
        elif isinstance(error, ResultStale):
            status, code = 409, "result_stale"
        elif isinstance(error, ResolutionConflict):
            status = 400 if str(error).endswith("_invalid") and str(error) != "resolution_state_invalid" else 409
            code = _safe_code(str(error))
        elif isinstance(error, RepositoryError) and str(error) == "kill_switch_enabled":
            status, code = 423, "kill_switch_enabled"
        elif isinstance(error, (LeaseConflict, InvalidTransition, RepositoryError)):
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
            # Operator job view (job.v3). Reclassified from the removed
            # worker-only job.read scope to the metadata-only operator scope.
            match = re.fullmatch(r"/v1/jobs/([^/]+)(?:/status)?", route)
            if method == "GET" and match:
                self._principal(headers, "operator.status.read")
                return ApiResponse(200, self.service.status(unquote(match.group(1))))
            if method == "POST" and route == "/v2/worker/claim":
                self._principal(headers, "worker.claim")
                return ApiResponse(200, self.service.claim(self._worker_session(headers)))
            match = re.fullmatch(r"/v2/jobs/([^/]+)/result", route)
            if method == "POST" and match:
                self._principal(headers, "worker.result")
                session = self._worker_session(headers)
                return ApiResponse(200, self.service.submit_result(_job_id(unquote(match.group(1))), session, value))
            match = re.fullmatch(r"/v2/control/jobs/([^/]+)/resolve", route)
            if method == "POST" and match:
                principal = self._principal(headers, "control.resolve")
                return ApiResponse(200, self.service.resolve(_job_id(unquote(match.group(1))), value, principal))
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
