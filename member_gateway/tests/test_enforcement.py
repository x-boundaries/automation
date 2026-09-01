import concurrent.futures
import json
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from xb_member_gateway.api import ApiError, GatewayService
from xb_member_gateway.canonical import build_source_event
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.eligibility import EligibilityContext, evaluate_eligibility
from xb_member_gateway.models import JobState, ProbeStatus, ResultRecord, ResultStatus
from xb_member_gateway.repository import (
    InMemoryRepository,
    LeaseConflict,
    PostgresRepository,
    RepositoryError,
    ResultConflict,
    WriterTerminationConflict,
    internal_fence_id,
    public_fence_id,
)


NOW = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)
SESSION_A = "ws-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SESSION_B = "ws-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def make_config(**changes):
    values = {
        "member_no_max_length": 20,
        "worker_token_sha256": "0" * 64,
        "production_activation_enabled": True,
        "kill_switch_enabled": False,
    }
    values.update(changes)
    return GatewayConfig.from_mapping(values)


def make_event(response_id: str, *, phone: str = "81234567"):
    return build_source_event(
        response_id=response_id,
        request_id=f"enforcement-request-{response_id}",
        create_time="2026-08-30T01:00:00Z",
        form_alias="member_registration",
        mapping_version="member-intake.v1",
        payload={
            "name": f"Synthetic {response_id}",
            "phone": phone,
            "email": f"{response_id}@example.test",
            "birthday_month": "January",
            "marketing_consent": "Yes",
            "pdpa_acknowledged": True,
        },
    )


def assert_result_schema(test_case, value, schema):
    required = set(schema["required"])
    test_case.assertEqual(set(value), required)
    for name, definition in schema["properties"].items():
        observed = value[name]
        if "const" in definition:
            test_case.assertEqual(observed, definition["const"], name)
        if "enum" in definition:
            test_case.assertIn(observed, definition["enum"], name)
        types = definition.get("type")
        if types == "string" or (isinstance(types, list) and "string" in types):
            if observed is not None:
                test_case.assertIsInstance(observed, str, name)
        if types == "integer":
            test_case.assertIsInstance(observed, int, name)
            test_case.assertNotIsInstance(observed, bool, name)
        if types == "boolean":
            test_case.assertIsInstance(observed, bool, name)
        if types == ["string", "null"]:
            test_case.assertTrue(observed is None or isinstance(observed, str), name)
        if "pattern" in definition and observed is not None:
            import re
            test_case.assertRegex(observed, definition["pattern"], name)
        if definition.get("format") == "date-time":
            test_case.assertIsInstance(observed, str, name)
            datetime.fromisoformat(observed.replace("Z", "+00:00"))


class DelayedClaimRepository(InMemoryRepository):
    def __init__(self):
        super().__init__()
        self.ready = threading.Event()
        self.proceed = threading.Event()

    def claim_job(self, *args, **kwargs):
        self.ready.set()
        self.proceed.wait(timeout=5)
        return super().claim_job(*args, **kwargs)


