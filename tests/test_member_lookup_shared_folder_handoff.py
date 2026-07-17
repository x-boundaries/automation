import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "member_lookup_shared_folder_handoff.py"
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"
DOCS = ROOT / "docs" / "autocount2-automation"
SHARED_FOLDER_RUNBOOK = DOCS / "member_intake_shared_folder_lookup_bridge_runbook.md"
PENDING_FILENAME = "member_lookup_bridge_gate4a_pending_queue.jsonl"
RESULT_FILENAME = "member_lookup_bridge_gate5a_result_copy.jsonl"

ENCODED_SYNTHETIC_VALUE = base64.b64encode(
    bytes([70, 73, 88, 84, 85, 82, 69])
).decode("ascii")

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


def evidence_map(stdout):
    rows = {}
    for line in stdout.splitlines():
        if " = " in line:
            key, value = line.split(" = ", 1)
            rows[key.strip()] = value.strip()
    return rows


class SharedFolderHandoffTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.share_root = base / "share"
        self.inbox = self.share_root / "inbox"
        self.outbox = self.share_root / "outbox"
        self.inbox.mkdir(parents=True)
        self.outbox.mkdir(parents=True)
        self.processed_dir = base / "vm_local" / "processed"
        self.failed_dir = base / "vm_local" / "failed"
        self.pending_path = self.inbox / PENDING_FILENAME
        self.results_path = self.outbox / RESULT_FILENAME

    def write_pending(self, jobs):
        lines = "".join(json.dumps(job, sort_keys=True) + "\n" for job in jobs)
        self.pending_path.write_text(lines, encoding="utf-8")

    def run_script(self, *extra_args, opt_in=True, mock=True):
        cmd = [
            sys.executable,
            str(SCRIPT),
            "--share-root",
            str(self.share_root),
            "--processed-dir",
            str(self.processed_dir),
            "--failed-dir",
            str(self.failed_dir),
        ]
        if opt_in:
            cmd.append("--enable-shared-folder-handoff-review")
        if mock:
            cmd.extend(["--lookup-mode", "mock"])
        cmd.extend(extra_args)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(ROOT / "scripts"),
        )

    def test_refuses_without_opt_in(self):
        self.write_pending([fixture_job()])
        proc = self.run_script(opt_in=False)
        self.assertEqual(proc.returncode, 2)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "refused")
        self.assertEqual(rows["lookup_attempt_count"], "0")
        self.assertFalse(self.results_path.exists())

    def test_refuses_powershell_mode_without_powershell_opt_in(self):
        self.write_pending([fixture_job()])
        proc = self.run_script(mock=False)
        self.assertEqual(proc.returncode, 2)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "refused")
        self.assertEqual(rows["lookup_mode"], "powershell")
        self.assertEqual(rows["powershell_lookup_enabled"], "false")
        self.assertFalse(self.results_path.exists())

    def test_refuses_marker_dirs_inside_share(self):
        self.write_pending([fixture_job()])
        inside_processed = self.share_root / "processed"
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--enable-shared-folder-handoff-review",
                "--share-root",
                str(self.share_root),
                "--processed-dir",
                str(inside_processed),
                "--failed-dir",
                str(self.failed_dir),
                "--lookup-mode",
                "mock",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT / "scripts"),
        )
        self.assertEqual(proc.returncode, 2)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "refused")
        self.assertEqual(rows["idempotency_markers_outside_share"], "false")
        self.assertFalse(self.results_path.exists())

    def test_refuses_invalid_approved_batch_size(self):
        self.write_pending([fixture_job()])
        for bad_size in ("0", "11"):
            proc = self.run_script("--approved-batch-size", bad_size)
            self.assertEqual(proc.returncode, 2)
            rows = evidence_map(proc.stdout)
            self.assertEqual(rows["status"], "refused")
        self.assertFalse(self.results_path.exists())

    def test_needs_fix_when_pending_file_missing(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "needs_fix")
        self.assertEqual(rows["lookup_attempt_count"], "0")

    def test_needs_fix_when_outbox_dir_missing(self):
        self.write_pending([fixture_job()])
        self.outbox.rmdir()
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "needs_fix")
        self.assertEqual(rows["lookup_attempt_count"], "0")

    def test_no_work_on_empty_pending_file(self):
        self.pending_path.write_text("", encoding="utf-8")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "no_work")
        self.assertEqual(rows["pending_rows_loaded_count"], "0")
        self.assertFalse(self.results_path.exists())

    def test_needs_fix_when_batch_exceeds_approved_size(self):
        self.write_pending(
            [
                fixture_job(),
                fixture_job(
                    job_id="job-synthetic-002",
                    source_reference="uat-queue-row-003",
                    source_row_ref="row-003",
                    row_number=3,
                    intake_id="intake-synthetic-002",
                    payload_hash="hash-synthetic-2",
                ),
            ]
        )
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "needs_fix")
        self.assertEqual(rows["pending_rows_loaded_count"], "2")
        self.assertEqual(rows["lookup_attempt_count"], "0")
        self.assertFalse(self.results_path.exists())

    def test_mock_run_writes_sanitized_result_to_outbox(self):
        self.write_pending([fixture_job()])
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "dry_run_only")
        self.assertEqual(rows["gate"], "shared_folder_lookup_bridge_handoff")
        self.assertEqual(rows["transport"], "private_host_vm_shared_folder")
        self.assertEqual(rows["n8n_runtime_location"], "main_physical_pc")
        self.assertEqual(rows["runtime_location"], "autocount_vm_bridge_worker")
        self.assertEqual(rows["pending_rows_loaded_count"], "1")
        self.assertEqual(rows["lookup_attempt_count"], "1")
        self.assertEqual(rows["lookup_success_count"], "1")
        self.assertEqual(rows["lookup_error_count"], "0")
        self.assertEqual(rows["idempotency_markers_outside_share"], "true")
        self.assertEqual(rows["member_create_or_update_invoked"], "false")
        self.assertEqual(rows["autocount_write_attempted"], "false")
        self.assertEqual(rows["direct_sql_write_attempted"], "false")
        self.assertEqual(rows["n8n_result_mapping_run"], "false")
        self.assertEqual(rows["workflow_activation"], "inactive")
        self.assertEqual(rows["queue_api_used"], "false")
        self.assertEqual(rows["tunnel_or_reverse_proxy_used"], "false")
        self.assertEqual(rows["webhook_used"], "false")
        self.assertEqual(rows["public_inbound_to_ac2_host"], "false")
        self.assertEqual(rows["final_write_automation"], "false")

        result_lines = [
            line
            for line in self.results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(result_lines), 1)
        result = json.loads(result_lines[0])
        self.assertEqual(set(result), ALLOWED_RESULT_FIELDS)
        self.assertTrue(result["dry_run_only"])
        self.assertFalse(result["final_write_automation"])
        self.assertNotIn("submitted_member_no_base64_utf8", result)

        marker_files = list(self.processed_dir.glob("*.json"))
        self.assertEqual(len(marker_files), 1)
        self.assertTrue(str(marker_files[0]).startswith(str(self.processed_dir)))

    def test_rerun_after_success_reports_already_processed(self):
        self.write_pending([fixture_job()])
        first = self.run_script()
        self.assertEqual(first.returncode, 0)
        second = self.run_script()
        self.assertEqual(second.returncode, 0)
        rows = evidence_map(second.stdout)
        self.assertEqual(rows["status"], "already_processed")
        self.assertEqual(rows["lookup_attempt_count"], "0")
        self.assertEqual(rows["duplicate_or_already_processed_count"], "1")
        result_lines = [
            line
            for line in self.results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(result_lines), 1)

    def test_needs_fix_on_stale_outbox_result_with_fresh_pending_row(self):
        self.write_pending([fixture_job()])
        self.results_path.write_text('{"stale": true}\n', encoding="utf-8")
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        rows = evidence_map(proc.stdout)
        self.assertEqual(rows["status"], "needs_fix")
        self.assertEqual(rows["lookup_attempt_count"], "0")
        self.assertEqual(
            self.results_path.read_text(encoding="utf-8"),
            '{"stale": true}\n',
        )


