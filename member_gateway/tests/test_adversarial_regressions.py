"""Regressions for defects found by the W-G3-152 adversarial challenge.

- review / rejection results must carry a reason from their own closed set,
  and a prior-attempt branch must agree with the MemberNo (section 4.4);
- a legacy (pre-v2) MANUAL_REVIEW job cannot be requeued into a state no
  claim can ever pick up (section 3);
- the primitive's data-quality flags are reported, as codes only
  (sections 2.6 and 2.7 step 8).
"""

import unittest

from xb_member_gateway.models import JobState
from xb_member_gateway.repository import ResolutionConflict
from xb_member_gateway.results import consistency_violations

try:
    from .v2_support import NOW, SESSION, headers, later, make_app, make_repository, make_service, outcome_body, policy, source_event
except ImportError:  # discovered as a top-level module
    from v2_support import NOW, SESSION, headers, later, make_app, make_repository, make_service, outcome_body, policy, source_event


class Flow(unittest.TestCase):
    def setUp(self):
        self.repository = make_repository()
        self.service = make_service(self.repository)
        self.app = make_app(self.service)

    def ingest(self, response_id="adv-a", **changes):
        return self.repository.ingest_source_event(source_event(response_id, **changes), policy=policy(), now=NOW, initial_window_max=None).job

    def claim(self, at=NOW):
        self.service.clock = at
        claim = self.service.claim(SESSION)
        self.assertTrue(claim["claimed"], claim)
        return claim

    def post(self, claim, body, at=NOW):
        self.service.clock = at
        return self.app.handle("POST", f"/v2/jobs/{claim['job_id']}/result", headers=headers("worker"), body=body)


class OutcomeReasonTests(Flow):
    def test_rejection_with_a_review_reason_goes_to_review_not_terminal_rejection(self):
        job = self.ingest()
        claim = self.claim()
        response = self.post(claim, outcome_body(claim, "REJECTED_VALIDATION", reason_code="inactive_match"))
        self.assertEqual(response.status, 200, response.body)
        stored = self.repository.get_job(job.job_id)
        self.assertEqual((stored.state, stored.outcome_reason), (JobState.MANUAL_REVIEW, "result_contract_violation"))

    def test_review_with_a_pre_write_reason_is_a_contract_violation(self):
        job = self.ingest()
        claim = self.claim()
        self.post(claim, outcome_body(claim, "MANUAL_REVIEW", reason_code="clock_skew"))
        self.assertEqual(self.repository.get_job(job.job_id).outcome_reason, "result_contract_violation")

    def test_review_without_a_reason_is_a_contract_violation(self):
        job = self.ingest()
        claim = self.claim()
        self.post(claim, outcome_body(claim, "MANUAL_REVIEW", reason_code=None))
        self.assertEqual(self.repository.get_job(job.job_id).outcome_reason, "result_contract_violation")

    def test_every_review_and_rejection_reason_the_primitive_emits_is_accepted(self):
        job = self.ingest()
        claim = self.claim()
        record = self.repository.get_job(job.job_id)
        for reason in ("prior_attempt_ambiguous", "multiple_same_person", "inactive_match", "format_variant_other_person",
                       "holder_identity_unknown", "name_component_empty", "name_candidate_collision", "request_contract_violation"):
            self.assertEqual(consistency_violations(record, outcome_body(claim, "MANUAL_REVIEW", reason_code=reason)), (), reason)
        foreign = outcome_body(
            claim, "MANUAL_REVIEW", reason_code="readback_foreign_row", save_invoked=True, save_invocation_count=1,
            readback={"found": True, "match": False, "created_by_integration_user": False},
        )
        self.assertEqual(consistency_violations(record, foreign), ())
        for reason in ("name_exceeds_autocount_limit", "email_exceeds_autocount_limit", "synthetic_in_production"):
            self.assertEqual(consistency_violations(record, outcome_body(claim, "REJECTED_VALIDATION", reason_code=reason)), (), reason)

    def test_prior_attempt_branch_must_agree_with_member_no(self):
        job = self.ingest()
        claim = dict(self.claim(), attempt_no=2)
        record = self.repository.get_job(job.job_id)
        base = claim["request"]["base_member_no"]
        appended = base + claim["request"]["name_component"]
        good_base = outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", member_no=base, branch="BASE")
        good_appended = outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", member_no=appended, branch="NAME_APPENDED")
        self.assertEqual(consistency_violations(record, good_base), ())
        self.assertEqual(consistency_violations(record, good_appended), ())
        for body in (
            outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", member_no=base, branch="NAME_APPENDED"),
            outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", member_no=appended, branch="BASE"),
            outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", member_no=base, branch="EXISTING"),
        ):
            self.assertIn("prior_attempt_branch_mismatch", consistency_violations(record, body))


