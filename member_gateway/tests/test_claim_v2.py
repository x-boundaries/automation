"""U-CL: claim under the control lock, single active lease, kill switch,
reaper, backoff, write/busy budgets and the claim-time defence-in-depth gates
(W-G2-149 sections 3 and 4.1; relocated eligibility.py predicates)."""

import concurrent.futures
import re
import unittest
from datetime import timedelta

from xb_member_gateway.admission import AdmissionPolicy, ClaimGate
from xb_member_gateway.models import JobState
from xb_member_gateway.repository import RepositoryError, parse_timestamp

try:
    from .v2_support import NOW, SESSION, SESSION_B, later, make_repository, make_service, open_gate, outcome_body, policy, source_event
except ImportError:  # discovered as a top-level module
    from v2_support import NOW, SESSION, SESSION_B, later, make_repository, make_service, open_gate, outcome_body, policy, source_event


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.repository = make_repository()
        self.service = make_service(self.repository)

    def ingest(self, response_id="claim-a", **changes):
        return self.repository.ingest_source_event(source_event(response_id, **changes), policy=policy(), now=NOW, initial_window_max=None).job

    def claim(self, at=NOW, session=SESSION):
        self.service.clock = at
        return self.service.claim(session)

    def post(self, claim, outcome, at=None, **changes):
        self.service.clock = at or NOW
        return self.service.submit_result(claim["job_id"], SESSION, outcome_body(claim, outcome, **changes))

    def test_claim_v2_shape_token_lease_and_first_claimed_at(self):
        job = self.ingest()
        claim = self.claim()
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["schema_version"], "xb.member.gateway.worker_claim.v2")
        self.assertRegex(claim["lease_id"], re.compile(r"^lease-[0-9a-f]{32}$"))
        self.assertEqual((claim["attempt_no"], claim["state_version"]), (1, job.state_version + 1))
        self.assertEqual(parse_timestamp(claim["lease_expires_at"]) - NOW, timedelta(seconds=600))
        self.assertEqual((claim["first_claimed_at"], claim["server_time_utc"]), ("2026-09-20T01:00:00Z", "2026-09-20T01:00:00Z"))
        request = claim["request"]
        self.assertEqual(set(request), {"rule", "base_member_no", "name_component", "phone", "name", "email", "MemberType", "DOB", "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual"})
        self.assertEqual((request["rule"], request["base_member_no"], request["MemberType"], request["OpeningPoints"]), ("XB-MN-1", "81234567", "Default", 0))
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.LEASED)

    def test_no_claim_reasons_are_closed(self):
        self.assertEqual(self.claim()["reason"], "no_eligible_job")
        self.ingest("claim-a")
        self.ingest("claim-b", phone="81234568")
        self.assertTrue(self.claim()["claimed"])
        busy = self.claim(session=SESSION_B)
        self.assertEqual((busy["claimed"], busy["reason"]), (False, "singleton_busy"))
        self.assertEqual(set(busy), {"schema_version", "claimed", "reason", "server_time_utc"})

    def test_kill_switch_and_activation_block_new_claims_only(self):
        self.ingest()
        self.repository.set_control("kill_switch_enabled", True)
        self.assertEqual(self.claim()["reason"], "dispatch_disabled")
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", False)
        self.assertEqual(self.claim()["reason"], "dispatch_disabled")
        self.repository.set_control("production_activation_enabled", True)
        claim = self.claim()
        self.assertTrue(claim["claimed"])
        # A result for a job already in flight is accepted with the kill switch on.
        self.repository.set_control("kill_switch_enabled", True)
        self.assertEqual(self.post(claim, "CREATED_VERIFIED")["state"], "CREATED_VERIFIED")

    def test_readiness_and_environment_gate_the_claim(self):
        self.ingest()
        not_ready = make_service(self.repository, worker_token_sha256=None)
        self.assertEqual(not_ready.claim(SESSION)["reason"], "dispatch_disabled")
        wrong_env = make_service(self.repository, environment="staging")
        self.assertEqual(wrong_env.claim(SESSION)["reason"], "dispatch_disabled")
        adapter_down = make_service(self.repository)
        adapter_down.adapter_ready = False
        self.assertEqual(adapter_down.claim(SESSION)["reason"], "dispatch_disabled")
        closed = ClaimGate(policy(), True, False)
        self.assertEqual(self.repository.claim_job(SESSION, gate=closed, now=NOW).reason, "dispatch_disabled")
        self.assertEqual(self.repository.claim_job(SESSION, gate=ClaimGate(policy(), False, True), now=NOW).reason, "dispatch_disabled")
        self.assertEqual(self.repository.get_job(self.repository.all_jobs()[0].job_id).state, JobState.QUEUED)

    def test_oldest_eligible_job_is_claimed_first(self):
        self.service.clock = NOW
        second = self.repository.ingest_source_event(source_event("claim-z", phone="81234568"), policy=policy(), now=NOW + timedelta(seconds=5), initial_window_max=None).job
        first = self.repository.ingest_source_event(source_event("claim-y"), policy=policy(), now=NOW, initial_window_max=None).job
        self.assertEqual(self.claim()["job_id"], first.job_id)
        self.assertNotEqual(first.job_id, second.job_id)

    def test_concurrent_claimers_get_one_job(self):
        self.ingest("claim-a")
        self.ingest("claim-b", phone="81234568")
        sessions = ["ws-" + f"{index:032x}" for index in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda session: self.repository.claim_job(session, gate=open_gate(), now=NOW), sessions))
        self.assertEqual(sum(outcome.claimed for outcome in outcomes), 1)
        self.assertEqual({outcome.reason for outcome in outcomes if not outcome.claimed}, {"singleton_busy"})

    def test_reaper_expired_lease_is_uncertain_and_waits_five_minutes(self):
        job = self.ingest()
        first = self.claim()
        self.assertEqual(self.claim(at=later(10), session=SESSION_B)["reason"], "no_eligible_job")
        reaped = self.repository.get_job(job.job_id)
        self.assertEqual((reaped.state, reaped.outcome_reason, reaped.write_attempts), (JobState.RETRY_WAIT, "lease_expired", 1))
        self.assertEqual(reaped.next_attempt_at, "2026-09-20T01:15:00Z")
        self.assertEqual(self.repository.attempts(job.job_id)[0].outcome, "LEASE_EXPIRED")
        self.assertEqual(self.claim(at=later(14.9))["reason"], "no_eligible_job")
        second = self.claim(at=later(15))
        self.assertEqual((second["attempt_no"], second["first_claimed_at"]), (2, first["first_claimed_at"]))
        self.assertNotEqual(second["lease_id"], first["lease_id"])

    def test_third_uncertain_attempt_is_uncertain_exhausted(self):
        job = self.ingest()
        at = 0
        for _ in range(3):
            self.assertTrue(self.claim(at=later(at))["claimed"])
            at += 16
        self.claim(at=later(at))
        final = self.repository.get_job(job.job_id)
        self.assertEqual((final.state, final.outcome_reason, final.write_attempts), (JobState.MANUAL_REVIEW, "uncertain_exhausted", 3))

    def test_write_class_backoff_is_five_then_thirty_then_review(self):
        job = self.ingest()
        claim = self.claim()
        self.assertEqual(self.post(claim, "FAILED_BEFORE_WRITE")["state"], "RETRY_WAIT")
        self.assertEqual(self.repository.get_job(job.job_id).next_attempt_at, "2026-09-20T01:05:00Z")
        claim = self.claim(at=later(5))
        self.post(claim, "NOT_CREATED", at=later(5))
        self.assertEqual(self.repository.get_job(job.job_id).next_attempt_at, "2026-09-20T01:35:00Z")
        self.assertEqual(self.claim(at=later(34))["reason"], "no_eligible_job")
        claim = self.claim(at=later(35))
        response = self.post(claim, "NOT_CREATED_CONFLICT", at=later(35))
        self.assertEqual((response["state"], response["outcome_reason"]), ("MANUAL_REVIEW", "attempts_exhausted"))

    def test_uncertain_history_marks_exhaustion_as_uncertain(self):
        job = self.ingest()
        self.post(self.claim(), "OUTCOME_UNCERTAIN")
        self.post(self.claim(at=later(5)), "FAILED_BEFORE_WRITE", at=later(5))
        response = self.post(self.claim(at=later(35)), "FAILED_BEFORE_WRITE", at=later(35))
        self.assertEqual(response["outcome_reason"], "uncertain_exhausted")
        self.assertEqual(self.repository.get_job(job.job_id).write_attempts, 3)

    def test_mutex_busy_has_its_own_twelve_attempt_budget(self):
        job = self.ingest()
        at = 0
        for index in range(11):
            response = self.post(self.claim(at=later(at)), "MUTEX_BUSY", at=later(at))
            self.assertEqual(response["state"], "RETRY_WAIT", index)
            at += 5
        response = self.post(self.claim(at=later(at)), "MUTEX_BUSY", at=later(at))
        self.assertEqual((response["state"], response["outcome_reason"]), ("MANUAL_REVIEW", "mutex_busy_exhausted"))
        final = self.repository.get_job(job.job_id)
        self.assertEqual((final.busy_attempts, final.write_attempts), (12, 0))

    def test_lease_seconds_is_fixed_and_session_is_validated(self):
        with self.assertRaisesRegex(RepositoryError, "lease_seconds_must_be_600"):
            self.repository.claim_job(SESSION, gate=open_gate(), lease_seconds=120, now=NOW)
        with self.assertRaisesRegex(RepositoryError, "worker_id_invalid"):
            self.repository.claim_job("bad session", gate=open_gate(), now=NOW)
        with self.assertRaisesRegex(RepositoryError, "claim_gate_required"):
            self.repository.claim_job(SESSION, gate=None, now=NOW)


