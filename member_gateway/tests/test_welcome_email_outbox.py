"""Durable welcome_v1 outbox: atomic creation, state machine, auth isolation.

The in-memory tests drive the real gateway service and HTTP router. The
``RealPostgres*`` tests drive the same service over the production
``PostgresRepository`` and psycopg against a disposable loopback database
(see test_postgres_cursor); they skip when it is not configured.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from xb_member_gateway.api import GatewayApp, GatewayService
from xb_member_gateway.auth import (
    CONTROL_SCOPES, MAILER_SCOPES, OPERATOR_SCOPES, RECOVERY_SCOPES, SOURCE_SCOPES, WORKER_SCOPES,
    StaticAuthenticator, principal,
)
from xb_member_gateway.canonical import build_source_event
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.models import JobState, WelcomeEmailState
from xb_member_gateway.notifications import WELCOME_V1, build_welcome_message, welcome_message_hash
from xb_member_gateway.repository import InMemoryRepository, PostgresRepository, ResultConflict

try:
    from .test_postgres_cursor import CUTOVER, FORM, RealPostgresTestCase
except ImportError:  # discovered as a top-level module
    from test_postgres_cursor import CUTOVER, FORM, RealPostgresTestCase


NOW = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)
SESSION = "ws-" + "a" * 32
RECIPIENT = "welcome-synthetic@example.test"
TOKENS = {
    "source": ("configured-source", SOURCE_SCOPES), "operator": ("configured-operator", OPERATOR_SCOPES),
    "control": ("configured-control", CONTROL_SCOPES), "worker": ("configured-worker", WORKER_SCOPES),
    "recovery": ("configured-recovery", RECOVERY_SCOPES), "mailer": ("configured-mailer", MAILER_SCOPES),
}


def config(**changes):
    values = {
        "member_no_max_length": 20, "worker_token_sha256": "0" * 64, "recovery_token_sha256": "1" * 64,
        "production_activation_enabled": True, "kill_switch_enabled": False,
        "source_cutover_watermark": CUTOVER, "source_production_cutover_exact": CUTOVER,
        "source_form_id": FORM, "source_admission_mode": "continuous",
    }
    values.update(changes)
    return GatewayConfig.from_mapping(values)


def source_event(response_id, email=RECIPIENT, phone="81234567"):
    return build_source_event(
        response_id=response_id, request_id=f"welcome-request-{response_id}", create_time="2026-09-19T08:30:00.123456789Z",
        form_alias="member_registration", mapping_version="member-intake.v1",
        payload={"name": "Welcome Synthetic", "phone": phone, "email": email, "birthday_month": "March", "marketing_consent": "No", "pdpa_acknowledged": True},
    )


class MemberFlow:
    """Drives one member through the real worker contract to a result."""

    def __init__(self, service, repository):
        self.service = service
        self.repository = repository

    def to_result(self, response_id, status="CREATED_VERIFIED", *, phone="81234567", email=RECIPIENT):
        service = self.service
        service.ingest(source_event(response_id, email=email, phone=phone))
        job = service.claim(SESSION)["job"]
        job_id = job["job_id"]
        service.precheck(job_id, SESSION)
        candidate = service.allocation_candidate(job_id, SESSION)["candidate"]
        service.allocation_probe(job_id, SESSION, {"candidate": candidate, "status": "FREE", "probe_reference": f"probe-{response_id}"})
        service.allocation_recheck(job_id, SESSION, {"status": "FREE", "probe_reference": f"recheck-{response_id}"})
        record = self.repository.get_job(job_id)
        service.write_intent(job_id, SESSION, {"operation": "member.create", "member_no": record.allocation_member_no, "payload_hash": record.payload_hash}, principal_valid=True)
        fence = service.dispatch_fence(job_id, SESSION, {"operation": "member.create", "member_no": record.allocation_member_no}, principal_valid=True)
        writer = dict(
            fence_id=fence["dispatch_fence_id"], attempt=1, worker_session=SESSION, host_binding=f"host-{SESSION}",
            execution_id=fence["execution_id"], pid=4321, process_start_time="2026-09-20T01:00:01Z", now=service.clock,
        )
        self.repository.register_writer_execution(job_id, **writer)
        self.repository.confirm_writer_termination(job_id, **writer, evidence_type="process_exit", evidence_reference=f"exit-{response_id}", exit_code=0)
        positive = status == "CREATED_VERIFIED"
        body = {
            "schema_version": "xb.member.gateway.result.v1", "job_id": job_id, "operation": "member.create",
            "dispatch_fence_id": fence["dispatch_fence_id"], "status": status, "member_no": record.allocation_member_no,
            "save_invocation_count": 1, "readback_found": positive or status == "CREATED_READBACK_MISMATCH",
            "readback_match": positive, "error_code": None if positive else "synthetic_non_positive",
        }
        acknowledged = service.acknowledge_result(job_id, SESSION, body)
        return job_id, body, acknowledged


class WelcomeOutboxTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository(source_cutover_watermark=CUTOVER, source_form_id=FORM)
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", True)
        self.service = GatewayService(config(), self.repository, adapter_ready=True, clock=NOW)
        self.flow = MemberFlow(self.service, self.repository)
        self.app = GatewayApp(self.service, StaticAuthenticator({name: principal(*spec) for name, spec in TOKENS.items()}))

    def at(self, delta):
        self.service.clock = NOW + delta
        return self.service

    def claim(self, delta=timedelta(minutes=2)):
        return self.at(delta).claim_welcome_email({})

    def post(self, token, path, body=None):
        return self.app.handle("POST", path, headers={"Authorization": f"Bearer {token}", "X-XB-Worker-Session": SESSION}, body=json.dumps(body or {}))

    # --- atomic creation -----------------------------------------------------

    def test_created_verified_creates_exactly_one_immutable_welcome_v1_outbox(self):
        job_id, _, _ = self.flow.to_result("welcome-positive")
        outbox = self.repository.welcome_outbox_for_job(job_id)
        message = build_welcome_message(RECIPIENT)
        self.assertEqual((outbox.state, outbox.template_id, outbox.message_hash), (WelcomeEmailState.PENDING, "welcome_v1", welcome_message_hash(message)))
        self.assertEqual(len(self.repository.welcome_outboxes()), 1)
        self.assertEqual(self.claim(timedelta(seconds=30))["claimed"], False)
        claimed = self.claim()
        self.assertTrue(claimed["claimed"])
        envelope = claimed["job"]
        self.assertEqual(envelope["schema_version"], "xb.member.welcome_email.job.v1")
        self.assertEqual(envelope["operation"], "welcome_email.send")
        self.assertEqual(envelope["message"], {**WELCOME_V1, "to": RECIPIENT})
        self.assertIsNone(envelope["message"]["reply_to"])
        self.assertEqual((envelope["message"]["from_address"], envelope["message"]["subject"], envelope["message"]["text"]), ("noreply@x-boundaries.com", "Welcome to X-Boundaries!", "Welcome to X-Boundaries!"))
        for private in ("response_id", "member_no", "phone"):
            self.assertNotIn(private, json.dumps({key: value for key, value in envelope.items() if key != "message"}))

    def test_non_positive_member_outcomes_never_create_an_outbox(self):
        for index, status in enumerate(("WRITE_OUTCOME_UNCERTAIN", "CONFIRMED_NOT_CREATED", "CREATED_READBACK_MISMATCH")):
            with self.subTest(status=status):
                job_id, _, _ = self.flow.to_result(f"welcome-negative-{index}", status, phone=f"8200000{index}")
                self.assertIsNone(self.repository.welcome_outbox_for_job(job_id))
        self.assertEqual(self.repository.welcome_outboxes(), ())

    def test_exact_match_reconciliation_creates_the_outbox_atomically(self):
        job_id, body, _ = self.flow.to_result("welcome-reconcile", "WRITE_OUTCOME_UNCERTAIN")
        self.assertIsNone(self.repository.welcome_outbox_for_job(job_id))
        reconciled = self.service.reconcile(job_id, {"member_no": body["member_no"], "lookup_status": "exact_match", "readback_found": True, "readback_match": True, "error_code": None})
        self.assertEqual(reconciled["state"], "CREATED_VERIFIED")
        self.assertIsNotNone(self.repository.welcome_outbox_for_job(job_id))

    def test_duplicate_positive_ack_verifies_and_never_backfills(self):
        job_id, body, _ = self.flow.to_result("welcome-duplicate")
        again = self.service.acknowledge_result(job_id, SESSION, body)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.repository.welcome_outboxes()), 1)
        with self.repository._lock:
            outbox_id = self.repository._outbox_by_job.pop(job_id)
            self.repository._outbox.pop(outbox_id)
        with self.assertRaisesRegex(ResultConflict, "welcome_outbox_identity_missing"):
            self.service.acknowledge_result(job_id, SESSION, body)
        self.assertEqual(self.repository.welcome_outboxes(), ())

    # --- state machine -------------------------------------------------------

    def test_success_is_smtp_acceptance_and_result_ack_is_idempotent(self):
        self.flow.to_result("welcome-sent")
        job = self.claim()["job"]
        intent = self.service.welcome_send_intent(job["outbox_id"], {"lease_id": job["lease_id"], "state_version": job["state_version"]})
        self.assertEqual(intent["state"], "SEND_INTENT_RECORDED")
        replay = self.service.welcome_send_intent(job["outbox_id"], {"lease_id": job["lease_id"], "state_version": job["state_version"]})
        self.assertTrue(replay["replayed"])
        result = {"schema_version": "xb.member.welcome_email.result.v1", "lease_id": job["lease_id"], "state_version": intent["state_version"], "outcome": "smtp_accepted", "error_code": None}
        sent = self.service.welcome_result(job["outbox_id"], result)
        self.assertEqual((sent["state"], sent["duplicate"]), ("SENT", False))
        self.assertTrue(self.service.welcome_result(job["outbox_id"], result)["duplicate"])
        response = self.post("mailer", f"/v1/welcome-emails/{job['outbox_id']}/result", {**result, "outcome": "delivery_outcome_uncertain", "error_code": "late"})
        self.assertEqual((response.status, response.body["error_code"]), (409, "welcome_email_state_conflict"))
        self.assertFalse(self.claim(timedelta(hours=2))["claimed"])

    def test_send_intent_then_crash_is_uncertain_and_never_resent(self):
        self.flow.to_result("welcome-crash")
        job = self.claim()["job"]
        intent = self.service.welcome_send_intent(job["outbox_id"], {"lease_id": job["lease_id"], "state_version": job["state_version"]})
        # The mailer crashes after intent; the lease expires unacknowledged.
        self.assertFalse(self.claim(timedelta(minutes=10))["claimed"])
        outbox = self.repository.welcome_outboxes()[0]
        self.assertEqual((outbox.state, outbox.last_error_code), (WelcomeEmailState.DELIVERY_OUTCOME_UNCERTAIN, "send_intent_lease_expired"))
        for later in (timedelta(hours=1), timedelta(days=3)):
            self.assertFalse(self.claim(later)["claimed"])
        response = self.post("mailer", f"/v1/welcome-emails/{job['outbox_id']}/result", {"schema_version": "xb.member.welcome_email.result.v1", "lease_id": job["lease_id"], "state_version": intent["state_version"], "outcome": "smtp_accepted", "error_code": None})
        self.assertEqual(response.status, 409)
        events = [event["event_type"] for event in self.repository.welcome_events]
        self.assertEqual(events.count("send_intent_recorded"), 1)
        self.assertEqual(events[-1], "delivery_outcome_uncertain")

    def test_unclassified_post_intent_outcome_is_uncertain(self):
        self.flow.to_result("welcome-uncertain")
        job = self.claim()["job"]
        intent = self.service.welcome_send_intent(job["outbox_id"], {"lease_id": job["lease_id"], "state_version": job["state_version"]})
        uncertain = self.service.welcome_result(job["outbox_id"], {"schema_version": "xb.member.welcome_email.result.v1", "lease_id": job["lease_id"], "state_version": intent["state_version"], "outcome": "delivery_outcome_uncertain", "error_code": "smtp_node_timeout"})
        self.assertEqual(uncertain["state"], "DELIVERY_OUTCOME_UNCERTAIN")
        # A "safe failure" claim is impossible once intent exists.
        response = self.post("mailer", f"/v1/welcome-emails/{job['outbox_id']}/result", {"schema_version": "xb.member.welcome_email.result.v1", "lease_id": job["lease_id"], "state_version": intent["state_version"], "outcome": "failed_before_send_intent", "error_code": "late"})
        self.assertEqual(response.status, 409)
        self.assertFalse(self.claim(timedelta(days=1))["claimed"])

    def test_pre_intent_failures_retry_at_five_and_thirty_minutes_then_dead_letter(self):
        self.flow.to_result("welcome-retry")
        elapsed = timedelta(minutes=2)
        expected = (("RETRY_WAIT", timedelta(minutes=5)), ("RETRY_WAIT", timedelta(minutes=30)), ("DEAD_LETTER", None))
        for attempt, (state, delay) in enumerate(expected, start=1):
            job = self.claim(elapsed)["job"]
            self.assertEqual(job["attempt"], attempt)
            failed = self.service.welcome_result(job["outbox_id"], {"schema_version": "xb.member.welcome_email.result.v1", "lease_id": job["lease_id"], "state_version": job["state_version"], "outcome": "failed_before_send_intent", "error_code": "smtp_credential_unavailable"})
            self.assertEqual(failed["state"], state)
            if delay is not None:
                self.assertFalse(self.claim(elapsed + delay - timedelta(seconds=1))["claimed"])
                elapsed = elapsed + delay
        self.assertFalse(self.claim(timedelta(days=7))["claimed"])
        self.assertEqual([event["event_type"] for event in self.repository.welcome_events].count("send_intent_recorded"), 0)

    def test_lease_expiry_before_intent_is_a_safe_retry(self):
        self.flow.to_result("welcome-lease")
        job = self.claim()["job"]
        self.at(timedelta(minutes=5))
        stale = self.post("mailer", f"/v1/welcome-emails/{job['outbox_id']}/send-intent", {"lease_id": job["lease_id"], "state_version": job["state_version"]})
        self.assertEqual((stale.status, stale.body["error_code"]), (409, "welcome_email_lease_expired"))
        self.assertFalse(self.claim(timedelta(minutes=5))["claimed"])
        outbox = self.repository.welcome_outboxes()[0]
        self.assertEqual((outbox.state, outbox.attempt), (WelcomeEmailState.RETRY_WAIT, 1))
        again = self.claim(timedelta(minutes=11))["job"]
        self.assertEqual(again["attempt"], 2)
        self.assertNotEqual(again["lease_id"], job["lease_id"])

    def test_lease_and_cas_bindings_fail_closed_and_only_one_row_is_leased(self):
        self.flow.to_result("welcome-cas-a", phone="81111111")
        self.flow.to_result("welcome-cas-b", phone="82222222", email="second-synthetic@example.test")
        job = self.claim()["job"]
        self.assertFalse(self.claim()["claimed"])
        for body, code in (
            ({"lease_id": "lease-" + "0" * 32, "state_version": job["state_version"]}, "welcome_email_lease_conflict"),
            ({"lease_id": job["lease_id"], "state_version": job["state_version"] + 5}, "welcome_email_state_version_mismatch"),
            ({"lease_id": "not-a-lease", "state_version": job["state_version"]}, "welcome_email_lease_invalid"),
        ):
            response = self.post("mailer", f"/v1/welcome-emails/{job['outbox_id']}/send-intent", body)
            self.assertEqual(response.body["error_code"], code)
        missing = self.post("mailer", "/v1/welcome-emails/welcome-" + "e" * 32 + "/send-intent", {"lease_id": job["lease_id"], "state_version": 1})
        self.assertEqual((missing.status, missing.body["error_code"]), (404, "welcome_email_not_found"))

    def test_tampered_message_identity_is_never_sent(self):
        self.flow.to_result("welcome-tamper")
        with self.repository._lock:
            outbox = self.repository.welcome_outboxes()[0]
            self.repository._outbox[outbox.outbox_id] = outbox.__class__(**{**{field: getattr(outbox, field) for field in outbox.__slots__}, "recipient": "attacker@example.test"})
        self.at(timedelta(minutes=2))
        response = self.post("mailer", "/v1/welcome-emails/claim")
        self.assertEqual((response.status, response.body["error_code"]), (409, "welcome_email_message_identity_mismatch"))

    def test_email_failure_never_touches_the_member_job_or_allocation(self):
        job_id, _, _ = self.flow.to_result("welcome-isolation")
        before = self.repository.get_job(job_id)
        allocation = self.repository.get_allocation(job_id)
        job = self.claim()["job"]
        self.service.welcome_result(job["outbox_id"], {"schema_version": "xb.member.welcome_email.result.v1", "lease_id": job["lease_id"], "state_version": job["state_version"], "outcome": "failed_before_send_intent", "error_code": "smtp_unreachable"})
        after = self.repository.get_job(job_id)
        self.assertEqual((after.state, after.state_version, after.save_invocation_count), (JobState.CREATED_VERIFIED, before.state_version, 1))
        self.assertEqual(self.repository.get_allocation(job_id), allocation)
        self.assertEqual(len(self.repository.all_jobs()), 1)
        # Replaying the same Forms response returns the same job and outbox.
        self.assertTrue(self.service.ingest(source_event("welcome-isolation"))["replayed"])
        self.assertEqual(len(self.repository.welcome_outboxes()), 1)

    def test_kill_switch_blocks_mailer_claims(self):
        self.flow.to_result("welcome-kill")
        self.repository.set_control("kill_switch_enabled", True)
        self.at(timedelta(minutes=2))
        response = self.post("mailer", "/v1/welcome-emails/claim")
        self.assertEqual((response.status, response.body["error_code"]), (423, "kill_switch_enabled"))

    # --- authentication / cross-scope denial -------------------------------

    def test_mailer_principal_is_denied_every_member_source_control_and_operator_route(self):
        job_id, _, _ = self.flow.to_result("welcome-scope")
        forbidden = [
            ("POST", "/v1/source-events"), ("POST", "/v1/source-rejections"), ("GET", "/v1/source/cursor?form_alias=member_registration&mapping_version=member-intake.v1"),
            ("POST", "/v1/source/epochs/begin"), ("POST", "/v1/source/epochs/epoch-" + "0" * 32 + "/restart"),
            ("POST", "/v1/source/epochs/epoch-" + "0" * 32 + "/pages/open"), ("POST", "/v1/source/pages/page-" + "0" * 32 + "/commit"),
            ("POST", "/v1/worker/claim"), ("POST", f"/v1/jobs/{job_id}/precheck"), ("POST", f"/v1/jobs/{job_id}/lease"),
            ("POST", f"/v1/jobs/{job_id}/allocation/candidate"), ("POST", f"/v1/jobs/{job_id}/allocation"),
            ("POST", f"/v1/jobs/{job_id}/write-intent"), ("POST", f"/v1/jobs/{job_id}/dispatch-fence"),
            ("POST", f"/v1/jobs/{job_id}/writer/register"), ("POST", f"/v1/jobs/{job_id}/writer/termination"),
            ("POST", f"/v1/jobs/{job_id}/writer/quarantine"), ("POST", f"/v1/jobs/{job_id}/writer/recover"),
            ("POST", f"/v1/jobs/{job_id}/result"), ("POST", f"/v1/jobs/{job_id}/reconcile"), ("GET", f"/v1/jobs/{job_id}"),
            ("GET", "/v1/operator/status"), ("GET", f"/v1/operator/reconciliation/{job_id}"),
            ("POST", "/v1/control/kill-switch/disable"), ("POST", "/v1/control/kill-switch/enable"), ("POST", "/v1/control/activation"),
        ]
        for method, path in forbidden:
            with self.subTest(path=path):
                response = self.app.handle(method, path, headers={"Authorization": "Bearer mailer", "X-XB-Worker-Session": SESSION}, body="{}")
                self.assertEqual((response.status, response.body["error_code"]), (403, "scope_denied"))
        self.assertEqual(self.repository.get_job(job_id).state, JobState.CREATED_VERIFIED)

    def test_no_other_principal_can_reach_the_welcome_routes(self):
        routes = ("/v1/welcome-emails/claim", "/v1/welcome-emails/welcome-" + "0" * 32 + "/send-intent", "/v1/welcome-emails/welcome-" + "0" * 32 + "/result")
        for token in ("source", "operator", "control", "worker", "recovery"):
            for route in routes:
                with self.subTest(token=token, route=route):
                    response = self.post(token, route)
                    self.assertEqual((response.status, response.body["error_code"]), (403, "scope_denied"))
        self.assertEqual(self.post("nobody", routes[0]).status, 401)


class RealPostgresWelcomeTests(RealPostgresTestCase):
    """Real psycopg production path: the F1 results insert and the atomic
    welcome_v1 outbox must both execute against PostgreSQL."""

    def make_service(self):
        self.seed_cursor()
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", True)
        self.service = GatewayService(config(), self.repository, adapter_ready=True, clock=NOW)
        return MemberFlow(self.service, self.repository)

    def test_first_positive_result_inserts_result_and_outbox_then_mails_once(self):
        flow = self.make_service()
        job_id, body, acknowledged = flow.to_result("real-welcome-positive")
        self.assertEqual((acknowledged["state"], acknowledged["duplicate"]), ("CREATED_VERIFIED", False))
        self.assertEqual(self.sql("SELECT status,save_invocation_count FROM xb_member_gateway.results WHERE job_id=%s", (job_id,)), [("CREATED_VERIFIED", 1)])
        rows = self.sql("SELECT template_id,recipient,message_hash,state FROM xb_member_gateway.welcome_email_outbox WHERE job_id=%s", (job_id,))
        self.assertEqual(rows, [("welcome_v1", RECIPIENT, welcome_message_hash(build_welcome_message(RECIPIENT)), "PENDING")])
        self.assertTrue(self.service.acknowledge_result(job_id, SESSION, body)["duplicate"])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.welcome_email_outbox"), [(1,)])
        self.service.clock = NOW + timedelta(minutes=2)
        job = self.service.claim_welcome_email({})["job"]
        self.assertFalse(self.service.claim_welcome_email({})["claimed"])
        intent = self.service.welcome_send_intent(job["outbox_id"], {"lease_id": job["lease_id"], "state_version": job["state_version"]})
        sent = self.service.welcome_result(job["outbox_id"], {"schema_version": "xb.member.welcome_email.result.v1", "lease_id": job["lease_id"], "state_version": intent["state_version"], "outcome": "smtp_accepted", "error_code": None})
        self.assertEqual(sent["state"], "SENT")
        self.assertEqual([row[0] for row in self.sql("SELECT event_type FROM xb_member_gateway.welcome_email_events ORDER BY event_id")], ["created", "claimed", "send_intent_recorded", "sent"])
        self.assertTrue(self.service.ingest(source_event("real-welcome-positive"))["replayed"])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.jobs"), [(1,)])
        with self.assertRaisesRegex(Exception, "welcome_email_transition_forbidden"):
            self.sql("UPDATE xb_member_gateway.welcome_email_outbox SET state='PENDING',state_version=state_version+1")
        with self.assertRaisesRegex(Exception, "welcome_email_outbox_delete_forbidden"):
            self.sql("DELETE FROM xb_member_gateway.welcome_email_outbox")

    def test_uncertain_result_has_no_outbox_and_exact_reconciliation_creates_it(self):
        flow = self.make_service()
        job_id, body, _ = flow.to_result("real-welcome-reconcile", "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.welcome_email_outbox"), [(0,)])
        self.service.reconcile(job_id, {"member_no": body["member_no"], "lookup_status": "exact_match", "readback_found": True, "readback_match": True, "error_code": None})
        self.assertEqual(self.sql("SELECT state FROM xb_member_gateway.welcome_email_outbox WHERE job_id=%s", (job_id,)), [("PENDING",)])

    def test_post_intent_crash_is_uncertain_in_postgres(self):
        flow = self.make_service()
        flow.to_result("real-welcome-crash")
        self.service.clock = NOW + timedelta(minutes=2)
        job = self.service.claim_welcome_email({})["job"]
        self.service.welcome_send_intent(job["outbox_id"], {"lease_id": job["lease_id"], "state_version": job["state_version"]})
        self.service.clock = NOW + timedelta(minutes=30)
        self.assertFalse(self.service.claim_welcome_email({})["claimed"])
        self.assertEqual(self.sql("SELECT state,last_error_code FROM xb_member_gateway.welcome_email_outbox"), [("DELIVERY_OUTCOME_UNCERTAIN", "send_intent_lease_expired")])

    def test_readiness_blocks_created_verified_without_outbox_and_insert_requires_result(self):
        from xb_member_gateway.repository import RepositoryError
        flow = self.make_service()
        job_id, _, _ = flow.to_result("real-welcome-readiness", "CONFIRMED_NOT_CREATED")
        with self.assertRaisesRegex(Exception, "welcome_email_requires_created_verified"):
            self.sql(
                "INSERT INTO xb_member_gateway.welcome_email_outbox(outbox_id,job_id,response_id,source_response_ref,template_id,recipient,message_hash,state,state_version,attempt,max_attempts,created_at,updated_at) SELECT 'welcome-" + "1" * 32 + "',job_id,response_id,'hmac-v1:" + "0" * 64 + "','welcome_v1','x@example.test','sha256:" + "0" * 64 + "','PENDING',0,0,3,now(),now() FROM xb_member_gateway.jobs WHERE job_id=%s",
                (job_id,),
            )
        self.sql("ALTER TABLE xb_member_gateway.results DISABLE TRIGGER USER")
        self.sql("UPDATE xb_member_gateway.results SET status='CREATED_VERIFIED' WHERE job_id=%s", (job_id,))
        self.sql("ALTER TABLE xb_member_gateway.results ENABLE TRIGGER USER")
        self.sql("UPDATE xb_member_gateway.control_flags SET enabled=(flag_name='kill_switch_enabled')")
        cfg = GatewayConfig(source_cutover_watermark=CUTOVER, source_production_cutover_exact=CUTOVER, source_form_id=FORM)
        with self.assertRaisesRegex(RepositoryError, "created_verified_without_welcome_outbox"):
            self.repository.verify_bootstrap_readiness(cfg)


if __name__ == "__main__":
    unittest.main()