class RemainingOutcomeReasonTests(Flow):
    def setUp(self):
        super().setUp()
        job = self.ingest()
        self.claim_body = self.claim()
        self.record = self.repository.get_job(job.job_id)

    def violations(self, outcome, **changes):
        return consistency_violations(self.record, outcome_body(self.claim_body, outcome, **changes))

    def test_emitted_reasons_are_accepted(self):
        for reason in ("clock_skew", "mutex_unavailable", "session_unavailable", "book_binding_mismatch", "integration_user_mismatch",
                       "probe_unavailable", "fault_injection_refused", "test_book_requires_synthetic", "primitive_config_invalid",
                       "primitive_launch_failed", "unexpected_error"):
            self.assertEqual(self.violations("FAILED_BEFORE_WRITE", reason_code=reason), (), reason)
        for reason in ("readback_absent_after_save", "readback_unavailable", "unexpected_error", "child_deadline_exceeded",
                       "child_termination_unconfirmed", "primitive_output_invalid"):
            self.assertEqual(self.violations("OUTCOME_UNCERTAIN", reason_code=reason), (), reason)
        self.assertEqual(self.violations("MUTEX_BUSY"), ())
        self.assertEqual(self.violations("NOT_CREATED"), ())
        self.assertEqual(self.violations("NOT_CREATED_CONFLICT"), ())
        self.assertEqual(self.violations("CREATED_READBACK_MISMATCH"), ())
        # Fields match but the Guid is missing: still a readback mismatch.
        self.assertEqual(self.violations("CREATED_READBACK_MISMATCH", member_guid=None, readback={"found": True, "match": True, "created_by_integration_user": True}), ())

    def test_misattributed_reasons_are_contract_violations(self):
        self.assertIn("pre_write_reason_invalid", self.violations("FAILED_BEFORE_WRITE", reason_code="readback_foreign_row"))
        self.assertIn("pre_write_reason_invalid", self.violations("FAILED_BEFORE_WRITE", reason_code=None))
        self.assertIn("uncertain_reason_invalid", self.violations("OUTCOME_UNCERTAIN", reason_code="clock_skew"))
        self.assertIn("mutex_busy_reason_invalid", self.violations("MUTEX_BUSY", reason_code="clock_skew"))
        self.assertIn("not_created_reason_invalid", self.violations("NOT_CREATED", reason_code="inactive_match"))
        self.assertIn("mismatch_reason_invalid", self.violations("CREATED_READBACK_MISMATCH", reason_code="synthetic_in_production"))

    def test_readback_mismatch_requires_our_saved_row(self):
        self.assertIn("mismatch_without_save", self.violations("CREATED_READBACK_MISMATCH", save_invoked=False, save_invocation_count=0))
        self.assertIn("mismatch_readback_inconsistent", self.violations("CREATED_READBACK_MISMATCH", readback={"found": True, "match": False, "created_by_integration_user": False}))
        self.assertIn("mismatch_readback_inconsistent", self.violations("CREATED_READBACK_MISMATCH", readback=None))
        self.assertIn("mismatch_readback_inconsistent", self.violations("CREATED_READBACK_MISMATCH", readback={"found": True, "match": True, "created_by_integration_user": True}))

    def test_misattributed_result_goes_to_review_with_contract_violation(self):
        self.post(self.claim_body, outcome_body(self.claim_body, "CREATED_READBACK_MISMATCH", save_invoked=False, save_invocation_count=0, reason_code="synthetic_in_production"))
        stored = self.repository.get_job(self.record.job_id)
        self.assertEqual((stored.state, stored.outcome_reason), (JobState.MANUAL_REVIEW, "result_contract_violation"))

