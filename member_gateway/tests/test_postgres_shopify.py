"""Real-PostgreSQL evidence for migration 0006 (Shopify M1). Synthetic data only.

Runs only against a disposable loopback database named by
``XB_MEMBER_GATEWAY_TEST_DATABASE_URL``; otherwise every test skips.
"""

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.models import JobState, ShopifyAdmissionState
from xb_member_gateway.repository import PostgresRepository, RepositoryError, SourceConflict
from xb_member_gateway.shopify_admission import baseline_digest
from xb_member_gateway.shopify_receiver import capture_baseline

try:
    from .test_postgres_cursor import CUTOVER as FORMS_CUTOVER, FORM, MIGRATIONS, RealPostgresTestCase, psycopg, source_event
    from ._shopify_support import API_VERSION, BASELINE_GID, CUTOVER, NEW_GID, NOW, PROOF_GID, SECOND_GID, SHOP, ShopifyHarness, customer
except ImportError:  # discovered as a top-level module
    from test_postgres_cursor import CUTOVER as FORMS_CUTOVER, FORM, MIGRATIONS, RealPostgresTestCase, psycopg, source_event  # type: ignore
    from _shopify_support import API_VERSION, BASELINE_GID, CUTOVER, NEW_GID, NOW, PROOF_GID, SECOND_GID, SHOP, ShopifyHarness, customer  # type: ignore

HASH = "sha256:" + "b" * 64
PII = ("Synthetic", "Shopify Alpha", "example.test", "15550100101", "555-010")


