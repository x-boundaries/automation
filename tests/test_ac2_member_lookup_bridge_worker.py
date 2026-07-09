import json
import base64
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_lookup_bridge_worker.py"
GATE3B_PREP_SCRIPT = ROOT / "scripts" / "member_lookup_gate3b_prepare_local_queue.py"
GATE3B_SUMMARY_SCRIPT = ROOT / "scripts" / "member_lookup_gate3b_evidence_summary.py"
GATE3C_RUNTIME_SCRIPT = ROOT / "scripts" / "member_lookup_gate3c_local_bridge_runtime.py"
GATE3D_PREP_SCRIPT = ROOT / "scripts" / "member_lookup_gate3d_prepare_small_batch.py"
GATE3D_SMALL_BATCH_SCRIPT = ROOT / "scripts" / "member_lookup_gate3d_local_bridge_small_batch.py"
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"
DOCS = ROOT / "docs" / "autocount2-automation"
BRIDGE_RUNBOOK = DOCS / "member_intake_local_lookup_bridge_runbook.md"
NODE_CONTRACT = DOCS / "member_intake_n8n_node_contract.md"
BRIDGE_DESIGN = DOCS / "member_intake_local_bridge_design.md"
UAT_PLAN = DOCS / "member_intake_n8n_lookup_bridge_uat_plan.md"


ENCODED_SYNTHETIC_VALUE = base64.b64encode(
    bytes([70, 73, 88, 84, 85, 82, 69])
).decode("ascii")
ALLOWED_QUEUE_FIELDS = {
    "job_id",
    "intake_source",
    "source_reference",
    "source_row_ref",
    "row_number",
    "intake_id",
    "state",
    "submitted_member_no_base64_utf8",
    "consent_status",
    "pdpa_status",
    "payload_hash",
    "attempt",
    "max_attempts",
    "created_at",
    "updated_at",
    "lease_owner",
    "lease_expires_at",
    "timeout_at",
    "last_error_code",
}
ALLOWED_RESULT_FIELDS = {
    "job_id",
    "intake_source",
    "source_reference",
    "source_row_ref",
    "row_number",
    "state",
    "status",
    "authentication_success",
    "user_session_available",
    "member_command_found",
    "get_member_found",
    "submitted_member_no_status",
    "normalized_member_no_length",
    "member_exists",
    "member_found_by",
    "manual_review_required",
    "warning_count",
    "error_code",
    "consent_status",
    "pdpa_status",
    "attempt",
    "dry_run_only",
    "final_write_automation",
    "result_created_at",
    "result_applied_at",
}
FORBIDDEN_WRITE_TOKENS = [
    "Save" + "Member",
    "New" + "Member",
    "Delete" + "Member",
    "GetNext" + "MemberNo",
]


def fixture_job(**overrides):
    job = {
        "job_id": "job-synthetic-001",
        "intake_source": "google_sheets_uat",
        "source_reference": "uat-queue-row-002",
        "source_row_ref": "row-002",
        "row_number": 2,
        "intake_id": "intake-synthetic-001",
        "state": "PENDING_LOOKUP",
        "submitted_member_no_base64_utf8": ENCODED_SYNTHETIC_VALUE,
        "consent_status": "acknowledged",
        "pdpa_status": "yes",
        "attempt": 0,
        "max_attempts": 3,
        "payload_hash": "hash-synthetic",
        "created_at": "fixture-created-at",
        "updated_at": "fixture-updated-at",
        "lease_owner": "fixture-bridge",
        "lease_expires_at": "fixture-lease-expires-at",
        "timeout_at": "fixture-timeout-at",
        "last_error_code": None,
    }
    job.update(overrides)
    return job


def mock_result(job_id, **overrides):
    row = {
        "job_id": job_id,
        "status": "ok",
        "authentication_success": True,
        "user_session_available": True,
        "member_command_found": True,
        "get_member_found": True,
        "submitted_member_no_status": "canonical_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": False,
        "member_found_by": None,
        "manual_review_required": False,
        "warning_count": 0,
    }
    row.update(overrides)
    return row


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_fake_powershell_lookup(path):
    lookup_json = json.dumps(
        {
            "status": "ok",
            "authentication_success": True,
            "user_session_available": True,
            "member_command_found": True,
            "get_member_found": True,
            "submitted_member_no_status": "canonical_65_mobile",
            "normalized_member_no_length": 10,
            "member_exists": False,
            "member_found_by": None,
            "manual_review_required": False,
            "warning_count": 0,
            "error": None,
        },
        sort_keys=True,
    )
    path.write_text(f"@echo off\r\necho {lookup_json}\r\n", encoding="utf-8")


def parse_key_value_evidence(text):
    evidence = {}
    for line in text.splitlines():
        key, value = line.split(" = ", 1)
        evidence[key] = value
    return evidence


