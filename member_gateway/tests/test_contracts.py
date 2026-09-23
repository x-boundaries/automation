import json
import unittest
from pathlib import Path

from xb_member_gateway.allocation import AllocationError, MemberNoAllocator
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
        self.assertIsNone(config["source_production_cutover_exact"])
        self.assertEqual(config["source_admission_mode"], "first_member")
        self.assertIsNone(config["worker_token_sha256"])
        self.assertIsNone(config["recovery_token_sha256"])
        self.assertIsNone(config["mailer_token_sha256"])
        self.assertEqual(config["mailer_token_env"], "XB_MEMBER_GATEWAY_MAILER_TOKEN")
        loaded = GatewayConfig.from_mapping(config, require_complete=True)
        self.assertFalse(loaded.gateway_ready)
        self.assertEqual(loaded.initial_window_max, 1)
        self.assertIn("source_cutover_watermark_required", loaded.readiness_reasons())
        self.assertIn("source_production_cutover_exact_required", loaded.readiness_reasons())
        self.assertIn("recovery_credential_digest_required", loaded.readiness_reasons())
        self.assertIn("mailer_credential_digest_required", loaded.readiness_reasons())

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
        fifth = (ROOT / "member_gateway/migrations/0005_member_vertical_slice.sql").read_text(encoding="utf-8")
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
        for table in (
            "source_rejections", "source_handling_receipts", "source_scan_epochs",
            "source_scan_pages", "source_scan_page_items", "welcome_email_outbox", "welcome_email_events",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS xb_member_gateway.{table}", fifth)
        for required in (
            "ADD COLUMN IF NOT EXISTS create_time_exact", "ADD COLUMN IF NOT EXISTS production_cutover_exact",
            "source_scan_epochs_one_active_idx", "WHERE status = 'ACTIVE'",
            "source_scan_pages_next_token_per_epoch_idx", "ON xb_member_gateway.source_scan_pages(epoch_id, next_page_token)",
            "REFERENCES xb_member_gateway.source_handling_receipts(response_id)",
            "restart_reason IN ('token_invalidated', 'ambiguous_crashed_attempt')",
            "UNIQUE (response_id, template_id)", "job_id text NOT NULL UNIQUE",
            "welcome_email_requires_created_verified", "welcome_email_transition_forbidden",
            "source_production_cutover_immutable", "('0005_member_vertical_slice')",
            "(\\.\\d{3}|\\.\\d{6}|\\.\\d{9})?Z$",
        ):
            self.assertIn(required, fifth, required)
        # Additive only: no destructive statement and no seeded production value.
        for forbidden in ("DROP TABLE", "DROP COLUMN", "DELETE FROM", "TRUNCATE", "ON DELETE CASCADE", "INSERT INTO xb_member_gateway.source_ingest_cursors"):
            self.assertNotIn(forbidden, fifth.upper() if forbidden.isupper() else fifth)
        for earlier in (first, second, third, fourth):
            self.assertNotIn("0005_member_vertical_slice", earlier)

    def test_cursor_v1_is_retained_and_v2_new_schemas_bind_the_contract(self):
        v1 = self.read_json("schemas/member_gateway_source_cursor.v1.schema.json")
        self.assertEqual(v1["properties"]["schema_version"]["const"], "xb.member.gateway.source_cursor.v1")
        v2 = self.read_json("schemas/member_gateway_source_cursor.v2.schema.json")
        self.assertEqual(v2["properties"]["schema_version"]["const"], "xb.member.gateway.source_cursor.v2")
        self.assertFalse(v2["additionalProperties"])
        for deprecated in ("last_admitted_create_time", "last_admitted_response_id", "resume_page_token", "watermark", "scan_lower_bound"):
            self.assertNotIn(deprecated, v2["properties"])
        self.assertEqual(v2["properties"]["page_size"]["const"], 1)
        self.assertEqual(v2["properties"]["active_epoch"]["properties"]["restart_reason"]["enum"], [None, "token_invalidated", "ambiguous_crashed_attempt"])
        rejection = self.read_json("schemas/member_gateway_source_rejection.v1.schema.json")
        self.assertTrue({"name", "phone", "email", "payload"}.isdisjoint(rejection["properties"]))
        from xb_member_gateway.canonical import CUSTOMER_REJECTION_CODES
        self.assertEqual(set(rejection["properties"]["error_code"]["enum"]), CUSTOMER_REJECTION_CODES)
        job = self.read_json("schemas/member_gateway_welcome_email_job.v1.schema.json")
        message = job["properties"]["message"]["properties"]
        from xb_member_gateway.notifications import WELCOME_V1
        for field, value in WELCOME_V1.items():
            if value is None:
                self.assertEqual(message[field], {"type": "null"})
            else:
                self.assertEqual(message[field]["const"], value)
        self.assertNotIn("response_id", job["properties"])
        result = self.read_json("schemas/member_gateway_welcome_email_result.v1.schema.json")
        self.assertEqual(result["properties"]["outcome"]["enum"], ["smtp_accepted", "delivery_outcome_uncertain", "failed_before_send_intent"])
        import re
        exact = re.compile(v2["properties"]["production_cutover_exact"]["pattern"])
        for accepted in ("2026-09-15T00:00:00Z", "2026-09-15T00:00:00.123Z", "2026-09-15T00:00:00.123456Z", "2026-09-15T00:00:00.123456789Z"):
            self.assertRegex(accepted, exact)
        for rejected in ("2026-09-15T00:00:00.1Z", "2026-09-15T00:00:00+00:00", "2026-09-15T00:00:00.1234567Z"):
            self.assertNotRegex(rejected, exact)

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

    def test_schemas_bind_the_opaque_digit_phone_and_total_member_no_length(self):
        phone_pattern = "^[0-9]{6,15}$"
        member_no_pattern = "^[0-9]{6,15}(?:X[1-9][0-9]*)?$"
        for relative in (
            "schemas/member_gateway_source_event.v1.schema.json",
            "schemas/member_gateway_job.v1.schema.json",
            "schemas/member_gateway_job.v2.schema.json",
        ):
            with self.subTest(schema=relative):
                schema = self.read_json(relative)
                payload = schema["properties"].get("payload") or schema["properties"]["member_payload"]
                self.assertEqual(payload["properties"]["phone"]["pattern"], phone_pattern)

        for relative, pointer in (
            ("schemas/member_gateway_job.v1.schema.json", ("allocation",)),
            ("schemas/member_gateway_job.v2.schema.json", ("allocation",)),
            ("schemas/member_gateway_result.v1.schema.json", ()),
        ):
            with self.subTest(schema=relative):
                node = self.read_json(relative)["properties"]
                for key in pointer:
                    node = node[key]["properties"]
                member_no = node["member_no"]
                self.assertEqual(member_no["pattern"], member_no_pattern)
                # The suffix is deliberately uncapped in the pattern; total length
                # is what the allocator and AutoCount actually constrain.
                self.assertEqual(member_no["maxLength"], 20)

    def test_fifteen_digit_base_keeps_the_full_production_allocation_horizon(self):
        allocator = MemberNoAllocator(20)
        base = "1" * 15
        candidates = allocator.candidates(base)
        self.assertEqual(next(candidates), base)
        self.assertEqual(next(candidates), base + "X1")
        self.assertTrue(allocator.validate_candidate(base, base + "X9999"))
        self.assertEqual(len(base + "X9999"), 20)
        # X10000 would be 21 characters, so the length-derived generator stops
        # exactly at the worker's existing 10,000-probe horizon.
        self.assertFalse(allocator.validate_candidate(base, base + "X10000"))

    def test_allocator_accepts_the_opaque_range_and_rejects_outside_it(self):
        allocator = MemberNoAllocator(20)
        for base in ("1" * 6, "91234567", "6591234567", "1" * 15):
            with self.subTest(base=base):
                self.assertEqual(next(allocator.candidates(base)), base)
        for base in ("1" * 5, "1" * 16, "65912345a7", "+6591234567"):
            with self.subTest(base=base):
                with self.assertRaises(AllocationError):
                    next(allocator.candidates(base))

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
