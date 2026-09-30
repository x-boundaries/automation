"""U-RS / U-RV / U-GU / U-WE: result v2 CAS (stale, replay, conflict), the
section 4.4 consistency matrix, section 4.1 dispositions, Guid demotion and
welcome-outbox linkage (W-G2-149 sections 3, 4.1, 4.3, 4.4, 4.6)."""

import copy
import unittest

from xb_member_gateway.models import JobRecord, JobState
from xb_member_gateway.repository import ResultConflict, ResultStale
from xb_member_gateway.results import consistency_violations

try:
    from .v2_support import NOW, SESSION, SESSION_B, guid, headers, later, make_app, make_repository, make_service, outcome_body, policy, source_event
except ImportError:  # discovered as a top-level module
    from v2_support import NOW, SESSION, SESSION_B, guid, headers, later, make_app, make_repository, make_service, outcome_body, policy, source_event


class ResultFlow(unittest.TestCase):
    def setUp(self):
        self.repository = make_repository()
        self.service = make_service(self.repository)
        self.app = make_app(self.service)

    def ingest(self, response_id="result-a", **changes):
        return self.repository.ingest_source_event(source_event(response_id, **changes), policy=policy(), now=NOW, initial_window_max=None).job

    def claim(self, at=NOW):
        self.service.clock = at
        claim = self.service.claim(SESSION)
        self.assertTrue(claim["claimed"], claim)
        return claim

    def post(self, claim, body, at=NOW, session=SESSION):
        self.service.clock = at
        return self.app.handle("POST", f"/v2/jobs/{claim['job_id']}/result", headers=headers("worker", session), body=body)

    def second_attempt(self, response_id="result-a", **changes):
        """Attempt 1 fails before write; returns the attempt-2 claim."""

        self.ingest(response_id, **changes)
        first = self.claim()
        self.assertEqual(self.post(first, outcome_body(first, "FAILED_BEFORE_WRITE")).status, 200)
        return self.claim(later(5))


class CompareAndSetTests(ResultFlow):
    """U-RS."""

    def test_accepted_then_identical_replay_returns_the_stored_response(self):
        job = self.ingest()
        claim = self.claim()
        body = outcome_body(claim, "CREATED_VERIFIED")
        first = self.post(claim, body)
        replay = self.post(claim, copy.deepcopy(body), at=later(1))
        self.assertEqual((first.status, replay.status), (200, 200))
        self.assertEqual(first.body, {"job_id": job.job_id, "attempt_no": 1, "state": "CREATED_VERIFIED", "outcome_reason": None, "replayed": False})
        self.assertEqual(replay.body, dict(first.body, replayed=True))
        self.assertEqual(len(self.repository.welcome_outboxes()), 1)

    def test_different_body_for_the_same_lease_is_a_recorded_conflict(self):
        job = self.ingest()
        claim = self.claim()
        self.post(claim, outcome_body(claim, "CREATED_VERIFIED"))
        conflict = self.post(claim, outcome_body(claim, "CREATED_VERIFIED", dq_flags=["email_seen_on_other_member"]))
        self.assertEqual((conflict.status, conflict.body["error_code"]), (409, "result_conflict"))
        self.assertEqual(self.repository.result_conflicts, ({"job_id": job.job_id, "code": "result_payload_conflict"},))
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.CREATED_VERIFIED)

    def test_mismatched_lease_bindings_are_stale_and_change_nothing(self):
        job = self.ingest()
        claim = self.claim()
        before = self.repository.get_job(job.job_id)
        cases = {
            "token": dict(lease_id="lease-" + "f" * 32),
            "attempt": dict(attempt_no=2),
            "state_version": dict(state_version=claim["state_version"] + 1),
        }
        for name, change in cases.items():
            with self.subTest(name):
                response = self.post(claim, outcome_body(claim, "CREATED_VERIFIED", **change))
                self.assertEqual((response.status, response.body["error_code"]), (409, "result_stale"))
        response = self.post(claim, outcome_body(claim, "CREATED_VERIFIED"), session=SESSION_B)
        self.assertEqual((response.status, response.body["error_code"]), (409, "result_stale"))
        after = self.repository.get_job(job.job_id)
        self.assertEqual((after.state, after.state_version, after.write_attempts), (before.state, before.state_version, before.write_attempts))
        self.assertIsNone(self.repository.member_outcome(job.job_id))
        self.assertEqual(self.post(claim, outcome_body(claim, "CREATED_VERIFIED")).status, 200)

    def test_expired_and_replaced_leases_are_stale(self):
        job = self.ingest()
        first = self.claim()
        expired = self.post(first, outcome_body(first, "CREATED_VERIFIED"), at=later(10))
        self.assertEqual((expired.status, expired.body["error_code"]), (409, "result_stale"))
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.LEASED)
        second = self.claim(later(15))
        late = self.post(first, outcome_body(first, "CREATED_VERIFIED"), at=later(16))
        self.assertEqual((late.status, late.body["error_code"]), (409, "result_stale"))
        prior = self.post(second, outcome_body(second, "CREATED_VERIFIED_PRIOR_ATTEMPT"), at=later(16))
        self.assertEqual((prior.status, prior.body["state"]), (200, "CREATED_VERIFIED"))

    def test_invalid_body_and_path_mismatch_change_nothing(self):
        job = self.ingest()
        claim = self.claim()
        for body in ({}, dict(outcome_body(claim, "CREATED_VERIFIED"), extra=1), dict(outcome_body(claim, "CREATED_VERIFIED"), save_invocation_count=2), dict(outcome_body(claim, "CREATED_VERIFIED"), reason_code="result_contract_violation")):
            response = self.post(claim, body)
            self.assertEqual((response.status, response.body["error_code"]), (400, "result_invalid"))
        other = dict(outcome_body(claim, "CREATED_VERIFIED"), job_id="job-" + "0" * 32)
        self.assertEqual(self.post(claim, other).body["error_code"], "result_job_mismatch")
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.LEASED)
        missing = self.app.handle("POST", "/v2/jobs/job-" + "9" * 32 + "/result", headers=headers("worker"), body=dict(outcome_body(claim, "CREATED_VERIFIED"), job_id="job-" + "9" * 32))
        self.assertEqual(missing.status, 404)

    def test_repository_raises_bounded_errors(self):
        self.ingest()
        claim = self.claim()
        with self.assertRaises(ResultStale):
            self.repository.submit_result(claim["job_id"], outcome_body(claim, "CREATED_VERIFIED", lease_id="lease-" + "e" * 32), worker_session=SESSION, now=NOW)
        self.repository.submit_result(claim["job_id"], outcome_body(claim, "CREATED_VERIFIED"), worker_session=SESSION, now=NOW)
        with self.assertRaises(ResultConflict):
            self.repository.submit_result(claim["job_id"], outcome_body(claim, "CREATED_VERIFIED", error_code="x"), worker_session=SESSION, now=NOW)


