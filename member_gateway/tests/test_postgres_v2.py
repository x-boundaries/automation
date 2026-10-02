"""U-MG plus the PostgreSQL v2 lifecycle (W-G2-149 sections 2.9, 3, 4.1, 4.6).

Static checks of ``0006_member_write_v2.sql`` always run. The real-database
classes use the disposable loopback PostgreSQL named only by
``XB_MEMBER_GATEWAY_TEST_DATABASE_URL`` (see test_postgres_cursor); they drop
and recreate their own ``xb_member_gateway`` schema and skip without it.
"""

import json
import re
import unittest
import uuid
from datetime import timedelta

from xb_member_gateway.models import JobState
from xb_member_gateway.repository import ResolutionConflict, ResultConflict, ResultStale

try:
    from .test_postgres_cursor import CUTOVER_DT, MIGRATIONS, RealPostgresTestCase, psycopg, real_postgres_dsn
    from .v2_support import NOW, ROOT, SESSION, SESSION_B, guid, later, make_config, open_gate, outcome_body, policy, source_event
except ImportError:  # discovered as a top-level module
    from test_postgres_cursor import CUTOVER_DT, MIGRATIONS, RealPostgresTestCase, psycopg, real_postgres_dsn
    from v2_support import NOW, ROOT, SESSION, SESSION_B, guid, later, make_config, open_gate, outcome_body, policy, source_event


MIGRATION_0006 = ROOT / "member_gateway/migrations/0006_member_write_v2.sql"
ALLOWED_PRE_STATES = ("CREATED_VERIFIED", "REJECTED_VALIDATION", "CONFIRMED_NOT_CREATED", "CREATED_READBACK_MISMATCH", "MANUAL_REVIEW", "DEAD_LETTER")
HASH = "sha256:" + "a" * 64


class MigrationStaticTests(unittest.TestCase):
    """U-MG (offline): 0006 only adds and carries the guard."""

    def setUp(self):
        self.text = MIGRATION_0006.read_text(encoding="utf-8")
        self.code = "\n".join(line.split("--", 1)[0] for line in self.text.splitlines())

    def test_migration_is_additive_and_ascii(self):
        self.text.encode("ascii")
        drops = re.findall(r"\bDROP\b[^;\n]*", self.code, re.IGNORECASE)
        self.assertEqual(drops, ["DROP CONSTRAINT IF EXISTS jobs_state_check"])
        for forbidden in ("DELETE FROM", "TRUNCATE", "ON DELETE CASCADE", "ALTER COLUMN", "RENAME"):
            self.assertNotIn(forbidden, self.code.upper(), forbidden)
        self.assertNotRegex(self.code, re.compile(r"\bUPDATE\s+xb_member_gateway\.", re.IGNORECASE))

    def test_state_check_is_a_strict_superset(self):
        old = re.search(r"CHECK \(state IN \((.*?)\)\)", (ROOT / "member_gateway/migrations/0003_writer_termination_quarantine.sql").read_text(encoding="utf-8"), re.S).group(1)
        new = re.search(r"CHECK \(state IN \((.*?)\)\)", self.code, re.S).group(1)
        old_states, new_states = set(re.findall(r"'([A-Z_]+)'", old)), set(re.findall(r"'([A-Z_]+)'", new))
        self.assertEqual(new_states - old_states, {"LINKED_EXISTING", "RESOLVED"})
        self.assertTrue(old_states <= new_states)

    def test_required_objects_are_declared(self):
        for required in (
            "member_write_v2_guard_nonterminal_jobs_present", "member_write_v2_guard_writer_hold_not_cleared",
            "ADD COLUMN IF NOT EXISTS member_no_rule", "ADD COLUMN IF NOT EXISTS base_member_no",
            "ADD COLUMN IF NOT EXISTS name_component", "ADD COLUMN IF NOT EXISTS first_claimed_at",
            "ADD COLUMN IF NOT EXISTS write_attempts", "ADD COLUMN IF NOT EXISTS busy_attempts",
            "ADD COLUMN IF NOT EXISTS outcome_reason", "ADD COLUMN IF NOT EXISTS lease_token",
            "ADD COLUMN IF NOT EXISTS save_invoked", "ADD COLUMN IF NOT EXISTS result_hash",
            "CREATE TABLE IF NOT EXISTS xb_member_gateway.member_outcomes",
            "member_guid uuid NOT NULL", "WHERE outcome = 'CREATED_VERIFIED'",
            "CREATE TABLE IF NOT EXISTS xb_member_gateway.job_resolutions",
            "CREATE OR REPLACE FUNCTION xb_member_gateway.require_created_verified_for_welcome()",
            "FROM xb_member_gateway.member_outcomes", "('0006_member_write_v2')",
        ):
            self.assertIn(required, self.text, required)
        guard = self.text.index("member_write_v2_guard_nonterminal_jobs_present")
        self.assertLess(guard, self.text.index("ALTER TABLE"))
        for state in ALLOWED_PRE_STATES:
            self.assertIn(f"'{state}'", self.text[:guard])

    def test_earlier_migrations_are_unchanged_in_shape(self):
        for path in MIGRATIONS[:5]:
            self.assertNotIn("0006_member_write_v2", path.read_text(encoding="utf-8"))
        self.assertEqual([path.name for path in MIGRATIONS][-1], "0006_member_write_v2.sql")


