import unittest
from datetime import datetime, timedelta, timezone

from xb_member_gateway.api import ApiError, GatewayService
from xb_member_gateway.canonical import (
    build_source_event,
    dedupe_poll_responses,
    iter_forms_pages,
)
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.models import JobState
from xb_member_gateway.repository import AllocationConflict, InMemoryRepository, ResultConflict


NOW = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)


def make_config(**changes):
    value = {
        "member_no_max_length": 20,
        "worker_token_sha256": "0" * 64,
        "production_activation_enabled": True,
        "kill_switch_enabled": False,
    }
    value.update(changes)
    return GatewayConfig.from_mapping(value)


def make_event(response_id, *, phone="81234567", pdpa_acknowledged=True):
    return build_source_event(
        response_id=response_id,
        request_id=f"additional-request-{response_id}",
        create_time="2026-08-30T01:00:00Z",
        form_alias="member_registration",
        mapping_version="member-intake.v1",
        payload={
            "name": f"Synthetic {response_id}",
            "phone": phone,
            "email": f"{response_id}@example.test",
            "birthday_month": "April",
            "marketing_consent": "No",
            "pdpa_acknowledged": pdpa_acknowledged,
        },
    )


def prepare(service, response_id="additional-response", worker="worker-1"):
    service.ingest(make_event(response_id))
    job = service.claim(worker)["job"]
    service.precheck(job["job_id"], worker)
    candidate = service.allocation_candidate(job["job_id"])["candidate"]
    service.allocation_probe(
        job["job_id"],
        worker,
        {
            "candidate": candidate,
            "status": "FREE",
            "probe_reference": f"additional-free-{response_id}",
        },
    )
    service.allocation_recheck(
        job["job_id"],
        worker,
        {"status": "FREE", "probe_reference": f"additional-recheck-{response_id}"},
    )
    return service.repository.get_job(job["job_id"])


def prove_writer(repository, job, fence, worker="worker-1"):
    repository.register_writer_execution(
        job.job_id, fence_id=fence["dispatch_fence_id"], attempt=job.attempt,
        worker_session=worker, host_binding=f"host-{worker}", execution_id=fence["execution_id"],
        pid=4321, process_start_time="2026-08-30T01:00:01Z", now=NOW,
    )
    repository.confirm_writer_termination(
        job.job_id, fence_id=fence["dispatch_fence_id"], attempt=job.attempt,
        worker_session=worker, host_binding=f"host-{worker}", execution_id=fence["execution_id"],
        pid=4321, process_start_time="2026-08-30T01:00:01Z", evidence_type="process_exit",
        evidence_reference="evidence-additional", exit_code=0, now=NOW,
    )


