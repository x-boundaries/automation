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
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"
DOCS = ROOT / "docs" / "autocount2-automation"
BRIDGE_RUNBOOK = DOCS / "member_intake_local_lookup_bridge_runbook.md"
NODE_CONTRACT = DOCS / "member_intake_n8n_node_contract.md"


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


class BridgeWorkerCliTests(unittest.TestCase):
    def run_cli(self, arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + arguments,
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


class BridgeWorkerStaticGuardrailTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