@unittest.skipIf(psycopg is None or real_postgres_dsn() is None, "disposable PostgreSQL not configured (XB_MEMBER_GATEWAY_TEST_DATABASE_URL)")
class RealMigration0006Tests(unittest.TestCase):
    """U-MG (real): 0001->0005, seed, guard refuses, clear, apply once."""

    def setUp(self):
        self.dsn = real_postgres_dsn()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS xb_member_gateway CASCADE")
            for path in MIGRATIONS[:5]:
                connection.execute(path.read_text(encoding="utf-8"))
        self.addCleanup(self.drop_schema)

    def drop_schema(self):
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS xb_member_gateway CASCADE")

    def sql(self, statement, params=()):
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            cursor = connection.execute(statement, params)
            try:
                return cursor.fetchall()
            except psycopg.ProgrammingError:
                return []

    def apply_0006(self):
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(MIGRATION_0006.read_text(encoding="utf-8"))

    def seed_job(self, index, state):
        response_id = f"legacy-response-{index}"
        self.sql(
            "INSERT INTO xb_member_gateway.source_responses(response_id,source_response_ref,create_time,mapping_version,payload_hash,canonical_payload) VALUES(%s,%s,%s,'member-intake.v1',%s,'{}'::jsonb)",
            (response_id, "hmac-v1:" + f"{index:064x}", CUTOVER_DT, HASH),
        )
        job_id = f"job-{index:032x}"
        self.sql(
            "INSERT INTO xb_member_gateway.jobs(job_id,response_id,operation,payload_hash,canonical_payload,state,state_version,attempt_count,max_attempts) VALUES(%s,%s,'member.create',%s,'{}'::jsonb,%s,3,1,3)",
            (job_id, response_id, HASH, state),
        )
        return job_id

    def versions(self):
        return {row[0] for row in self.sql("SELECT version FROM xb_member_gateway.schema_migrations")}

    def test_guard_refuses_until_cleared_then_applies_once(self):
        seeded = {state: self.seed_job(index, state) for index, state in enumerate(ALLOWED_PRE_STATES, start=1)}
        live = self.seed_job(50, "QUEUED")
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "member_write_v2_guard_nonterminal_jobs_present"):
            self.apply_0006()
        self.assertNotIn("0006_member_write_v2", self.versions())
        self.assertEqual(self.sql("SELECT to_regclass('xb_member_gateway.member_outcomes')"), [(None,)])
        # Clear the live job; leave an uncleared writer hold on a terminal job.
        self.sql("UPDATE xb_member_gateway.jobs SET state='DEAD_LETTER' WHERE job_id=%s", (live,))
        fence = uuid.uuid4()
        holder = seeded["MANUAL_REVIEW"]
        self.sql("INSERT INTO xb_member_gateway.dispatch_fences(fence_id,job_id,operation,member_no,created_at) VALUES(%s,%s,'member.create','91234567',now())", (fence, holder))
        self.sql(
            "INSERT INTO xb_member_gateway.writer_execution_holds(hold_id,job_id,fence_id,attempt_count,execution_id,member_no,lifecycle,state_version,evidence_type,evidence_reference,quarantined_at) VALUES(%s,%s,%s,1,'exec-legacy-1','91234567','QUARANTINED',1,'quarantine','quarantine-ref-1',now())",
            (uuid.uuid4(), holder, fence),
        )
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "member_write_v2_guard_writer_hold_not_cleared"):
            self.apply_0006()
        self.assertNotIn("0006_member_write_v2", self.versions())
        self.sql(
            "UPDATE xb_member_gateway.writer_execution_holds SET lifecycle='CLEARED',state_version=state_version+1,process_pid=4321,"
            "process_start_at='2026-09-01T00:00:00Z',termination_confirmed_at=now(),evidence_type='process_exit',evidence_reference='exit-ref-1',cleared_at=now()"
        )
        before = self.sql("SELECT job_id,state,state_version FROM xb_member_gateway.jobs ORDER BY job_id")
        legacy_tables = ("member_allocations", "write_intents", "dispatch_fences", "writer_execution_holds", "results", "result_events", "reconciliation_cases")
        legacy_counts = {table: self.sql(f"SELECT count(*) FROM xb_member_gateway.{table}") for table in legacy_tables}
        self.apply_0006()
        self.assertIn("0006_member_write_v2", self.versions())
        self.assertEqual(self.sql("SELECT job_id,state,state_version FROM xb_member_gateway.jobs ORDER BY job_id"), before)
        self.assertEqual({table: self.sql(f"SELECT count(*) FROM xb_member_gateway.{table}") for table in legacy_tables}, legacy_counts)
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.jobs WHERE write_attempts=0 AND busy_attempts=0 AND base_member_no IS NULL"), [(len(before),)])
        # Idempotent re-run: the guard is skipped once 0006 is recorded.
        self.sql("UPDATE xb_member_gateway.jobs SET state='LINKED_EXISTING' WHERE job_id=%s", (seeded["MANUAL_REVIEW"],))
        self.sql("UPDATE xb_member_gateway.jobs SET state='RESOLVED' WHERE job_id=%s", (seeded["DEAD_LETTER"],))
        self.apply_0006()
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.schema_migrations WHERE version='0006_member_write_v2'"), [(1,)])
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.sql("UPDATE xb_member_gateway.jobs SET state='NOT_A_STATE' WHERE job_id=%s", (live,))


