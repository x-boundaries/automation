import unittest
from datetime import datetime, timezone

from xb_member_gateway.api import GatewayApp, GatewayService
from xb_member_gateway.auth import Principal, StaticAuthenticator
from xb_member_gateway.canonical import build_source_event
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.repository import InMemoryRepository


NOW = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)


def make_config(**changes):
    value = {
        "member_no_max_length": 20,
        "worker_token_sha256": "0" * 64,
        "production_activation_enabled": True,
        "kill_switch_enabled": False,
    }
    value.update(changes)
    return GatewayConfig.from_mapping(value)


def make_event(response_id="api-response-001"):
    payload = {
        "name": "API Synthetic Member",
        "phone": "81234567",
        "email": "api-synthetic@example.test",
        "birthday_month": "March",
        "marketing_consent": "No",
        "pdpa_acknowledged": True,
    }
    from xb_member_gateway.canonical import canonical_json
    from xb_member_gateway.crypto import payload_hash

    canonical_payload = {
        "name": payload["name"],
        "phone": "6581234567",
        "email": payload["email"],
        "birthday_month": payload["birthday_month"],
        "marketing_consent": payload["marketing_consent"],
        "pdpa_acknowledged": payload["pdpa_acknowledged"],
    }
    return build_source_event(
        response_id=response_id,
        request_id=f"api-request-{response_id}",
        create_time="2026-08-30T01:00:00Z",
        form_alias="member_registration",
        mapping_version="member-intake.v1",
        payload=payload,
    )