class AdditionalGatewayTests(unittest.TestCase):
    def new_service(self, **config_changes):
        repository = InMemoryRepository()
        repository.set_control("kill_switch_enabled", False)
        repository.set_control("production_activation_enabled", True)
        service = GatewayService(
            make_config(**config_changes),
            repository,
            adapter_ready=True,
            clock=NOW,
        )
        return service, repository

    def test_google_page_token_and_response_id_camel_case_are_supported(self):
        pages = {
            None: {
                "responses": [{"responseId": "google-001"}],
                "nextPageToken": "page-2",
            },
            "page-2": {
                "responses": [
                    {"responseId": "google-001"},
                    {"response_id": "google-002"},
                ]
            },
        }
        rows = list(iter_forms_pages(lambda token: pages[token]))
        unique = dedupe_poll_responses(rows)
        self.assertEqual([row.get("responseId", row.get("response_id")) for row in unique], ["google-001", "google-002"])

    def test_duplicate_result_ack_is_idempotent_and_conflicting_result_is_rejected(self):
        service, repository = self.new_service()
        job = prepare(service, worker="worker-1")
        member_no = job.allocation_member_no
        intent = service.write_intent(
            job.job_id,
            "worker-1",
            {
                "operation": "member.create",
                "member_no": member_no,
                "payload_hash": job.payload_hash,
            },
            principal_valid=True,
        )
        fence = service.dispatch_fence(
            job.job_id,
            "worker-1",
            {"operation": "member.create", "member_no": member_no},
            principal_valid=True,
        )
        prove_writer(repository, job, fence)
        uncertain = {
            "schema_version": "xb.member.gateway.result.v1",
            "job_id": job.job_id,
            "operation": "member.create",
            "dispatch_fence_id": fence["dispatch_fence_id"],
            "status": "WRITE_OUTCOME_UNCERTAIN",
            "member_no": member_no,
            "save_invocation_count": 1,
            "readback_found": False,
            "readback_match": False,
            "error_code": "save_timeout",
        }
        first = service.acknowledge_result(job.job_id, "worker-1", uncertain)
        duplicate = service.acknowledge_result(job.job_id, "worker-1", uncertain)
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        conflicting = dict(uncertain, error_code="save_exception")
        with self.assertRaises(ResultConflict):
            service.acknowledge_result(job.job_id, "worker-1", conflicting)
        self.assertIsNotNone(intent)
        self.assertIsNotNone(repository.get_result(job.job_id))

    def test_readback_mismatch_is_manual_reconciliation_state(self):
        service, _ = self.new_service()
        job = prepare(service)
        member_no = job.allocation_member_no
        service.write_intent(
            job.job_id,
            "worker-1",
            {
                "operation": "member.create",
                "member_no": member_no,
                "payload_hash": job.payload_hash,
            },
            principal_valid=True,
        )
        fence = service.dispatch_fence(
            job.job_id,
            "worker-1",
            {"operation": "member.create", "member_no": member_no},
            principal_valid=True,
        )
        prove_writer(service.repository, job, fence)
        result = service.acknowledge_result(
            job.job_id,
            "worker-1",
            {
                "schema_version": "xb.member.gateway.result.v1",
                "job_id": job.job_id,
                "operation": "member.create",
                "dispatch_fence_id": fence["dispatch_fence_id"],
                "status": "CREATED_READBACK_MISMATCH",
                "member_no": member_no,
                "save_invocation_count": 1,
                "readback_found": True,
                "readback_match": False,
                "error_code": "readback_mismatch_manual_review",
            },
        )
        self.assertEqual(result["state"], "CREATED_READBACK_MISMATCH")
        self.assertEqual(
            service.repository.get_job(job.job_id).state,
            JobState.CREATED_READBACK_MISMATCH,
        )

    def test_ambiguous_probe_cannot_offer_next_suffix(self):
        service, _ = self.new_service()
        service.ingest(make_event("ambiguous-additional"))
        job = service.claim("worker-1")["job"]
        service.precheck(job["job_id"], "worker-1")
        candidate = service.allocation_candidate(job["job_id"])["candidate"]
        with self.assertRaises(ApiError):
            service.allocation_probe(
                job["job_id"],
                "worker-1",
                {
                    "candidate": candidate,
                    "status": "UNAVAILABLE",
                    "probe_reference": "additional-unknown",
                },
            )
        with self.assertRaises(ApiError):
            service.allocation_candidate(job["job_id"])

    def test_reclaimed_attempts_end_in_dead_letter_without_dispatch(self):
        service, repository = self.new_service()
        service.ingest(make_event("dead-letter-additional"))
        job = service.claim("worker-1")["job"]
        for attempt in (1, 2):
            reclaim_at = NOW + timedelta(seconds=601 * attempt)
            self.assertEqual(repository.reclaim_expired(now=reclaim_at), 1)
            self.assertEqual(repository.get_job(job["job_id"]).state, JobState.RETRY_WAIT)
            repository.claim_job(
                f"worker-{attempt + 1}",
                now=reclaim_at + timedelta(seconds=1),
            )
        final_reclaim = NOW + timedelta(seconds=1803)
        self.assertEqual(repository.reclaim_expired(now=final_reclaim), 1)
        self.assertEqual(repository.get_job(job["job_id"]).state, JobState.DEAD_LETTER)
        self.assertIsNone(repository.get_dispatch_fence(job["job_id"]))

    def test_post_fence_allocation_probe_cannot_rebind_or_advance(self):
        service, _ = self.new_service()
        job = prepare(service)
        member_no = job.allocation_member_no
        service.write_intent(
            job.job_id,
            "worker-1",
            {
                "operation": "member.create",
                "member_no": member_no,
                "payload_hash": job.payload_hash,
            },
            principal_valid=True,
        )
        service.dispatch_fence(
            job.job_id,
            "worker-1",
            {"operation": "member.create", "member_no": member_no},
            principal_valid=True,
        )
        with self.assertRaises(AllocationConflict):
            service.allocation_probe(
                job.job_id,
                "worker-1",
                {
                    "candidate": member_no,
                    "status": "FREE",
                    "probe_reference": "post-fence",
                },
            )


if __name__ == "__main__":
    unittest.main()