class DispositionTests(ResultFlow):
    """Section 4.1 outcome -> gateway state for every consistent outcome."""

    EXPECTED = {
        "CREATED_VERIFIED": ("CREATED_VERIFIED", None, "CREATED_VERIFIED", True),
        "CREATED_VERIFIED_NAME": ("CREATED_VERIFIED", None, "CREATED_VERIFIED", True),
        "LINKED_EXISTING": ("LINKED_EXISTING", None, "LINKED_EXISTING", False),
        "MANUAL_REVIEW": ("MANUAL_REVIEW", "format_variant_other_person", None, False),
        "CREATED_READBACK_MISMATCH": ("MANUAL_REVIEW", "created_readback_mismatch", None, False),
        "REJECTED_VALIDATION": ("REJECTED_VALIDATION", "name_exceeds_autocount_limit", None, False),
        "FAILED_BEFORE_WRITE": ("RETRY_WAIT", "probe_unavailable", None, False),
        "NOT_CREATED": ("RETRY_WAIT", None, None, False),
        "NOT_CREATED_CONFLICT": ("RETRY_WAIT", None, None, False),
        "OUTCOME_UNCERTAIN": ("RETRY_WAIT", "child_deadline_exceeded", None, False),
        "MUTEX_BUSY": ("RETRY_WAIT", None, None, False),
    }

    def test_every_outcome_disposition(self):
        for index, (outcome, (state, reason, row, welcome)) in enumerate(self.EXPECTED.items()):
            with self.subTest(outcome):
                self.setUp()
                job = self.ingest(f"disp-{index}", name="Tan Ah Kow")
                claim = self.claim()
                response = self.post(claim, outcome_body(claim, outcome))
                self.assertEqual((response.status, response.body["state"], response.body["outcome_reason"]), (200, state, reason))
                stored = self.repository.member_outcome(job.job_id)
                self.assertEqual(None if stored is None else stored.outcome, row)
                self.assertEqual(self.repository.welcome_outbox_for_job(job.job_id) is not None, welcome)
                attempt = self.repository.attempts(job.job_id)[0]
                self.assertEqual((attempt.outcome, attempt.save_invoked), (outcome_body(claim, outcome)["outcome"], outcome_body(claim, outcome)["save_invoked"]))
                self.assertFalse(self.repository.lease(job.job_id).active)

    def test_prior_attempt_on_attempt_two_is_created_with_one_welcome(self):
        claim = self.second_attempt()
        response = self.post(claim, outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT"), at=later(5))
        self.assertEqual(response.body["state"], "CREATED_VERIFIED")
        outcome = self.repository.member_outcome(claim["job_id"])
        self.assertEqual((outcome.outcome, outcome.rule, outcome.attempt_number), ("CREATED_VERIFIED", "R0", 2))
        self.assertEqual(len(self.repository.welcome_outboxes()), 1)


def job_record(base="91234567", component="TANAHKOW"):
    return JobRecord(
        job_id="job-" + "1" * 32, request_id="r", source_response_ref="hmac-v1:" + "0" * 64, response_id="resp",
        payload_hash="sha256:" + "0" * 64, operation="member.create", member_payload={}, created_at="2026-09-20T00:00:00Z",
        member_no_rule="XB-MN-1", base_member_no=base, name_component=component,
    )


def claim_for(job, attempt_no=1):
    return {
        "job_id": job.job_id, "attempt_no": attempt_no, "lease_id": "lease-" + "a" * 32, "state_version": 3,
        "request": {"base_member_no": job.base_member_no, "name_component": job.name_component},
    }


class ConsistencyMatrixTests(unittest.TestCase):
    """U-RV: every section 4.4 row, positive and negative."""

    def assert_consistent(self, body, job=None):
        self.assertEqual(consistency_violations(job or job_record(), body), ())

    def assert_violates(self, body, job=None):
        self.assertNotEqual(consistency_violations(job or job_record(), body), (), body)

    def test_created_verified_row(self):
        job = job_record()
        claim = claim_for(job)
        self.assert_consistent(outcome_body(claim, "CREATED_VERIFIED"))
        self.assert_consistent(outcome_body(claim, "CREATED_VERIFIED_NAME"))
        base = job.base_member_no
        for change in (
            dict(rule="R4"), dict(branch="NAME_APPENDED"), dict(member_no=base + "X"), dict(member_no=base + "TANAHKOW"),
            dict(rule="R0"), dict(save_invoked=False, save_invocation_count=0), dict(readback=None),
            dict(readback={"found": True, "match": False, "created_by_integration_user": True}),
            dict(readback={"found": True, "match": True, "created_by_integration_user": False}),
            dict(readback={"found": False, "match": True, "created_by_integration_user": True}),
            dict(member_guid=None),
        ):
            with self.subTest(change=change):
                self.assert_violates(outcome_body(claim, "CREATED_VERIFIED", **change))
        self.assert_violates(outcome_body(claim, "CREATED_VERIFIED_NAME", member_no=base))
        empty = job_record(component="")
        self.assert_violates(outcome_body(claim_for(empty), "CREATED_VERIFIED_NAME"), empty)

    def test_prior_attempt_row(self):
        job = job_record()
        claim = claim_for(job, attempt_no=2)
        self.assert_consistent(outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT"))
        self.assert_consistent(outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", member_no=job.base_member_no + job.name_component, branch="NAME_APPENDED"))
        for change in (dict(rule="R1"), dict(attempt_no=1), dict(save_invoked=True, save_invocation_count=1), dict(member_no="00000000"), dict(member_no=None), dict(member_guid=None)):
            with self.subTest(change=change):
                self.assert_violates(outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", **change))

    def test_linked_existing_row(self):
        claim = claim_for(job_record())
        self.assert_consistent(outcome_body(claim, "LINKED_EXISTING"))
        for change in (dict(rule="R2a"), dict(branch="BASE"), dict(save_invoked=True, save_invocation_count=1), dict(member_no=None), dict(member_guid=None)):
            with self.subTest(change=change):
                self.assert_violates(outcome_body(claim, "LINKED_EXISTING", **change))

    def test_pre_write_rows_include_rejected_validation(self):
        claim = claim_for(job_record())
        for outcome in ("FAILED_BEFORE_WRITE", "MUTEX_BUSY", "REJECTED_VALIDATION"):
            with self.subTest(outcome):
                self.assert_consistent(outcome_body(claim, outcome))
                self.assert_violates(outcome_body(claim, outcome, save_invoked=True, save_invocation_count=1))

    def test_not_created_rows(self):
        claim = claim_for(job_record())
        for outcome in ("NOT_CREATED", "NOT_CREATED_CONFLICT"):
            with self.subTest(outcome):
                self.assert_consistent(outcome_body(claim, outcome))
                self.assert_violates(outcome_body(claim, outcome, save_invoked=False, save_invocation_count=0))
                self.assert_violates(outcome_body(claim, outcome, readback=None))
                self.assert_violates(outcome_body(claim, outcome, readback={"found": True, "match": True, "created_by_integration_user": True}))

    def test_uncertain_row_and_worker_synthesised_bound(self):
        claim = claim_for(job_record())
        self.assert_consistent(outcome_body(claim, "OUTCOME_UNCERTAIN"))
        self.assert_violates(outcome_body(claim, "OUTCOME_UNCERTAIN", save_invoked=False, save_invocation_count=0))

    def test_any_outcome_save_flags_must_be_coherent(self):
        claim = claim_for(job_record())
        self.assert_violates(outcome_body(claim, "MANUAL_REVIEW", save_invoked=True, save_invocation_count=0))
        self.assert_violates(outcome_body(claim, "CREATED_READBACK_MISMATCH", save_invoked=False, save_invocation_count=1))

    def test_job_without_identity_is_always_a_violation(self):
        legacy = job_record(base=None, component=None)
        self.assert_violates(outcome_body(claim_for(job_record()), "FAILED_BEFORE_WRITE"), legacy)


class ContractViolationFlowTests(ResultFlow):
    def test_lease_matching_violation_is_manual_review_without_outcome_or_welcome(self):
        job = self.ingest()
        claim = self.claim()
        response = self.post(claim, outcome_body(claim, "CREATED_VERIFIED", member_no="00000000"))
        self.assertEqual((response.status, response.body["state"], response.body["outcome_reason"]), (200, "MANUAL_REVIEW", "result_contract_violation"))
        self.assertIsNone(self.repository.member_outcome(job.job_id))
        self.assertIsNone(self.repository.welcome_outbox_for_job(job.job_id))

    def test_rejected_validation_with_a_save_is_a_violation(self):
        self.ingest()
        claim = self.claim()
        response = self.post(claim, outcome_body(claim, "REJECTED_VALIDATION", save_invoked=True, save_invocation_count=1))
        self.assertEqual((response.body["state"], response.body["outcome_reason"]), ("MANUAL_REVIEW", "result_contract_violation"))


class GuidUniquenessTests(ResultFlow):
    """U-GU: an AutoCount member is credited CREATED_VERIFIED to one job."""

    def create_first(self):
        first = self.ingest("guid-first")
        claim = self.claim()
        self.post(claim, outcome_body(claim, "CREATED_VERIFIED", member_guid=guid(7)))
        return first

    def test_second_prior_attempt_claimant_is_demoted_to_linked_existing(self):
        first = self.create_first()
        claim = self.second_attempt("guid-second", email="other-synthetic@example.test")
        response = self.post(claim, outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", member_guid=guid(7)), at=later(5))
        self.assertEqual((response.body["state"], response.body["outcome_reason"]), ("LINKED_EXISTING", "guid_already_verified"))
        self.assertEqual(self.repository.member_outcome(claim["job_id"]).outcome, "LINKED_EXISTING")
        self.assertIsNone(self.repository.welcome_outbox_for_job(claim["job_id"]))
        self.assertEqual([item.job_id for item in self.repository.welcome_outboxes()], [first.job_id])

    def test_fresh_create_hitting_a_credited_guid_is_manual_review(self):
        self.create_first()
        second = self.ingest("guid-fresh", email="fresh-synthetic@example.test")
        claim = self.claim()
        self.assertEqual(claim["job_id"], second.job_id)
        response = self.post(claim, outcome_body(claim, "CREATED_VERIFIED", member_guid=guid(7)))
        self.assertEqual((response.body["state"], response.body["outcome_reason"]), ("MANUAL_REVIEW", "guid_conflict_on_fresh_create"))
        self.assertIsNone(self.repository.member_outcome(second.job_id))
        self.assertEqual(len(self.repository.welcome_outboxes()), 1)

    def test_many_jobs_may_link_the_same_member(self):
        self.create_first()
        for index in range(2):
            job = self.ingest(f"guid-link-{index}", email=f"link-{index}@example.test")
            claim = self.claim()
            self.assertEqual(self.post(claim, outcome_body(claim, "LINKED_EXISTING", member_guid=guid(7))).body["state"], "LINKED_EXISTING")
            self.assertEqual(self.repository.member_outcome(job.job_id).outcome, "LINKED_EXISTING")
        self.assertEqual(len(self.repository.welcome_outboxes()), 1)


if __name__ == "__main__":
    unittest.main()
