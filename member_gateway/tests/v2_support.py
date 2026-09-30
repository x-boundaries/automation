"""Shared synthetic fixtures for the v2 gateway tests (not a test module).

Everything here is synthetic: example.test / example.invalid addresses,
made-up numbers and placeholder digests. No real person or credential.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xb_member_gateway.admission import AdmissionPolicy, ClaimGate
from xb_member_gateway.api import GatewayApp, GatewayService
from xb_member_gateway.auth import (
    CONTROL_SCOPES, MAILER_SCOPES, OPERATOR_SCOPES, SOURCE_SCOPES, WORKER_SCOPES, StaticAuthenticator, principal,
)
from xb_member_gateway.canonical import build_source_event, canonicalize_source_event
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.repository import InMemoryRepository


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)
CUTOVER = "2026-09-15T00:00:00Z"
FORM = "synthetic-form"
SESSION = "ws-" + "a" * 32
SESSION_B = "ws-" + "b" * 32
RELEASE = "c" * 64
DIGESTS = {name: str(index + 1) * 64 for index, name in enumerate(("source", "operator", "control", "worker", "mailer"))}
# Every scope the removed v1 surface used. No v2 principal carries them.
REMOVED_SCOPES = frozenset(
    {
        "worker.heartbeat", "worker.allocation", "worker.write_intent", "worker.dispatch",
        "worker.writer_register", "worker.writer_termination", "worker.writer_quarantine",
        "worker.reconcile", "job.read", "worker.writer_termination_recovery",
    }
)
TOKENS = {
    "source": ("configured-source", SOURCE_SCOPES),
    "operator": ("configured-operator", OPERATOR_SCOPES),
    "control": ("configured-control", CONTROL_SCOPES),
    "worker": ("configured-worker", WORKER_SCOPES),
    "mailer": ("configured-mailer", MAILER_SCOPES),
    # A stale principal that still carries every removed v1 scope.
    "legacy": ("legacy-v1-worker", REMOVED_SCOPES),
}


def load_vectors() -> dict:
    return json.loads((ROOT / "tests/fixtures/xb_mn_1_vectors.v1.fixture").read_text(encoding="utf-8"))


def make_config(**changes) -> GatewayConfig:
    values = {
        "member_book_mode": "production",
        **{f"{name}_token_sha256": digest for name, digest in DIGESTS.items()},
        "production_activation_enabled": True,
        "kill_switch_enabled": False,
        "source_cutover_watermark": CUTOVER,
        "source_production_cutover_exact": CUTOVER,
        "source_form_id": FORM,
        "source_admission_mode": "continuous",
    }
    values.update(changes)
    return GatewayConfig.from_mapping(values)


def policy(book_mode: str = "production") -> AdmissionPolicy:
    return AdmissionPolicy(book_mode, ("member_registration",), ("member-intake.v1",))


def open_gate(book_mode: str = "production") -> ClaimGate:
    return ClaimGate(policy(book_mode), True, True)


def payload(**changes) -> dict:
    value = {
        "name": "Synthetic Member",
        "phone": "81234567",
        "email": "synthetic-member@example.test",
        "birthday_month": "March",
        "marketing_consent": "No",
        "pdpa_acknowledged": True,
    }
    value.update(changes)
    return value


def make_event(response_id: str = "v2-response-001", *, create_time: str = "2026-09-20T00:30:00.123Z", **payload_changes) -> dict:
    return build_source_event(
        response_id=response_id,
        # The request id never embeds the (private) response id.
        request_id="v2-request-" + hashlib.sha256(response_id.encode("utf-8")).hexdigest()[:16],
        create_time=create_time,
        form_alias="member_registration",
        mapping_version="member-intake.v1",
        payload=payload(**payload_changes),
    )


def source_event(response_id: str = "v2-response-001", **payload_changes):
    return canonicalize_source_event(make_event(response_id, **payload_changes))


def make_repository() -> InMemoryRepository:
    repository = InMemoryRepository(source_cutover_watermark=CUTOVER, source_form_id=FORM)
    repository.set_control("kill_switch_enabled", False)
    repository.set_control("production_activation_enabled", True)
    return repository


def make_service(repository=None, *, clock=NOW, **config_changes) -> GatewayService:
    return GatewayService(make_config(**config_changes), repository or make_repository(), adapter_ready=True, clock=clock)


def make_app(service: GatewayService) -> GatewayApp:
    return GatewayApp(service, StaticAuthenticator({name: principal(*spec) for name, spec in TOKENS.items()}))


def headers(token: str, session: str = SESSION) -> dict:
    return {"Authorization": f"Bearer {token}", "X-XB-Worker-Session": session}


def guid(number: int) -> str:
    return f"{number:08x}-0000-4000-8000-{number:012x}"


def result_body(claim: dict, **changes) -> dict:
    """A consistent CREATED_VERIFIED R1 BASE body for ``claim``."""

    body = {
        "schema_version": "xb.member.gateway.result.v2",
        "job_id": claim["job_id"],
        "attempt_no": claim["attempt_no"],
        "lease_id": claim["lease_id"],
        "state_version": claim["state_version"],
        "outcome": "CREATED_VERIFIED",
        "rule": "R1",
        "branch": "BASE",
        "member_no": claim["request"]["base_member_no"],
        "member_guid": guid(1),
        "save_invoked": True,
        "save_invocation_count": 1,
        "readback": {"found": True, "match": True, "created_by_integration_user": True},
        "reason_code": None,
        "dq_flags": [],
        "primitive": {"release_sha256": RELEASE, "rule_version": "XB-MN-1"},
        "error_code": None,
    }
    body.update(changes)
    return body


# Consistent bodies for every section 4.1 outcome (member_no/guid per claim).
def outcome_body(claim: dict, outcome: str, **changes) -> dict:
    base = claim["request"]["base_member_no"]
    shapes = {
        "CREATED_VERIFIED": {},
        "CREATED_VERIFIED_NAME": {"outcome": "CREATED_VERIFIED", "rule": "R4", "branch": "NAME_APPENDED", "member_no": base + claim["request"]["name_component"]},
        "CREATED_VERIFIED_PRIOR_ATTEMPT": {"rule": "R0", "branch": "BASE", "save_invoked": False, "save_invocation_count": 0, "readback": None},
        "LINKED_EXISTING": {"rule": "R2c", "branch": "EXISTING", "member_no": "LEGACY001", "save_invoked": False, "save_invocation_count": 0, "readback": None},
        "MANUAL_REVIEW": {"rule": "R3", "branch": "NONE", "member_no": None, "member_guid": None, "save_invoked": False, "save_invocation_count": 0, "readback": None, "reason_code": "format_variant_other_person"},
        "REJECTED_VALIDATION": {"rule": "NONE", "branch": "NONE", "member_no": None, "member_guid": None, "save_invoked": False, "save_invocation_count": 0, "readback": None, "reason_code": "name_exceeds_autocount_limit"},
        "FAILED_BEFORE_WRITE": {"rule": "NONE", "branch": "NONE", "member_no": None, "member_guid": None, "save_invoked": False, "save_invocation_count": 0, "readback": None, "reason_code": "probe_unavailable"},
        "MUTEX_BUSY": {"rule": "NONE", "branch": "NONE", "member_no": None, "member_guid": None, "save_invoked": False, "save_invocation_count": 0, "readback": None, "reason_code": None},
        "NOT_CREATED": {"rule": "R1", "branch": "BASE", "member_guid": None, "readback": {"found": False, "match": False, "created_by_integration_user": False}, "error_code": "save_member_threw"},
        "NOT_CREATED_CONFLICT": {"rule": "R1", "branch": "BASE", "member_guid": None, "readback": {"found": True, "match": False, "created_by_integration_user": False}, "error_code": "save_member_threw"},
        "OUTCOME_UNCERTAIN": {"rule": "NONE", "branch": "NONE", "member_no": None, "member_guid": None, "readback": None, "reason_code": "child_deadline_exceeded"},
        "CREATED_READBACK_MISMATCH": {"rule": "R1", "branch": "BASE", "readback": {"found": True, "match": False, "created_by_integration_user": True}},
    }
    body = result_body(claim, **shapes[outcome])
    if outcome != "CREATED_VERIFIED_NAME":
        body["outcome"] = outcome
    body.update(changes)
    return body


def later(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)
