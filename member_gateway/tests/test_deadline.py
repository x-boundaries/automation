import unittest
from datetime import datetime, timedelta, timezone

from xb_member_gateway.api import ApiError, GatewayService
from xb_member_gateway.canonical import build_source_event
from xb_member_gateway.config import GatewayConfig
from xb_member_gateway.repository import InMemoryRepository


NOW = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)


class AttemptDeadlineTests(unittest.TestCase):
    def test_dispatch_fails_closed_after_configured_attempt_deadline(self):
        repository = InMemoryRepository()
        repository.set_control("kill_switch_enabled", False)
        repository.set_control("production_activation_enabled", True)
        service = GatewayService(
            GatewayConfig.from_mapping(
                {
                    "member_no_max_length": 20,
                    "worker_token_sha256": "0" * 64,
                    "recovery_token_sha256": "1" * 64,
                    "production_activation_enabled": True,
                    "kill_switch_enabled": False,
                    "execution_deadline_seconds": 300,
                }
            ),
            repository,
            adapter_ready=True,
            clock=NOW,
        )
        event = build_source_event(
            response_id="deadline-response-001",
            request_id="deadline-request-001",
            create_time="2026-08-30T01:00:00Z",
            form_alias="member_registration",
            mapping_version="member-intake.v1",
            payload={
                "name": "Deadline Synthetic",
                "phone": "81234567",
                "email": "deadline@example.test",
                "birthday_month": "June",
                "marketing_consent": "Yes",
                "pdpa_acknowledged": True,
            },
        )
        service.ingest(event)
        job = service.claim("worker-1")["job"]
        service.precheck(job["job_id"], "worker-1")
        candidate = service.allocation_candidate(job["job_id"])["candidate"]
        service.allocation_probe(
            job["job_id"],
            "worker-1",
            {
                "candidate": candidate,
                "status": "FREE",
                "probe_reference": "deadline-free-001",
            },
        )
        service.allocation_recheck(
            job["job_id"],
            "worker-1",
            {"status": "FREE", "probe_reference": "deadline-recheck-001"},
        )
        service.clock = NOW + timedelta(seconds=301)
        with self.assertRaises(ApiError):
            service.write_intent(
                job["job_id"],
                "worker-1",
                {
                    "operation": "member.create",
                    "member_no": candidate,
                    "payload_hash": job["payload_hash"],
                },
                principal_valid=True,
            )


if __name__ == "__main__":
    unittest.main()
