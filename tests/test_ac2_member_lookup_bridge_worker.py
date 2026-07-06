import json
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


ENCODED_SYNTHETIC_VALUE = "U1lOVEhFVElD"
FORBIDDEN_WRITE_TOKENS = [
    "Save" + "Member",
    "New" + "Member",
    "Delete" + "Member",
    "GetNext" + "MemberNo",
]


def fixture_job(**overrides):
    job = {
        "job_id": "job-synthetic-001",
        "row_number": 2,
        "state": "PENDING_LOOKUP",
        "submitted_member_no_base64_utf8": ENCODED_SYNTHETIC_VALUE,
        "attempt": 0,
        "payload_hash": "hash-synthetic",
    }
    job.update(overrides)
    return job


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
            results_path = tmp_path / "member_lookup_bridge_results.jsonl"
            write_jsonl(
                jobs_path,
                [
                    fixture_job(job_id="job-ready", row_number=2),
                    fixture_job(job_id="job-existing", row_number=3, mock_member_exists=True),
                    fixture_job(
                        job_id="job-manual",
                        row_number=4,
                        mock_manual_review_required=True,
                        mock_warning_count=1,
                    ),
                    fixture_job(job_id="job-error", row_number=5, mock_status="error", mock_error_code="mock_error"),
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
                self.assertTrue(result["dry_run_only"])
                self.assertFalse(result["final_write_automation"])
                self.assertIn("job_id", result)
                self.assertIn("row_number", result)
                self.assertIn("normalized_member_no_length", result)
                self.assertNotIn("submitted_member_no_base64_utf8", result)
                self.assertNotIn(ENCODED_SYNTHETIC_VALUE, json.dumps(result, sort_keys=True))

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
            self.assertEqual(result["error_code"], "request_or_lookup_contract_error")
            self.assertNotIn("redacted-fixture-value", result_text)
            self.assertNotIn(ENCODED_SYNTHETIC_VALUE, result_text)

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
        self.assertNotRegex(source, r"(?i)\b(requests|urllib|socket|http://|https://|webhook)\b")
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
        self.assertRegex(combined, r"(?i)disabled-by-default|default invocation refuses")
        self.assertRegex(combined, r"(?i)fixture queue mode|fixture/mock mode")
        self.assertRegex(combined, r"(?i)PowerShell lookup mode.*requires")
        self.assertRegex(combined, r"(?i)never writes to AutoCount|never writes to AutoCount")
        self.assertRegex(combined, r"(?i)dry_run_only")
        self.assertRegex(combined, r"(?i)final_write_automation")


if __name__ == "__main__":
    unittest.main()
