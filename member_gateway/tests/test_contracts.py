import json
import unittest
from pathlib import Path

from xb_member_gateway.allocation import MemberNoAllocator
from xb_member_gateway.config import ConfigError, GatewayConfig
from xb_member_gateway.models import JobState
from xb_member_gateway.state_machine import ALLOWED_TRANSITIONS


ROOT = Path(__file__).resolve().parents[2]


class ContractSurfaceTests(unittest.TestCase):
    def read_json(self, relative):
        path = ROOT / relative
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def test_json_contracts_are_parseable_and_closed(self):
        for relative in (
            "schemas/member_gateway_source_event.v1.schema.json",
            "schemas/member_gateway_job.v1.schema.json",
            "schemas/member_gateway_job.v2.schema.json",
            "schemas/member_gateway_result.v1.schema.json",
            "schemas/member_gateway_error.v1.schema.json",
            "schemas/member_gateway_source_cursor.v1.schema.json",
            "schemas/member_gateway_operator_status.v1.schema.json",
            "schemas/member_gateway_operator_reconciliation.v1.schema.json",
            "config/member_gateway.production.example.json",
        ):
            value = self.read_json(relative)
            self.assertIsInstance(value, dict, relative)

        def assert_closed(node):
            if isinstance(node, dict):
                if "properties" in node:
                    self.assertIs(
                        node.get("additionalProperties"),
                        False,
                        "schema object must be closed",
                    )
                for child in node.values():
                    assert_closed(child)
            elif isinstance(node, list):
                for child in node:
                    assert_closed(child)

        for relative in (
            "schemas/member_gateway_source_event.v1.schema.json",
            "schemas/member_gateway_job.v1.schema.json",
            "schemas/member_gateway_job.v2.schema.json",
            "schemas/member_gateway_result.v1.schema.json",
            "schemas/member_gateway_error.v1.schema.json",
            "schemas/member_gateway_source_cursor.v1.schema.json",
            "schemas/member_gateway_operator_status.v1.schema.json",
            "schemas/member_gateway_operator_reconciliation.v1.schema.json",
        ):
            assert_closed(self.read_json(relative))

    def test_schema_constants_and_consent_contract(self):
        source = self.read_json("schemas/member_gateway_source_event.v1.schema.json")
        self.assertEqual(source["properties"]["source_system"]["const"], "google_forms")
        self.assertEqual(source["properties"]["operation"]["const"], "member.create")
        payload = source["properties"]["payload"]
        self.assertEqual(
            payload["properties"]["marketing_consent"]["enum"], ["Yes", "No"]
        )
        self.assertEqual(payload["properties"]["pdpa_acknowledged"]["type"], "boolean")

        result = self.read_json("schemas/member_gateway_result.v1.schema.json")
        self.assertEqual(result["properties"]["save_invocation_count"]["const"], 1)

    def test_production_example_fails_closed_by_default(self):
        config = self.read_json("config/member_gateway.production.example.json")
        self.assertFalse(config["production_activation_enabled"])
        self.assertTrue(config["kill_switch_enabled"])
        self.assertEqual(config["member_no_max_length"], 20)
        self.assertEqual(config["schema_version"], "xb.member.gateway.config.v2")
        self.assertEqual(config["initial_source_window_max"], 1)
        self.assertFalse(config["autocount_adapter_ready"])
        self.assertIsNone(config["source_cutover_watermark"])
        self.assertIsNone(config["worker_token_sha256"])
        self.assertIsNone(config["recovery_token_sha256"])
        loaded = GatewayConfig.from_mapping(config)
        self.assertFalse(loaded.gateway_ready)
        self.assertIn("source_cutover_watermark_required", loaded.readiness_reasons())
        self.assertIn("recovery_credential_digest_required", loaded.readiness_reasons())

    def test_writer_timing_order_is_strictly_nested(self):
        base = {
            "member_no_max_length": 20,
            "worker_token_sha256": "0" * 64,
            "recovery_token_sha256": "1" * 64,
            "production_activation_enabled": True,
            "kill_switch_enabled": False,
        }
        with self.assertRaisesRegex(ConfigError, "heartbeat_must_be_shorter_than_execution_deadline"):
            GatewayConfig.from_mapping({**base, "heartbeat_seconds": 300, "execution_deadline_seconds": 300})
        with self.assertRaisesRegex(ConfigError, "execution_deadline_exceeds_lease"):
            GatewayConfig.from_mapping({**base, "lease_seconds": 300, "execution_deadline_seconds": 300})

    def test_migration_has_durable_model_and_restrictive_history(self):
        first = (ROOT / "member_gateway/migrations/0001_member_gateway.sql").read_text(
            encoding="utf-8"
        )
        second = (
            ROOT / "member_gateway/migrations/0002_result_event_history.sql"
        ).read_text(encoding="utf-8")
        third = (ROOT / "member_gateway/migrations/0003_writer_termination_quarantine.sql").read_text(encoding="utf-8")
        fourth = (ROOT / "member_gateway/migrations/0004_forms_ingest_cursor.sql").read_text(encoding="utf-8")
        for table in (
            "source_responses",
            "source_observations",
            "ingest_receipts",
            "jobs",
            "attempts",
            "leases",
            "allocation_probes",
            "member_allocations",
            "write_intents",
            "dispatch_fences",
            "results",
            "result_conflicts",
            "reconciliation_cases",
            "reconciliation_checks",
            "dead_letters",
            "rejections",
            "control_flags",
            "audit_events",
            "schema_migrations",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS xb_member_gateway.{table}", first)
        self.assertNotIn("ON DELETE CASCADE", first.upper())
        self.assertIn("job_id text NOT NULL UNIQUE", first)
        self.assertIn("UNIQUE(job_id, attempt_number)", first)
        self.assertIn("member_no text NOT NULL UNIQUE", first)
        self.assertIn("CREATE TABLE IF NOT EXISTS xb_member_gateway.result_events", second)
        self.assertIn("save_invocation_count = 1", second)
        self.assertIn("WRITER_TERMINATION_UNCONFIRMED", third)
        self.assertIn("writer_termination_gate", third)
        self.assertIn("writer_execution_holds", third)
        self.assertIn("lifecycle IN", third)
        self.assertIn("legacy_unproven", third)
        self.assertIn("ADD COLUMN IF NOT EXISTS recheck_id", third)
        self.assertNotIn("ON DELETE CASCADE", third.upper())
        self.assertIn("source_ingest_cursors", fourth)
        self.assertIn("source_ingest_page_receipts", fourth)
        self.assertIn("initial_window_admission_count", fourth)
        self.assertIn("protect_source_ingest_cursor_identity", fourth)
        self.assertIn("source_ingest_cursor_identity_immutable", fourth)
        self.assertIn("reject_source_page_receipt_mutation", fourth)
        self.assertNotIn("INSERT INTO xb_member_gateway.source_ingest_cursors", fourth)

    def test_state_machine_contains_all_required_states_and_blocks_requeue(self):
        required = {
            "RECEIVED",
            "VALIDATED",
            "QUEUED",
            "LEASED",
            "PRECHECKING",
            "ALLOCATION_BOUND",
            "WRITE_INTENT_RECORDED",
            "WRITING",
            "READBACK",
            "CREATED_VERIFIED",
            "REJECTED_VALIDATION",
            "RETRY_WAIT",
            "AMBIGUOUS_LOOKUP",
            "WRITE_OUTCOME_UNCERTAIN",
            "WRITER_TERMINATION_UNCONFIRMED",
            "CONFIRMED_NOT_CREATED",
            "CREATED_READBACK_MISMATCH",
            "MANUAL_REVIEW",
            "DEAD_LETTER",
        }
        self.assertTrue(required.issubset({state.value for state in JobState}))
        self.assertNotIn(JobState.QUEUED, ALLOWED_TRANSITIONS[JobState.WRITING])
        self.assertNotIn(JobState.QUEUED, ALLOWED_TRANSITIONS[JobState.WRITE_OUTCOME_UNCERTAIN])
        self.assertNotIn(JobState.WRITING, ALLOWED_TRANSITIONS[JobState.WRITER_TERMINATION_UNCONFIRMED])
        self.assertNotIn(JobState.CREATED_VERIFIED, ALLOWED_TRANSITIONS[JobState.WRITER_TERMINATION_UNCONFIRMED])
        self.assertNotIn(JobState.CONFIRMED_NOT_CREATED, ALLOWED_TRANSITIONS[JobState.WRITER_TERMINATION_UNCONFIRMED])

    def test_allocator_rejects_truncation_and_uses_only_deterministic_suffixes(self):
        allocator = MemberNoAllocator(12)
        values = list(allocator.candidates("6581234567"))
        self.assertEqual(values[0], "6581234567")
        self.assertEqual(values[1], "6581234567X1")
        self.assertEqual(values[-1], "6581234567X9")
        self.assertNotIn("6581234567X10", values)
        self.assertFalse(allocator.validate_candidate("6581234567", "658123456"))
        self.assertFalse(allocator.validate_candidate("6581234567", "6581234567-1"))
        self.assertFalse(allocator.validate_candidate("6581234567", "6581234567X01"))

        production_allocator = MemberNoAllocator(20)
        self.assertFalse(
            production_allocator.validate_candidate("1" * 20, "1" * 21)
        )

    def test_governed_mapping_surfaces_do_not_bind_unused_udfs(self):
        governed = (
            ROOT / "member_gateway/src/xb_member_gateway/canonical.py",
            ROOT / "member_gateway/src/xb_member_gateway/models.py",
            ROOT / "member_gateway/src/xb_member_gateway/repository.py",
            ROOT / "config/member_gateway.production.example.json",
            ROOT / "n8n-workflows/member_forms_gateway_ingest.workflow.json",
        )
        text = "\n".join(path.read_text(encoding="utf-8") for path in governed)
        self.assertNotIn("UDF_CustId", text)
        self.assertNotIn("UDF_MemberId", text)


if __name__ == "__main__":
    unittest.main()
