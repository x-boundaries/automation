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
    ROOT / "config/member_gateway.production.example.json",
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
