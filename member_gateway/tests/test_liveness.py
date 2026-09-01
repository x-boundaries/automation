import unittest
from datetime import datetime, timedelta, timezone

from xb_member_gateway.api import ApiError, GatewayService
from xb_member_gateway.canonical import build_source_event
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.models import JobState, ProbeStatus, ResultStatus, WriterHoldState
from xb_member_gateway.repository import InMemoryRepository, WriterTerminationConflict


NOW = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
WORKER = "worker-a"
HOST = "host-a"
PROCESS_START = "2026-09-01T01:00:01.0000000Z"


def make_config():
    return GatewayConfig.from_mapping({
        "member_no_max_length": 20,
        "worker_token_sha256": "0" * 64,
        "production_activation_enabled": True,
        "kill_switch_enabled": False,
    })


def make_event(response_id="liveness-001"):
    return build_source_event(
        response_id=response_id,
        request_id=f"request-{response_id}",
        create_time="2026-09-01T01:00:00Z",
        form_alias="member_registration",
        mapping_version="member-intake.v1",
        payload={
            "name": "Liveness Member",
            "phone": "81234567",
            "email": "liveness@example.test",
            "birthday_month": "January",
            "marketing_consent": "Yes",
            "pdpa_acknowledged": True,
        },
    )


class WriterLivenessTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository()
        self.repository.set_control("kill_switch_enabled", False)
        self.repository.set_control("production_activation_enabled", True)
        self.service = GatewayService(make_config(), self.repository, adapter_ready=True, clock=NOW)

    def fenced(self, response_id="liveness-001", worker=WORKER, host=HOST):
        self.service.ingest(make_event(response_id))
        claimed = self.service.claim(worker)
        job = claimed["job"]
        self.service.precheck(job["job_id"], worker)
        candidate = self.service.allocation_candidate(job["job_id"])["candidate"]
        self.service.allocation_probe(job["job_id"], worker, {
            "candidate": candidate, "status": "FREE", "probe_reference": f"probe-{response_id}",
        })
        self.service.allocation_recheck(job["job_id"], worker, {
            "status": "FREE", "probe_reference": f"recheck-{response_id}",
        })
        self.service.write_intent(job["job_id"], worker, {
            "operation": "member.create", "member_no": candidate, "payload_hash": job["payload_hash"],
        }, principal_valid=True)
        fence = self.service.dispatch_fence(job["job_id"], worker, {
            "operation": "member.create", "member_no": candidate, "host_binding": host,
        }, principal_valid=True)
        return job, fence

    def register(self, job, fence, *, worker=WORKER, host=HOST, pid=4321, process_start=PROCESS_START):
        return self.repository.register_writer_execution(
            job["job_id"], fence_id=fence["dispatch_fence_id"], attempt=job["attempt"],
            worker_session=worker, host_binding=host, execution_id=fence["execution_id"],
            pid=pid, process_start_time=process_start, now=NOW,
        )

    def confirm(self, job, fence, *, worker=WORKER, host=HOST, pid=4321, process_start=PROCESS_START):
        return self.repository.confirm_writer_termination(
            job["job_id"], fence_id=fence["dispatch_fence_id"], attempt=job["attempt"],
            worker_session=worker, host_binding=host, execution_id=fence["execution_id"],
            pid=pid, process_start_time=process_start, evidence_type="process_exit",
            evidence_reference="evidence-normal", exit_code=0, now=NOW,
        )

    def result_body(self, job, fence, status="CREATED_VERIFIED", found=True, match=True):
        return {
            "schema_version": "xb.member.gateway.result.v1", "job_id": job["job_id"],
            "operation": "member.create", "dispatch_fence_id": fence["dispatch_fence_id"],
            "status": status, "member_no": job["member_payload"]["phone"],
            "save_invocation_count": 1, "readback_found": found, "readback_match": match,
            "error_code": None if status == "CREATED_VERIFIED" else "save_outcome_uncertain",
        }

    def quarantine(self, job, fence):
        return self.repository.quarantine_writer_execution(
            job["job_id"], fence_id=fence["dispatch_fence_id"], attempt=job["attempt"],
            worker_session=WORKER, host_binding=HOST, execution_id=fence["execution_id"],
            evidence_reference="quarantine-test", reason="writer_termination_unconfirmed", now=NOW,
        )

    def test_fence_creates_pending_hold_and_registration_is_exact(self):
        job, fence = self.fenced()
        status = self.service.status(job["job_id"])
        self.assertEqual(status["schema_version"], "xb.member.gateway.job.v2")
        self.assertEqual(status["writer_termination_state"], WriterHoldState.PENDING.value)
        self.assertTrue(status["writer_termination_hold_active"])
        self.assertTrue(status["writer_termination_proof_required"])
        for private_field in ("pid", "process_pid", "process_start_time", "host_binding", "nonce", "evidence"):
            self.assertNotIn(private_field, status)
        hold = self.repository.get_writer_execution_hold(job["job_id"])
        self.assertEqual(hold.state, WriterHoldState.PENDING)
        self.assertIsNone(hold.pid)
        self.assertEqual(hold.fence_id, fence["dispatch_fence_id"])
        self.assertEqual(hold.execution_id, fence["execution_id"])
        self.assertEqual(self.register(job, fence).state, WriterHoldState.REGISTERED)
        with self.assertRaisesRegex(WriterTerminationConflict, "writer_process_identity_mismatch"):
            self.register(job, fence, process_start="pid-reuse")

    def test_result_before_confirmation_and_kill_without_exit_proof_stay_quarantined(self):
        job, fence = self.fenced()
        with self.assertRaisesRegex(WriterTerminationConflict, "writer_termination_proof_required"):
            self.service.acknowledge_result(job["job_id"], WORKER, self.result_body(job, fence))
        self.register(job, fence)
        self.quarantine(job, fence)
        self.assertEqual(self.repository.get_job(job["job_id"]).state, JobState.WRITER_TERMINATION_UNCONFIRMED)
        self.assertIsNone(self.repository.get_result(job["job_id"]))
        with self.assertRaisesRegex(WriterTerminationConflict, "writer_termination_proof_required"):
            self.confirm(job, fence)
        self.repository.reclaim_expired(now=NOW + timedelta(seconds=601))
        self.assertEqual(self.repository.get_writer_execution_hold(job["job_id"]).state, WriterHoldState.QUARANTINED)
        self.assertIsNone(self.repository.claim_job("worker-b", now=NOW + timedelta(seconds=602)))
        with self.assertRaisesRegex(WriterTerminationConflict, "writer_termination_quarantine_active"):
            self.repository.open_reconciliation_case(job["job_id"], job["member_payload"]["phone"], now=NOW + timedelta(seconds=602))

    def test_snapshot_restart_and_stale_worker_cannot_clear_quarantine(self):
        job, fence = self.fenced()
        self.register(job, fence)
        self.quarantine(job, fence)
        restarted = InMemoryRepository.from_snapshot(self.repository.snapshot_state())
        hold = restarted.get_writer_execution_hold(job["job_id"])
        self.assertEqual(hold.state, WriterHoldState.QUARANTINED)
        with self.assertRaisesRegex(WriterTerminationConflict, "stale_worker_session"):
            restarted.recover_writer_termination(
                job["job_id"], fence_id=fence["dispatch_fence_id"], attempt=job["attempt"],
                recovery_session=WORKER, host_binding=HOST, execution_id=fence["execution_id"],
                pid=4321, process_start_time=PROCESS_START, evidence_reference="recovery-1", exit_code=0, now=NOW + timedelta(seconds=700),
            )
        with self.assertRaisesRegex(WriterTerminationConflict, "writer_termination_binding_invalid"):
            restarted.recover_writer_termination(
                job["job_id"], fence_id=fence["dispatch_fence_id"], attempt=job["attempt"],
                recovery_session="worker-b", host_binding=HOST, execution_id=fence["execution_id"],
                pid=4321, process_start_time="pid-reused", evidence_reference="recovery-2", exit_code=0, now=NOW + timedelta(seconds=700),
            )
        result, duplicate = restarted.recover_writer_termination(
            job["job_id"], fence_id=fence["dispatch_fence_id"], attempt=job["attempt"],
            recovery_session="worker-b", host_binding=HOST, execution_id=fence["execution_id"],
            pid=4321, process_start_time=PROCESS_START, evidence_reference="recovery-3", exit_code=0, now=NOW + timedelta(seconds=700),
        )
        self.assertFalse(duplicate)
        self.assertEqual(result.status, ResultStatus.WRITE_OUTCOME_UNCERTAIN)
        self.assertEqual(restarted.get_job(job["job_id"]).state, JobState.WRITE_OUTCOME_UNCERTAIN)
        self.assertEqual(restarted.get_writer_execution_hold(job["job_id"]).state, WriterHoldState.CLEARED)
        self.assertEqual(len(restarted.result_history(job["job_id"])), 1)
        again, duplicate = restarted.recover_writer_termination(
            job["job_id"], fence_id=fence["dispatch_fence_id"], attempt=job["attempt"],
            recovery_session="worker-c", host_binding=HOST, execution_id=fence["execution_id"],
            pid=4321, process_start_time=PROCESS_START, evidence_reference="recovery-4", exit_code=0, now=NOW + timedelta(seconds=701),
        )
        self.assertTrue(duplicate)
        self.assertEqual(again.result_hash, result.result_hash)
        self.assertEqual(len(restarted.result_history(job["job_id"])), 1)

    def test_normal_confirmation_precedes_actual_result_and_clears_hold_after_result(self):
        job, fence = self.fenced()
        self.register(job, fence)
        self.confirm(job, fence)
        self.assertEqual(self.repository.get_writer_execution_hold(job["job_id"]).state, WriterHoldState.TERMINATION_CONFIRMED)
        acknowledged = self.service.acknowledge_result(job["job_id"], WORKER, self.result_body(job, fence))
        self.assertEqual(acknowledged["state"], ResultStatus.CREATED_VERIFIED.value)
        self.assertEqual(self.repository.get_writer_execution_hold(job["job_id"]).state, WriterHoldState.CLEARED)
        self.assertIsNone(self.repository.get_job(job["job_id"]).lease_owner)
        self.assertEqual(self.repository.get_job(job["job_id"]).save_invocation_count, 1)

    def test_confirmed_termination_expiry_atomically_enters_existing_uncertainty_lane(self):
        job, fence = self.fenced()
        self.register(job, fence)
        self.confirm(job, fence)
        self.assertEqual(self.repository.reclaim_expired(now=NOW + timedelta(seconds=601)), 1)
        current = self.repository.get_job(job["job_id"])
        self.assertEqual(current.state, JobState.WRITE_OUTCOME_UNCERTAIN)
        self.assertEqual(current.result_status, ResultStatus.WRITE_OUTCOME_UNCERTAIN)
        self.assertEqual(self.repository.get_writer_execution_hold(job["job_id"]).state, WriterHoldState.CLEARED)
        self.assertIsNone(current.lease_owner)
        self.assertEqual(len(self.repository.result_history(job["job_id"])), 1)
        self.assertEqual(self.repository.reclaim_expired(now=NOW + timedelta(seconds=602)), 0)
        self.assertEqual(self.repository.get_writer_execution_hold(job["job_id"]).state, WriterHoldState.CLEARED)
        reconciled = self.service.reconcile(job["job_id"], {
            "member_no": job["member_payload"]["phone"], "lookup_status": "absent",
            "readback_found": False, "readback_match": False, "error_code": "confirmed_absent_manual_followup",
        })
        self.assertEqual(reconciled["state"], ResultStatus.CONFIRMED_NOT_CREATED.value)

    def test_global_quarantine_blocks_claim_and_member_allocation(self):
        job, fence = self.fenced("liveness-first")
        self.service.ingest(make_event("liveness-second"))
        self.register(job, fence)
        self.quarantine(job, fence)
        self.assertIsNone(self.repository.claim_job("worker-b", now=NOW + timedelta(seconds=1)))
        with self.assertRaises(WriterTerminationConflict):
            self.service.allocation_candidate(job["job_id"])


if __name__ == "__main__":
    unittest.main()