class RealPostgresV2Tests(RealPostgresTestCase):
    """The production PostgresRepository through psycopg on migrations 0001-0006."""

    def setUp(self):
        super().setUp()
        self.seed_cursor()
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", True)

    def ingest(self, response_id="pg-a", **changes):
        return self.repository.ingest_source_event(source_event(response_id, **changes), policy=policy(), now=NOW, initial_window_max=None).job

    def claim(self, at=NOW, session=SESSION):
        outcome = self.repository.claim_job(session, gate=open_gate(), now=at)
        if not outcome.claimed:
            return outcome
        job, lease = outcome.job, outcome.lease
        return {
            "job_id": job.job_id, "attempt_no": job.attempt, "lease_id": lease.lease_token, "state_version": job.state_version,
            "request": {"base_member_no": job.base_member_no, "name_component": job.name_component},
        }

    def submit(self, claim, outcome, at=NOW, **changes):
        return self.repository.submit_result(claim["job_id"], outcome_body(claim, outcome, **changes), worker_session=SESSION, now=at)

    def test_ingest_stores_identity_and_rejects_at_validated(self):
        job = self.ingest(name="Tan Ah Kow", phone="+65 9123 4567")
        self.assertEqual(self.sql("SELECT state,member_no_rule,base_member_no,name_component,state_version FROM xb_member_gateway.jobs WHERE job_id=%s", (job.job_id,)), [("QUEUED", "XB-MN-1", "6591234567", "TANAHKOW", 2)])
        rejected = self.ingest("pg-pdpa", pdpa_acknowledged=False, phone="81234568")
        self.assertEqual(self.sql("SELECT state,outcome_reason,base_member_no FROM xb_member_gateway.jobs WHERE job_id=%s", (rejected.job_id,)), [("REJECTED_VALIDATION", "pdpa_not_acknowledged", None)])
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "job_member_identity_immutable"):
            self.sql("UPDATE xb_member_gateway.jobs SET base_member_no='123456' WHERE job_id=%s", (job.job_id,))
        replay = self.repository.ingest_source_event(source_event("pg-a", name="Tan Ah Kow", phone="+65 9123 4567"), policy=policy("test"), now=NOW, initial_window_max=None)
        self.assertEqual((replay.replayed, replay.job.base_member_no), (True, "6591234567"))

    def test_claim_result_outcome_outbox_and_replay(self):
        job = self.ingest()
        claim = self.claim()
        self.assertEqual(self.sql("SELECT state,attempt_count,first_claimed_at IS NOT NULL FROM xb_member_gateway.jobs WHERE job_id=%s", (job.job_id,)), [("LEASED", 1, True)])
        self.assertEqual(self.sql("SELECT lease_token,active FROM xb_member_gateway.leases WHERE job_id=%s", (job.job_id,)), [(claim["lease_id"], True)])
        busy = self.repository.claim_job(SESSION_B, gate=open_gate(), now=NOW)
        self.assertEqual(busy.reason, "singleton_busy")
        response, replayed = self.submit(claim, "CREATED_VERIFIED", member_guid=guid(11))
        self.assertEqual((response["state"], replayed), ("CREATED_VERIFIED", False))
        self.assertEqual(self.sql("SELECT outcome,rule,member_no,member_guid::text,attempt_number FROM xb_member_gateway.member_outcomes"), [("CREATED_VERIFIED", "R1", "81234567", guid(11), 1)])
        self.assertEqual(self.sql("SELECT outcome,rule,save_invoked,result_hash IS NOT NULL,outcome_code,lease_token FROM xb_member_gateway.attempts"), [("CREATED_VERIFIED", "R1", True, True, "CREATED_VERIFIED", claim["lease_id"])])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.welcome_email_outbox WHERE job_id=%s", (job.job_id,)), [(1,)])
        self.assertEqual(self.sql("SELECT active FROM xb_member_gateway.leases"), [(False,)])
        again, replayed = self.submit(claim, "CREATED_VERIFIED", member_guid=guid(11))
        self.assertEqual((again, replayed), (dict(response, replayed=True), True))
        with self.assertRaises(ResultConflict):
            self.submit(claim, "CREATED_VERIFIED", member_guid=guid(12))
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.result_conflicts"), [(1,)])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.member_outcomes"), [(1,)])
        audit = json.dumps([list(row) for row in self.sql("SELECT event_type,state,metadata::text FROM xb_member_gateway.audit_events")])
        for private in ("81234567", guid(11), claim["lease_id"]):
            self.assertNotIn(private, audit)

    def test_stale_results_change_nothing_and_kill_switch_never_blocks_results(self):
        job = self.ingest()
        claim = self.claim()
        with self.assertRaises(ResultStale):
            self.submit(claim, "CREATED_VERIFIED", lease_id="lease-" + "f" * 32)
        with self.assertRaises(ResultStale):
            self.repository.submit_result(claim["job_id"], outcome_body(claim, "CREATED_VERIFIED"), worker_session=SESSION_B, now=NOW)
        with self.assertRaises(ResultStale):
            self.submit(claim, "CREATED_VERIFIED", at=later(10))
        self.assertEqual(self.sql("SELECT state,state_version FROM xb_member_gateway.jobs WHERE job_id=%s", (job.job_id,)), [("LEASED", claim["state_version"])])
        self.repository.set_control("kill_switch_enabled", True)
        self.assertEqual(self.repository.claim_job(SESSION_B, gate=open_gate(), now=NOW).reason, "dispatch_disabled")
        response, _ = self.submit(claim, "FAILED_BEFORE_WRITE", at=later(1))
        self.assertEqual((response["state"], response["outcome_reason"]), ("RETRY_WAIT", "probe_unavailable"))
        self.assertEqual(self.sql("SELECT write_attempts,next_attempt_at=%s FROM xb_member_gateway.jobs WHERE job_id=%s", (later(6), job.job_id)), [(1, True)])

    def test_reaper_retry_budget_and_uncertain_exhaustion(self):
        job = self.ingest()
        first = self.claim()
        self.assertEqual(self.repository.claim_job(SESSION, gate=open_gate(), now=later(10)).reason, "no_eligible_job")
        self.assertEqual(self.sql("SELECT state,outcome_reason,write_attempts FROM xb_member_gateway.jobs"), [("RETRY_WAIT", "lease_expired", 1)])
        self.assertEqual(self.sql("SELECT outcome,outcome_code FROM xb_member_gateway.attempts WHERE attempt_number=1"), [("LEASE_EXPIRED", "RETRY_WAIT:lease_expired")])
        second = self.claim(later(15))
        self.assertEqual((second["attempt_no"], second["job_id"]), (2, first["job_id"]))
        self.submit(second, "OUTCOME_UNCERTAIN", at=later(16))
        third = self.claim(later(46))
        response, _ = self.submit(third, "NOT_CREATED", at=later(47))
        self.assertEqual((response["state"], response["outcome_reason"]), ("MANUAL_REVIEW", "uncertain_exhausted"))
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.welcome_email_outbox"), [(0,)])
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.MANUAL_REVIEW)

    def test_guid_index_demotion_and_fresh_create_conflict(self):
        self.ingest("pg-first")
        first = self.claim()
        self.submit(first, "CREATED_VERIFIED", member_guid=guid(21))
        fresh = self.ingest("pg-fresh", email="fresh@example.test")
        claim = self.claim(later(1))
        self.assertEqual(claim["job_id"], fresh.job_id)
        response, _ = self.submit(claim, "CREATED_VERIFIED", at=later(1), member_guid=guid(21))
        self.assertEqual((response["state"], response["outcome_reason"]), ("MANUAL_REVIEW", "guid_conflict_on_fresh_create"))
        demoted = self.ingest("pg-demoted", email="demoted@example.test")
        claim = self.claim(later(2))
        self.submit(claim, "FAILED_BEFORE_WRITE", at=later(2))
        claim = self.claim(later(7))
        self.assertEqual(claim["job_id"], demoted.job_id)
        response, _ = self.submit(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", at=later(7), member_guid=guid(21))
        self.assertEqual((response["state"], response["outcome_reason"]), ("LINKED_EXISTING", "guid_already_verified"))
        self.assertEqual(self.sql("SELECT outcome,count(*) FROM xb_member_gateway.member_outcomes GROUP BY outcome ORDER BY outcome"), [("CREATED_VERIFIED", 1), ("LINKED_EXISTING", 1)])
        self.assertEqual(self.sql("SELECT count(*) FROM xb_member_gateway.welcome_email_outbox"), [(1,)])
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.sql(
                "INSERT INTO xb_member_gateway.member_outcomes(job_id,outcome,rule,member_no,member_guid,attempt_number,result_hash,recorded_at) VALUES(%s,'CREATED_VERIFIED','R1','81234567',%s,1,%s,now())",
                (fresh.job_id, guid(21), HASH),
            )
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "member_outcomes_append_only"):
            self.sql("UPDATE xb_member_gateway.member_outcomes SET member_no='X'")

    def test_resolve_close_requeue_and_append_only_history(self):
        job = self.ingest()
        claim = self.claim()
        self.submit(claim, "MANUAL_REVIEW")
        resolution = self.repository.resolve_job(job.job_id, action="REQUEUE", resolution_code="staff_checked", resolved_by="configured-control", now=later(1))
        self.assertEqual((resolution.resulting_state, resolution.write_budget), ("QUEUED", 6))
        claim = self.claim(later(2))
        self.submit(claim, "MANUAL_REVIEW", at=later(2))
        closed = self.repository.resolve_job(job.job_id, action="CLOSE", resolution_code="linked_by_staff", member_no="LEGACY001", member_guid=guid(31), resolved_by="configured-control", now=later(3))
        self.assertEqual(closed.resulting_state, "RESOLVED")
        self.assertEqual(self.sql("SELECT action,resulting_state,write_budget,member_no,member_guid::text FROM xb_member_gateway.job_resolutions ORDER BY resolved_at"), [("REQUEUE", "QUEUED", 6, None, None), ("CLOSE", "RESOLVED", 6, "LEGACY001", guid(31))])
        with self.assertRaises(ResolutionConflict):
            self.repository.resolve_job(job.job_id, action="CLOSE", resolution_code="again", resolved_by="configured-control", now=later(4))
        with self.assertRaisesRegex(psycopg.errors.RaiseException, "job_resolutions_append_only"):
            self.sql("DELETE FROM xb_member_gateway.job_resolutions")

    def test_dq_flags_are_recorded_once_and_reported_as_codes(self):
        self.ingest()
        claim = self.claim()
        flags = ["email_seen_on_other_member", "post_save_same_person_other_row"]
        self.submit(claim, "CREATED_VERIFIED", member_guid=guid(41), dq_flags=flags)
        self.submit(claim, "CREATED_VERIFIED", member_guid=guid(41), dq_flags=flags)  # identical replay
        self.assertEqual(self.repository.operator_status()["dq_flag_counts"], {"email_seen_on_other_member": 1, "post_save_same_person_other_row": 1})
        self.assertEqual(self.sql("SELECT metadata->>'reason' FROM xb_member_gateway.audit_events WHERE event_type='member_dq_flag' ORDER BY 1"), [("email_seen_on_other_member",), ("post_save_same_person_other_row",)])

    def test_claim_time_gate_skips_a_stored_job_failing_a_source_predicate(self):
        job = self.ingest()
        self.sql("UPDATE xb_member_gateway.jobs SET canonical_payload=jsonb_set(canonical_payload,'{pdpa_acknowledged}','false') WHERE job_id=%s", (job.job_id,))
        self.assertEqual(self.repository.claim_job(SESSION, gate=open_gate(), now=NOW).reason, "no_eligible_job")
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.QUEUED)

    def test_operator_status_v2_and_bootstrap_expect_six_migrations(self):
        self.ingest()
        status = self.repository.operator_status()
        self.assertEqual((status["schema_version"], status["migrations_ready"], status["job_state_counts"]["QUEUED"]), ("xb.member.gateway.operator_status.v2", True, 1))
        self.repository.set_control("kill_switch_enabled", True)
        self.repository.set_control("production_activation_enabled", False)
        config = make_config(production_activation_enabled=False, kill_switch_enabled=True)
        self.repository.verify_bootstrap_readiness(config)
        self.sql("DELETE FROM xb_member_gateway.schema_migrations WHERE version='0006_member_write_v2'")
        with self.assertRaisesRegex(Exception, "required_migrations_missing"):
            self.repository.verify_bootstrap_readiness(config)
        self.assertFalse(self.repository.operator_status()["migrations_ready"])

    def test_reaper_timing_is_five_minutes_after_expiry(self):
        self.ingest()
        self.claim()
        self.repository.claim_job(SESSION, gate=open_gate(), now=later(10) + timedelta(seconds=1))
        self.assertEqual(self.sql("SELECT next_attempt_at=%s FROM xb_member_gateway.jobs", (later(15),)), [(True,)])


if __name__ == "__main__":
    unittest.main()