class ApiBoundaryTests(unittest.TestCase):
    worker_scopes = frozenset(
        {
            "source.ingest",
            "worker.claim",
            "worker.heartbeat",
            "worker.allocation",
            "worker.write_intent",
            "worker.dispatch",
            "worker.result",
            "worker.reconcile",
            "job.read",
            "control.kill_switch",
            "control.activate",
        }
    )

    def setUp(self):
        self.repository = InMemoryRepository()
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", True)
        service = GatewayService(make_config(), self.repository, adapter_ready=True, clock=NOW)
        self.app = GatewayApp(
            service,
            StaticAuthenticator(
                {
                    "synthetic-worker-token": Principal(
                        subject="synthetic-worker", scopes=self.worker_scopes
                    )
                }
            ),
        )
        self.headers = {"Authorization": "Bearer synthetic-worker-token"}

    def call(self, method, path, body=None, headers=None):
        return self.app.handle(
            method,
            path,
            headers=self.headers if headers is None else headers,
            body={} if body is None else body,
        )

    def test_health_is_liveness_only_and_missing_auth_is_rejected_elsewhere(self):
        health = self.call("GET", "/livez", headers={})
        self.assertEqual(health.status, 200)
        denied = self.call("POST", "/v1/source-events", make_event(), headers={})
        self.assertEqual(denied.status, 401)
        self.assertEqual(
            set(denied.body),
            {"schema_version", "trace_id", "error_code", "message"},
        )
        self.assertNotIn("API Synthetic Member", str(denied.body))

    def test_scope_denial_is_distinct_from_authentication(self):
        read_only = GatewayApp(
            self.app.service,
            StaticAuthenticator(
                {"read-only-token": Principal("read-only", frozenset({"job.read"}))}
            ),
        )
        response = read_only.handle(
            "POST",
            "/v1/source-events",
            headers={"Authorization": "Bearer read-only-token"},
            body=make_event(),
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error_code"], "scope_denied")

    def test_member_vertical_slice_routes_and_safe_status(self):
        ingested = self.call("POST", "/v1/source-events", make_event())
        self.assertEqual(ingested.status, 202)
        job_id = ingested.body["job_id"]
        claimed = self.call("POST", "/v1/worker/claim", {})
        self.assertEqual(claimed.status, 200)
        job = claimed.body["job"]
        self.assertEqual(job["job_id"], job_id)
        self.assertNotIn("response_id", job)

        self.assertEqual(
            self.call("POST", f"/v1/jobs/{job_id}/precheck", {}).status, 200
        )
        candidate = self.call("POST", f"/v1/jobs/{job_id}/allocation/candidate", {})
        self.assertEqual(candidate.status, 200)
        member_no = candidate.body["candidate"]
        probed = self.call(
            "POST",
            f"/v1/jobs/{job_id}/allocation/probe",
            {
                "candidate": member_no,
                "status": "FREE",
                "probe_reference": "api-synthetic-free-001",
            },
        )
        self.assertEqual(probed.status, 200)
        intent = self.call(
            "POST",
            f"/v1/jobs/{job_id}/write-intent",
            {
                "operation": "member.create",
                "member_no": member_no,
                "payload_hash": job["payload_hash"],
            },
        )
        self.assertEqual(intent.status, 200)
        fence = self.call(
            "POST",
            f"/v1/jobs/{job_id}/dispatch-fence",
            {"operation": "member.create", "member_no": member_no},
        )
        self.assertEqual(fence.status, 200)
        result = self.call(
            "POST",
            f"/v1/jobs/{job_id}/result",
            {
                "schema_version": "xb.member.gateway.result.v1",
                "job_id": job_id,
                "operation": "member.create",
                "dispatch_fence_id": fence.body["dispatch_fence_id"],
                "status": "CREATED_VERIFIED",
                "member_no": member_no,
                "save_invocation_count": 1,
                "readback_found": True,
                "readback_match": True,
                "error_code": None,
            },
        )
        self.assertEqual(result.status, 200)
        status = self.call("GET", f"/v1/jobs/{job_id}")
        self.assertEqual(status.status, 200)
        self.assertEqual(status.body["state"], "CREATED_VERIFIED")
        self.assertNotIn("member_payload", status.body)
        self.assertNotIn("response_id", status.body)

    def test_operation_surface_is_exact_member_create(self):
        response = self.call(
            "POST",
            "/v1/jobs/synthetic/write-intent",
            {
                "operation": "member.update",
                "member_no": "6581234567",
                "payload_hash": "sha256:" + "0" * 64,
            },
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body["error_code"], "operation_invalid")
        self.assertEqual(self.call("POST", "/v1/jobs/synthetic/update", {}).status, 404)
        self.assertEqual(self.call("DELETE", "/v1/jobs/synthetic", {}).status, 404)

    def test_readiness_fails_closed_without_effective_member_constraint(self):
        repository = InMemoryRepository()
        service = GatewayService(
            GatewayConfig.from_mapping({}),
            repository,
            adapter_ready=False,
            clock=NOW,
        )
        response = GatewayApp(service).handle("GET", "/readyz", headers={})
        self.assertEqual(response.status, 503)
        self.assertFalse(response.body["ready"])
        self.assertIn("member_no_max_length_required", response.body["reasons"])

    def test_activation_and_kill_switch_are_controlled_operations(self):
        repository = InMemoryRepository()
        repository.set_control("kill_switch_enabled", True)
        service = GatewayService(
            make_config(production_activation_enabled=False, kill_switch_enabled=True),
            repository,
            adapter_ready=True,
            clock=NOW,
        )
        app = GatewayApp(
            service,
            StaticAuthenticator(
                {
                    "operator-token": Principal(
                        "synthetic-operator",
                        frozenset({"control.kill_switch", "control.activate"}),
                    )
                }
            ),
        )
        headers = {"Authorization": "Bearer operator-token"}
        activation = app.handle(
            "POST",
            "/v1/control/activation",
            headers=headers,
            body={
                "enabled": True,
                "environment": "production",
                "approval_reference": "synthetic-approval-001",
            },
        )
        self.assertEqual(activation.status, 200)
        self.assertTrue(repository.get_control()["production_activation_enabled"])
        disabled = app.handle(
            "POST",
            "/v1/control/kill-switch/disable",
            headers=headers,
            body={},
        )
        self.assertEqual(disabled.status, 200)
        self.assertFalse(repository.get_control()["kill_switch_enabled"])



    def test_kill_switch_engage_blocks_claim_and_controlled_clear_restores_eligibility(self):
        engaged = self.call("POST", "/v1/control/kill-switch/enable", {})
        self.assertEqual(engaged.status, 200)
        self.assertTrue(engaged.body["kill_switch_enabled"])
        self.assertTrue(self.repository.get_control()["kill_switch_enabled"])

        ingested = self.call(
            "POST", "/v1/source-events", make_event("api-kill-switch-response")
        )
        self.assertEqual(ingested.status, 202)
        blocked = self.call("POST", "/v1/worker/claim", {})
        self.assertEqual(blocked.status, 423)
        self.assertEqual(blocked.body["error_code"], "kill_switch_enabled")

        cleared = self.call("POST", "/v1/control/kill-switch/disable", {})
        self.assertEqual(cleared.status, 200)
        self.assertFalse(cleared.body["kill_switch_enabled"])
        claimed = self.call("POST", "/v1/worker/claim", {})
        self.assertEqual(claimed.status, 200)
        self.assertTrue(claimed.body["claimed"])

if __name__ == "__main__":
    unittest.main()
