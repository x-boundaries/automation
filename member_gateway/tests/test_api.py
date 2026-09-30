"""U-RT / U-RZ / U-PV: v2 route surface, removed routes and scopes, readyz,
resolve and privacy of every response (W-G2-149 sections 2.8, 3, 4.3)."""

import json
import unittest

from xb_member_gateway.auth import CONTROL_SCOPES, MAILER_SCOPES, OPERATOR_SCOPES, SOURCE_SCOPES, WORKER_SCOPES
from xb_member_gateway.config import ConfigError, GatewayConfig

try:
    from .v2_support import (
        NOW, REMOVED_SCOPES, SESSION, guid, headers, later, make_app, make_event, make_repository, make_service, outcome_body,
    )
except ImportError:  # discovered as a top-level module
    from v2_support import (
        NOW, REMOVED_SCOPES, SESSION, guid, headers, later, make_app, make_event, make_repository, make_service, outcome_body,
    )


JOB = "job-" + "1" * 32
# Every v1 worker route removed from code (section 2.8), with its old method.
REMOVED_ROUTES = (
    ("POST", "/v1/worker/claim"),
    ("POST", f"/v1/jobs/{JOB}/precheck"),
    ("POST", f"/v1/jobs/{JOB}/lease"),
    ("POST", f"/v1/jobs/{JOB}/allocation/candidate"),
    ("POST", f"/v1/jobs/{JOB}/allocation/probe"),
    ("POST", f"/v1/jobs/{JOB}/allocation/recheck"),
    ("POST", f"/v1/jobs/{JOB}/allocation"),
    ("POST", f"/v1/jobs/{JOB}/write-intent"),
    ("POST", f"/v1/jobs/{JOB}/dispatch-fence"),
    ("POST", f"/v1/jobs/{JOB}/writer/register"),
    ("POST", f"/v1/jobs/{JOB}/writer/termination"),
    ("POST", f"/v1/jobs/{JOB}/writer/quarantine"),
    ("POST", f"/v1/jobs/{JOB}/writer/recover"),
    ("POST", f"/v1/jobs/{JOB}/result"),
    ("POST", f"/v1/jobs/{JOB}/reconcile"),
)


class ApiSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.repository = make_repository()
        self.service = make_service(self.repository)
        self.app = make_app(self.service)

    def call(self, method, path, token, body=None, session=SESSION):
        return self.app.handle(method, path, headers=headers(token, session), body={} if body is None else body)

    def ingest(self, response_id="api-a", **changes):
        response = self.call("POST", "/v1/source-events", "source", make_event(response_id, **changes))
        self.assertEqual(response.status, 202)
        return response.body["job_id"]

    def claim(self):
        response = self.call("POST", "/v2/worker/claim", "worker")
        self.assertEqual(response.status, 200)
        return response.body

    def to_review(self, response_id="api-review"):
        job_id = self.ingest(response_id)
        claim = self.claim()
        self.assertEqual(claim["job_id"], job_id)
        body = outcome_body(claim, "MANUAL_REVIEW")
        self.assertEqual(self.call("POST", f"/v2/jobs/{job_id}/result", "worker", body).body["state"], "MANUAL_REVIEW")
        return job_id

    # --- U-RT: removed routes and scopes ----------------------------------

    def test_removed_routes_return_404_route_not_found_for_every_principal(self):
        for token in ("worker", "legacy", "control", "operator", "source", "mailer", "nobody"):
            for method, path in REMOVED_ROUTES:
                with self.subTest(token=token, path=path):
                    response = self.call(method, path, token)
                    self.assertEqual((response.status, response.body["error_code"]), (404, "route_not_found"))

    def test_removed_scopes_are_refused_on_every_v2_route(self):
        self.ingest()
        for method, path in (("POST", "/v2/worker/claim"), ("POST", f"/v2/jobs/{JOB}/result"), ("POST", f"/v2/control/jobs/{JOB}/resolve"), ("GET", f"/v1/jobs/{JOB}")):
            with self.subTest(path=path):
                response = self.call(method, path, "legacy")
                self.assertEqual((response.status, response.body["error_code"]), (403, "scope_denied"))

    def test_v2_principals_carry_no_removed_scope(self):
        for scopes in (SOURCE_SCOPES, OPERATOR_SCOPES, CONTROL_SCOPES, WORKER_SCOPES, MAILER_SCOPES):
            self.assertTrue(REMOVED_SCOPES.isdisjoint(scopes))
        self.assertEqual(WORKER_SCOPES, {"worker.claim", "worker.result"})
        self.assertIn("control.resolve", CONTROL_SCOPES)

    def test_route_matrix_has_no_role_union(self):
        job_id = self.ingest()
        cases = (
            ("POST", "/v2/worker/claim", "worker"),
            ("POST", f"/v2/jobs/{job_id}/result", "worker"),
            ("POST", f"/v2/control/jobs/{job_id}/resolve", "control"),
            ("GET", f"/v1/jobs/{job_id}", "operator"),
            ("GET", "/v1/operator/status", "operator"),
            ("POST", "/v1/control/kill-switch/enable", "control"),
        )
        for method, path, allowed in cases:
            for token in ("source", "operator", "control", "worker", "mailer"):
                if token == allowed:
                    continue
                with self.subTest(path=path, token=token):
                    response = self.call(method, path, token)
                    self.assertEqual((response.status, response.body["error_code"]), (403, "scope_denied"))

    def test_worker_session_header_is_required_on_worker_routes(self):
        for method, path in (("POST", "/v2/worker/claim"), ("POST", f"/v2/jobs/{JOB}/result")):
            response = self.app.handle(method, path, headers={"Authorization": "Bearer worker"}, body={})
            self.assertEqual((response.status, response.body["error_code"]), (400, "worker_session_invalid"))
            response = self.call(method, path, "worker", session="ws-short")
            self.assertEqual(response.status, 400)

    def test_missing_auth_is_401_and_liveness_is_open(self):
        self.assertEqual(self.app.handle("GET", "/livez").status, 200)
        self.assertEqual(self.app.handle("POST", "/v2/worker/claim", headers={}, body={}).status, 401)

    # --- U-RZ: readyz and resolve -----------------------------------------

    def test_readyz_has_exactly_the_contract_fields(self):
        response = self.app.handle("GET", "/readyz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, {"ready": True, "reasons": [], "dispatch_enabled": True, "server_time_utc": "2026-09-20T01:00:00Z"})
        self.repository.set_control("kill_switch_enabled", True)
        self.assertFalse(self.app.handle("GET", "/readyz").body["dispatch_enabled"])
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", False)
        self.assertFalse(self.app.handle("GET", "/readyz").body["dispatch_enabled"])
        dark = make_app(make_service(self.repository, worker_token_sha256=None))
        response = dark.handle("GET", "/readyz")
        self.assertEqual((response.status, response.body["ready"], response.body["reasons"]), (503, False, ["worker_credential_digest_required"]))

    def test_resolve_close_and_requeue_with_budget_cap(self):
        job_id = self.to_review()
        closed = self.call("POST", f"/v2/control/jobs/{job_id}/resolve", "control", {"action": "CLOSE", "resolution_code": "staff_linked_manually", "member_no": "LEGACY001", "member_guid": guid(9)})
        self.assertEqual(closed.status, 200)
        self.assertEqual(set(closed.body), {"schema_version", "resolution_id", "job_id", "action", "resolution_code", "state", "state_version", "write_budget", "resolved_at"})
        self.assertEqual((closed.body["schema_version"], closed.body["state"]), ("xb.member.gateway.resolution.v1", "RESOLVED"))
        self.assertNotIn("LEGACY001", json.dumps(closed.body))
        self.assertNotIn(guid(9), json.dumps(closed.body))
        again = self.call("POST", f"/v2/control/jobs/{job_id}/resolve", "control", {"action": "CLOSE", "resolution_code": "again"})
        self.assertEqual((again.status, again.body["error_code"]), (409, "resolution_state_invalid"))
        stored = self.repository.job_resolutions(job_id)
        self.assertEqual([(item.action, item.member_no, item.member_guid) for item in stored], [("CLOSE", "LEGACY001", guid(9))])

    def test_requeue_raises_the_write_budget_by_three_up_to_twelve(self):
        job_id = self.to_review()
        budgets = []
        for step in range(4):
            response = self.call("POST", f"/v2/control/jobs/{job_id}/resolve", "control", {"action": "REQUEUE", "resolution_code": f"retry_{step}"})
            self.assertEqual((response.status, response.body["state"]), (200, "QUEUED"))
            budgets.append(response.body["write_budget"])
            self.service.clock = later(step + 1)
            claim = self.claim()
            self.assertEqual(self.call("POST", f"/v2/jobs/{job_id}/result", "worker", outcome_body(claim, "MANUAL_REVIEW")).status, 200)
        self.assertEqual(budgets, [6, 9, 12, 12])
        self.assertEqual(self.repository.get_job(job_id).max_attempts, 12)

    def test_requeue_refused_when_the_capped_budget_is_spent(self):
        job_id = self.to_review()
        record = self.repository._jobs[job_id]
        record.max_attempts, record.write_attempts = 12, 12
        response = self.call("POST", f"/v2/control/jobs/{job_id}/resolve", "control", {"action": "REQUEUE", "resolution_code": "retry"})
        self.assertEqual((response.status, response.body["error_code"]), (409, "write_budget_cap_reached"))
        self.assertEqual(self.repository.get_job(job_id).state.value, "MANUAL_REVIEW")

    def test_resolve_validation_and_state(self):
        review = self.to_review("api-review-2")
        job_id = self.ingest()
        wrong_state = self.call("POST", f"/v2/control/jobs/{job_id}/resolve", "control", {"action": "CLOSE", "resolution_code": "x"})
        self.assertEqual((wrong_state.status, wrong_state.body["error_code"]), (409, "resolution_state_invalid"))
        for body, code in (
            ({"action": "DELETE", "resolution_code": "x"}, "resolution_action_invalid"),
            ({"action": "CLOSE", "resolution_code": "Bad Code"}, "resolution_code_invalid"),
            ({"action": "CLOSE", "resolution_code": "x", "member_guid": "not-a-guid"}, "resolution_member_guid_invalid"),
            ({"action": "CLOSE", "resolution_code": "x", "member_no": "1" * 21}, "resolution_member_no_invalid"),
            ({"action": "CLOSE"}, "request_fields_invalid"),
            ({"action": "CLOSE", "resolution_code": "x", "note": "free text"}, "request_fields_invalid"),
        ):
            with self.subTest(code=code):
                response = self.call("POST", f"/v2/control/jobs/{review}/resolve", "control", body)
                self.assertEqual((response.status, response.body["error_code"]), (400, code))

    # --- job.v3 view and operator status ------------------------------------

    def test_job_v3_view_is_operator_scoped_and_metadata_only(self):
        job_id = self.ingest(name="Private Person", phone="+65 9123 4567")
        claim = self.claim()
        self.call("POST", f"/v2/jobs/{job_id}/result", "worker", outcome_body(claim, "CREATED_VERIFIED", member_guid=guid(3)))
        for path in (f"/v1/jobs/{job_id}", f"/v1/jobs/{job_id}/status"):
            response = self.call("GET", path, "operator")
            self.assertEqual((response.status, response.body["schema_version"]), (200, "xb.member.gateway.job.v3"))
            text = json.dumps(response.body)
            for private in ("Private Person", "6591234567", "PRIVATEPERSON", guid(3), "synthetic-member@example.test", "api-a", claim["lease_id"]):
                self.assertNotIn(private, text)
        self.assertEqual(self.call("GET", f"/v1/jobs/{job_id}", "worker").status, 403)

    def test_operator_status_v2_counts_without_private_values(self):
        self.to_review()
        status = self.call("GET", "/v1/operator/status", "operator").body
        self.assertEqual(status["schema_version"], "xb.member.gateway.operator_status.v2")
        self.assertEqual((status["job_state_counts"]["MANUAL_REVIEW"], status["manual_review_reason_counts"]), (1, {"format_variant_other_person": 1}))
        self.assertTrue(status["dispatch_enabled"])

    # --- U-PV: privacy ------------------------------------------------------

    def test_no_member_no_guid_or_customer_data_in_logs_audit_or_non_worker_views(self):
        job_id = self.ingest(name="Hidden Name", phone="+65 9876 5432", email="hidden-person@example.test")
        claim = self.claim()
        member_no = claim["request"]["base_member_no"] + claim["request"]["name_component"]
        body = outcome_body(claim, "CREATED_VERIFIED_NAME", member_guid=guid(5))
        accepted = self.call("POST", f"/v2/jobs/{job_id}/result", "worker", body)
        replay = self.call("POST", f"/v2/jobs/{job_id}/result", "worker", body)
        conflict = self.call("POST", f"/v2/jobs/{job_id}/result", "worker", dict(body, error_code="x"))
        views = [
            accepted.body, replay.body, conflict.body,
            self.call("GET", f"/v1/jobs/{job_id}", "operator").body,
            self.call("GET", "/v1/operator/status", "operator").body,
            self.call("GET", f"/v1/operator/reconciliation/{job_id}", "operator").body,
            list(self.repository.audit_events), list(self.repository.result_conflicts),
        ]
        text = json.dumps(views)
        for private in (member_no, claim["request"]["base_member_no"], guid(5), "Hidden Name", "hidden-person@example.test", "api-a", claim["lease_id"]):
            self.assertNotIn(private, text)
        for event in self.repository.audit_events:
            self.assertTrue(set(event) <= {"event_type", "recorded_at", "job_id", "state", "operation", "error_code", "count"})
        # MemberNo and Guid are stored privately for the job.
        stored = self.repository.member_outcome(job_id)
        self.assertEqual((stored.member_no, stored.member_guid), (member_no, guid(5)))

    def test_error_envelopes_are_generic_with_a_trace_reference(self):
        response = self.call("POST", "/v1/worker/claim", "worker")
        self.assertEqual(set(response.body), {"schema_version", "trace_id", "error_code", "message"})
        self.assertEqual(response.body["message"], "Request rejected; use trace_id for support.")


class ConfigV3Tests(unittest.TestCase):
    """Config v3: five principals; removed keys are configuration errors."""

    def test_five_principals_and_book_mode(self):
        config = GatewayConfig.from_mapping({"member_book_mode": "production"})
        self.assertEqual(len(config.credential_env_names), 5)
        self.assertEqual(config.schema_version, "xb.member.gateway.config.v3")
        self.assertIn("member_book_mode_invalid", GatewayConfig().readiness_reasons())
        with self.assertRaisesRegex(ConfigError, "member_book_mode_invalid"):
            GatewayConfig.from_mapping({"member_book_mode": "either"})

    def test_removed_keys_fail_closed(self):
        for key in ("recovery_token_sha256", "recovery_token_env"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, "recovery_principal_removed"):
                    GatewayConfig.from_mapping({"member_book_mode": "production", key: None})
        for key, value in (("heartbeat_seconds", 120), ("member_no_max_length", 20)):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, "removed_config_field_present"):
                    GatewayConfig.from_mapping({"member_book_mode": "production", key: value})
        with self.assertRaisesRegex(ConfigError, "config_schema_version_invalid"):
            GatewayConfig.from_mapping({"member_book_mode": "production", "schema_version": "xb.member.gateway.config.v2"})

    def test_fixed_timing_and_budget(self):
        for key, value, code in (("lease_seconds", 300, "lease_seconds_must_be_600"), ("execution_deadline_seconds", 120, "execution_deadline_seconds_must_be_300"), ("max_attempts", 5, "max_attempts_must_be_three")):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, code):
                    GatewayConfig.from_mapping({"member_book_mode": "production", key: value})

    def test_activation_requires_a_valid_book_mode(self):
        repository = make_repository()
        service = make_service(repository)
        response = make_app(service).handle("POST", "/v1/control/activation", headers=headers("control"), body={"enabled": True, "environment": "production", "approval_reference": "approval-1"})
        self.assertEqual(response.status, 200)
        service.config = GatewayConfig()
        response = make_app(service).handle("POST", "/v1/control/activation", headers=headers("control"), body={"enabled": True, "environment": "production", "approval_reference": "approval-1"})
        self.assertEqual((response.status, response.body["error_code"]), (422, "member_book_mode_invalid"))


if __name__ == "__main__":
    unittest.main()
