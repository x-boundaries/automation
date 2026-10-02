"""Gateway ingest idempotency and the claim request derivation (v2).

The v1 allocation, fence, eligibility and reconciliation tests that lived here
were deleted with that code (W-G2-149 section 11); their v2 replacements are
test_claim_v2, test_result_v2 and test_api."""

import concurrent.futures
import unittest

from xb_member_gateway.canonical import canonicalize_source_event
from xb_member_gateway.models import JobState
from xb_member_gateway.repository import SourceConflict
from xb_member_gateway.results import build_claim_request

try:
    from .v2_support import NOW, SESSION, make_event, make_repository, make_service, policy
except ImportError:  # discovered as a top-level module
    from v2_support import NOW, SESSION, make_event, make_repository, make_service, policy


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.repository = make_repository()
        self.service = make_service(self.repository)

    def test_same_response_same_hash_is_idempotent(self):
        first = self.service.ingest(make_event())
        second = self.service.ingest(make_event())
        self.assertEqual((first["replayed"], second["replayed"]), (False, True))
        self.assertEqual(first["job_id"], second["job_id"])

    def test_same_response_changed_hash_is_conflict(self):
        self.service.ingest(make_event())
        with self.assertRaises(SourceConflict):
            self.service.ingest(make_event(name="Changed"))

    def test_different_response_identical_customer_data_is_distinct(self):
        first = self.service.ingest(make_event("resp-001"))
        second = self.service.ingest(make_event("resp-002"))
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_marketing_no_is_queued_but_false_pdpa_is_rejected_at_validated(self):
        queued = self.service.ingest(make_event("resp-no", marketing_consent="No"))
        rejected = self.service.ingest(make_event("resp-pdpa", phone="89876543", pdpa_acknowledged=False))
        self.assertEqual((queued["state"], rejected["state"]), ("QUEUED", "REJECTED_VALIDATION"))
        self.assertEqual(self.repository.get_job(rejected["job_id"]).outcome_reason, "pdpa_not_acknowledged")

    def test_missing_marketing_consent_is_rejected(self):
        with self.assertRaises(ValueError):
            make_event(marketing_consent="")

    def test_concurrent_ingest_has_one_job(self):
        source = canonicalize_source_event(make_event())
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda event: self.repository.ingest_source_event(event, policy=policy(), now=NOW, initial_window_max=None), [source] * 8))
        self.assertEqual(len({outcome.job.job_id for outcome in outcomes}), 1)
        self.assertEqual(sum(not outcome.replayed for outcome in outcomes), 1)

    def test_claim_request_is_the_member_record_from_the_stored_identity(self):
        job_id = self.service.ingest(make_event(name="Tan Ah Kow", phone="+65 9123 4567", birthday_month="December"))["job_id"]
        claim = self.service.claim(SESSION)
        self.assertEqual(claim["job_id"], job_id)
        job = self.repository.get_job(job_id)
        self.assertEqual(job.state, JobState.LEASED)
        request = build_claim_request(job)
        self.assertEqual(claim["request"], request)
        self.assertEqual(request["phone"], request["base_member_no"])
        self.assertEqual((request["DOB"], request["RegisterDate"], request["ExpiryDate"]), ("2000-12-01", "2026-09-20", "2028-09-19"))
        self.assertEqual((request["IsActive"], request["Individual"]), (True, True))


if __name__ == "__main__":
    unittest.main()