class BridgeWorkerCliTests(unittest.TestCase):
    def run_cli(self, arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + arguments,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_gate3b_prep(self, arguments, *, input_text=None):
        return subprocess.run(
            [sys.executable, str(GATE3B_PREP_SCRIPT)] + arguments,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_gate3b_summary(self, arguments):
        return subprocess.run(
            [sys.executable, str(GATE3B_SUMMARY_SCRIPT)] + arguments,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_gate3c_runtime(self, arguments):
        return subprocess.run(
            [sys.executable, str(GATE3C_RUNTIME_SCRIPT)] + arguments,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_gate3d_prep(self, arguments, *, input_text=None):
        return subprocess.run(
            [sys.executable, str(GATE3D_PREP_SCRIPT)] + arguments,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_gate3d_small_batch(self, arguments):
        return subprocess.run(
            [sys.executable, str(GATE3D_SMALL_BATCH_SCRIPT)] + arguments,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_invocation_refuses_before_processing_any_queue(self):
        completed = self.run_cli([])

        self.assertEqual(completed.returncode, 2)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "refused")
        self.assertEqual(summary["error_code"], "explicit_opt_in_required")
        self.assertTrue(summary["dry_run_only"])
        self.assertFalse(summary["final_write_automation"])

    def test_fixture_mock_mode_writes_sanitized_result_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            mock_results_path = tmp_path / "mock_results.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            write_jsonl(
                jobs_path,
                [
                    fixture_job(job_id="job-ready", row_number=2),
                    fixture_job(job_id="job-existing", row_number=3),
                    fixture_job(job_id="job-manual", row_number=4),
                    fixture_job(job_id="job-error", row_number=5),
                ],
            )
            write_jsonl(
                mock_results_path,
                [
                    mock_result("job-existing", member_exists=True),
                    mock_result("job-manual", manual_review_required=True, warning_count=1),
                    mock_result("job-error", status="error", error_code="mock_error"),
                ],
            )

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--fixture-mock-results",
                    str(mock_results_path),
                    "--results-jsonl",
                    str(results_path),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["lookup_mode"], "mock")
            self.assertEqual(summary["processed_count"], 4)
            self.assertEqual(summary["result_state_counts"]["READY_FOR_CREATE_REVIEW"], 1)
            self.assertEqual(summary["result_state_counts"]["EXISTING_MEMBER_REVIEW"], 1)
            self.assertEqual(summary["result_state_counts"]["MANUAL_REVIEW_REQUIRED"], 1)
            self.assertEqual(summary["result_state_counts"]["LOOKUP_ERROR_REVIEW"], 1)

            results = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({result["state"] for result in results}, set(summary["result_state_counts"]))
            for result in results:
                self.assertEqual(set(result), ALLOWED_RESULT_FIELDS)
                self.assertTrue(result["dry_run_only"])
                self.assertFalse(result["final_write_automation"])
                self.assertIn("job_id", result)
                self.assertEqual(result["intake_source"], "google_sheets_uat")
                self.assertIn("source_reference", result)
                self.assertIn("row_number", result)
                self.assertIn("normalized_member_no_length", result)
                self.assertIn("result_created_at", result)
                self.assertIsNone(result["result_applied_at"])
                self.assertNotIn("submitted_member_no_base64_utf8", result)
                self.assertNotIn(ENCODED_SYNTHETIC_VALUE, json.dumps(result, sort_keys=True))

    def test_fixture_queue_rows_match_uat_lookup_queue_contract(self):
        job = fixture_job()

        self.assertEqual(set(job), ALLOWED_QUEUE_FIELDS)
        self.assertNotIn("mock_member_exists", job)
        self.assertNotIn("mock_status", job)
        self.assertNotIn("raw_phone_number", job)
        self.assertNotIn("normalized_member_no", job)

    def test_fixture_queue_rows_are_source_agnostic_beyond_google_sheets_uat(self):
        job = fixture_job(
            intake_source="hosted_intake_api_uat",
            source_reference="api-submission-fixture-001",
            source_row_ref=None,
            row_number=None,
        )

        self.assertEqual(set(job), ALLOWED_QUEUE_FIELDS)
        self.assertEqual(job["intake_source"], "hosted_intake_api_uat")
        self.assertEqual(job["source_reference"], "api-submission-fixture-001")
        self.assertIsNone(job["source_row_ref"])
        self.assertIsNone(job["row_number"])

    def test_valid_pdpa_status_is_normalized_yes_not_old_i_agree_label(self):
        source = SCRIPT.read_text(encoding="utf-8")
        job = fixture_job()

        self.assertEqual(job["pdpa_status"], "yes")
        self.assertIn('ALLOWED_PDPA_STATUS_LABELS = {"yes"}', source)


    def test_fixture_job_with_imported_pdpa_status_routes_to_lookup_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            write_jsonl(
                jobs_path,
                [fixture_job(job_id="job-imported-pdpa", consent_status="imported", pdpa_status="imported")],
            )

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--results-jsonl",
                    str(results_path),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result_text = results_path.read_text(encoding="utf-8")
            result = json.loads(result_text)
            self.assertEqual(result["state"], "LOOKUP_ERROR_REVIEW")
            self.assertEqual(result["error_code"], "request_or_lookup_contract_error")
            self.assertNotIn(ENCODED_SYNTHETIC_VALUE, result_text)

    def test_imported_pdpa_status_is_blocked_even_when_consent_status_is_acknowledged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            write_jsonl(
                jobs_path,
                [
                    fixture_job(
                        job_id="job-pdpa-imported-consent-ack",
                        consent_status="acknowledged",
                        pdpa_status="imported",
                    )
                ],
            )

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--results-jsonl",
                    str(results_path),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result_text = results_path.read_text(encoding="utf-8")
            result = json.loads(result_text)
            self.assertEqual(result["state"], "LOOKUP_ERROR_REVIEW")
            self.assertEqual(result["error_code"], "request_or_lookup_contract_error")
            self.assertNotIn(ENCODED_SYNTHETIC_VALUE, result_text)

    def test_valid_pdpa_status_allows_separate_imported_consent_category_in_review_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            write_jsonl(
                jobs_path,
                [fixture_job(job_id="job-pdpa-valid-consent-imported", consent_status="imported")],
            )

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--results-jsonl",
                    str(results_path),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result_text = results_path.read_text(encoding="utf-8")
            result = json.loads(result_text)
            self.assertEqual(result["state"], "READY_FOR_CREATE_REVIEW")
            self.assertEqual(result["pdpa_status"], "yes")
            self.assertEqual(result["consent_status"], "imported")
            self.assertTrue(result["dry_run_only"])
            self.assertFalse(result["final_write_automation"])
            self.assertNotIn(ENCODED_SYNTHETIC_VALUE, result_text)

    def test_fixture_job_with_forbidden_field_routes_to_lookup_error_review_without_echoing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            write_jsonl(
                jobs_path,
                [
                    fixture_job(
                        job_id="job-forbidden",
                        email_address="redacted-fixture-value",
                    )
                ],
            )

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--results-jsonl",
                    str(results_path),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result_text = results_path.read_text(encoding="utf-8")
            result = json.loads(result_text)
            self.assertEqual(result["state"], "LOOKUP_ERROR_REVIEW")
            self.assertEqual(set(result), ALLOWED_RESULT_FIELDS)
            self.assertEqual(result["error_code"], "request_or_lookup_contract_error")
            self.assertNotIn("redacted-fixture-value", result_text)
            self.assertNotIn(ENCODED_SYNTHETIC_VALUE, result_text)

    def test_fixture_job_with_mock_control_field_is_rejected_as_queue_contract_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            write_jsonl(jobs_path, [fixture_job(job_id="job-bad-mock", mock_member_exists=True)])

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--results-jsonl",
                    str(results_path),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result_text = results_path.read_text(encoding="utf-8")
            result = json.loads(result_text)
            self.assertEqual(result["state"], "LOOKUP_ERROR_REVIEW")
            self.assertEqual(result["error_code"], "request_or_lookup_contract_error")
            self.assertNotIn("mock_member_exists", result_text)

    def test_invalid_base64_shape_routes_to_lookup_error_without_echoing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            invalid_encoded_value = "not_base64!"
            write_jsonl(
                jobs_path,
                [fixture_job(job_id="job-invalid-base64", submitted_member_no_base64_utf8=invalid_encoded_value)],
            )

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--results-jsonl",
                    str(results_path),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result_text = results_path.read_text(encoding="utf-8")
            result = json.loads(result_text)
            self.assertEqual(result["state"], "LOOKUP_ERROR_REVIEW")
            self.assertEqual(result["error_code"], "request_or_lookup_contract_error")
            self.assertNotIn(invalid_encoded_value, result_text)

    def test_mock_result_with_unbounded_status_label_is_rejected_without_echoing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            mock_results_path = tmp_path / "mock_results.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            unsafe_label = "unsafe-fixture-label"
            write_jsonl(jobs_path, [fixture_job(job_id="job-unsafe-label")])
            write_jsonl(
                mock_results_path,
                [mock_result("job-unsafe-label", submitted_member_no_status=unsafe_label)],
            )

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--fixture-mock-results",
                    str(mock_results_path),
                    "--results-jsonl",
                    str(results_path),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result_text = results_path.read_text(encoding="utf-8")
            result = json.loads(result_text)
            self.assertEqual(result["state"], "LOOKUP_ERROR_REVIEW")
            self.assertEqual(result["error_code"], "request_or_lookup_contract_error")
            self.assertNotIn(unsafe_label, result_text)

    def test_powershell_lookup_mode_requires_second_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jobs_path = tmp_path / "jobs.jsonl"
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            write_jsonl(jobs_path, [fixture_job()])

            completed = self.run_cli(
                [
                    "--enable-local-lookup-bridge-review",
                    "--queue-mode",
                    "fixture",
                    "--fixture-jobs",
                    str(jobs_path),
                    "--results-jsonl",
                    str(results_path),
                    "--lookup-mode",
                    "powershell",
                ]
            )

            self.assertEqual(completed.returncode, 2)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["error_code"], "powershell_lookup_opt_in_required")
            self.assertFalse(results_path.exists())

    def test_gate3b_queue_helper_writes_one_row_and_prints_no_member_values(self):
        raw_member_value = "MEMBER-FIXTURE-VALUE"
        encoded_member_value = base64.b64encode(raw_member_value.encode("utf-8")).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "member_lookup_bridge_gate3b_pending_queue.jsonl"

            completed = self.run_gate3b_prep(
                [
                    "--member-value-stdin",
                    "--queue-jsonl",
                    str(queue_path),
                ],
                input_text=raw_member_value,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["gate"], "gate3b_ac2_local_bridge_readiness_queue_prep")
            self.assertTrue(summary["queue_file_written"])
            self.assertEqual(summary["queue_row_count"], 1)
            self.assertEqual(summary["encoded_present_count"], 1)
            self.assertTrue(summary["no_row_values_printed"])
            self.assertFalse(summary["n8n_required"])
            self.assertFalse(summary["google_sheets_required"])
            self.assertTrue(summary["dry_run_only"])
            self.assertFalse(summary["final_write_automation"])
            self.assertNotIn(raw_member_value, completed.stdout)
            self.assertNotIn(encoded_member_value, completed.stdout)
            self.assertEqual(completed.stderr, "")

            rows = [json.loads(line) for line in queue_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(set(row), ALLOWED_QUEUE_FIELDS)
            self.assertEqual(row["state"], "PENDING_LOOKUP")
            self.assertEqual(row["intake_source"], "ac2_local_gate3b")
            self.assertEqual(row["row_number"], 1)
            self.assertEqual(row["pdpa_status"], "yes")
            self.assertEqual(row["submitted_member_no_base64_utf8"], encoded_member_value)
            self.assertNotIn(raw_member_value, queue_path.read_text(encoding="utf-8"))

    def test_gate3b_queue_helper_error_output_is_aggregate_only(self):
        raw_member_value = "MEMBER-FIXTURE-VALUE"

        completed = self.run_gate3b_prep(
            [
                "--member-value-stdin",
                "--queue-jsonl",
                "",
            ],
            input_text=raw_member_value,
        )

        self.assertEqual(completed.returncode, 2)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "error")
        self.assertFalse(summary["queue_file_written"])
        self.assertEqual(summary["queue_row_count"], 0)
        self.assertEqual(summary["encoded_present_count"], 0)
        self.assertTrue(summary["no_row_values_printed"])
        self.assertNotIn(raw_member_value, completed.stdout)

    def test_gate3b_summary_prints_local_bridge_readiness_evidence_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "member_lookup_bridge_gate3b_results.jsonl"
            write_jsonl(
                results_path,
                [
                    result_envelope := {
                        "job_id": "gate3b-local-safe",
                        "intake_source": "ac2_local_gate3b",
                        "source_reference": "gate3b-local-one-row",
                        "source_row_ref": "local-one-row",
                        "row_number": 1,
                        "state": "MANUAL_REVIEW_REQUIRED",
                        "status": "ok",
                        "authentication_success": True,
                        "user_session_available": True,
                        "member_command_found": True,
                        "get_member_found": True,
                        "submitted_member_no_status": "manual_review",
                        "normalized_member_no_length": 0,
                        "member_exists": False,
                        "member_found_by": None,
                        "manual_review_required": True,
                        "warning_count": 1,
                        "error_code": None,
                        "consent_status": "operator_supplied_test",
                        "pdpa_status": "yes",
                        "attempt": 0,
                        "dry_run_only": True,
                        "final_write_automation": False,
                        "result_created_at": "fixture-time",
                        "result_applied_at": None,
                    }
                ],
            )
            self.assertEqual(set(result_envelope), ALLOWED_RESULT_FIELDS)

            completed = self.run_gate3b_summary(
                [
                    "--results-jsonl",
                    str(results_path),
                    "--local-queue-row-count",
                    "1",
                    "--local-queue-rows-loaded-count",
                    "1",
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = {}
            for line in completed.stdout.splitlines():
                key, value = line.split(" = ", 1)
                evidence[key] = value
            self.assertEqual(evidence["status"], "ok")
            self.assertEqual(evidence["gate"], "gate3b_ac2_local_bridge_readiness")
            self.assertEqual(evidence["runtime_location"], "windows_ac2_bridge_host_only")
            self.assertEqual(evidence["execution_mode"], "manual_local_one_row_read_only_lookup")
            self.assertEqual(evidence["local_queue_row_count"], "1")
            self.assertEqual(evidence["local_queue_rows_loaded_count"], "1")
            self.assertEqual(evidence["lookup_attempt_count"], "1")
            self.assertEqual(evidence["lookup_success_count"], "1")
            self.assertEqual(evidence["lookup_manual_review_count"], "1")
            self.assertEqual(evidence["lookup_error_count"], "0")
            self.assertEqual(evidence["n8n_required"], "false")
            self.assertEqual(evidence["n8n_involved"], "false")
            self.assertEqual(evidence["google_sheets_required"], "false")
            self.assertEqual(evidence["hosted_or_vps_service_called"], "false")
            self.assertEqual(evidence["scheduler_enabled"], "false")
            self.assertEqual(evidence["public_inbound_to_ac2_host"], "false")
            self.assertEqual(evidence["member_create_or_update_invoked"], "false")
            self.assertEqual(evidence["autocount_write_attempted"], "false")
            self.assertEqual(evidence["direct_sql_write_attempted"], "false")
            self.assertEqual(evidence["final_write_automation"], "false")
            self.assertEqual(evidence["no_row_values_printed"], "true")

            for forbidden in [
                "job_id",
                "gate3b-local-safe",
                "source_reference",
                "source_row_ref",
                "row_number",
                "submitted_member_no_base64_utf8",
                "fixture-time",
            ]:
                self.assertNotIn(forbidden, completed.stdout)

    def test_gate3b_summary_marks_needs_fix_for_count_mismatch_or_lookup_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "member_lookup_bridge_gate3b_results.jsonl"
            write_jsonl(
                results_path,
                [
                    {
                        "state": "LOOKUP_ERROR_REVIEW",
                        "status": "error",
                        "dry_run_only": True,
                        "final_write_automation": False,
                    }
                ],
            )

            completed = self.run_gate3b_summary(
                [
                    "--results-jsonl",
                    str(results_path),
                    "--local-queue-row-count",
                    "1",
                    "--local-queue-rows-loaded-count",
                    "0",
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = {}
            for line in completed.stdout.splitlines():
                key, value = line.split(" = ", 1)
                evidence[key] = value
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["lookup_error_count"], "1")
            self.assertEqual(evidence["direct_sql_write_attempted"], "false")

    def test_gate3c_runtime_processes_once_and_repeated_run_counts_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pending_path = tmp_path / "member_lookup_bridge_gate3c_pending_queue.jsonl"
            mock_results_path = tmp_path / "member_lookup_bridge_mock_results.jsonl"
            results_path = tmp_path / "member_lookup_bridge_gate3c_results.jsonl"
            processed_dir = tmp_path / "member_lookup_bridge_gate3c_processed"
            failed_dir = tmp_path / "member_lookup_bridge_gate3c_failed"
            write_jsonl(
                pending_path,
                [
                    fixture_job(job_id="gate3c-ready", intake_source="ac2_local_gate3c", payload_hash="hash-ready"),
                    fixture_job(
                        job_id="gate3c-existing",
                        intake_source="ac2_local_gate3c",
                        source_reference="gate3c-existing-ref",
                        payload_hash="hash-existing",
                    ),
                ],
            )
            write_jsonl(mock_results_path, [mock_result("gate3c-existing", member_exists=True)])
            base_args = [
                "--enable-local-bridge-runtime-review",
                "--pending-jsonl",
                str(pending_path),
                "--results-jsonl",
                str(results_path),
                "--processed-dir",
                str(processed_dir),
                "--failed-dir",
                str(failed_dir),
                "--lookup-mode",
                "mock",
                "--fixture-mock-results",
                str(mock_results_path),
            ]

            first = self.run_gate3c_runtime(base_args)

            self.assertEqual(first.returncode, 0, first.stderr)
            first_evidence = parse_key_value_evidence(first.stdout)
            self.assertEqual(first_evidence["status"], "dry_run_only")
            self.assertEqual(first_evidence["gate"], "gate3c_ac2_local_bridge_runtime_hardening")
            self.assertEqual(first_evidence["runtime_location"], "windows_ac2_bridge_host_only")
            self.assertEqual(first_evidence["execution_mode"], "manual_local_filesystem_runtime_hardening")
            self.assertEqual(first_evidence["lookup_mode"], "mock")
            self.assertEqual(first_evidence["powershell_lookup_enabled"], "false")
            self.assertEqual(first_evidence["pending_rows_loaded_count"], "2")
            self.assertEqual(first_evidence["lookup_attempt_count"], "2")
            self.assertEqual(first_evidence["lookup_success_count"], "2")
            self.assertEqual(first_evidence["lookup_error_count"], "0")
            self.assertEqual(first_evidence["processed_or_archived_count"], "2")
            self.assertEqual(first_evidence["failed_or_dead_letter_count"], "0")
            self.assertEqual(first_evidence["duplicate_or_already_processed_count"], "0")
            self.assertEqual(first_evidence["member_create_or_update_invoked"], "false")
            self.assertEqual(first_evidence["autocount_write_attempted"], "false")
            self.assertEqual(first_evidence["direct_sql_write_attempted"], "false")
            self.assertEqual(first_evidence["final_write_automation"], "false")
            self.assertEqual(first_evidence["n8n_required"], "false")
            self.assertEqual(first_evidence["google_sheets_required"], "false")
            self.assertEqual(first_evidence["hosted_or_vps_service_called"], "false")
            self.assertEqual(first_evidence["scheduler_enabled"], "false")
            self.assertEqual(first_evidence["public_inbound_to_ac2_host"], "false")
            self.assertEqual(first_evidence["no_row_values_printed"], "true")

            result_rows = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(result_rows), 2)
            self.assertEqual({row["state"] for row in result_rows}, {"READY_FOR_CREATE_REVIEW", "EXISTING_MEMBER_REVIEW"})
            self.assertEqual(len(list(processed_dir.glob("*.json"))), 2)
            self.assertFalse(failed_dir.exists())
            self.assertNotIn(ENCODED_SYNTHETIC_VALUE, results_path.read_text(encoding="utf-8"))
            self.assertNotIn("submitted_member_no_base64_utf8", results_path.read_text(encoding="utf-8"))

            second = self.run_gate3c_runtime(base_args)

            self.assertEqual(second.returncode, 0, second.stderr)
            second_evidence = parse_key_value_evidence(second.stdout)
            self.assertEqual(second_evidence["status"], "dry_run_only")
            self.assertEqual(second_evidence["pending_rows_loaded_count"], "2")
            self.assertEqual(second_evidence["lookup_attempt_count"], "0")
            self.assertEqual(second_evidence["lookup_success_count"], "0")
            self.assertEqual(second_evidence["processed_or_archived_count"], "0")
            self.assertEqual(second_evidence["duplicate_or_already_processed_count"], "2")
            self.assertEqual(len(results_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_gate3c_runtime_powershell_fresh_pass_then_duplicate_only_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pending_path = tmp_path / "member_lookup_bridge_gate3c_pending_queue.jsonl"
            results_path = tmp_path / "member_lookup_bridge_gate3c_results.jsonl"
            processed_dir = tmp_path / "member_lookup_bridge_gate3c_processed"
            failed_dir = tmp_path / "member_lookup_bridge_gate3c_failed"
            fake_powershell = tmp_path / "fake-powershell.cmd"
            fake_lookup_script = tmp_path / "fake-lookup-script.ps1"
            raw_member_value = "MEMBER-FIXTURE-PRIVATE"
            encoded_value = base64.b64encode(raw_member_value.encode("utf-8")).decode("ascii")
            write_fake_powershell_lookup(fake_powershell)
            fake_lookup_script.write_text("# fake lookup script path only\n", encoding="utf-8")
            write_jsonl(
                pending_path,
                [
                    fixture_job(
                        job_id="gate3c-powershell-fresh",
                        intake_source="ac2_local_gate3c",
                        payload_hash="hash-powershell-fresh",
                        submitted_member_no_base64_utf8=encoded_value,
                    )
                ],
            )
            base_args = [
                "--enable-local-bridge-runtime-review",
                "--pending-jsonl",
                str(pending_path),
                "--results-jsonl",
                str(results_path),
                "--processed-dir",
                str(processed_dir),
                "--failed-dir",
                str(failed_dir),
                "--lookup-mode",
                "powershell",
                "--enable-powershell-lookup",
                "--powershell-exe",
                str(fake_powershell),
                "--lookup-script",
                str(fake_lookup_script),
            ]

            fresh = self.run_gate3c_runtime(base_args)

            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            fresh_evidence = parse_key_value_evidence(fresh.stdout)
            self.assertEqual(fresh_evidence["status"], "ok")
            self.assertEqual(fresh_evidence["lookup_mode"], "powershell")
            self.assertEqual(fresh_evidence["powershell_lookup_enabled"], "true")
            self.assertEqual(fresh_evidence["pending_rows_loaded_count"], "1")
            self.assertEqual(fresh_evidence["lookup_attempt_count"], "1")
            self.assertEqual(fresh_evidence["lookup_success_count"], "1")
            self.assertEqual(fresh_evidence["lookup_error_count"], "0")
            self.assertEqual(fresh_evidence["processed_or_archived_count"], "1")
            self.assertEqual(fresh_evidence["failed_or_dead_letter_count"], "0")
            self.assertEqual(fresh_evidence["duplicate_or_already_processed_count"], "0")

            duplicate = self.run_gate3c_runtime(base_args)

            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
            duplicate_evidence = parse_key_value_evidence(duplicate.stdout)
            self.assertEqual(duplicate_evidence["status"], "already_processed")
            self.assertEqual(duplicate_evidence["pending_rows_loaded_count"], "1")
            self.assertEqual(duplicate_evidence["lookup_attempt_count"], "0")
            self.assertEqual(duplicate_evidence["lookup_success_count"], "0")
            self.assertEqual(duplicate_evidence["lookup_error_count"], "0")
            self.assertEqual(duplicate_evidence["processed_or_archived_count"], "0")
            self.assertEqual(duplicate_evidence["failed_or_dead_letter_count"], "0")
            self.assertEqual(duplicate_evidence["duplicate_or_already_processed_count"], "1")
            self.assertEqual(len(results_path.read_text(encoding="utf-8").splitlines()), 1)

            for forbidden in [
                raw_member_value,
                encoded_value,
                "submitted_member_no_base64_utf8",
                "normalized-member-fixture-private",
                "Forbidden Person",
                "forbidden@example.test",
                "61234567",
                "2000-01-01",
                "AC2_PROBE_PASSWORD",
                "stdout",
                "stderr",
            ]:
                self.assertNotIn(forbidden, duplicate.stdout)

    def test_gate3c_runtime_empty_pending_file_is_no_work_not_pass_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pending_path = tmp_path / "member_lookup_bridge_gate3c_pending_queue.jsonl"
            results_path = tmp_path / "member_lookup_bridge_gate3c_results.jsonl"
            processed_dir = tmp_path / "member_lookup_bridge_gate3c_processed"
            failed_dir = tmp_path / "member_lookup_bridge_gate3c_failed"
            pending_path.write_text("", encoding="utf-8")

            completed = self.run_gate3c_runtime(
                [
                    "--enable-local-bridge-runtime-review",
                    "--pending-jsonl",
                    str(pending_path),
                    "--results-jsonl",
                    str(results_path),
                    "--processed-dir",
                    str(processed_dir),
                    "--failed-dir",
                    str(failed_dir),
                    "--lookup-mode",
                    "powershell",
                    "--enable-powershell-lookup",
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_key_value_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "no_work")
            self.assertEqual(evidence["runtime_location"], "windows_ac2_bridge_host_only")
            self.assertEqual(evidence["execution_mode"], "manual_local_filesystem_runtime_hardening")
            self.assertEqual(evidence["lookup_mode"], "powershell")
            self.assertEqual(evidence["powershell_lookup_enabled"], "true")
            self.assertEqual(evidence["pending_rows_loaded_count"], "0")
            self.assertEqual(evidence["lookup_attempt_count"], "0")
            self.assertEqual(evidence["lookup_success_count"], "0")
            self.assertEqual(evidence["lookup_error_count"], "0")
            self.assertEqual(evidence["failed_or_dead_letter_count"], "0")
            self.assertFalse(results_path.exists())
            self.assertFalse(processed_dir.exists())
            self.assertFalse(failed_dir.exists())

    def test_gate3c_runtime_powershell_mode_evidence_includes_metadata_without_row_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pending_path = tmp_path / "member_lookup_bridge_gate3c_pending_queue.jsonl"
            results_path = tmp_path / "member_lookup_bridge_gate3c_results.jsonl"
            processed_dir = tmp_path / "member_lookup_bridge_gate3c_processed"
            failed_dir = tmp_path / "member_lookup_bridge_gate3c_failed"
            encoded_value = base64.b64encode(b"member-fixture-private").decode("ascii")
            write_jsonl(
                pending_path,
                [
                    fixture_job(
                        job_id="gate3c-powershell-metadata",
                        intake_source="ac2_local_gate3c",
                        payload_hash="hash-powershell-metadata",
                        submitted_member_no_base64_utf8=encoded_value,
                    )
                ],
            )

            completed = self.run_gate3c_runtime(
                [
                    "--enable-local-bridge-runtime-review",
                    "--pending-jsonl",
                    str(pending_path),
                    "--results-jsonl",
                    str(results_path),
                    "--processed-dir",
                    str(processed_dir),
                    "--failed-dir",
                    str(failed_dir),
                    "--lookup-mode",
                    "powershell",
                    "--enable-powershell-lookup",
                    "--lookup-script",
                    str(tmp_path / "missing_lookup_script.ps1"),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_key_value_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["runtime_location"], "windows_ac2_bridge_host_only")
            self.assertEqual(evidence["execution_mode"], "manual_local_filesystem_runtime_hardening")
            self.assertEqual(evidence["lookup_mode"], "powershell")
            self.assertEqual(evidence["powershell_lookup_enabled"], "true")
            self.assertEqual(evidence["pending_rows_loaded_count"], "1")
            self.assertEqual(evidence["lookup_attempt_count"], "1")
            self.assertEqual(evidence["lookup_success_count"], "0")
            self.assertEqual(evidence["lookup_error_count"], "1")
            self.assertEqual(evidence["failed_or_dead_letter_count"], "1")

            result_text = results_path.read_text(encoding="utf-8")
            failed_marker_text = "\n".join(path.read_text(encoding="utf-8") for path in failed_dir.glob("*.json"))
            for forbidden in [
                "member-fixture-private",
                encoded_value,
                "submitted_member_no_base64_utf8",
                "normalized-member-fixture-private",
                "Forbidden Person",
                "forbidden@example.test",
                "61234567",
                "2000-01-01",
                "AC2_PROBE_PASSWORD",
                "missing_lookup_script.ps1",
                "stdout",
                "stderr",
            ]:
                self.assertNotIn(forbidden, completed.stdout)
                self.assertNotIn(forbidden, result_text)
                self.assertNotIn(forbidden, failed_marker_text)

    def test_gate3c_runtime_dead_letters_malformed_queue_rows_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pending_path = tmp_path / "member_lookup_bridge_gate3c_pending_queue.jsonl"
            results_path = tmp_path / "member_lookup_bridge_gate3c_results.jsonl"
            processed_dir = tmp_path / "member_lookup_bridge_gate3c_processed"
            failed_dir = tmp_path / "member_lookup_bridge_gate3c_failed"
            forbidden_value = "forbidden-person@example.test"
            write_jsonl(
                pending_path,
                [
                    fixture_job(
                        job_id="gate3c-forbidden",
                        intake_source="ac2_local_gate3c",
                        payload_hash="hash-forbidden",
                        email_address=forbidden_value,
                    )
                ],
            )

            completed = self.run_gate3c_runtime(
                [
                    "--enable-local-bridge-runtime-review",
                    "--pending-jsonl",
                    str(pending_path),
                    "--results-jsonl",
                    str(results_path),
                    "--processed-dir",
                    str(processed_dir),
                    "--failed-dir",
                    str(failed_dir),
                    "--lookup-mode",
                    "mock",
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_key_value_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["pending_rows_loaded_count"], "1")
            self.assertEqual(evidence["lookup_attempt_count"], "0")
            self.assertEqual(evidence["failed_or_dead_letter_count"], "1")
            self.assertEqual(evidence["no_row_values_printed"], "true")
            self.assertFalse(processed_dir.exists())
            self.assertEqual(len(list(failed_dir.glob("*.json"))), 1)
            result_text = results_path.read_text(encoding="utf-8")
            failed_marker_text = "\n".join(path.read_text(encoding="utf-8") for path in failed_dir.glob("*.json"))
            self.assertIn("LOOKUP_ERROR_REVIEW", result_text)
            self.assertIn("request_or_lookup_contract_error", result_text)
            for forbidden in [
                forbidden_value,
                "email_address",
                ENCODED_SYNTHETIC_VALUE,
                "submitted_member_no_base64_utf8",
            ]:
                self.assertNotIn(forbidden, completed.stdout)
                self.assertNotIn(forbidden, result_text)
                self.assertNotIn(forbidden, failed_marker_text)

    def test_gate3d_small_batch_mixed_expected_and_rerun_idempotency_are_aggregate_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pending_path = tmp_path / "member_lookup_bridge_gate3d_pending_queue.jsonl"
            results_path = tmp_path / "member_lookup_bridge_gate3d_results.jsonl"
            processed_dir = tmp_path / "member_lookup_bridge_gate3d_processed"
            failed_dir = tmp_path / "member_lookup_bridge_gate3d_failed"
            fake_powershell = tmp_path / "fake-powershell.cmd"
            fake_lookup_script = tmp_path / "fake-lookup-script.ps1"
            raw_member_value = "GATE3D-PRIVATE-SYNTHETIC"
            encoded_value = base64.b64encode(raw_member_value.encode("utf-8")).decode("ascii")
            write_fake_powershell_lookup(fake_powershell)
            fake_lookup_script.write_text("# fake lookup script path only\n", encoding="utf-8")

            seed_prep = self.run_gate3d_prep(
                [
                    "--enable-local-bridge-small-batch-review",
                    "--mode",
                    "duplicate-seed",
                    "--member-value-stdin",
                    "--queue-jsonl",
                    str(pending_path),
                ],
                input_text=raw_member_value,
            )

            self.assertEqual(seed_prep.returncode, 0, seed_prep.stderr)
            seed_summary = json.loads(seed_prep.stdout)
            self.assertEqual(seed_summary["status"], "ok")
            self.assertEqual(seed_summary["gate"], "gate3d_ac2_local_bridge_small_batch_queue_prep")
            self.assertEqual(seed_summary["mode"], "duplicate-seed")
            self.assertEqual(seed_summary["queue_row_count"], 1)
            self.assertEqual(seed_summary["encoded_present_count"], 1)
            self.assertTrue(seed_summary["no_row_values_printed"])
            self.assertNotIn(raw_member_value, seed_prep.stdout)
            self.assertNotIn(encoded_value, seed_prep.stdout)

            base_args = [
                "--enable-local-bridge-small-batch-review",
                "--pending-jsonl",
                str(pending_path),
                "--results-jsonl",
                str(results_path),
                "--processed-dir",
                str(processed_dir),
                "--failed-dir",
                str(failed_dir),
                "--lookup-mode",
                "powershell",
                "--enable-powershell-lookup",
                "--powershell-exe",
                str(fake_powershell),
                "--lookup-script",
                str(fake_lookup_script),
            ]

            seed_run = self.run_gate3d_small_batch(base_args)

            self.assertEqual(seed_run.returncode, 0, seed_run.stderr)
            seed_evidence = parse_key_value_evidence(seed_run.stdout)
            self.assertEqual(seed_evidence["status"], "ok")
            self.assertEqual(seed_evidence["gate"], "gate3d_ac2_local_bridge_small_batch")
            self.assertEqual(seed_evidence["execution_mode"], "manual_local_filesystem_small_batch")
            self.assertEqual(seed_evidence["lookup_mode"], "powershell")
            self.assertEqual(seed_evidence["powershell_lookup_enabled"], "true")
            self.assertEqual(seed_evidence["pending_rows_loaded_count"], "1")
            self.assertEqual(seed_evidence["lookup_success_count"], "1")
            self.assertEqual(len(results_path.read_text(encoding="utf-8").splitlines()), 1)

            mixed_prep = self.run_gate3d_prep(
                [
                    "--enable-local-bridge-small-batch-review",
                    "--mode",
                    "mixed-batch",
                    "--member-value-stdin",
                    "--queue-jsonl",
                    str(pending_path),
                ],
                input_text=raw_member_value,
            )

            self.assertEqual(mixed_prep.returncode, 0, mixed_prep.stderr)
            mixed_summary = json.loads(mixed_prep.stdout)
            self.assertEqual(mixed_summary["status"], "ok")
            self.assertEqual(mixed_summary["mode"], "mixed-batch")
            self.assertEqual(mixed_summary["queue_row_count"], 3)
            self.assertEqual(mixed_summary["encoded_present_count"], 2)
            self.assertEqual(mixed_summary["malformed_row_count"], 1)
            self.assertTrue(mixed_summary["no_row_values_printed"])
            self.assertNotIn(raw_member_value, mixed_prep.stdout)
            self.assertNotIn(encoded_value, mixed_prep.stdout)

            mixed_run = self.run_gate3d_small_batch(base_args)

            self.assertEqual(mixed_run.returncode, 0, mixed_run.stderr)
            mixed_evidence = parse_key_value_evidence(mixed_run.stdout)
            self.assertEqual(mixed_evidence["status"], "mixed_expected")
            self.assertEqual(mixed_evidence["gate"], "gate3d_ac2_local_bridge_small_batch")
            self.assertEqual(mixed_evidence["runtime_location"], "windows_ac2_bridge_host_only")
            self.assertEqual(mixed_evidence["execution_mode"], "manual_local_filesystem_small_batch")
            self.assertEqual(mixed_evidence["lookup_mode"], "powershell")
            self.assertEqual(mixed_evidence["powershell_lookup_enabled"], "true")
            self.assertEqual(mixed_evidence["pending_rows_loaded_count"], "3")
            self.assertEqual(mixed_evidence["lookup_attempt_count"], "1")
            self.assertEqual(mixed_evidence["lookup_success_count"], "1")
            self.assertEqual(mixed_evidence["lookup_error_count"], "0")
            self.assertEqual(mixed_evidence["processed_or_archived_count"], "1")
            self.assertEqual(mixed_evidence["failed_or_dead_letter_count"], "1")
            self.assertEqual(mixed_evidence["duplicate_or_already_processed_count"], "1")
            self.assertEqual(mixed_evidence["member_create_or_update_invoked"], "false")
            self.assertEqual(mixed_evidence["autocount_write_attempted"], "false")
            self.assertEqual(mixed_evidence["direct_sql_write_attempted"], "false")
            self.assertEqual(mixed_evidence["final_write_automation"], "false")
            self.assertEqual(mixed_evidence["n8n_required"], "false")
            self.assertEqual(mixed_evidence["google_sheets_required"], "false")
            self.assertEqual(mixed_evidence["hosted_or_vps_service_called"], "false")
            self.assertEqual(mixed_evidence["scheduler_enabled"], "false")
            self.assertEqual(mixed_evidence["public_inbound_to_ac2_host"], "false")
            self.assertEqual(mixed_evidence["no_row_values_printed"], "true")

            result_text = results_path.read_text(encoding="utf-8")
            failed_marker_text = "\n".join(path.read_text(encoding="utf-8") for path in failed_dir.glob("*.json"))
            processed_marker_text = "\n".join(path.read_text(encoding="utf-8") for path in processed_dir.glob("*.json"))
            self.assertEqual(len(result_text.splitlines()), 3)
            self.assertEqual(len(list(processed_dir.glob("*.json"))), 2)
            self.assertEqual(len(list(failed_dir.glob("*.json"))), 1)
            self.assertIn("LOOKUP_ERROR_REVIEW", result_text)
            self.assertIn("request_or_lookup_contract_error", result_text)

            for forbidden in [
                raw_member_value,
                encoded_value,
                "submitted_member_no_base64_utf8",
                "unexpected_gate3d_field",
                "gate3d-local-malformed-control",
                "normalized-member-fixture-private",
                "Forbidden Person",
                "forbidden@example.test",
                "61234567",
                "2000-01-01",
                "AC2_PROBE_PASSWORD",
                "AC2_SERVER",
                "stdout",
                "stderr",
                "secret",
            ]:
                self.assertNotIn(forbidden, mixed_run.stdout)
                self.assertNotIn(forbidden, result_text)
                self.assertNotIn(forbidden, failed_marker_text)
                self.assertNotIn(forbidden, processed_marker_text)

            rerun = self.run_gate3d_small_batch(base_args)

            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            rerun_evidence = parse_key_value_evidence(rerun.stdout)
            self.assertEqual(rerun_evidence["status"], "already_processed")
            self.assertEqual(rerun_evidence["pending_rows_loaded_count"], "3")
            self.assertEqual(rerun_evidence["lookup_attempt_count"], "0")
            self.assertEqual(rerun_evidence["lookup_success_count"], "0")
            self.assertEqual(rerun_evidence["processed_or_archived_count"], "0")
            self.assertEqual(rerun_evidence["failed_or_dead_letter_count"], "0")
            self.assertEqual(rerun_evidence["duplicate_or_already_processed_count"], "3")
            self.assertEqual(len(results_path.read_text(encoding="utf-8").splitlines()), 3)


class BridgeWorkerStaticGuardrailTests(unittest.TestCase):
    def recorded_gate3_evidence(self):
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")
        match = re.search(
            r"Recorded Gate 3 sanitized evidence:\n\n```text\n(?P<body>.*?)\n```",
            bridge_runbook,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        evidence = {}
        for line in body.splitlines():
            key, value = line.split(" = ", 1)
            evidence[key] = value
        return body, evidence

    def test_worker_is_disabled_by_default_and_has_no_network_endpoint_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("--enable-local-lookup-bridge-review", source)
        self.assertIn("--enable-powershell-lookup", source)
        self.assertIn("fixture", source)
        self.assertIn("mock", source)
        self.assertIn("ac2_member_lookup_review.ps1", source)
        self.assertIn("-EnableMemberLookupReview", source)
        self.assertIn("-MemberNoBase64Utf8", source)
        self.assertIn("ALLOWED_QUEUE_FIELDS", source)
        self.assertIn("ALLOWED_RESULT_FIELDS", source)
        self.assertIn("intake_source", source)
        self.assertIn("source_reference", source)
        self.assertIn("pdpa_status", source)
        self.assertIn("--fixture-mock-results", source)
        blocked_terms = [
            "google" + "apiclient",
            "g" + "spread",
            "req" + "uests",
            "url" + "lib",
            "sock" + "et",
            "http" + ".client",
            "http" + "://",
            "https" + "://",
            "web" + "hook",
        ]
        for term in blocked_terms:
            self.assertNotRegex(source, rf"(?i)\b{re.escape(term)}\b")
        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, source, token)
        self.assertNotRegex(
            source,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )

    def test_docs_readme_and_gitignore_cover_worker_bridge_guardrails(self):
        readme = README.read_text(encoding="utf-8")
        gitignore = GITIGNORE.read_text(encoding="utf-8")
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")
        node_contract = NODE_CONTRACT.read_text(encoding="utf-8")
        combined = "\n".join([readme, bridge_runbook, node_contract])

        self.assertIn("scripts/ac2_member_lookup_bridge_worker.py", readme)
        self.assertIn("member_intake_local_lookup_bridge_runbook.md", readme)
        self.assertIn("member_lookup_bridge_results.jsonl", gitignore)
        self.assertIn("member_lookup_bridge_fixture_jobs.jsonl", gitignore)
        self.assertIn("member_lookup_bridge_mock_results.jsonl", gitignore)
        self.assertRegex(combined, r"(?i)disabled-by-default|default invocation refuses")
        self.assertRegex(combined, r"(?i)fixture queue mode|fixture/mock mode")
        self.assertRegex(
            combined,
            r"(?i)Google Sheets(?: UAT)? queue.*fixture-only|fixture-only.*Google Sheets(?: UAT)? queue",
        )
        self.assertRegex(combined, r"(?i)custom web form|hosted intake API")
        self.assertRegex(combined, r"(?i)PowerShell lookup mode.*requires")
        self.assertRegex(combined, r"(?i)never writes to AutoCount|never writes to AutoCount")
        self.assertRegex(combined, r"(?i)dry_run_only")
        self.assertRegex(combined, r"(?i)final_write_automation")

    def test_gate3b_docs_define_ac2_side_local_bridge_readiness_not_gate4a_or_n8n(self):
        readme = README.read_text(encoding="utf-8")
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")
        node_contract = NODE_CONTRACT.read_text(encoding="utf-8")
        combined = "\n".join([readme, bridge_runbook, node_contract])

        for phrase in [
            "Gate 3B AC2 Local Bridge Readiness",
            "AC2-side local bridge readiness only",
            "This is not Gate 4A",
            "not n8n evidence",
            "not Google Sheets evidence",
            "local one-row ignored queue JSONL",
            "scripts/ac2_member_lookup_bridge_worker.py",
            "read-only PowerShell lookup",
            "local sanitized result JSONL",
            "aggregate-only evidence summary",
            "scripts/member_lookup_gate3b_prepare_local_queue.py",
            "scripts/member_lookup_gate3b_evidence_summary.py",
            "member_lookup_bridge_gate3b_pending_queue.jsonl",
            "member_lookup_bridge_gate3b_results.jsonl",
            "does not require or use n8n",
            "Google Sheets",
            "hosted n8n",
            "scheduler",
            "webhook",
            "result mapping",
            "Gate 3B proves only",
            "does not approve Gate 4A",
        ]:
            self.assertIn(phrase, combined)

        for evidence_field in [
            "gate = gate3b_ac2_local_bridge_readiness",
            "runtime_location = windows_ac2_bridge_host_only",
            "execution_mode = manual_local_one_row_read_only_lookup",
            "local_queue_row_count = <aggregate-count-only>",
            "local_queue_rows_loaded_count = <aggregate-count-only>",
            "lookup_attempt_count = <aggregate-count-only>",
            "lookup_success_count = <aggregate-count-only>",
            "lookup_ready_for_create_review_count = <aggregate-count-only>",
            "lookup_existing_member_review_count = <aggregate-count-only>",
            "lookup_manual_review_count = <aggregate-count-only>",
            "lookup_error_count = <aggregate-count-only>",
            "n8n_required = false",
            "n8n_involved = false",
            "google_sheets_required = false",
            "hosted_or_vps_service_called = false",
            "scheduler_enabled = false",
            "public_inbound_to_ac2_host = false",
            "member_create_or_update_invoked = false",
            "autocount_write_attempted = false",
            "direct_sql_write_attempted = false",
            "final_write_automation = false",
            "no_row_values_printed = true",
        ]:
            self.assertIn(evidence_field, bridge_runbook)

        self.assertRegex(bridge_runbook, r"(?i)Do not paste.*raw.*encoded.*decoded.*normalized")
        self.assertRegex(bridge_runbook, r"(?i)Keep `member_lookup_bridge_gate3b_pending_queue\.jsonl` and `member_lookup_bridge_gate3b_results\.jsonl` local and ignored")
        self.assertNotRegex(bridge_runbook, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(bridge_runbook, r"submitted_member_no_base64_utf8\"\s*:\s*\"[A-Za-z0-9+/]+=*\"")

    def test_gate3b_docs_state_n8n_needs_no_ac2_env_and_bridge_host_owns_runtime(self):
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")

        for phrase in [
            "n8n may later run on a dev PC, VPS, hosted machine, or other non-AC2 environment",
            "n8n does not need AC2 environment variables, AutoCount assemblies, direct SQL access, or local PowerShell execution",
            "Only the Windows AC2 bridge host has AC2 environment variables and the AutoCount runtime",
            "n8n writes sanitized PENDING_LOOKUP jobs to the queue API",
            "AC2 bridge polls/reads the queue API outbound over HTTPS",
            "AC2 bridge claims one job at a time using state/lease fields",
            "AC2 bridge writes sanitized results back to the queue API",
            "n8n reads/routes the sanitized result",
            "Hosted/cloud/VPS n8n must not use Execute Command for AC2 lookup",
            "no public inbound webhook, tunnel, or reverse proxy may expose the AC2 host",
        ]:
            self.assertIn(phrase, bridge_runbook)

    def test_future_cloudflared_queue_api_is_allowed_but_direct_ac2_tunnels_are_forbidden(self):
        combined = "\n".join(
            [
                BRIDGE_RUNBOOK.read_text(encoding="utf-8"),
                NODE_CONTRACT.read_text(encoding="utf-8"),
                BRIDGE_DESIGN.read_text(encoding="utf-8"),
                UAT_PLAN.read_text(encoding="utf-8"),
            ]
        )

        for phrase in [
            "Cloudflare Tunnel / reverse proxy may front that queue/API surface",
            "Cloudflare Tunnel / reverse proxy may be used for development and likely integration only for a narrow protected queue/API surface over HTTPS",
            "the queue API may run on the operator local dev PC behind `cloudflared`",
            "allowed as future/dev architecture",
            "protected queue/API surface",
            "sanitized queue/result operations",
            "Cloudflare Access/service-token or equivalent machine authentication",
            "rate limits",
            "audit logging",
            "least-privilege request/response schema",
            "documented rollback/disable procedure",
        ]:
            self.assertIn(phrase, combined)

        for phrase in [
            "never exposes AC2, AutoCount, PowerShell, SQL, RDP, or member write paths",
            "must not expose AC2, AutoCount, PowerShell, SQL, RDP, or member create/update/delete/write paths",
            "direct tunnel access to AC2, AutoCount, PowerShell, SQL, RDP, or member write paths remains forbidden",
            "direct tunnel to AC2, AutoCount, PowerShell, SQL, RDP, or member write paths",
        ]:
            self.assertIn(phrase, combined)

    def test_bridge_design_replaces_stale_direct_post_flow_with_queue_polling(self):
        bridge_design = BRIDGE_DESIGN.read_text(encoding="utf-8")

        for phrase in [
            "## Current Queue/Polling Request Flow",
            "n8n writes sanitized PENDING_LOOKUP jobs to the protected queue/API",
            "AC2 bridge polls the queue/API outbound over HTTPS",
            "AC2 bridge claims one job with state/lease fields",
            "AC2 bridge runs read-only AutoCount lookup locally",
            "AC2 bridge posts sanitized result back",
            "n8n reads/routes sanitized result",
            "PR #90 does not approve direct POST to the AC2 bridge",
            "a direct tunneled endpoint to AC2/AutoCount/PowerShell/SQL/RDP",
            "a `/sync` write endpoint",
            "member create/update/delete",
            "AutoCount writes",
            "production activation",
        ]:
            self.assertIn(phrase, bridge_design)

        for stale_phrase in [
            "HTTP POST /member-intake/dry-run",
            "HTTP POST /member-intake/sync",
            "/member-intake/dry-run",
            "/member-intake/sync",
            "Only the dry-run endpoint is in scope for the first implementation",
            "live sync endpoint is a future design placeholder",
        ]:
            self.assertNotIn(stale_phrase, bridge_design)

    def test_gate3b_stays_local_only_without_queue_api_cloudflared_or_hosted_services(self):
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")

        for phrase in [
            "Gate 3B does not require or use n8n, Google Sheets, hosted n8n, a queue API, Cloudflare Tunnel, `cloudflared`, a hosted service",
            "does not approve Gate 4A",
            "queue API use",
            "Cloudflare Tunnel / `cloudflared` use",
            "hosted/VPS runtime readiness",
        ]:
            self.assertIn(phrase, bridge_runbook)

    def test_queue_api_polling_uses_outbound_https_claims_and_retry_semantics(self):
        combined = "\n".join(
            [
                BRIDGE_RUNBOOK.read_text(encoding="utf-8"),
                NODE_CONTRACT.read_text(encoding="utf-8"),
                BRIDGE_DESIGN.read_text(encoding="utf-8"),
                UAT_PLAN.read_text(encoding="utf-8"),
            ]
        )

        for phrase in [
            "AC2 bridge polls the queue API outbound over HTTPS",
            "AC2 bridge claims one job at a time with state/lease fields",
            "AC2 bridge runs read-only AutoCount lookup locally",
            "AC2 bridge posts sanitized result back to the queue API over HTTPS",
            "n8n reads/routes the sanitized result",
            "moving it to `LOOKUP_IN_PROGRESS` with lease metadata",
            "If the `LOOKUP_IN_PROGRESS` lease expires before a result is posted",
            "return the job to `PENDING_LOOKUP` with incremented attempt metadata",
            "Retry exhaustion routes to `LOOKUP_ERROR_REVIEW`",
            "For UAT, polling may be manual or Windows Task Scheduler every 1 minute",
            "For production, prefer a long-running Windows service/worker polling every 15-60 seconds with idle backoff",
            "Hourly polling is too slow for the intake duplicate-check flow and should not be the default",
        ]:
            self.assertIn(phrase, combined)

    def test_cloudflare_architecture_docs_do_not_contain_real_endpoint_or_secret_literals(self):
        combined = "\n".join(
            [
                BRIDGE_RUNBOOK.read_text(encoding="utf-8"),
                NODE_CONTRACT.read_text(encoding="utf-8"),
                BRIDGE_DESIGN.read_text(encoding="utf-8"),
                UAT_PLAN.read_text(encoding="utf-8"),
            ]
        )

        self.assertNotRegex(combined, r"https?://")
        self.assertNotRegex(combined, r"(?i)\b[a-z0-9-]+\.trycloudflare\.com\b")
        self.assertNotRegex(combined, r"(?i)\b[a-z0-9-]+\.cloudflareaccess\.com\b")
        self.assertNotRegex(combined, r"(?i)cloudflare\s+account\s+id\s*[:=]")
        self.assertNotRegex(combined, r"(?i)access\s+client\s+id\s*[:=]")
        self.assertNotRegex(combined, r"(?i)access\s+client\s+secret\s*[:=]")
        self.assertNotRegex(combined, r"(?i)service\s+token\s*[:=]\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(combined, r"(?i)cloudflared\s+tunnel\s+--url")
        self.assertNotRegex(combined, r"(?i)tunnel\s+token\s*[:=]")
        self.assertNotRegex(combined, r"(?i)(server|database|user|password)\s*[:=]\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(combined, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(combined, r"(?i)\b(?:\+?65)?[689]\d{7}\b")
        self.assertNotRegex(combined, r"(?i)\b(stdin|stdout|stderr)\s*=")
        self.assertNotRegex(combined, r"submitted_member_no_base64_utf8\"\s*:\s*\"[A-Za-z0-9+/]+=*\"")

    def test_gate3b_scripts_and_docs_do_not_authorize_write_or_direct_sql_paths(self):
        combined = "\n".join(
            [
                GATE3B_PREP_SCRIPT.read_text(encoding="utf-8"),
                GATE3B_SUMMARY_SCRIPT.read_text(encoding="utf-8"),
                BRIDGE_RUNBOOK.read_text(encoding="utf-8"),
                README.read_text(encoding="utf-8"),
            ]
        )

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, combined, token)
        self.assertNotRegex(
            combined,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )
        self.assertRegex(combined, r"(?i)does not create, update, delete, or otherwise write AutoCount members")
        self.assertRegex(combined, r"(?i)does not perform direct SQL writes")

    def test_gate3b_generated_local_queue_and_result_artifacts_are_ignored(self):
        gitignore = GITIGNORE.read_text(encoding="utf-8")

        for ignored_name in [
            "member_lookup_bridge_gate3b_pending_queue.jsonl",
            "member_lookup_bridge_gate3b_results.jsonl",
            "autocount_outputs/**/member_lookup_bridge_gate3b_pending_queue.jsonl",
            "autocount_outputs/**/member_lookup_bridge_gate3b_results.jsonl",
        ]:
            self.assertIn(ignored_name, gitignore)

        for ignored_path in [
            "member_lookup_bridge_gate3b_pending_queue.jsonl",
            "member_lookup_bridge_gate3b_results.jsonl",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3b_pending_queue.jsonl",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3b_results.jsonl",
        ]:
            completed = subprocess.run(
                ["git", "check-ignore", ignored_path],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, ignored_path)

    def test_gate3c_runtime_source_docs_and_artifacts_are_local_only(self):
        source = GATE3C_RUNTIME_SCRIPT.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        gitignore = GITIGNORE.read_text(encoding="utf-8")
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")
        combined = "\n".join([source, readme, bridge_runbook])

        for phrase in [
            "scripts/member_lookup_gate3c_local_bridge_runtime.py",
            "Gate 3C AC2 Local Bridge Runtime Hardening",
            "local ignored pending queue JSONL",
            "local ignored sanitized results JSONL",
            "local ignored processed idempotency markers",
            "local ignored failed/dead-letter markers",
            "aggregate-only evidence",
            "--enable-local-bridge-runtime-review",
            "--pending-jsonl",
            "--results-jsonl",
            "--processed-dir",
            "--failed-dir",
            "--lookup-mode powershell",
            "--enable-powershell-lookup",
            "gate = gate3c_ac2_local_bridge_runtime_hardening",
            "runtime_location = windows_ac2_bridge_host_only",
            "execution_mode = manual_local_filesystem_runtime_hardening",
            "lookup_mode = <mock/powershell>",
            "powershell_lookup_enabled = <true/false>",
            "pending_rows_loaded_count = <aggregate-count-only>",
            "processed_or_archived_count = <aggregate-count-only>",
            "failed_or_dead_letter_count = <aggregate-count-only>",
            "duplicate_or_already_processed_count = <aggregate-count-only>",
            "n8n_required = false",
            "google_sheets_required = false",
            "hosted_or_vps_service_called = false",
            "scheduler_enabled = false",
            "public_inbound_to_ac2_host = false",
            "no_row_values_printed = true",
            "Mock mode is allowed only for local harness validation and automated tests.",
            "Mock-mode evidence is not Gate 3C AC2 runtime pass evidence.",
            "lookup_mode = powershell",
            "powershell_lookup_enabled = true",
            "pending_rows_loaded_count >= 1",
            "lookup_attempt_count >= 1",
            "lookup_success_count >= 1",
            "lookup_error_count = 0",
            "failed_or_dead_letter_count = 0",
            "`status = no_work` is not Gate 3C pass evidence.",
            "`status = already_processed` is not fresh Gate 3C AC2 lookup pass evidence.",
            "Duplicate-only evidence proves local idempotency only.",
            "duplicate_or_already_processed_count >= 1",
        ]:
            self.assertIn(phrase, combined)

        for phrase in [
            "does not require or use n8n",
            "Google Sheets",
            "a queue API",
            "Cloudflare Tunnel",
            "hosted service",
            "scheduler",
            "public inbound path",
            "final write automation",
            "does not create, update, delete, or otherwise write AutoCount members",
            "does not perform direct SQL writes",
        ]:
            self.assertIn(phrase, bridge_runbook)

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, source, token)
        self.assertNotRegex(
            source,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )

        for ignored_name in [
            "member_lookup_bridge_gate3c_pending_queue.jsonl",
            "member_lookup_bridge_gate3c_results.jsonl",
            "member_lookup_bridge_gate3c_processed/",
            "member_lookup_bridge_gate3c_failed/",
            "autocount_outputs/**/member_lookup_bridge_gate3c_pending_queue.jsonl",
            "autocount_outputs/**/member_lookup_bridge_gate3c_results.jsonl",
            "autocount_outputs/**/member_lookup_bridge_gate3c_processed/",
            "autocount_outputs/**/member_lookup_bridge_gate3c_failed/",
        ]:
            self.assertIn(ignored_name, gitignore)

        for ignored_path in [
            "member_lookup_bridge_gate3c_pending_queue.jsonl",
            "member_lookup_bridge_gate3c_results.jsonl",
            "member_lookup_bridge_gate3c_processed/handled.json",
            "member_lookup_bridge_gate3c_failed/failed.json",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3c_pending_queue.jsonl",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3c_results.jsonl",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3c_processed/handled.json",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3c_failed/failed.json",
        ]:
            completed = subprocess.run(
                ["git", "check-ignore", ignored_path],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, ignored_path)

    def test_gate3d_small_batch_docs_source_and_artifacts_are_local_only(self):
        prep_source = GATE3D_PREP_SCRIPT.read_text(encoding="utf-8")
        source = GATE3D_SMALL_BATCH_SCRIPT.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        gitignore = GITIGNORE.read_text(encoding="utf-8")
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")
        combined = "\n".join([prep_source, source, readme, bridge_runbook])

        for phrase in [
            "scripts/member_lookup_gate3d_prepare_small_batch.py",
            "scripts/member_lookup_gate3d_local_bridge_small_batch.py",
            "Gate 3D AC2 Local Bridge Small-Batch Proof",
            "local ignored Gate 3D pending queue JSONL",
            "Gate 3C runtime harness",
            "read-only PowerShell lookup",
            "local ignored sanitized results JSONL",
            "local ignored processed idempotency markers",
            "local ignored failed/dead-letter markers",
            "aggregate-only evidence",
            "--enable-local-bridge-small-batch-review",
            "--mode duplicate-seed",
            "--mode mixed-batch",
            "--pending-jsonl",
            "--results-jsonl",
            "--processed-dir",
            "--failed-dir",
            "--lookup-mode powershell",
            "--enable-powershell-lookup",
            "gate = gate3d_ac2_local_bridge_small_batch",
            "runtime_location = windows_ac2_bridge_host_only",
            "execution_mode = manual_local_filesystem_small_batch",
            "lookup_mode = powershell",
            "powershell_lookup_enabled = true",
            "pending_rows_loaded_count = <aggregate-count-only>",
            "processed_or_archived_count = <aggregate-count-only>",
            "failed_or_dead_letter_count = <aggregate-count-only>",
            "duplicate_or_already_processed_count = <aggregate-count-only>",
            "status = <ok/mixed_expected/needs_fix/no_work/dry_run_only/already_processed>",
            "`status = mixed_expected` is the expected status",
            "Duplicate-only evidence proves local idempotency only.",
            "Malformed/dead-letter evidence proves failure routing only.",
            "n8n_required = false",
            "google_sheets_required = false",
            "hosted_or_vps_service_called = false",
            "scheduler_enabled = false",
            "public_inbound_to_ac2_host = false",
            "no_row_values_printed = true",
        ]:
            self.assertIn(phrase, combined)

        for phrase in [
            "Gate 3D does not require or use n8n, Google Sheets, a queue API, Cloudflare Tunnel, `cloudflared`, a hosted service",
            "a scheduler",
            "a Windows service activation",
            "a webhook",
            "a public inbound path",
            "final write automation",
            "does not create, update, delete, or otherwise write AutoCount members",
            "does not perform direct SQL writes",
            "does not approve Gate 4A",
            "queue API use",
            "Cloudflare Tunnel / `cloudflared` use",
            "hosted/VPS runtime readiness",
            "scheduler activation",
            "webhook activation",
            "Windows service activation",
        ]:
            self.assertIn(phrase, bridge_runbook)

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, prep_source, token)
            self.assertNotIn(token, source, token)
        self.assertNotRegex(
            "\n".join([prep_source, source]),
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )
        self.assertNotRegex(combined, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(combined, r"submitted_member_no_base64_utf8\"\s*:\s*\"[A-Za-z0-9+/]+=*\"")
        self.assertRegex(bridge_runbook, r"(?i)Do not paste pending rows, result rows, processed markers, failed markers")
        self.assertRegex(bridge_runbook, r"(?i)raw member values, encoded member values, decoded member values, normalized member values")
        self.assertRegex(bridge_runbook, r"(?i)names, emails, phone numbers, birthday values, AC2 environment values")
        self.assertRegex(bridge_runbook, r"(?i)command transcripts, stdout/stderr transcripts")

        for ignored_name in [
            "member_lookup_bridge_gate3d_pending_queue.jsonl",
            "member_lookup_bridge_gate3d_results.jsonl",
            "member_lookup_bridge_gate3d_processed/",
            "member_lookup_bridge_gate3d_failed/",
            "autocount_outputs/**/member_lookup_bridge_gate3d_pending_queue.jsonl",
            "autocount_outputs/**/member_lookup_bridge_gate3d_results.jsonl",
            "autocount_outputs/**/member_lookup_bridge_gate3d_processed/",
            "autocount_outputs/**/member_lookup_bridge_gate3d_failed/",
        ]:
            self.assertIn(ignored_name, gitignore)

        for ignored_path in [
            "member_lookup_bridge_gate3d_pending_queue.jsonl",
            "member_lookup_bridge_gate3d_results.jsonl",
            "member_lookup_bridge_gate3d_processed/handled.json",
            "member_lookup_bridge_gate3d_failed/failed.json",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3d_pending_queue.jsonl",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3d_results.jsonl",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3d_processed/handled.json",
            "autocount_outputs/review/member_lookup_bridge/member_lookup_bridge_gate3d_failed/failed.json",
        ]:
            completed = subprocess.run(
                ["git", "check-ignore", ignored_path],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, ignored_path)

    def test_local_fixture_uat_runbook_defines_sanitized_operator_evidence_only(self):
        readme = README.read_text(encoding="utf-8")
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")

        self.assertIn("member_intake_local_lookup_bridge_runbook.md", readme)
        self.assertIn("Local Fixture UAT Pass", bridge_runbook)
        self.assertIn(r"C:\XB\autocount_outputs\review\member_lookup_bridge", bridge_runbook)
        self.assertIn("member_lookup_bridge_fixture_jobs.jsonl", bridge_runbook)
        self.assertIn("member_lookup_bridge_mock_results.jsonl", bridge_runbook)
        self.assertIn("member_lookup_bridge_results.jsonl", bridge_runbook)
        self.assertIn("--enable-local-lookup-bridge-review", bridge_runbook)
        self.assertIn("--queue-mode fixture", bridge_runbook)
        self.assertIn("--fixture-jobs", bridge_runbook)
        self.assertIn("--fixture-mock-results", bridge_runbook)
        self.assertIn("--results-jsonl", bridge_runbook)
        self.assertIn("UTF8Encoding", bridge_runbook)
        self.assertIn("$safeFixturePlaintext = 'SYNTHETIC'", bridge_runbook)
        self.assertIn("$localGeneratedSafeFixtureValue", bridge_runbook)
        self.assertIn("[Convert]::ToBase64String", bridge_runbook)
        self.assertIn("[System.Text.Encoding]::UTF8.GetBytes($safeFixturePlaintext)", bridge_runbook)
        self.assertIn("$fixtureJobsTemplate", bridge_runbook)
        self.assertIn(".Replace(", bridge_runbook)
        self.assertIn("<local-generated-safe-fixture-value>", bridge_runbook)
        self.assertNotIn(ENCODED_SYNTHETIC_VALUE, bridge_runbook)
        self.assertNotIn("Set-Content -NoNewline -Encoding utf8", bridge_runbook)

        for scenario in [
            "job-uat-ready",
            "job-uat-existing",
            "job-uat-manual",
            "job-uat-error",
            "job-uat-imported-pdpa",
            "READY_FOR_CREATE_REVIEW",
            "EXISTING_MEMBER_REVIEW",
            "MANUAL_REVIEW_REQUIRED",
            "LOOKUP_ERROR_REVIEW",
            "pdpa_status",
            "imported",
        ]:
            self.assertIn(scenario, bridge_runbook)

        for evidence_field in [
            "status",
            "queue_mode",
            "lookup_mode",
            "processed_count",
            "result_state_counts",
            "dry_run_only",
            "final_write_automation",
            "sanitized_note",
        ]:
            self.assertIn(evidence_field, bridge_runbook)

        self.assertRegex(bridge_runbook, r"(?i)paste back only")
        self.assertRegex(bridge_runbook, r"(?i)Do not paste result rows")
        self.assertRegex(bridge_runbook, r"(?i)raw fixture input")
        self.assertRegex(bridge_runbook, r"(?i)encoded submitted values")
        self.assertRegex(bridge_runbook, r"(?i)raw member values")
        self.assertRegex(bridge_runbook, r"(?i)READY_FOR_CREATE_REVIEW.*not approval to create")
        self.assertRegex(bridge_runbook, r"(?i)Imported.*blocked")
        self.assertNotRegex(bridge_runbook, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(bridge_runbook, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(bridge_runbook, r"\b\d{8,}\b")

    def test_gate3_local_powershell_preflight_is_local_read_only_and_aggregate_only(self):
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")

        self.assertIn("Gate 3 Local PowerShell Lookup Preflight", bridge_runbook)
        self.assertIn("local Windows AC2 lookup environment", bridge_runbook)
        self.assertIn("PowerShell lookup mode explicitly enabled", bridge_runbook)
        self.assertIn("Call only `scripts/ac2_member_lookup_review.ps1`", bridge_runbook)
        self.assertIn("MemberCommand.GetMember", bridge_runbook)
        self.assertIn("lookup remains read-only", bridge_runbook)
        self.assertIn("n8n is not involved and did not call the bridge", bridge_runbook)
        self.assertIn("Required Gate 3 paste-back shape", bridge_runbook)

        for evidence_field in [
            "gate = gate3_local_powershell_lookup_preflight",
            "runtime_location = local_windows_ac2_lookup_environment",
            "execution_mode = manual_read_only_preflight",
            "autocount_session_bootstrap_available = <true/false>",
            "member_command_found = <true/false>",
            "get_member_found = <true/false>",
            "lookup_attempt_count = <aggregate-count-only>",
            "lookup_success_count = <aggregate-count-only>",
            "lookup_manual_review_count = <aggregate-count-only>",
            "lookup_error_count = <aggregate-count-only>",
            "member_create_or_update_invoked = false",
            "autocount_write_attempted = false",
            "direct_sql_write_attempted = false",
            "n8n_involved = false",
            "bridge_called_by_n8n = false",
            "final_write_automation = false",
        ]:
            self.assertIn(evidence_field, bridge_runbook)

        for forbidden_phrase in [
            "raw member values",
            "encoded member values",
            "normalized member values",
            "command transcripts",
            "stderr/stdout",
            "result rows",
            "node payloads",
            "PII",
        ]:
            self.assertIn(forbidden_phrase, bridge_runbook)

        self.assertIn("does not authorize Gate 4", bridge_runbook)
        self.assertIn("not approval to create", bridge_runbook)
        self.assertRegex(bridge_runbook, r"(?i)Do not paste the bridge worker stdout directly")
        self.assertNotRegex(bridge_runbook, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(bridge_runbook, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(bridge_runbook, r"submitted_member_no_base64_utf8\"\s*:\s*\"[A-Za-z0-9+/]+=*\"")

    def test_recorded_gate3_pass_keeps_lookup_counts_and_write_flags_safe(self):
        body, evidence = self.recorded_gate3_evidence()

        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["gate"], "gate3_local_powershell_lookup_preflight")
        self.assertEqual(evidence["runtime_location"], "local_windows_ac2_lookup_environment")
        self.assertEqual(evidence["execution_mode"], "manual_read_only_preflight")
        self.assertEqual(evidence["autocount_session_bootstrap_available"], "true")
        self.assertEqual(evidence["member_command_found"], "true")
        self.assertEqual(evidence["get_member_found"], "true")
        self.assertEqual(evidence["lookup_attempt_count"], "1")
        self.assertEqual(evidence["lookup_success_count"], "1")
        self.assertEqual(evidence["lookup_manual_review_count"], "1")
        self.assertEqual(evidence["lookup_error_count"], "0")

        for flag in [
            "member_create_or_update_invoked",
            "autocount_write_attempted",
            "direct_sql_write_attempted",
            "n8n_involved",
            "bridge_called_by_n8n",
            "final_write_automation",
        ]:
            self.assertEqual(evidence[flag], "false", flag)

        self.assertNotIn("<aggregate-count-only>", body)
        self.assertNotIn("<true/false>", body)
        self.assertNotIn("result_state_counts", body)
        self.assertNotIn("submitted_member_no_base64_utf8", body)
        self.assertNotIn("row_number", body)
        self.assertNotRegex(body, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(body, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(body, r"(?i)\b(?:\+?65)?[689]\d{7}\b")
        self.assertNotRegex(body, r"(?i)\b(stdin|stdout|stderr)\s*=")
        self.assertNotRegex(body, r"(?m)^PS [A-Z]:\\")

    def test_recorded_gate3_pass_does_not_unblock_gate4_or_writes(self):
        bridge_runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")

        for phrase in [
            "This pass proves only that the local Windows AC2 lookup environment was available",
            "AutoCount session/auth bootstrap was available",
            "`MemberCommand` was found",
            "`MemberCommand.GetMember` was found",
            "one lookup attempt succeeded",
            "manual-review rather than an error",
            "no create/update/write/direct SQL/n8n/final automation path was invoked",
            "does not prove production automation",
            "does not authorize member create/update",
            "does not authorize AutoCount writes",
            "does not by itself prove hosted/VPS n8n runtime readiness",
            "Gate 4 remains not approved to run until the reviewed Gate 4 plan PR is merged",
            "not approval to run a real queue UAT",
        ]:
            self.assertIn(phrase, bridge_runbook)


if __name__ == "__main__":
    unittest.main()