class SuccessOutcomeShapeTests(Flow):
    def test_success_outcomes_carry_no_reason_and_no_save_outcomes_no_readback(self):
        job = self.ingest()
        claim = dict(self.claim(), attempt_no=2)
        record = self.repository.get_job(job.job_id)
        self.assertIn("success_reason_present", consistency_violations(record, outcome_body(claim, "CREATED_VERIFIED", reason_code="clock_skew")))
        self.assertIn("success_reason_present", consistency_violations(record, outcome_body(claim, "LINKED_EXISTING", reason_code="inactive_match")))
        self.assertIn("no_save_outcome_has_readback", consistency_violations(record, outcome_body(claim, "LINKED_EXISTING", readback={"found": True, "match": True, "created_by_integration_user": False})))
        self.assertIn("no_save_outcome_has_readback", consistency_violations(record, outcome_body(claim, "CREATED_VERIFIED_PRIOR_ATTEMPT", readback={"found": True, "match": True, "created_by_integration_user": True})))

    def test_contract_violating_result_reports_no_dq_flags(self):
        self.ingest()
        claim = self.claim()
        self.post(claim, outcome_body(claim, "CREATED_VERIFIED", reason_code="clock_skew", dq_flags=["email_seen_on_other_member"]))
        self.assertEqual(self.repository.operator_status()["dq_flag_counts"], {})

class LegacyRequeueTests(Flow):
    def test_legacy_review_job_can_close_but_not_requeue(self):
        job = self.ingest()
        record = self.repository._jobs[job.job_id]
        # A pre-0006 MANUAL_REVIEW row: no XB-MN-1 identity was ever computed.
        record.state = JobState.MANUAL_REVIEW
        record.member_no_rule = None
        record.base_member_no = None
        record.name_component = None
        with self.assertRaises(ResolutionConflict) as raised:
            self.repository.resolve_job(job.job_id, action="REQUEUE", resolution_code="operator_retry", resolved_by="configured-control", now=NOW)
        self.assertEqual(str(raised.exception), "resolution_legacy_job_not_requeueable")
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.MANUAL_REVIEW)
        closed = self.repository.resolve_job(job.job_id, action="CLOSE", resolution_code="operator_closed", resolved_by="configured-control", now=NOW)
        self.assertEqual(closed.resulting_state, "RESOLVED")

    def test_v2_review_job_still_requeues(self):
        job = self.ingest()
        claim = self.claim()
        self.post(claim, outcome_body(claim, "MANUAL_REVIEW"))
        resolution = self.repository.resolve_job(job.job_id, action="REQUEUE", resolution_code="operator_retry", resolved_by="configured-control", now=later(1))
        self.assertEqual(resolution.resulting_state, "QUEUED")


class DataQualityFlagTests(Flow):
    def test_flags_are_counted_in_operator_status_as_codes_only(self):
        self.ingest("adv-a", phone="81230001")
        claim = self.claim()
        self.post(claim, outcome_body(claim, "CREATED_VERIFIED", dq_flags=["email_seen_on_other_member", "post_save_same_person_other_row"]))
        status = self.repository.operator_status()
        self.assertEqual(status["dq_flag_counts"], {"email_seen_on_other_member": 1, "post_save_same_person_other_row": 1})
        flagged = [entry for entry in self.repository.audit_events if entry["event_type"] == "member_dq_flag"]
        self.assertEqual(len(flagged), 2)
        for entry in flagged:
            self.assertTrue(set(entry).issubset({"event_type", "recorded_at", "job_id", "state", "operation", "error_code", "count"}))

    def test_replayed_result_does_not_double_count_flags(self):
        self.ingest()
        claim = self.claim()
        body = outcome_body(claim, "CREATED_VERIFIED", dq_flags=["email_seen_on_other_member"])
        self.post(claim, body)
        self.post(claim, dict(body), at=later(1))
        self.assertEqual(self.repository.operator_status()["dq_flag_counts"], {"email_seen_on_other_member": 1})


if __name__ == "__main__":
    unittest.main()