class RealPostgresShopifyTests(RealPostgresTestCase):
    def safe_controls(self):
        self.repository.set_control("kill_switch_enabled", True)
        self.repository.set_control("production_activation_enabled", False)

    def harness(self, **kwargs):
        return ShopifyHarness(self.repository, **kwargs)

    def full_dump(self):
        """Every row of every gateway table, rendered for PII scanning."""
        tables = [row[0] for row in self.sql("SELECT table_name FROM information_schema.tables WHERE table_schema='xb_member_gateway' AND table_type='BASE TABLE'")]
        return json.dumps({table: self.sql(f"SELECT * FROM xb_member_gateway.{table}") for table in tables}, default=str)

    def test_fresh_install_constraints_and_append_only_receipts(self):
        self.assertIn(("0006_shopify_member_m1",), self.sql("SELECT version FROM xb_member_gateway.schema_migrations"))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.sql("INSERT INTO xb_member_gateway.jobs(job_id,response_id,operation,payload_hash,canonical_payload,state,max_attempts,source_system) VALUES('job-x',NULL,'member.create',%s,'{\"name\":\"x\"}'::jsonb,'QUEUED',3,'shopify')", (HASH,))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.sql("INSERT INTO xb_member_gateway.jobs(job_id,response_id,operation,payload_hash,canonical_payload,state,max_attempts,source_system) VALUES('job-y',NULL,'member.create',%s,'{}'::jsonb,'QUEUED',3,'google_forms')", (HASH,))
        harness = self.harness(baseline=None)  # receipt behaviour is baseline-independent
        self.assertEqual(harness.deliver().status, 200)
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_webhook_receipts_append_only"):
            self.sql("DELETE FROM xb_member_gateway.shopify_webhook_receipts")
        self.assertEqual(self.sql("SELECT state FROM xb_member_gateway.shopify_member_admissions"), [("PENDING",)])
        self.assertTrue(harness.deliver().body["replayed"])
        self.assertEqual(harness.deliver(SECOND_GID).status, 409)
        columns = {row[0] for row in self.sql("SELECT column_name FROM information_schema.columns WHERE table_schema='xb_member_gateway' AND table_name='shopify_webhook_receipts'")}
        self.assertEqual(columns, {"webhook_id", "topic", "shop_domain", "api_version", "event_id", "triggered_at", "customer_gid", "received_at"})

    def test_baseline_seal_is_recomputed_by_database_and_partial_capture_cannot_commit(self):
        baseline_id = "baseline-" + uuid.uuid4().hex
        insert = "INSERT INTO xb_member_gateway.shopify_cutover_baselines(baseline_id,state,shop_domain,api_version,capture_started_at,member_count,member_digest) VALUES(%s,'CAPTURING',%s,%s,{c},0,%s)"
        whole = "date_trunc('second', now())"
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_baseline_must_start_capturing"):
            self.sql("INSERT INTO xb_member_gateway.shopify_cutover_baselines(baseline_id,state,shop_domain,api_version,capture_started_at,sealed_at,member_count,member_digest) VALUES(%s,'SEALED',%s,'2026-10',now(),now(),0,%s)", (baseline_id, SHOP, baseline_digest([])))
        # Receiver-before-C is enforced by the database itself.
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_receiver_not_verified_before_cutover"):
            self.sql(insert.format(c=whole), (baseline_id, SHOP, API_VERSION, baseline_digest([])))
        self.harness(baseline=None).deliver_receiver_proof()
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_receiver_not_verified_before_cutover"):
            self.sql(insert.format(c="%s"), (baseline_id, SHOP, API_VERSION, CUTOVER - timedelta(minutes=10), baseline_digest([])))
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_receiver_not_verified_before_cutover"):
            self.sql(insert.format(c=whole), (baseline_id, SHOP, "2026-07", baseline_digest([])))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.sql(insert.format(c="%s"), (baseline_id, SHOP, API_VERSION, CUTOVER + timedelta(microseconds=250000), baseline_digest([])))
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_cutover_in_future"):
            self.sql(insert.format(c="date_trunc('second', now()) + interval '1 hour'"), (baseline_id, SHOP, API_VERSION, baseline_digest([])))
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_baseline_partial_capture"):
            self.sql(insert.format(c=whole), (baseline_id, SHOP, API_VERSION, baseline_digest([])))
        with psycopg.connect(self.dsn) as connection:
            connection.execute("INSERT INTO xb_member_gateway.shopify_cutover_baselines(baseline_id,state,shop_domain,api_version,capture_started_at,member_count,member_digest) VALUES(%s,'CAPTURING',%s,'2026-10',date_trunc('second', now()),1,%s)", (baseline_id, SHOP, baseline_digest([NEW_GID])))
            connection.execute("INSERT INTO xb_member_gateway.shopify_baseline_members VALUES(%s,%s)", (baseline_id, BASELINE_GID))
            with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_baseline_seal_mismatch"):
                connection.execute("UPDATE xb_member_gateway.shopify_cutover_baselines SET state='SEALED',sealed_at=now() WHERE baseline_id=%s", (baseline_id,))
            connection.rollback()
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.shopify_cutover_baselines"), [(0,)])
        sealed = self.repository.store_shopify_baseline([SECOND_GID, BASELINE_GID], shop_domain=SHOP, api_version="2026-10", capture_started_at=CUTOVER, now=NOW)
        self.assertEqual((sealed.member_count, sealed.member_digest), (2, baseline_digest([BASELINE_GID, SECOND_GID])))
        self.assertTrue(self.repository.verify_shopify_baseline(sealed.baseline_id))
        self.assertEqual(self.sql("SELECT member_count,member_digest FROM xb_member_gateway.shopify_baseline_recomputed(%s)", (sealed.baseline_id,)), [(2, sealed.member_digest)])
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_baseline_sealed_immutable"):
            self.sql("INSERT INTO xb_member_gateway.shopify_baseline_members VALUES(%s,%s)", (sealed.baseline_id, NEW_GID))
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_baseline_members_append_only"):
            self.sql("DELETE FROM xb_member_gateway.shopify_baseline_members")
        with self.assertRaisesRegex(SourceConflict, "shopify_baseline_already_sealed"):
            self.repository.store_shopify_baseline([], shop_domain=SHOP, api_version="2026-10", capture_started_at=CUTOVER)

    def test_cutover_is_the_database_clock_whole_second_utc(self):
        cutover = self.repository.shopify_cutover_now()
        database_now = self.sql("SELECT now()")[0][0]
        self.assertEqual((cutover.microsecond, cutover.utcoffset()), (0, timedelta(0)))
        self.assertLessEqual(abs((database_now - cutover).total_seconds()), 5)
        self.assertFalse(self.repository.shopify_receiver_verified_before(SHOP, API_VERSION, cutover))
        self.harness(baseline=None).deliver_receiver_proof()
        self.assertTrue(self.repository.shopify_receiver_verified_before(SHOP, API_VERSION, cutover))
        self.assertFalse(self.repository.shopify_receiver_verified_before(SHOP, API_VERSION, CUTOVER - timedelta(minutes=10)))
        self.assertFalse(self.repository.shopify_receiver_verified_before(SHOP, "2026-07", cutover))

    def test_atomic_seal_writes_historical_and_gid_only_transition_rows(self):
        harness = self.harness(baseline=None)
        harness.deliver_receiver_proof()
        harness.deliver(SECOND_GID, webhook_id="wh-00000000-0610")
        existing = self.repository.get_shopify_admission(SECOND_GID)
        with self.assertRaisesRegex(SourceConflict, "shopify_receiver_not_verified_before_cutover"):
            self.repository.store_shopify_baseline([BASELINE_GID], transition_gids=[NEW_GID], shop_domain=SHOP, api_version="2026-07", capture_started_at=CUTOVER, now=NOW)
        sealed = self.repository.store_shopify_baseline([BASELINE_GID], transition_gids=[NEW_GID, SECOND_GID], shop_domain=SHOP, api_version=API_VERSION, capture_started_at=CUTOVER, now=NOW)
        self.assertEqual(self.sql("SELECT customer_gid FROM xb_member_gateway.shopify_baseline_members"), [(BASELINE_GID,)])
        self.assertEqual(self.sql("SELECT state,capture_started_at FROM xb_member_gateway.shopify_cutover_baselines"), [("SEALED", CUTOVER)])
        rows = dict(self.sql("SELECT customer_gid,state FROM xb_member_gateway.shopify_member_admissions"))
        self.assertEqual(rows, {PROOF_GID: "PENDING", SECOND_GID: "PENDING", NEW_GID: "PENDING"})
        self.assertEqual(self.repository.get_shopify_admission(SECOND_GID), existing)  # existing row authoritative
        transition = self.repository.get_shopify_admission(NEW_GID)
        self.assertEqual((transition.state_version, transition.job_id, transition.reason_code), (0, None, None))
        self.assertTrue(transition.gid_ref.startswith("hmac-v1:"))
        self.assertTrue(self.repository.verify_shopify_baseline(sealed.baseline_id))
        dump = self.full_dump()
        for value in PII:
            self.assertNotIn(value, dump)
        self.assertNotIn("createdAt", dump)

    def test_failure_inside_seal_transaction_leaves_no_partial_state(self):
        self.harness(baseline=None).deliver_receiver_proof()
        with mock.patch.object(PostgresRepository, "_verify_baseline_cursor", return_value=None):
            with self.assertRaisesRegex(SourceConflict, "shopify_baseline_seal_mismatch"):
                self.repository.store_shopify_baseline([BASELINE_GID], transition_gids=[NEW_GID], shop_domain=SHOP, api_version=API_VERSION, capture_started_at=CUTOVER, now=NOW)
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.shopify_cutover_baselines"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.shopify_baseline_members"), [(0,)])
        self.assertEqual(self.sql("SELECT customer_gid FROM xb_member_gateway.shopify_member_admissions"), [(PROOF_GID,)])
        self.assertIsNone(self.repository.get_shopify_baseline())

    def test_capture_end_to_end_on_database_clock(self):
        harness = self.harness(baseline=None)
        harness.deliver_receiver_proof()
        pages = [
            {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"id": BASELINE_GID, "createdAt": "2020-01-01T00:00:00Z", "tags": ["member-mg"]}]},
            {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": NEW_GID, "createdAt": "2099-01-01T00:00:00+08:00", "tags": ["member-mg"]}]},
        ]
        composition = SimpleNamespace(repository=self.repository, client=SimpleNamespace(baseline_page=lambda after: pages.pop(0)), shopify_config=SimpleNamespace(shop_domain=SHOP, api_version=API_VERSION))
        sealed = capture_baseline(composition)
        self.assertEqual((sealed["member_count"], sealed["transition_pending_count"]), (1, 1))
        cutover = self.sql("SELECT capture_started_at FROM xb_member_gateway.shopify_cutover_baselines")[0][0]
        self.assertEqual(cutover.microsecond, 0)
        self.repository.enable_shopify_admission(sealed["baseline_id"], "synthetic-approval-a2")
        current = datetime.now(timezone.utc) + timedelta(minutes=1)
        harness.clock = current
        harness.reader.customers[NEW_GID] = customer(NEW_GID, created_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
        harness.reader.customers[BASELINE_GID] = customer(BASELINE_GID, created_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
        harness.receiver.clock = current
        harness.deliver(BASELINE_GID, topic="customers/update", webhook_id="wh-00000000-0620")
        harness.processor.run_once()
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.ADMITTED)
        conflict = self.repository.get_shopify_admission(BASELINE_GID)
        self.assertEqual((conflict.state, conflict.reason_code), (ShopifyAdmissionState.MANUAL_REVIEW, "baseline_created_at_conflict"))
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.jobs WHERE source_system='shopify'"), [(1,)])

    def test_gateway_held_member_no_collision_is_bounded_in_database(self):
        harness = self.harness()
        harness.admit()
        job, held = harness.claim_through_allocation(member_no="M000701")
        fence, writer = harness.dispatch(job, held)
        harness.confirm(job, writer)
        harness.result(job, fence, held, "CREATED_VERIFIED", found=True, match=True)
        self.assertEqual(self.repository.member_no_owner(held), job["job_id"])
        self.assertIsNone(self.repository.member_no_owner("M000799"))
        harness.admit(SECOND_GID, profile=customer(SECOND_GID), webhook_id="wh-00000000-0702")
        second = harness.call("POST", "/v1/worker/claim", {}).body["job"]
        harness.call("POST", f"/v1/jobs/{second['job_id']}/precheck", {})
        harness.call("POST", f"/v1/jobs/{second['job_id']}/shopify/legacy-precheck", {"outcome": "NO_CANDIDATE", "phone_member_no_hit": False, "mobile_phone_hit": False, "email_hit": False})
        collision = harness.call("POST", f"/v1/jobs/{second['job_id']}/allocation/probe", {"candidate": held, "status": "FREE", "probe_reference": "probe-pg-1"})
        self.assertEqual((collision.status, collision.body["gateway_bound_collision"], collision.body["candidates_remaining"]), (200, True, 2))
        repeated = harness.call("POST", f"/v1/jobs/{second['job_id']}/allocation/probe", {"candidate": held, "status": "FREE", "probe_reference": "probe-pg-2"})
        self.assertEqual((repeated.status, repeated.body["error_code"]), (409, "member_no_generator_not_advancing"))
        self.assertEqual(self.repository.get_job(second["job_id"]).state, JobState.MANUAL_REVIEW)
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.member_allocations WHERE job_id=%s", (second["job_id"],)), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.shopify_protected_payloads WHERE job_id=%s", (second["job_id"],)), [(0,)])

    def test_admission_cannot_enable_or_admit_without_verified_baseline(self):
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_admission_requires_sealed_baseline|violates foreign key|check"):
            self.sql("UPDATE xb_member_gateway.shopify_admission_control SET admission_enabled=TRUE,baseline_id=NULL,approval_reference='x',state_version=state_version+1")
        harness = self.harness(enable=False)
        harness.reader.customers[NEW_GID] = customer()
        harness.deliver()
        self.assertEqual(harness.processor.run_once()["processed"], 0)
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_admission_disabled|shopify_member_admission"):
            self.sql("UPDATE xb_member_gateway.shopify_member_admissions SET state='EXCLUDED_BASELINE',state_version=state_version+1")
        with self.assertRaisesRegex(SourceConflict, "shopify_admission_disabled"):
            self.repository.admit_shopify_member(NEW_GID, expected_state_version=0, baseline_id=harness.baseline.baseline_id, job_id="job-" + "1" * 32, envelope="xbpp1.k1." + "A" * 16 + "." + "B" * 32, payload_digest="sha256:" + __import__("hashlib").sha256(("xbpp1.k1." + "A" * 16 + "." + "B" * 32).encode()).hexdigest(), key_id="k1", now=NOW)
        self.repository.enable_shopify_admission(harness.baseline.baseline_id, "synthetic-approval")
        self.assertIsNotNone(self.repository.shopify_admission_gate())
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_admission_baseline_immutable"):
            self.sql("UPDATE xb_member_gateway.shopify_admission_control SET baseline_id=NULL,admission_enabled=FALSE,state_version=state_version+1")

    def test_end_to_end_create_scrubs_payload_without_outbox_and_stays_pii_free(self):
        harness = self.harness()
        admission, counts = harness.admit()
        self.assertEqual((admission.state, counts["admitted"]), (ShopifyAdmissionState.ADMITTED, 1))
        job_row = self.sql("SELECT source_system,response_id,canonical_payload FROM xb_member_gateway.jobs WHERE job_id=%s", (admission.job_id,))
        self.assertEqual(job_row, [("shopify", None, {})])
        self.assertNotIn("Synthetic", self.full_dump())
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_protected_payload_retention_required"):
            self.sql("DELETE FROM xb_member_gateway.shopify_protected_payloads")
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "shopify_protected_payload_immutable"):
            self.sql("UPDATE xb_member_gateway.shopify_protected_payloads SET key_id='k2'")
        job, member_no = harness.claim_through_allocation()
        fence, writer = harness.dispatch(job, member_no)
        harness.confirm(job, writer)
        result = harness.result(job, fence, member_no, "CREATED_VERIFIED", found=True, match=True)
        self.assertEqual(result.status, 200, result.body)
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.shopify_protected_payloads"), [(0,)])
        self.assertEqual(self.sql("SELECT job_state FROM xb_member_gateway.shopify_payload_scrubs WHERE job_id=%s", (job["job_id"],)), [("CREATED_VERIFIED",)])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.welcome_email_outbox"), [(0,)])
        self.assertEqual(self.sql("SELECT customer_gid,member_no,job_state FROM xb_member_gateway.shopify_member_mappings"), [(NEW_GID, member_no, "CREATED_VERIFIED")])
        dump = self.full_dump()
        for value in PII:
            self.assertNotIn(value, dump)
        self.seed_cursor()
        config = GatewayConfig(source_cutover_watermark=FORMS_CUTOVER, source_production_cutover_exact=FORMS_CUTOVER, source_form_id=FORM)
        self.safe_controls()
        self.repository.verify_bootstrap_readiness(config)
        self.assertTrue(harness.deliver(webhook_id="wh-00000000-0777").status == 200)
        harness.processor.run_once()
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.jobs WHERE source_system='shopify'"), [(1,)])
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "welcome_email_requires_forms_source"):
            self.sql("INSERT INTO xb_member_gateway.welcome_email_outbox(outbox_id,job_id,response_id,source_response_ref,template_id,recipient,message_hash,state,state_version,attempt,max_attempts,next_attempt_at,created_at,updated_at) SELECT 'welcome-' || md5('x'),%s,'r','hmac-v1:' || repeat('0',64),'welcome_v1','x@y.z',%s,'PENDING',0,0,3,now(),now(),now()", (job["job_id"], HASH))

    def test_uncertain_write_retains_until_exact_reconciliation_and_readiness_blocks_leftover(self):
        harness = self.harness()
        harness.admit()
        job, member_no = harness.claim_through_allocation()
        fence, writer = harness.dispatch(job, member_no)
        harness.confirm(job, writer)
        harness.result(job, fence, member_no, "WRITE_OUTCOME_UNCERTAIN", found=False, match=False, error_code="save_outcome_uncertain")
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.shopify_protected_payloads"), [(1,)])
        claim = harness.call("POST", "/v1/worker/reconcile/claim", {})
        self.assertEqual(claim.body["job"]["member_no"], member_no)
        response = harness.call("POST", f"/v1/jobs/{job['job_id']}/reconcile", {"member_no": member_no, "lookup_status": "exact_match", "readback_found": True, "readback_match": True, "error_code": None})
        self.assertEqual((response.status, response.body["state"]), (200, "CREATED_VERIFIED"))
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.shopify_protected_payloads"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.welcome_email_outbox"), [(0,)])
        # Readiness backstop: force a terminal leftover by bypassing the trigger.
        second = ShopifyHarness(self.repository, baseline=None)
        second.reader.customers[SECOND_GID] = customer(SECOND_GID)
        second.processor.cipher = harness.cipher
        harness.reader.customers[SECOND_GID] = customer(SECOND_GID)
        harness.deliver(SECOND_GID, webhook_id="wh-00000000-0888")
        harness.processor.run_once()
        second_job = self.repository.get_shopify_admission(SECOND_GID).job_id
        self.sql("ALTER TABLE xb_member_gateway.jobs DISABLE TRIGGER jobs_shopify_payload_scrub")
        try:
            self.sql("UPDATE xb_member_gateway.jobs SET state='DEAD_LETTER' WHERE job_id=%s", (second_job,))
        finally:
            self.sql("ALTER TABLE xb_member_gateway.jobs ENABLE TRIGGER jobs_shopify_payload_scrub")
        self.assertEqual(self.repository.protected_payload_leftover_count(), 1)
        self.seed_cursor()
        config = GatewayConfig(source_cutover_watermark=FORMS_CUTOVER, source_production_cutover_exact=FORMS_CUTOVER, source_form_id=FORM)
        self.safe_controls()
        with self.assertRaisesRegex(RepositoryError, "terminal_job_protected_payload_present"):
            self.repository.verify_bootstrap_readiness(config)
        self.assertIn("terminal_job_protected_payload_present", harness.service.readiness()["reasons"])

    def test_precheck_review_and_allocation_ambiguity_scrub_in_database(self):
        harness = self.harness()
        harness.admit()
        job, _ = harness.claim_through_allocation(precheck={"outcome": "CANDIDATE_FOUND", "phone_member_no_hit": False, "mobile_phone_hit": True, "email_hit": False})
        self.assertEqual(self.repository.get_job(job["job_id"]).state, JobState.MANUAL_REVIEW)
        self.assertEqual(self.sql("SELECT outcome,mobile_phone_hit FROM xb_member_gateway.shopify_legacy_prechecks"), [("CANDIDATE_FOUND", True)])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.shopify_protected_payloads"), [(0,)])
        status = harness.call("GET", "/v1/operator/shopify-status")
        self.assertEqual(status.body["terminal_payload_leftover_count"], 0)
        self.assertNotIn("gid://", json.dumps(status.body))


