import json
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "member_gateway/src"))
IMPLEMENTATION_FILES = (
    *((ROOT / "member_gateway/src/xb_member_gateway").glob("*.py")),
    ROOT / "scripts/ac2_member_gateway_worker.ps1",
    ROOT / "scripts/ac2_member_gateway_worker_lib.ps1",
    ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1",
    ROOT / "n8n-workflows/member_forms_gateway_ingest.workflow.json",
    ROOT / "n8n-workflows/member_welcome_email_outbox.workflow.json",
    ROOT / "config/member_gateway.production.example.json",
    ROOT / "config/member_forms_gateway_bounded_import.v2.template.json",
    ROOT / "config/member_welcome_email_bounded_import.v1.template.json",
    ROOT / "member_gateway/migrations/0005_member_vertical_slice.sql",
)


class MemberGatewayPrivacyTests(unittest.TestCase):
    def test_safe_status_and_audit_surfaces_are_metadata_only(self):
        from xb_member_gateway.canonical import build_source_event
        from xb_member_gateway.repository import InMemoryRepository

        payload = {
            "name": "Privacy Synthetic Member",
            "phone": "81234567",
            "email": "privacy-synthetic@example.test",
            "birthday_month": "May",
            "marketing_consent": "No",
            "pdpa_acknowledged": True,
        }
        event = build_source_event(
            response_id="privacy-response-001",
            request_id="privacy-request-001",
            create_time="2026-08-30T01:00:00Z",
            form_alias="member_registration",
            mapping_version="member-intake.v1",
            payload=payload,
        )
        repository = InMemoryRepository()
        outcome = repository.ingest_source_event(
            __import__("xb_member_gateway.canonical", fromlist=["canonicalize_source_event"]).canonicalize_source_event(event),
            now=datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc),
        )
        safe = outcome.job.safe_dict()
        forbidden_keys = {
            "name",
            "phone",
            "email",
            "DOB",
            "MemberNo",
            "response_id",
            "member_payload",
        }
        self.assertTrue(forbidden_keys.isdisjoint(safe))
        self.assertTrue(safe["source_response_ref"].startswith("hmac-v1:"))
        for audit in repository.audit_events:
            self.assertTrue(
                set(audit).issubset({"event_type", "recorded_at", "job_id", "state", "operation", "error_code", "count"})
            )
            self.assertNotIn("privacy-response-001", json.dumps(audit))
            self.assertNotIn("81234567", json.dumps(audit))
            self.assertNotIn("privacy-synthetic@example.test", json.dumps(audit))

    def test_rejection_and_page_protocol_responses_are_pii_and_token_free(self):
        from xb_member_gateway.api import GatewayService
        from xb_member_gateway.canonical import canonical_json
        from xb_member_gateway.config import GatewayConfig
        from xb_member_gateway.crypto import payload_hash
        from xb_member_gateway.repository import InMemoryRepository

        repository = InMemoryRepository(source_cutover_watermark="2026-08-30T00:00:00Z")
        service = GatewayService(GatewayConfig(source_cutover_watermark="2026-08-30T00:00:00Z", source_production_cutover_exact="2026-08-30T00:00:00Z", source_admission_mode="first_member"), repository, adapter_ready=True)
        raw = {"name": "Privacy Reject Member", "phone": "privacy-bad-phone", "email": "privacy-reject@example.test", "birthday_month": "May", "marketing_consent": "No", "pdpa_acknowledged": "I agree"}
        rejection = {
            "schema_version": "xb.member.source_rejection.v1", "source_system": "google_forms", "form_alias": "member_registration",
            "response_id": "privacy-reject-response-001", "create_time": "2026-08-30T01:00:00.123Z", "mapping_version": "member-intake.v1",
            "request_id": "privacy-reject-request-001", "payload_hash": payload_hash(canonical_json(raw)), "error_code": "phone_contains_letters_or_unsupported_characters", "operation": "member.create",
        }
        epoch = service.begin_source_epoch({"form_alias": "member_registration", "mapping_version": "member-intake.v1"})["active_epoch"]
        opened = service.open_source_page(epoch["epoch_id"], {"expected_epoch_state_version": 0, "request_page_token": None, "next_page_token": "privacy-next-token", "terminal": False, "items": [{"response_id": rejection["response_id"], "create_time": rejection["create_time"], "payload_hash": rejection["payload_hash"]}]})
        rejected = service.reject_source(rejection)
        committed = service.commit_source_page(opened["page_id"], {"expected_epoch_state_version": opened["epoch_state_version"]})
        public = json.dumps([opened, rejected, repository.audit_events])
        for private in ("privacy-reject-response-001", "Privacy Reject Member", "privacy-reject@example.test", "privacy-bad-phone", "privacy-next-token"):
            self.assertNotIn(private, public)
        self.assertTrue(rejected["source_response_ref"].startswith("hmac-v1:"))
        stored = json.dumps(repository.snapshot_state()["_rejections"], default=str)
        for private in ("Privacy Reject Member", "privacy-reject@example.test", "privacy-bad-phone"):
            self.assertNotIn(private, stored)
        # The commit response returns the next opaque token only to the source
        # principal; it carries no response identity or customer value.
        self.assertEqual(set(committed) - {"current_page_token"}, {"schema_version", "page_id", "epoch_id", "page_state", "page_ordinal", "item_count", "terminal", "epoch_state_version", "epoch_status", "replayed"})

    def test_new_implementation_has_no_obvious_secret_or_private_value_literals(self):
        patterns = (
            re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
            re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
            re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}", re.IGNORECASE),
            re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
            re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
        )
        for path in IMPLEMENTATION_FILES:
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                self.assertIsNone(pattern.search(text), path.name)
        self.assertNotIn("Privacy Synthetic Member", "\n".join(path.read_text(encoding="utf-8") for path in IMPLEMENTATION_FILES))
        self.assertNotIn("privacy-synthetic@example.test", "\n".join(path.read_text(encoding="utf-8") for path in IMPLEMENTATION_FILES))

    def test_gateway_source_does_not_emit_general_logging_or_raw_headers(self):
        for path in ROOT.joinpath("member_gateway/src/xb_member_gateway").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("logging.basicConfig", text)
            self.assertNotIn("print(", text)
        api = (ROOT / "member_gateway/src/xb_member_gateway/api.py").read_text(encoding="utf-8")
        self.assertNotIn("headers[", api)
        self.assertIn("Request rejected; use trace_id for support.", api)


if __name__ == "__main__":
    unittest.main()