class DelayedFenceRepository(InMemoryRepository):
    def __init__(self):
        super().__init__()
        self.ready = threading.Event()
        self.proceed = threading.Event()

    def record_dispatch_fence(self, *args, **kwargs):
        self.ready.set()
        self.proceed.wait(timeout=5)
        return super().record_dispatch_fence(*args, **kwargs)


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository()
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", True)
        self.service = GatewayService(
            make_config(), self.repository, adapter_ready=True, clock=NOW
        )

    def prepare(self, response_id="enforcement-001", worker=SESSION_A, *, phone="81234567", recheck=False):
        self.service.ingest(make_event(response_id, phone=phone))
        job = self.service.claim(worker)["job"]
        self.service.precheck(job["job_id"], worker)
        candidate = self.service.allocation_candidate(job["job_id"], worker)["candidate"]
        self.service.allocation_probe(
            job["job_id"],
            worker,
            {
                "candidate": candidate,
                "status": "FREE",
                "probe_reference": f"allocation-{response_id}",
            },
        )
        if recheck:
            self.service.allocation_recheck(
                job["job_id"],
                worker,
                {"status": "FREE", "probe_reference": f"recheck-{response_id}"},
            )
        return self.repository.get_job(job["job_id"])

    def write_fence(self, job, worker=SESSION_A):
        member_no = job.allocation_member_no
        self.service.write_intent(
            job.job_id,
            worker,
            {
                "operation": "member.create",
                "member_no": member_no,
                "payload_hash": job.payload_hash,
            },
            principal_valid=True,
        )
        return self.service.dispatch_fence(
            job.job_id,
            worker,
            {"operation": "member.create", "member_no": member_no},
            principal_valid=True,
        )

    def prove_writer(self, job, fence, worker=SESSION_A):
        host = f"host-{worker}"
        self.repository.register_writer_execution(
            job.job_id, fence_id=fence["dispatch_fence_id"], attempt=job.attempt,
            worker_session=worker, host_binding=host, execution_id=fence["execution_id"],
            pid=4321, process_start_time="2026-08-30T01:00:01Z", now=NOW,
        )
        self.repository.confirm_writer_termination(
            job.job_id, fence_id=fence["dispatch_fence_id"], attempt=job.attempt,
            worker_session=worker, host_binding=host, execution_id=fence["execution_id"],
            pid=4321, process_start_time="2026-08-30T01:00:01Z", evidence_type="process_exit",
            evidence_reference="evidence-enforcement", exit_code=0, now=NOW,
        )

    def uncertain(self, response_id="uncertain-001", *, phone="81234567"):
        job = self.prepare(response_id, phone=phone, recheck=True)
        fence = self.write_fence(job)
        self.prove_writer(job, fence)
        result = self.service.acknowledge_result(
            job.job_id,
            SESSION_A,
            {
                "schema_version": "xb.member.gateway.result.v1",
                "job_id": job.job_id,
                "operation": "member.create",
                "dispatch_fence_id": fence["dispatch_fence_id"],
                "status": "WRITE_OUTCOME_UNCERTAIN",
                "member_no": job.allocation_member_no,
                "save_invocation_count": 1,
                "readback_found": False,
                "readback_match": False,
                "error_code": "save_timeout",
            },
        )
        return job, fence, result

    def test_kill_switch_serialization_wins_before_claim_and_fence(self):
        claim_repo = DelayedClaimRepository()
        claim_repo.set_control("kill_switch_enabled", False)
        claim_service = GatewayService(make_config(), claim_repo, adapter_ready=True, clock=NOW)
        claim_service.ingest(make_event("atomic-claim"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(claim_repo.claim_job, SESSION_A, now=NOW)
            self.assertTrue(claim_repo.ready.wait(timeout=5))
            claim_repo.set_control("kill_switch_enabled", True)
            claim_repo.proceed.set()
            with self.assertRaises(RepositoryError):
                pending.result(timeout=5)
        self.assertEqual(claim_repo.all_jobs()[0].state, JobState.QUEUED)

        fence_repo = DelayedFenceRepository()
        fence_repo.set_control("kill_switch_enabled", False)
        fence_repo.set_control("production_activation_enabled", True)
        fence_service = GatewayService(make_config(), fence_repo, adapter_ready=True, clock=NOW)
        fence_service.ingest(make_event("atomic-fence", phone="89876543"))
        claimed = fence_service.claim(SESSION_A)["job"]
        fence_service.precheck(claimed["job_id"], SESSION_A)
        candidate = fence_service.allocation_candidate(claimed["job_id"], SESSION_A)["candidate"]
        fence_service.allocation_probe(claimed["job_id"], SESSION_A, {"candidate": candidate, "status": "FREE", "probe_reference": "atomic-allocation"})
        fence_service.allocation_recheck(claimed["job_id"], SESSION_A, {"status": "FREE", "probe_reference": "atomic-recheck"})
        fence_service.write_intent(claimed["job_id"], SESSION_A, {"operation": "member.create", "member_no": candidate, "payload_hash": claimed["payload_hash"]}, principal_valid=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(fence_repo.record_dispatch_fence, claimed["job_id"], candidate, "member.create", SESSION_A, now=NOW)
            self.assertTrue(fence_repo.ready.wait(timeout=5))
            fence_repo.set_control("kill_switch_enabled", True)
            fence_repo.proceed.set()
            with self.assertRaises(RepositoryError):
                pending.result(timeout=5)
        self.assertIsNone(fence_repo.get_dispatch_fence(claimed["job_id"]))
        self.assertEqual(fence_repo.get_job(claimed["job_id"]).state, JobState.WRITE_INTENT_RECORDED)

    def test_singleton_claim_is_durable_and_concurrent_claimers_get_one_job(self):
        self.service.ingest(make_event("singleton-a"))
        self.service.ingest(make_event("singleton-b", phone="89876543"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda worker: self.repository.claim_job(worker, now=NOW), (SESSION_A, SESSION_B)))
        claimed = [item for item in claims if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertIsNone(self.repository.claim_job(SESSION_B, now=NOW))

    def test_session_a_lease_cannot_be_used_by_session_b(self):
        job = self.prepare("session-bound", worker=SESSION_A)
        job_id = job.job_id
        with self.assertRaises(LeaseConflict):
            self.repository.heartbeat(job_id, SESSION_B, expected_state_version=job.state_version, now=NOW)
        with self.assertRaises(LeaseConflict):
            self.repository.begin_prechecking(job_id, SESSION_B, now=NOW)
        with self.assertRaises(LeaseConflict):
            self.repository.assert_lease(job_id, SESSION_B, now=NOW)
        candidate = job.allocation_member_no
        with self.assertRaises(LeaseConflict):
            self.repository.record_probe(job_id, candidate, ProbeStatus.FREE, "wrong-session-probe", SESSION_B, now=NOW)
        with self.assertRaises(ApiError):
            self.service.write_intent(job_id, SESSION_B, {"operation": "member.create", "member_no": candidate, "payload_hash": job.payload_hash}, principal_valid=True)

        self.service.allocation_recheck(job_id, SESSION_A, {"status": "FREE", "probe_reference": "session-recheck"})
        self.service.write_intent(job_id, SESSION_A, {"operation": "member.create", "member_no": candidate, "payload_hash": job.payload_hash}, principal_valid=True)
        with self.assertRaises(LeaseConflict):
            self.repository.record_dispatch_fence(job_id, candidate, "member.create", SESSION_B, now=NOW)
        fence = self.service.dispatch_fence(job_id, SESSION_A, {"operation": "member.create", "member_no": candidate}, principal_valid=True)
        self.prove_writer(job, fence, SESSION_A)
        result_body = {"schema_version": "xb.member.gateway.result.v1", "job_id": job_id, "operation": "member.create", "dispatch_fence_id": fence["dispatch_fence_id"], "status": "CREATED_VERIFIED", "member_no": candidate, "save_invocation_count": 1, "readback_found": True, "readback_match": True, "error_code": None}
        with self.assertRaises(LeaseConflict):
            self.service.acknowledge_result(job_id, SESSION_B, result_body)
        accepted = self.service.acknowledge_result(job_id, SESSION_A, result_body)
        self.assertEqual(accepted["state"], ResultStatus.CREATED_VERIFIED.value)

    def test_rate_evidence_is_fail_closed_and_has_no_default_true(self):
        job = self.prepare("rate-evidence")
        decision = evaluate_eligibility(EligibilityContext(config=make_config(), job=job, positive_free_evidence=True, fresh_bound_member_no_recheck=True, gateway_ready=True, autocount_adapter_ready=True, worker_credential_valid=True, kill_switch_rechecked=True, worker_id=SESSION_A, lease_owner=SESSION_A))
        self.assertFalse(decision.eligible)
        self.assertIn("rate_attempt_deadline_eligible_unknown", decision.reasons)
        self.assertTrue(self.repository.rate_allowed(job.job_id, SESSION_A, now=NOW))
        self.assertFalse(self.repository.rate_allowed(job.job_id, SESSION_B, now=NOW))

    def test_historic_free_probe_alone_cannot_reach_write_or_fence(self):
        job = self.prepare("historic-only")
        member_no = job.allocation_member_no
        with self.assertRaises(ApiError):
            self.service.write_intent(job.job_id, SESSION_A, {"operation": "member.create", "member_no": member_no, "payload_hash": job.payload_hash}, principal_valid=True)
        with self.assertRaises(RepositoryError):
            self.repository.record_write_intent(job.job_id, member_no, "member.create", job.payload_hash, SESSION_A, now=NOW)
        with self.assertRaises(RepositoryError):
            self.repository.record_dispatch_fence(job.job_id, member_no, "member.create", SESSION_A, now=NOW)
        self.assertIsNone(self.repository.get_dispatch_fence(job.job_id))

    def test_fresh_recheck_is_durable_and_lineage_is_current(self):
        job = self.prepare("fresh-lineage", recheck=True)
        marker = self.repository.get_fresh_recheck(job.job_id, SESSION_A, now=NOW)
        self.assertIsNotNone(marker)
        self.assertEqual(marker.job_id, job.job_id)
        self.assertEqual(marker.attempt, job.attempt)
        self.assertEqual(marker.worker_id, SESSION_A)
        self.assertEqual(marker.member_no, job.allocation_member_no)
        self.assertEqual(marker.status, ProbeStatus.FREE)
        self.assertNotEqual(marker.probe_reference, job.allocation_probe_reference)
        self.assertNotEqual(marker.recheck_id, job.allocation_probe_reference)
        fence = self.write_fence(job)
        self.assertRegex(fence["dispatch_fence_id"], r"^fence-[A-Za-z0-9]{16,64}$")
        stored_intent = self.repository.get_write_intent(job.job_id)
        stored_fence = self.repository.get_dispatch_fence(job.job_id)
        self.assertEqual(stored_intent.recheck_id, marker.recheck_id)
        self.assertEqual(stored_fence.recheck_id, marker.recheck_id)
        self.assertEqual(stored_fence.fence_id, fence["dispatch_fence_id"])

    def test_expired_writer_is_reclaimed_before_reconciliation_case_opens(self):
        job = self.prepare("reclaim-reconcile", recheck=True)
        fence = self.write_fence(job)
        self.prove_writer(job, fence)
        reclaim_time = NOW + timedelta(seconds=601)
        self.assertEqual(self.repository.reclaim_expired(now=reclaim_time), 1)
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.WRITE_OUTCOME_UNCERTAIN)
        case = self.repository.open_reconciliation_case(job.job_id, job.allocation_member_no, now=reclaim_time)
        self.assertEqual(case.job_id, job.job_id)
        self.assertEqual(case.member_no, job.allocation_member_no)
        self.assertEqual(fence["member_no"], case.member_no)

    def test_stale_prior_attempt_and_wrong_session_rechecks_are_not_reused(self):
        job = self.prepare("stale-recheck", worker=SESSION_A, recheck=True)
        with self.assertRaises(LeaseConflict):
            self.repository.get_fresh_recheck(job.job_id, SESSION_B, now=NOW)
        self.assertEqual(self.repository.reclaim_expired(now=NOW + timedelta(seconds=601)), 1)
        self.repository.claim_job(SESSION_B, now=NOW + timedelta(seconds=602))
        self.repository.begin_prechecking(job.job_id, SESSION_B, now=NOW + timedelta(seconds=602))
        self.service.clock = NOW + timedelta(seconds=602)
        self.assertIsNone(self.repository.get_fresh_recheck(job.job_id, SESSION_B, now=self.service.clock))
        with self.assertRaises(ApiError):
            self.service.write_intent(job.job_id, SESSION_B, {"operation": "member.create", "member_no": job.allocation_member_no, "payload_hash": job.payload_hash}, principal_valid=True)
        self.service.allocation_recheck(job.job_id, SESSION_B, {"status": "FREE", "probe_reference": "fresh-recheck-attempt-2"})
        intent = self.service.write_intent(job.job_id, SESSION_B, {"operation": "member.create", "member_no": job.allocation_member_no, "payload_hash": job.payload_hash}, principal_valid=True)
        self.assertTrue(intent["intent_id"].startswith("intent-"))
        self.assertEqual(self.repository.get_job(job.job_id).allocation_member_no, job.allocation_member_no)

    def test_failed_or_conflicting_recheck_never_allocates_next_suffix(self):
        for index, status in enumerate(("OCCUPIED", "AMBIGUOUS", "UNAVAILABLE"), start=1):
            job = self.prepare(f"failed-recheck-{index}", phone=f"89{index:06d}")
            member_no = job.allocation_member_no
            response = self.service.allocation_recheck(job.job_id, SESSION_A, {"status": status, "probe_reference": f"failed-recheck-{index}"})
            self.assertEqual(response["state"], JobState.MANUAL_REVIEW.value)
            self.assertEqual(self.repository.get_job(job.job_id).allocation_member_no, member_no)
            with self.assertRaises(ApiError):
                self.service.write_intent(job.job_id, SESSION_A, {"operation": "member.create", "member_no": member_no, "payload_hash": job.payload_hash}, principal_valid=True)
        job = self.prepare("conflicting-recheck", phone="89888888", recheck=True)
        self.service.allocation_recheck(job.job_id, SESSION_A, {"status": "FREE", "probe_reference": "conflicting-recheck-2"})
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.MANUAL_REVIEW)
        self.assertIsNone(self.repository.get_fresh_recheck(job.job_id, SESSION_A, now=NOW))
        self.assertEqual(len(self.repository._rechecks[job.job_id]), 2)

    def test_writing_and_live_writer_reconciliation_are_rejected(self):
        job = self.prepare("writing-reconcile", recheck=True)
        fence = self.write_fence(job)
        body = {"member_no": job.allocation_member_no, "lookup_status": "absent", "readback_found": False, "readback_match": False, "error_code": "confirmed_absent_manual_followup"}
        with self.assertRaises(ApiError):
            self.service.reconcile(job.job_id, body)
        self.repository.mark_state(job.job_id, JobState.WRITE_OUTCOME_UNCERTAIN, worker_id=SESSION_A, require_lease=True, now=NOW)
        with self.assertRaises(WriterTerminationConflict):
            self.repository.open_reconciliation_case(job.job_id, job.allocation_member_no, now=NOW)
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.WRITER_TERMINATION_UNCONFIRMED)
        self.assertEqual(fence["state"], JobState.WRITING.value)

    def test_reconciliation_requires_case_check_and_preserves_late_writer_safety(self):
        job, fence, _ = self.uncertain("reconcile-absence", phone="89812345")
        result = self.repository.get_result(job.job_id)
        with self.assertRaises(RepositoryError):
            self.repository.acknowledge_result(result, require_lease=False, now=NOW)
        response = self.service.reconcile(job.job_id, {"member_no": job.allocation_member_no, "lookup_status": "absent", "readback_found": False, "readback_match": False, "error_code": "confirmed_absent_manual_followup"})
        self.assertEqual(response["state"], ResultStatus.CONFIRMED_NOT_CREATED.value)
        case = self.repository.get_reconciliation_case(response["case_id"])
        checks = self.repository.get_reconciliation_checks(response["case_id"])
        self.assertEqual(case.state.value, "ABSENT")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].lookup_status, "absent")
        late = {"schema_version": "xb.member.gateway.result.v1", "job_id": job.job_id, "operation": "member.create", "dispatch_fence_id": fence["dispatch_fence_id"], "status": "CREATED_VERIFIED", "member_no": job.allocation_member_no, "save_invocation_count": 1, "readback_found": True, "readback_match": True, "error_code": None}
        with self.assertRaises(ResultConflict):
            self.service.acknowledge_result(job.job_id, SESSION_A, late)
        self.assertEqual(self.repository.get_job(job.job_id).state, JobState.CONFIRMED_NOT_CREATED)

    def test_reconciliation_outcomes_are_bound_to_read_only_checks(self):
        outcomes = (
            ("exact_match", True, True, ResultStatus.CREATED_VERIFIED),
            ("absent", False, False, ResultStatus.CONFIRMED_NOT_CREATED),
            ("mismatch", True, False, ResultStatus.CREATED_READBACK_MISMATCH),
            ("ambiguous", False, False, ResultStatus.WRITE_OUTCOME_UNCERTAIN),
        )
        for index, (lookup, found, matched, expected) in enumerate(outcomes, start=1):
            job, _, _ = self.uncertain(f"reconcile-outcome-{index}", phone=f"89{index:06d}")
            response = self.service.reconcile(job.job_id, {"member_no": job.allocation_member_no, "lookup_status": lookup, "readback_found": found, "readback_match": matched, "error_code": None if lookup == "exact_match" else "manual-followup"})
            self.assertEqual(response["state"], expected.value)

    def test_canonical_fence_and_result_validate_for_both_repository_shapes(self):
        internal = uuid.uuid4()
        public = public_fence_id(internal)
        self.assertRegex(public, r"^fence-[A-Za-z0-9]{16,64}$")
        self.assertEqual(internal_fence_id(public), str(internal))
        job, fence, _ = self.uncertain("schema-shaped", phone="89876540")
        stored = self.repository.get_result(job.job_id)
        self.assertRegex(stored.dispatch_fence_id, r"^fence-[A-Za-z0-9]{16,64}$")
        schema = json.loads((__import__("pathlib").Path(__file__).resolve().parents[2] / "schemas/member_gateway_result.v1.schema.json").read_text(encoding="utf-8"))
        assert_result_schema(self, stored.to_dict(), schema)
        row = (job.job_id, "request", "hmac", "response", job.payload_hash, "member.create", job.member_payload, job.created_at, "WRITING", job.state_version, job.attempt, job.max_attempts, None, SESSION_A, job.lease_expires_at, job.allocation_member_no, job.allocation_probe_reference, job.write_intent_id, str(internal), 0, None, None, "google_forms", "member_registration", "member-intake.v1", job.attempt_started_at)
        projected = PostgresRepository._from_row(row)
        self.assertEqual(projected.dispatch_fence_id, public)
        self.assertEqual(fence["dispatch_fence_id"], public_fence_id(internal_fence_id(fence["dispatch_fence_id"])))
        production_result = ResultRecord(
            job.job_id,
            stored.result_hash,
            stored.status,
            stored.member_no,
            projected.dispatch_fence_id,
            stored.save_invocation_count,
            stored.readback_found,
            stored.readback_match,
            stored.reconciliation_required,
            stored.error_code,
            stored.acknowledged_at,
        )
        assert_result_schema(self, production_result.to_dict(), schema)


class PostgresTransactionContractTests(unittest.TestCase):
    class Cursor:
        def __init__(self, enabled):
            self.enabled = enabled
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append(statement)

        def fetchone(self):
            return (self.enabled,)

    def test_kill_switch_final_check_locks_control_row(self):
        repository = PostgresRepository(connection_factory=lambda: None)
        clear = self.Cursor(False)
        repository._lock_kill_switch(clear)
        self.assertIn("FOR UPDATE", clear.statements[0])
        engaged = self.Cursor(True)
        with self.assertRaises(RepositoryError):
            repository._lock_kill_switch(engaged)
        self.assertEqual(len(engaged.statements), 1)


if __name__ == "__main__":
    unittest.main()