class RealPostgresUpgradeTests(RealPostgresTestCase):
    """0001-0005 with live Forms history, then 0006 applied in place."""

    safe_controls = RealPostgresShopifyTests.safe_controls

    def setUp(self):
        super().setUp()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS xb_member_gateway CASCADE")
            for path in MIGRATIONS[:5]:
                connection.execute(path.read_text(encoding="utf-8"))

    def test_upgrade_preserves_forms_history_and_forms_flows_continue(self):
        self.seed_cursor()
        fence = str(uuid.uuid4())
        self.sql("INSERT INTO xb_member_gateway.source_responses(response_id,source_response_ref,create_time,create_time_exact,mapping_version,payload_hash,canonical_payload) VALUES('forms-old-1','hmac-v1:' || repeat('1',64),now(),'2026-09-15T00:00:05Z','member-intake.v1',%s,'{\"email\":\"old@example.test\"}'::jsonb)", (HASH,))
        self.sql("INSERT INTO xb_member_gateway.jobs(job_id,response_id,operation,payload_hash,canonical_payload,state,state_version,attempt_count,max_attempts,dispatch_fence_id,save_invocation_count,result_status) VALUES('job-old-1','forms-old-1','member.create',%s,'{\"email\":\"old@example.test\"}'::jsonb,'CREATED_VERIFIED',9,1,3,%s,1,'CREATED_VERIFIED')", (HASH, fence))
        self.sql("INSERT INTO xb_member_gateway.dispatch_fences(fence_id,job_id,operation,member_no,created_at) VALUES(%s,'job-old-1','member.create','6590000009',now())", (fence,))
        self.sql("INSERT INTO xb_member_gateway.results(job_id,result_hash,status,member_no,dispatch_fence_id,save_invocation_count,readback_found,readback_match,reconciliation_required,acknowledged_at) VALUES('job-old-1',%s,'CREATED_VERIFIED','6590000009',%s,1,true,true,false,now())", (HASH, fence))
        self.sql("INSERT INTO xb_member_gateway.welcome_email_outbox(outbox_id,job_id,response_id,source_response_ref,template_id,recipient,message_hash,state,state_version,attempt,max_attempts,next_attempt_at,created_at,updated_at) VALUES('welcome-' || repeat('a',32),'job-old-1','forms-old-1','hmac-v1:' || repeat('1',64),'welcome_v1','old@example.test',%s,'PENDING',0,0,3,now(),now(),now())", (HASH,))
        self.sql("INSERT INTO xb_member_gateway.source_handling_receipts(response_id,source_response_ref,form_alias,form_id,mapping_version,create_time_exact,payload_fingerprint,outcome,job_id) VALUES('forms-old-1','hmac-v1:' || repeat('1',64),'member_registration',%s,'member-intake.v1','2026-09-15T00:00:05Z',%s,'ACCEPTED','job-old-1')", (FORM, HASH))
        before = self.sql("SELECT job_id,response_id,state,canonical_payload FROM xb_member_gateway.jobs ORDER BY job_id")
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(MIGRATIONS[5].read_text(encoding="utf-8"))
            connection.execute(MIGRATIONS[5].read_text(encoding="utf-8"))  # idempotent re-run
        self.assertEqual(self.sql("SELECT job_id,response_id,state,canonical_payload FROM xb_member_gateway.jobs ORDER BY job_id"), before)
        self.assertEqual(self.sql("SELECT source_system FROM xb_member_gateway.jobs"), [("google_forms",)])
        self.assertEqual(self.sql("SELECT state,recipient FROM xb_member_gateway.welcome_email_outbox"), [("PENDING", "old@example.test")])
        self.assertEqual(len(self.sql("SELECT version FROM xb_member_gateway.schema_migrations")), 6)
        config = GatewayConfig(source_cutover_watermark=FORMS_CUTOVER, source_production_cutover_exact=FORMS_CUTOVER, source_form_id=FORM)
        self.safe_controls()
        self.repository.verify_bootstrap_readiness(config)
        self.assertEqual(self.repository.get_job("job-old-1").source_system, "google_forms")
        outcome = self.repository.ingest_source_event(source_event("forms-after-upgrade", "2026-09-15T00:00:09Z"), initial_window_max=None)
        self.assertEqual(outcome.job.source_system, "google_forms")
        self.repository.set_control("kill_switch_enabled", False)
        claimed = self.repository.claim_job("ws-" + "c" * 32, now=NOW, source_systems=("google_forms", "shopify"))
        self.assertEqual(claimed.job_id, outcome.job.job_id)
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "job_source_identity_immutable"):
            self.sql("UPDATE xb_member_gateway.jobs SET source_system='shopify' WHERE job_id='job-old-1'")
        self.assertTrue(self.repository.operator_status()["migrations_ready"])


if __name__ == "__main__":
    unittest.main()