class SharedFolderHandoffDocsTest(unittest.TestCase):
    def test_runbook_exists_and_documents_contract_files(self):
        self.assertTrue(SHARED_FOLDER_RUNBOOK.exists())
        text = SHARED_FOLDER_RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(PENDING_FILENAME, text)
        self.assertIn(RESULT_FILENAME, text)
        self.assertIn("member_lookup_shared_folder_handoff.py", text)
        self.assertIn("Manual Trigger", text)
        self.assertIn("inactive", text)
        for forbidden in ("member create", "direct SQL"):
            self.assertIn(forbidden, text)

    def test_script_has_no_write_path_tokens(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for token in ("Save" + "Member", "New" + "Member", "Delete" + "Member"):
            self.assertNotIn(token, text)
        self.assertNotIn("INSERT ", text)
        self.assertNotIn("UPDATE ", text)

    def test_gitignore_covers_shared_folder_local_artifacts(self):
        text = GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("member_lookup_bridge_shared_folder_processed/", text)
        self.assertIn("member_lookup_bridge_shared_folder_failed/", text)

    def test_readme_mentions_handoff_script_and_runbook(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("scripts/member_lookup_shared_folder_handoff.py", text)
        self.assertIn(
            "docs/autocount2-automation/member_intake_shared_folder_lookup_bridge_runbook.md",
            text,
        )


if __name__ == "__main__":
    unittest.main()