class ClaimTimeGateTests(unittest.TestCase):
    """eligibility.py predicates relocated: a stored job that fails any
    source predicate is never returned by claim (defence in depth)."""

    def setUp(self):
        self.repository = make_repository()

    def stored(self, rid="gate-job", **mutations):
        job = self.repository.ingest_source_event(source_event(rid), policy=policy(), now=NOW, initial_window_max=None).job
        record = self.repository._jobs[job.job_id]  # simulate a legacy or tampered row
        for key, value in mutations.items():
            if key.startswith("payload_"):
                record.member_payload[key[len("payload_"):]] = value
            else:
                setattr(record, key, value)
        return job

    def assert_not_claimed(self, gate=None):
        outcome = self.repository.claim_job(SESSION, gate=gate or open_gate(), now=NOW)
        self.assertEqual((outcome.claimed, outcome.reason), (False, "no_eligible_job"))

    def test_each_stored_predicate_failure_blocks_the_claim(self):
        cases = {
            "pdpa": {"payload_pdpa_acknowledged": False},
            "consent": {"payload_marketing_consent": "Maybe"},
            "operation": {"operation": "member.update"},
            "source": {"source_system": "other_forms"},
            "form": {"form_alias": "other_form"},
            "mapping": {"mapping_version": "member-intake.v0"},
            "response": {"response_id": "bad id"},
            "hash": {"payload_hash": "sha256:x"},
            "fields": {"payload_udf": "x"},
            "identity": {"base_member_no": "99999999"},
            "missing-identity": {"member_no_rule": None},
        }
        for name, mutations in cases.items():
            with self.subTest(name):
                self.setUp()
                self.stored(f"gate-{name}", **mutations)
                self.assert_not_claimed()

    def test_config_allowlist_or_book_mode_change_blocks_stored_jobs(self):
        self.stored()
        self.assert_not_claimed(ClaimGate(AdmissionPolicy("production", ("other_form",), ("member-intake.v1",)), True, True))
        self.assert_not_claimed(ClaimGate(AdmissionPolicy("production", ("member_registration",), ("member-intake.v9",)), True, True))
        self.assert_not_claimed(ClaimGate(policy("test"), True, True))
        self.assertTrue(self.repository.claim_job(SESSION, gate=open_gate(), now=NOW).claimed)

    def test_failing_job_is_skipped_and_the_next_eligible_one_is_claimed(self):
        bad = self.stored("gate-bad", payload_pdpa_acknowledged=False)
        good = self.repository.ingest_source_event(source_event("gate-good", phone="81234568"), policy=policy(), now=NOW + timedelta(seconds=1), initial_window_max=None).job
        outcome = self.repository.claim_job(SESSION, gate=open_gate(), now=NOW + timedelta(seconds=2))
        self.assertEqual(outcome.job.job_id, good.job_id)
        self.assertEqual(self.repository.get_job(bad.job_id).state, JobState.QUEUED)


if __name__ == "__main__":
    unittest.main()
