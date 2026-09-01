import concurrent.futures
import unittest
from datetime import datetime, timedelta, timezone

from xb_member_gateway.api import ApiError, GatewayService
from xb_member_gateway.canonical import build_source_event, canonicalize_source_event
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.eligibility import EligibilityContext, evaluate_eligibility
from xb_member_gateway.models import JobRecord, JobState, ProbeStatus, ResultStatus
from xb_member_gateway.repository import InMemoryRepository, ResultConflict, SourceConflict
from xb_member_gateway.results import build_member_record, compare_readback


NOW = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)


def config(**changes):
    value = {
        "member_no_max_length": 20,
        "worker_token_sha256": "0" * 64,
        "production_activation_enabled": True,
        "kill_switch_enabled": False,
    }
    value.update(changes)
    return GatewayConfig.from_mapping(value)


def event(response_id="resp-001", request_id=None, **payload_changes):
    payload = {
        "name": "Synthetic Member",
        "phone": "81234567",
        "email": "synthetic@example.test",
        "birthday_month": "January",
        "marketing_consent": "Yes",
        "pdpa_acknowledged": True,
    }
    payload.update(payload_changes)
    return build_source_event(
        response_id=response_id,
        request_id=request_id or f"request-{response_id}",
        create_time="2026-08-30T01:00:00Z",
        form_alias="member_registration",
        mapping_version="member-intake.v1",
        payload=payload,
    )


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository()
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", True)
        self.service = GatewayService(config(), self.repository, adapter_ready=True, clock=NOW)

    def ingest_claim(self, current_event=None, worker="worker-1"):
        result = self.service.ingest(current_event or event())
        claim = self.service.claim(worker)
        self.assertTrue(claim["claimed"])
        job = claim["job"]
        self.service.precheck(job["job_id"], worker)
        return result, job

    def bind_base(self, job, worker="worker-1", status=ProbeStatus.FREE, service=None):
        service = service or self.service
        candidate = service.allocation_candidate(job["job_id"])["candidate"]
        return service.allocation_probe(job["job_id"], worker, {"candidate": candidate, "status": status.value, "probe_reference": f"synthetic-{candidate}"})

    def fence(self, job, worker="worker-1"):
        self.bind_base(job, worker)
        self.service.allocation_recheck(job["job_id"], worker, {"status": "FREE", "probe_reference": f"synthetic-recheck-{job['job_id']}"})
        self.service.write_intent(job["job_id"], worker, {"operation": "member.create", "member_no": job["member_payload"]["phone"], "payload_hash": job["payload_hash"]}, principal_valid=True)
        return self.service.dispatch_fence(job["job_id"], worker, {"operation": "member.create", "member_no": job["member_payload"]["phone"]}, principal_valid=True)

    def test_same_response_same_hash_is_idempotent(self):
        first = self.service.ingest(event())
        second = self.service.ingest(event())
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["job_id"], second["job_id"])

    def test_same_response_changed_hash_is_conflict(self):
        self.service.ingest(event())
        with self.assertRaises(SourceConflict):
            self.service.ingest(event(name="Changed"))

    def test_different_response_identical_customer_data_is_distinct(self):
        first = self.service.ingest(event("resp-001"))
        second = self.service.ingest(event("resp-002"))
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_marketing_no_is_eligible_but_false_pdpa_blocks(self):
        _, job = self.ingest_claim(event("resp-no", marketing_consent="No"))
        self.bind_base(job)
        self.service.allocation_recheck(job["job_id"], "worker-1", {"status": "FREE", "probe_reference": "marketing-recheck"})
        self.assertTrue(self.service._eligibility(job["job_id"], "worker-1", principal_valid=True).eligible)
        self.repository.reclaim_expired(now=NOW + timedelta(seconds=601))
        _, blocked = self.ingest_claim(event("resp-pdpa", phone="89876543", pdpa_acknowledged=False), worker="worker-2")
        self.bind_base(blocked, worker="worker-2")
        decision = self.service._eligibility(blocked["job_id"], "worker-2", principal_valid=True)
        self.assertFalse(decision.eligible)
        self.assertIn("pdpa_acknowledged", decision.reasons)

    def test_missing_marketing_consent_is_rejected(self):
        with self.assertRaises(ValueError):
            event(marketing_consent="")

    def test_concurrent_ingest_has_one_job(self):
        source = canonicalize_source_event(event())
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(self.repository.ingest_source_event, [source] * 8))
        self.assertEqual(len({outcome.job.job_id for outcome in outcomes}), 1)
        self.assertEqual(sum(not outcome.replayed for outcome in outcomes), 1)

    def test_collision_order_is_base_x1_x2_and_race_does_not_reallocate_silently(self):
        _, first = self.ingest_claim(event("resp-a"), "worker-a")
        base = self.service.allocation_candidate(first["job_id"])["candidate"]
        response = self.service.allocation_probe(first["job_id"], "worker-a", {"candidate": base, "status": "OCCUPIED", "probe_reference": "read-a"})
        self.assertEqual(response["candidate"], base + "X1")
        bound = self.service.allocation_probe(first["job_id"], "worker-a", {"candidate": base + "X1", "status": "FREE", "probe_reference": "read-x1"})
        self.assertEqual(bound["member_no"], base + "X1")
        self.assertEqual(self.repository.reclaim_expired(now=NOW + timedelta(seconds=601)), 1)
        _, second = self.ingest_claim(event("resp-b"), "worker-b")
        base2 = self.service.allocation_candidate(second["job_id"])["candidate"]
        self.service.allocation_probe(second["job_id"], "worker-b", {"candidate": base2, "status": "OCCUPIED", "probe_reference": "read-b"})
        x1 = self.service.allocation_candidate(second["job_id"])["candidate"]
        self.assertEqual(x1, base + "X1")
        response = self.service.allocation_probe(second["job_id"], "worker-b", {"candidate": x1, "status": "FREE", "probe_reference": "read-x1"})
        self.assertEqual(response["candidate"], base + "X2")
        self.assertEqual(self.service.allocation_candidate(second["job_id"])["candidate"], base + "X2")

    def test_ambiguous_lookup_does_not_advance_suffix(self):
        _, job = self.ingest_claim()
        base = self.service.allocation_candidate(job["job_id"])["candidate"]
        with self.assertRaises(ApiError):
            self.service.allocation_probe(job["job_id"], "worker-1", {"candidate": base, "status": "AMBIGUOUS", "probe_reference": "unknown"})
        self.assertEqual(self.repository.get_job(job["job_id"]).state, JobState.AMBIGUOUS_LOOKUP)
        self.assertEqual(self.repository.get_probes(job["job_id"])[0].candidate, base)

    def test_constraint_exhaustion_does_not_truncate(self):
        service = GatewayService(config(member_no_max_length=10), self.repository, adapter_ready=True, clock=NOW)
        service.ingest(event())
        job = service.claim("worker-1")["job"]
        service.precheck(job["job_id"], "worker-1")
        base = service.allocation_candidate(job["job_id"])["candidate"]
        with self.assertRaises(ApiError):
            service.allocation_probe(job["job_id"], "worker-1", {"candidate": base, "status": "OCCUPIED", "probe_reference": "occupied"})

    def test_lease_expiry_reclaims_before_fence(self):
        repo = InMemoryRepository()
        repo.set_control("kill_switch_enabled", False)
        repo.set_control("production_activation_enabled", True)
        service = GatewayService(config(), repo, adapter_ready=True, clock=NOW)
        service.ingest(event())
        job = service.claim("worker-1")["job"]
        self.assertEqual(repo.reclaim_expired(now=NOW + timedelta(seconds=601)), 1)
        self.assertEqual(repo.get_job(job["job_id"]).state, JobState.RETRY_WAIT)
        self.assertIsNotNone(repo.claim_job("worker-2", now=NOW + timedelta(seconds=602)))

    def test_crash_after_intent_reuses_allocation_and_intent(self):
        _, job = self.ingest_claim()
        self.bind_base(job)
        job_id = job["job_id"]
        self.service.allocation_recheck(job_id, "worker-1", {"status": "FREE", "probe_reference": "crash-recheck-1"})
        self.service.write_intent(job_id, "worker-1", {"operation": "member.create", "member_no": job["member_payload"]["phone"], "payload_hash": job["payload_hash"]}, principal_valid=True)
        self.assertEqual(self.repository.reclaim_expired(now=NOW + timedelta(seconds=601)), 1)
        self.repository.claim_job("worker-2", now=NOW + timedelta(seconds=602))
        self.repository.begin_prechecking(job_id, "worker-2", now=NOW + timedelta(seconds=602))
        self.service.clock = NOW + timedelta(seconds=602)
        self.service.allocation_recheck(job_id, "worker-2", {"status": "FREE", "probe_reference": "crash-recheck-2"})
        self.service.write_intent(job_id, "worker-2", {"operation": "member.create", "member_no": job["member_payload"]["phone"], "payload_hash": job["payload_hash"]}, principal_valid=True)
        self.assertEqual(self.repository.get_write_intent(job_id).member_no, job["member_payload"]["phone"])

    def test_dispatch_fence_is_idempotent_and_post_fence_not_requeued(self):
        _, job = self.ingest_claim()
        fence = self.fence(job)
        same = self.repository.record_dispatch_fence(job["job_id"], job["member_payload"]["phone"], "member.create", "worker-1", now=NOW)
        self.assertEqual(fence["dispatch_fence_id"], same.fence_id)
        self.assertEqual(self.repository.reclaim_expired(now=NOW + timedelta(seconds=601)), 1)
        self.assertEqual(self.repository.get_job(job["job_id"]).state, JobState.WRITE_OUTCOME_UNCERTAIN)
        self.assertIsNone(self.repository.claim_job("worker-2", now=NOW + timedelta(seconds=602)))

    def test_save_exception_is_uncertain_and_reconcile_absence_never_retries(self):
        _, job = self.ingest_claim()
        fence = self.fence(job)
        uncertain = {"schema_version": "xb.member.gateway.result.v1", "job_id": job["job_id"], "operation": "member.create", "dispatch_fence_id": fence["dispatch_fence_id"], "status": "WRITE_OUTCOME_UNCERTAIN", "member_no": job["member_payload"]["phone"], "save_invocation_count": 1, "readback_found": False, "readback_match": False, "error_code": "save_timeout"}
        self.service.acknowledge_result(job["job_id"], "worker-1", uncertain)
        result = self.service.reconcile(job["job_id"], {"member_no": job["member_payload"]["phone"], "lookup_status": "absent", "readback_found": False, "readback_match": False, "error_code": "confirmed_absent_manual_followup"})
        self.assertEqual(result["state"], ResultStatus.CONFIRMED_NOT_CREATED.value)
        self.assertIsNone(self.repository.claim_job("worker-2", now=NOW + timedelta(days=1)))

    def test_readback_exact_match_and_mismatch(self):
        _, job = self.ingest_claim()
        member_no = job["member_payload"]["phone"]
        expected = build_member_record(self.repository.get_job(job["job_id"]), member_no)
        self.assertTrue(compare_readback(expected, expected.to_dict()).match)
        mismatched = expected.to_dict()
        mismatched["ExpiryDate"] = "2099-01-01"
        self.assertFalse(compare_readback(expected, mismatched).match)

    def test_kill_switch_blocks_claim_and_immediate_dispatch_recheck(self):
        repo = InMemoryRepository()
        service = GatewayService(config(kill_switch_enabled=True), repo, adapter_ready=True, clock=NOW)
        service.ingest(event())
        with self.assertRaises(ApiError):
            service.claim("worker-1")
        service.disable_kill_switch()
        repo.set_control("production_activation_enabled", True)
        job = service.claim("worker-1")["job"]
        service.precheck(job["job_id"], "worker-1")
        self.bind_base(job, service=service)
        service.allocation_recheck(job["job_id"], "worker-1", {"status": "FREE", "probe_reference": "kill-recheck"})
        service.write_intent(job["job_id"], "worker-1", {"operation": "member.create", "member_no": job["member_payload"]["phone"], "payload_hash": job["payload_hash"]}, principal_valid=True)
        service.enable_kill_switch()
        with self.assertRaises(ApiError):
            service.dispatch_fence(job["job_id"], "worker-1", {"operation": "member.create", "member_no": job["member_payload"]["phone"]}, principal_valid=True)

    def test_unknown_eligibility_predicate_blocks(self):
        job = JobRecord("job-x", "req-x", "hmac-v1:" + "0" * 64, "resp-x", "sha256:" + "0" * 64, "member.create", {}, NOW.isoformat())
        decision = evaluate_eligibility(EligibilityContext(config=config(), job=job, positive_free_evidence=None))
        self.assertFalse(decision.eligible)
        self.assertIn("positive_free_candidate_evidence_unknown", decision.reasons)


if __name__ == "__main__":
    unittest.main()
