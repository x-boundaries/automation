import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "member_lookup_gate4_real_queue_lookup.py"
GITIGNORE = ROOT / ".gitignore"
README = ROOT / "README.md"
DOCS = ROOT / "docs" / "autocount2-automation"
BRIDGE_RUNBOOK = DOCS / "member_intake_local_lookup_bridge_runbook.md"
WORKFLOW_TEMPLATE = ROOT / "n8n-workflows" / "member_intake_gate4a_container_queue_write.workflow.json"

SOURCE_LABEL = "google_sheets_uat_gate4a"
CANONICAL_HASH_FIELDS = (
    "intake_source",
    "source_reference",
    "source_row_ref",
    "row_number",
    "intake_id",
    "state",
    "submitted_member_no_base64_utf8",
    "pdpa_status",
)

FORBIDDEN_WRITE_TOKENS = [
    "Save" + "Member",
    "New" + "Member",
    "Delete" + "Member",
    "GetNext" + "MemberNo",
]

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

EXPECTED_EVIDENCE_KEYS = [
    "status",
    "gate",
    "runtime_location",
    "execution_mode",
    "lookup_mode",
    "powershell_lookup_enabled",
    "ac2_lookup_invoked",
    "approved_batch_size",
    "queue_rows_read_count",
    "lookup_attempt_count",
    "lookup_success_count",
    "lookup_existing_member_review_count",
    "lookup_manual_review_count",
    "lookup_ready_for_create_review_count",
    "lookup_error_count",
    "review_rows_written_count",
    "member_create_or_update_invoked",
    "autocount_write_attempted",
    "direct_sql_write_attempted",
    "n8n_result_mapping_run",
    "workflow_activation",
    "scheduler_enabled",
    "public_inbound_to_ac2_host",
    "final_write_automation",
    "no_row_values_printed",
]


def fnv1a_hex(text):
    hash_value = 0x811C9DC5
    for char in text:
        hash_value ^= ord(char)
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return format(hash_value, "08x")


def canonical_payload_hash(row):
    canonical = {field: row.get(field) for field in CANONICAL_HASH_FIELDS}
    serialized = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return "fnv1a_" + fnv1a_hex(serialized)


def canonical_queue_row(*, row_number=2, member_digits="123456", **overrides):
    """Build a self-consistent canonical Gate 4A queue row (valid identity by default).

    Overrides are applied AFTER the canonical hash/job_id are computed, so overriding a
    hashed field leaves a stale hash (used for tamper tests).
    """
    encoded = base64.b64encode(member_digits.encode("utf-8")).decode("ascii")
    source_row_ref = f"row_{row_number}"
    intake_id = f"gate4a_{source_row_ref}"
    hash_source = {
        "intake_source": SOURCE_LABEL,
        "source_reference": SOURCE_LABEL,
        "source_row_ref": source_row_ref,
        "row_number": row_number,
        "intake_id": intake_id,
        "state": "PENDING_LOOKUP",
        "submitted_member_no_base64_utf8": encoded,
        "pdpa_status": "yes",
    }
    payload_hash = canonical_payload_hash(hash_source)
    row = {
        "job_id": f"gate4a_{payload_hash}",
        "intake_source": SOURCE_LABEL,
        "source_reference": SOURCE_LABEL,
        "source_row_ref": source_row_ref,
        "row_number": row_number,
        "intake_id": intake_id,
        "state": "PENDING_LOOKUP",
        "submitted_member_no_base64_utf8": encoded,
        "consent_status": "marketing_consent_not_queued",
        "pdpa_status": "yes",
        "payload_hash": payload_hash,
        "attempt": 0,
        "max_attempts": 1,
        "created_at": "safe-created-at",
        "updated_at": "safe-created-at",
        "timeout_at": "safe-timeout-at",
    }
    row.update(overrides)
    return row


def result_row(job_id, *, state="READY_FOR_CREATE_REVIEW", member_exists=False):
    return {
        "job_id": job_id,
        "intake_source": SOURCE_LABEL,
        "source_reference": SOURCE_LABEL,
        "source_row_ref": "row_2",
        "row_number": 2,
        "state": state,
        "status": "ok",
        "authentication_success": True,
        "user_session_available": True,
        "member_command_found": True,
        "get_member_found": True,
        "submitted_member_no_status": "canonical_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": member_exists,
        "member_found_by": "MemberCommand.GetMember" if member_exists else None,
        "manual_review_required": False,
        "warning_count": 0,
        "error_code": None,
        "consent_status": "marketing_consent_not_queued",
        "pdpa_status": "yes",
        "attempt": 0,
        "dry_run_only": True,
        "final_write_automation": False,
        "result_created_at": "fixture-time",
        "result_applied_at": None,
    }


def marker_json(job_id, *, marker_type="processed", state="READY_FOR_CREATE_REVIEW", payload_hash=None):
    return {
        "marker_type": marker_type,
        "job_id": job_id,
        "payload_hash": payload_hash,
        "state": state,
        "status": "ok",
        "error_code": None,
        "dry_run_only": True,
        "final_write_automation": False,
        "marked_at": "fixture-time",
    }


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def nonblank_lines(path):
    p = Path(path)
    if not p.exists():
        return 0
    return len([line for line in p.read_text(encoding="utf-8").splitlines() if line.strip()])


def write_marker_file(directory, job_id, marker):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{job_id}.json").write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")


def write_marker_named(directory, filename, marker):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")


def write_raw_file(directory, filename, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(text, encoding="utf-8")


def write_fake_powershell(path, *, status="ok"):
    payload = {
        "status": status,
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
        "error": None if status == "ok" else {"type": "mock_lookup_error"},
    }
    path.write_text(f"@echo off\r\necho {json.dumps(payload, sort_keys=True)}\r\n", encoding="utf-8")


def parse_pairs(text):
    return [tuple(line.split(" = ", 1)) for line in text.splitlines() if " = " in line]


def parse_evidence(text):
    return dict(parse_pairs(text))


class Gate4RealQueueLookupTests(unittest.TestCase):
    def paths(self, tmp_path):
        return {
            "queue": tmp_path / "member_lookup_bridge_gate4_pending_queue.jsonl",
            "results": tmp_path / "member_lookup_bridge_gate4_results.jsonl",
            "processed": tmp_path / "member_lookup_bridge_gate4_processed",
            "failed": tmp_path / "member_lookup_bridge_gate4_failed",
        }

    def fake_ps(self, tmp_path, *, status="ok"):
        exe = tmp_path / "fake-powershell.cmd"
        script = tmp_path / "fake-lookup-script.ps1"
        write_fake_powershell(exe, status=status)
        script.write_text("# fake read-only lookup script path only\n", encoding="utf-8")
        return ["--powershell-exe", str(exe), "--lookup-script", str(script)]

    def args(self, paths, *, gate4_opt_in=True, ps_opt_in=True, extra=None):
        arguments = []
        if gate4_opt_in:
            arguments.append("--enable-gate4-real-queue-lookup")
        if ps_opt_in:
            arguments.append("--enable-powershell-lookup")
        arguments += [
            "--queue-jsonl",
            str(paths["queue"]),
            "--results-jsonl",
            str(paths["results"]),
            "--processed-dir",
            str(paths["processed"]),
            "--failed-dir",
            str(paths["failed"]),
        ]
        if extra:
            arguments += extra
        return arguments

    def run_cli(self, arguments, *, env=None):
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + arguments,
            text=True,
            capture_output=True,
            check=False,
            env=run_env,
        )

    # --- 1-3: opt-in / mock absence -------------------------------------

    def test_missing_gate4_opt_in_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.paths(Path(tmp))
            write_jsonl(paths["queue"], [canonical_queue_row()])
            completed = self.run_cli(self.args(paths, gate4_opt_in=False))
            self.assertEqual(completed.returncode, 2)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "refused")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertFalse(paths["results"].exists())

    def test_missing_powershell_opt_in_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.paths(Path(tmp))
            write_jsonl(paths["queue"], [canonical_queue_row()])
            completed = self.run_cli(self.args(paths, ps_opt_in=False))
            self.assertEqual(completed.returncode, 2)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "refused")
            self.assertEqual(evidence["powershell_lookup_enabled"], "false")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

    def test_public_mock_mode_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.paths(Path(tmp))
            write_jsonl(paths["queue"], [canonical_queue_row()])
            for mock_flag in (["--lookup-mode", "mock"], ["--fixture-mock-results", str(paths["results"])]):
                completed = self.run_cli(self.args(paths, extra=mock_flag))
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("status = ok", completed.stdout)

    def test_mock_execution_can_never_produce_ok(self):
        # There is no mock route at all; the only lookup path is PowerShell.
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--lookup-mode", source)
        self.assertNotIn("--fixture-mock-results", source)
        self.assertNotIn("mock_lookup", source)
        self.assertIn('LOOKUP_MODE = "powershell"', source)

    # --- 5-6: successful PowerShell path --------------------------------

    def test_valid_fake_powershell_success_writes_one_result_and_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "ok")
            self.assertEqual(evidence["lookup_mode"], "powershell")
            self.assertEqual(evidence["powershell_lookup_enabled"], "true")
            self.assertEqual(evidence["ac2_lookup_invoked"], "true")
            self.assertEqual(evidence["approved_batch_size"], "1")
            self.assertEqual(evidence["queue_rows_read_count"], "1")
            self.assertEqual(evidence["lookup_attempt_count"], "1")
            self.assertEqual(evidence["lookup_success_count"], "1")
            self.assertEqual(evidence["lookup_ready_for_create_review_count"], "1")
            self.assertEqual(evidence["lookup_existing_member_review_count"], "0")
            self.assertEqual(evidence["lookup_manual_review_count"], "0")
            self.assertEqual(evidence["lookup_error_count"], "0")
            self.assertEqual(evidence["review_rows_written_count"], "1")

            result_rows = [json.loads(line) for line in paths["results"].read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(result_rows), 1)
            self.assertEqual(set(result_rows[0]), ALLOWED_RESULT_FIELDS)
            self.assertEqual(len(list(paths["processed"].glob("*.json"))), 1)
            self.assertFalse(paths["failed"].exists())

    def test_evidence_reports_powershell_mode_and_invocation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            pairs = parse_pairs(completed.stdout)
            self.assertEqual([key for key, _ in pairs], EXPECTED_EVIDENCE_KEYS)

    def test_canonical_six_and_twenty_digit_values_pass_validation(self):
        for digits in ("1" * 6, "1" * 20):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths = self.paths(tmp_path)
                write_jsonl(paths["queue"], [canonical_queue_row(member_digits=digits)])
                completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(parse_evidence(completed.stdout)["status"], "ok")

    # --- 7-12: numeric member-number contract ---------------------------

    def _assert_rejected_before_lookup(self, paths, tmp_path, row):
        write_jsonl(paths["queue"], [row])
        completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
        self.assertEqual(completed.returncode, 2)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        self.assertFalse(paths["processed"].exists())
        return completed

    def test_nonnumeric_five_twentyone_and_punctuation_values_fail_before_lookup(self):
        bad_values = ["12345", "1" * 21, "12 3456", "12-3456", "12+3456", "12A456", "1234.6"]
        for value in bad_values:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths = self.paths(tmp_path)
                completed = self._assert_rejected_before_lookup(paths, tmp_path, canonical_queue_row(member_digits=value))
                self.assertNotIn(value, completed.stdout)
                self.assertNotIn(value, completed.stderr)

    # --- 13-15: canonical payload identity ------------------------------

    def test_tampered_payload_hash_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            self._assert_rejected_before_lookup(paths, tmp_path, canonical_queue_row(payload_hash="fnv1a_deadbeef"))

    def test_tampered_job_id_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            self._assert_rejected_before_lookup(paths, tmp_path, canonical_queue_row(job_id="gate4a_fnv1a_00000000"))

    def test_modified_encoded_member_with_stale_hash_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            swapped = base64.b64encode(b"999999").decode("ascii")
            self._assert_rejected_before_lookup(
                paths, tmp_path, canonical_queue_row(submitted_member_no_base64_utf8=swapped)
            )

    def test_modified_row_number_with_stale_hash_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            # Consistent row for row_number=2, then mutate only row_number so hash and
            # derived source_row_ref/intake_id are stale.
            tampered = canonical_queue_row()
            tampered["row_number"] = 3
            self._assert_rejected_before_lookup(paths, tmp_path, tampered)

    def test_tampered_source_row_ref_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            self._assert_rejected_before_lookup(paths, tmp_path, canonical_queue_row(source_row_ref="row_999"))

    def test_tampered_intake_id_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            self._assert_rejected_before_lookup(paths, tmp_path, canonical_queue_row(intake_id="gate4a_row_999"))

    # --- precheck-level fail-closed validation --------------------------

    def test_missing_queue_file_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.paths(Path(tmp))
            completed = self.run_cli(self.args(paths))
            self.assertEqual(completed.returncode, 2)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["queue_rows_read_count"], "0")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

    def test_zero_rows_more_than_one_row_and_malformed_json_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            # zero rows
            paths["queue"].write_text("", encoding="utf-8")
            self.assertEqual(parse_evidence(self.run_cli(self.args(paths)).stdout)["status"], "needs_fix")
            # more than one row
            write_jsonl(paths["queue"], [canonical_queue_row(), canonical_queue_row(row_number=3)])
            self.assertEqual(parse_evidence(self.run_cli(self.args(paths)).stdout)["status"], "needs_fix")
            # malformed json -> dead-letter discipline
            paths["queue"].write_text("{not valid json\n", encoding="utf-8")
            completed = self.run_cli(self.args(paths))
            self.assertEqual(parse_evidence(completed.stdout)["status"], "needs_fix")
            self.assertTrue(paths["failed"].exists())
            self.assertGreaterEqual(len(list(paths["failed"].glob("*.json"))), 1)

    def test_unexpected_fields_and_invalid_pdpa_and_bad_state_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            for row in (
                canonical_queue_row(name="Forbidden Person"),
                canonical_queue_row(pdpa_status="imported"),
                canonical_queue_row(state="LOOKUP_IN_PROGRESS"),
            ):
                write_jsonl(paths["queue"], [row])
                completed = self.run_cli(self.args(paths))
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix")
                self.assertEqual(evidence["lookup_attempt_count"], "0")

    def test_dummy_marker_is_rejected_without_echoing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            dummy_encoded = base64.b64encode(b"rehearsal").decode("ascii")
            write_jsonl(paths["queue"], [canonical_queue_row(submitted_member_no_base64_utf8=dummy_encoded)])
            completed = self.run_cli(self.args(paths))
            self.assertEqual(parse_evidence(completed.stdout)["status"], "needs_fix")
            self.assertNotIn("rehearsal", completed.stdout)
            self.assertNotIn(dummy_encoded, completed.stdout)

    # --- 16-22: processed/failed state machine and durability -----------

    def test_valid_processed_marker_with_matching_result_returns_already_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])
            first = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            self.assertEqual(parse_evidence(first.stdout)["status"], "ok")

            second = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            self.assertEqual(second.returncode, 0, second.stderr)
            evidence = parse_evidence(second.stdout)
            self.assertEqual(evidence["status"], "already_processed")
            self.assertEqual(evidence["lookup_attempt_count"], "0")
            self.assertEqual(evidence["lookup_success_count"], "0")
            self.assertEqual(evidence["review_rows_written_count"], "0")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            rows = [line for line in paths["results"].read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)

    def test_failed_lookup_then_rerun_stays_needs_fix_never_already_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])

            first = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path, status="error")))
            self.assertEqual(first.returncode, 2)
            first_evidence = parse_evidence(first.stdout)
            self.assertEqual(first_evidence["status"], "needs_fix")
            self.assertEqual(first_evidence["lookup_error_count"], "1")
            self.assertEqual(first_evidence["ac2_lookup_invoked"], "true")
            self.assertEqual(len(list(paths["failed"].glob("*.json"))), 1)

            second = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            self.assertEqual(second.returncode, 2)
            second_evidence = parse_evidence(second.stdout)
            self.assertEqual(second_evidence["status"], "needs_fix")
            self.assertNotEqual(second_evidence["status"], "already_processed")
            self.assertEqual(second_evidence["ac2_lookup_invoked"], "false")

    def test_preexisting_failed_marker_never_becomes_already_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            row = canonical_queue_row()
            write_jsonl(paths["queue"], [row])
            write_marker_file(
                paths["failed"],
                row["job_id"],
                marker_json(row["job_id"], marker_type="dead_letter", state="LOOKUP_ERROR_REVIEW", payload_hash=row["payload_hash"]),
            )
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

    def test_processed_marker_without_result_returns_needs_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            row = canonical_queue_row()
            write_jsonl(paths["queue"], [row])
            write_marker_file(
                paths["processed"],
                row["job_id"],
                marker_json(row["job_id"], payload_hash=row["payload_hash"]),
            )
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

    def test_result_without_marker_does_not_rerun_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            row = canonical_queue_row()
            write_jsonl(paths["queue"], [row])
            write_jsonl(paths["results"], [result_row(row["job_id"])])
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            rows = [line for line in paths["results"].read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)

    def test_simulated_result_write_failure_cannot_later_become_already_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])

            faulted = self.run_cli(
                self.args(paths, extra=self.fake_ps(tmp_path)),
                env={"GATE4_TEST_FAULT_INJECT": "after_result_before_marker"},
            )
            self.assertEqual(faulted.returncode, 2)
            self.assertEqual(parse_evidence(faulted.stdout)["status"], "needs_fix")
            self.assertFalse(paths["processed"].exists())

            rerun = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            evidence = parse_evidence(rerun.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertNotEqual(evidence["status"], "already_processed")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

    def test_payload_conflict_stays_needs_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            row = canonical_queue_row()
            write_jsonl(paths["queue"], [row])
            write_marker_file(
                paths["processed"],
                row["job_id"],
                marker_json(row["job_id"], payload_hash="fnv1a_00000000"),
            )
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

    # --- 23-24: evidence and privacy ------------------------------------

    def test_exactly_one_recognised_review_routing_state_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])
            evidence = parse_evidence(self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path))).stdout)
            routing = [
                int(evidence["lookup_existing_member_review_count"]),
                int(evidence["lookup_manual_review_count"]),
                int(evidence["lookup_ready_for_create_review_count"]),
            ]
            self.assertEqual(sum(routing), 1)
            self.assertEqual(routing.count(1), 1)
            self.assertEqual(routing.count(0), 2)

    def test_no_member_values_appear_in_stdout_or_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            row = canonical_queue_row(member_digits="654321")
            write_jsonl(paths["queue"], [row])
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            for forbidden in [
                "654321",
                row["submitted_member_no_base64_utf8"],
                "submitted_member_no_base64_utf8",
                row["job_id"],
            ]:
                self.assertNotIn(forbidden, completed.stdout)
                self.assertNotIn(forbidden, completed.stderr)
            result_text = paths["results"].read_text(encoding="utf-8")
            self.assertNotIn("654321", result_text)
            self.assertNotIn(row["submitted_member_no_base64_utf8"], result_text)

    # --- 25 + static guards ---------------------------------------------

    def test_script_has_no_write_member_api_or_direct_sql(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, source, token)
        import re as _re
        self.assertIsNone(
            _re.search(r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b", source)
        )
        for activation_token in ["scheduleTrigger", "cloudflared", "Task Scheduler", "win32serviceutil", "subprocess.Popen"]:
            self.assertNotIn(activation_token, source)

    def test_gate4_artifacts_are_gitignored_and_wired(self):
        gitignore = GITIGNORE.read_text(encoding="utf-8")
        for pattern in [
            "member_lookup_bridge_gate4_pending_queue.jsonl",
            "member_lookup_bridge_gate4_results.jsonl",
            "member_lookup_bridge_gate4_processed/",
            "member_lookup_bridge_gate4_failed/",
        ]:
            self.assertIn(pattern, gitignore)
        self.assertIn("scripts/member_lookup_gate4_real_queue_lookup.py", README.read_text(encoding="utf-8"))

    def test_runbook_documents_powershell_only_and_state_machine(self):
        runbook = BRIDGE_RUNBOOK.read_text(encoding="utf-8")
        for phrase in [
            "## Gate 4 Real Queue UAT AC2 Lookup-Only Handoff",
            "Gate 4 PASS is PowerShell-only",
            "Mock mode cannot satisfy Gate 4",
            "lookup_mode = powershell",
            "powershell_lookup_enabled = true",
            "ac2_lookup_invoked = <true/false>",
            "matching durable sanitized result",
            "does not map results back to n8n",
            "n8n result mapping is the next separate gate and is not performed here",
            "does not create, update, or delete an AutoCount member",
            "does not perform direct SQL",
            "Operators must not delete result or marker files merely to force a rerun.",
            "The real AC2 lookup must not be run during this PR amendment.",
            "member_create_or_update_invoked = false",
            "autocount_write_attempted = false",
            "direct_sql_write_attempted = false",
            "The dedicated Gate 4 result file must be absent or empty before a fresh lookup.",
            "malformed, partial, duplicate, stale, unrelated, or unexpected result content",
            "Processed-marker validity is mandatory.",
            "Both marker directories and the result file must be absent or empty before a fresh Gate 4 lookup.",
            "Any marker artifact at all blocks a fresh lookup",
            "`already_processed` requires exactly one valid processed marker, zero failed markers, and exactly one matching valid result.",
            "Fixed queue retry-contract fields.",
            "A missing lookup script is a precondition failure",
            "recovery requires reviewed instructions",
        ]:
            self.assertIn(phrase, runbook)

    def test_payload_hash_matches_canonical_workflow(self):
        """Cross-check the Python FNV-1a port against the canonical n8n workflow (needs node)."""
        node_exe = shutil.which("node")
        if not node_exe:
            raise unittest.SkipTest("node executable is required for the workflow parity cross-check")

        template = json.loads(WORKFLOW_TEMPLATE.read_text(encoding="utf-8"))
        code = next(
            node["parameters"]["jsCode"]
            for node in template["nodes"]
            if node["name"] == "Validate And Build One Sanitized Queue Row"
        )
        source_row = {
            "Full Name": "source-name-present",
            "AutoCount MemberNo": "1" * 6,
            "Email Address": "source-email-present",
            "Birthday Month": "June",
            "Marketing Consent": "optional-marketing-source-value",
            "PDPA Acknowledged": "Yes",
            "Gate4AApprovedForLookup": "YES",
            "row_number": 2,
        }
        wrapper = (
            "const fs=require('fs');const p=JSON.parse(fs.readFileSync(0,'utf8'));"
            "const run=new Function('$input','Buffer',p.code);"
            "const r=run({all:()=>p.rows.map(json=>({json}))},Buffer);"
            "const b=r[0].binary.data.data;"
            "process.stdout.write(Buffer.from(b,'base64').toString('utf8'));"
        )
        completed = subprocess.run(
            [node_exe, "-e", wrapper],
            input=json.dumps({"code": code, "rows": [source_row]}),
            text=True,
            capture_output=True,
            check=True,
        )
        workflow_row = json.loads(completed.stdout.strip().splitlines()[0])
        # The wrapper must accept a genuinely workflow-produced row end-to-end.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [workflow_row])
            result = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(parse_evidence(result.stdout)["status"], "ok")
        # And the Python recompute must equal the workflow-emitted hash/job_id exactly.
        self.assertEqual(canonical_payload_hash(workflow_row), workflow_row["payload_hash"])
        self.assertEqual(f"gate4a_{workflow_row['payload_hash']}", workflow_row["job_id"])

    # --- strict result-artifact inspection (blocker 1) ------------------

    def _seed_success(self, tmp_path):
        paths = self.paths(tmp_path)
        row = canonical_queue_row()
        write_jsonl(paths["queue"], [row])
        completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
        self.assertEqual(parse_evidence(completed.stdout)["status"], "ok", completed.stdout)
        return paths, row

    def _rerun(self, paths, tmp_path):
        return self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))

    def test_fresh_success_leaves_single_result_and_single_processed_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, _ = self._seed_success(tmp_path)
            result_lines = [l for l in paths["results"].read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(result_lines), 1)
            self.assertEqual(len(list(paths["processed"].glob("*.json"))), 1)
            self.assertFalse(paths["failed"].exists())

    def test_preexisting_malformed_result_blocks_and_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])
            paths["results"].parent.mkdir(parents=True, exist_ok=True)
            paths["results"].write_text("{partial malformed line\n", encoding="utf-8")
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["lookup_attempt_count"], "0")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertFalse(paths["processed"].exists())
            # The malformed line is neither consumed nor followed by a fresh lookup append.
            self.assertEqual(paths["results"].read_text(encoding="utf-8"), "{partial malformed line\n")

    def test_unrelated_result_row_blocks_fresh_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])
            write_jsonl(paths["results"], [result_row("gate4a_fnv1a_22222222")])
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertFalse(paths["processed"].exists())
            self.assertEqual(len([l for l in paths["results"].read_text(encoding="utf-8").splitlines() if l.strip()]), 1)

    def test_multiple_result_rows_block_fresh_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            row = canonical_queue_row()
            write_jsonl(paths["queue"], [row])
            write_jsonl(paths["results"], [result_row(row["job_id"]), result_row(row["job_id"])])
            completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

    # --- processed-marker field validity (blocker 2) --------------------

    def test_processed_marker_field_variants_all_need_fix(self):
        def valid_marker(row):
            return marker_json(row["job_id"], payload_hash=row["payload_hash"])

        variants = {
            "missing_hash": lambda row: {k: v for k, v in valid_marker(row).items() if k != "payload_hash"},
            "null_hash": lambda row: {**valid_marker(row), "payload_hash": None},
            "invalid_hash": lambda row: {**valid_marker(row), "payload_hash": "not-the-expected-hash"},
            "wrong_marker_type": lambda row: {**valid_marker(row), "marker_type": "dead_letter"},
            "invalid_state": lambda row: {**valid_marker(row), "state": "LOOKUP_ERROR_REVIEW"},
            "dry_run_false": lambda row: {**valid_marker(row), "dry_run_only": False},
            "final_write_true": lambda row: {**valid_marker(row), "final_write_automation": True},
        }
        for label, build in variants.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, row = self._seed_success(tmp_path)
                write_marker_file(paths["processed"], row["job_id"], build(row))
                completed = self._rerun(paths, tmp_path)
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix", label)
                self.assertNotEqual(evidence["status"], "already_processed", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)

    # --- durable-result identity/routing validation (blocker 3) ---------

    def _write_result_lines(self, paths, updated_row=None, extra_rows=None, raw_extra=None):
        rows = [json.loads(l) for l in paths["results"].read_text(encoding="utf-8").splitlines() if l.strip()]
        if updated_row is not None:
            rows[0] = updated_row
        lines = [json.dumps(r, sort_keys=True) for r in rows]
        for extra in extra_rows or []:
            lines.append(json.dumps(extra, sort_keys=True))
        for raw in raw_extra or []:
            lines.append(raw)
        paths["results"].write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_tampered_durable_result_needs_fix_without_lookup(self):
        field_updates = {
            "status_error": {"status": "error"},
            "auth_false": {"authentication_success": False},
            "session_unavailable": {"user_session_available": False},
            "member_command_missing": {"member_command_found": False},
            "get_member_missing": {"get_member_found": False},
            "wrong_source_reference": {"source_reference": "safe-wrong-ref"},
            "wrong_row_number": {"row_number": 3},
            "wrong_pdpa": {"pdpa_status": "no"},
            "wrong_attempt": {"attempt": 5},
            "routing_vs_member_exists": {"member_exists": True},
            "routing_vs_warning": {"warning_count": 1},
            "error_code_present": {"error_code": "some_error"},
            "applied_at_set": {"result_applied_at": "2026-07-13T00:00:00+00:00"},
        }
        for label, update in field_updates.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self._seed_success(tmp_path)
                current = [json.loads(l) for l in paths["results"].read_text(encoding="utf-8").splitlines() if l.strip()][0]
                self._write_result_lines(paths, updated_row={**current, **update})
                completed = self._rerun(paths, tmp_path)
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)

    def test_extra_duplicate_malformed_or_nonobject_result_rows_need_fix(self):
        cases = {
            "extra_unrelated_row": {"extra_rows": [result_row("gate4a_fnv1a_33333333")]},
            "duplicate_matching_row": "DUPLICATE",
            "malformed_line": {"raw_extra": ["{not valid json"]},
            "non_object_line": {"raw_extra": ['"just-a-string"']},
        }
        for label, spec in cases.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths, _ = self._seed_success(tmp_path)
                if spec == "DUPLICATE":
                    current = [json.loads(l) for l in paths["results"].read_text(encoding="utf-8").splitlines() if l.strip()][0]
                    self._write_result_lines(paths, extra_rows=[current])
                else:
                    self._write_result_lines(paths, **spec)
                completed = self._rerun(paths, tmp_path)
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix", label)
                self.assertNotEqual(evidence["status"], "already_processed", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)

    # --- invocation accuracy --------------------------------------------

    def test_missing_lookup_script_reports_no_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])
            fake_exe = tmp_path / "fake-powershell.cmd"
            write_fake_powershell(fake_exe)
            missing_script = tmp_path / "missing_lookup_script.ps1"
            completed = self.run_cli(
                self.args(paths, extra=["--powershell-exe", str(fake_exe), "--lookup-script", str(missing_script)])
            )
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["lookup_attempt_count"], "0")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertFalse(paths["results"].exists())
            self.assertFalse(paths["processed"].exists())

    # --- persistence: fault after result before marker ------------------

    def test_fault_after_result_before_marker_then_rerun_needs_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = self.paths(tmp_path)
            write_jsonl(paths["queue"], [canonical_queue_row()])
            faulted = self.run_cli(
                self.args(paths, extra=self.fake_ps(tmp_path)),
                env={"GATE4_TEST_FAULT_INJECT": "after_result_before_marker"},
            )
            self.assertEqual(faulted.returncode, 2)
            faulted_evidence = parse_evidence(faulted.stdout)
            self.assertEqual(faulted_evidence["status"], "needs_fix")
            # A durable result was written before the marker: report the actual count, not 0.
            self.assertEqual(faulted_evidence["review_rows_written_count"], "1")
            self.assertFalse(paths["processed"].exists())
            self.assertEqual(len([l for l in paths["results"].read_text(encoding="utf-8").splitlines() if l.strip()]), 1)

            rerun = self._rerun(paths, tmp_path)
            evidence = parse_evidence(rerun.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["lookup_attempt_count"], "0")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertNotEqual(evidence["status"], "already_processed")

    # --- strict marker-artifact inspection (this amendment) -------------

    def _assert_marker_blocks_fresh_lookup(self, tmp_path, *, processed_files=None, failed_files=None):
        """Seed marker artifacts with an empty results file and assert the lookup is blocked."""
        paths = self.paths(tmp_path)
        write_jsonl(paths["queue"], [canonical_queue_row()])
        for directory, files in ((paths["processed"], processed_files or []), (paths["failed"], failed_files or [])):
            for filename, payload in files:
                if isinstance(payload, str):
                    write_raw_file(directory, filename, payload)
                else:
                    write_marker_named(directory, filename, payload)
        completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertEqual(evidence["lookup_attempt_count"], "0")
        self.assertEqual(evidence["ac2_lookup_invoked"], "false")
        # No fresh lookup ran: no result row is appended.
        self.assertEqual(nonblank_lines(paths["results"]), 0)
        return completed

    def test_empty_marker_dirs_permit_fresh_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, _ = self._seed_success(tmp_path)
            self.assertEqual(len(list(paths["processed"].glob("*.json"))), 1)

    def test_malformed_processed_marker_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            self._assert_marker_blocks_fresh_lookup(
                tmp_path, processed_files=[(f"{row['job_id']}.json", "{not valid json")]
            )

    def test_malformed_failed_marker_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            self._assert_marker_blocks_fresh_lookup(
                tmp_path, failed_files=[(f"{row['job_id']}.json", "{not valid json")]
            )

    def test_non_object_marker_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            self._assert_marker_blocks_fresh_lookup(
                tmp_path, processed_files=[(f"{row['job_id']}.json", '"just-a-string"')]
            )

    def test_marker_named_for_job_but_wrong_content_job_id_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            marker = marker_json("gate4a_fnv1a_00000000", payload_hash=row["payload_hash"])
            self._assert_marker_blocks_fresh_lookup(
                tmp_path, processed_files=[(f"{row['job_id']}.json", marker)]
            )

    def test_marker_without_job_id_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            marker = {k: v for k, v in marker_json(row["job_id"], payload_hash=row["payload_hash"]).items() if k != "job_id"}
            self._assert_marker_blocks_fresh_lookup(
                tmp_path, processed_files=[(f"{row['job_id']}.json", marker)]
            )

    def test_unrelated_processed_marker_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            other = "gate4a_fnv1a_aaaaaaaa"
            self._assert_marker_blocks_fresh_lookup(
                tmp_path, processed_files=[(f"{other}.json", marker_json(other, payload_hash="fnv1a_aaaaaaaa"))]
            )

    def test_unrelated_failed_marker_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            other = "gate4a_fnv1a_bbbbbbbb"
            self._assert_marker_blocks_fresh_lookup(
                tmp_path,
                failed_files=[(f"{other}.json", marker_json(other, marker_type="dead_letter", state="LOOKUP_ERROR_REVIEW", payload_hash="fnv1a_bbbbbbbb"))],
            )

    def test_duplicate_processed_markers_block_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            marker = marker_json(row["job_id"], payload_hash=row["payload_hash"])
            self._assert_marker_blocks_fresh_lookup(
                tmp_path,
                processed_files=[(f"{row['job_id']}.json", marker), (f"{row['job_id']}-copy.json", marker)],
            )

    def test_duplicate_failed_markers_block_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            marker = marker_json(row["job_id"], marker_type="dead_letter", state="LOOKUP_ERROR_REVIEW", payload_hash=row["payload_hash"])
            self._assert_marker_blocks_fresh_lookup(
                tmp_path,
                failed_files=[(f"{row['job_id']}.json", marker), (f"{row['job_id']}-copy.json", marker)],
            )

    def test_temporary_marker_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            self._assert_marker_blocks_fresh_lookup(
                tmp_path, processed_files=[(f"{row['job_id']}.json.tmp", marker_json(row["job_id"], payload_hash=row["payload_hash"]))]
            )

    def test_unexpected_file_in_marker_dir_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._assert_marker_blocks_fresh_lookup(tmp_path, processed_files=[("operator-notes.txt", "not a marker")])

    def test_processed_marker_with_unexpected_filename_blocks_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            self._assert_marker_blocks_fresh_lookup(
                tmp_path, processed_files=[("weirdname.json", marker_json(row["job_id"], payload_hash=row["payload_hash"]))]
            )

    def test_wrong_job_id_marker_blocks_with_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            row = canonical_queue_row()
            # Invalid marker present (filename = job, content job_id wrong), results empty.
            marker = marker_json("gate4a_fnv1a_99999999", payload_hash=row["payload_hash"])
            completed = self._assert_marker_blocks_fresh_lookup(
                tmp_path, processed_files=[(f"{row['job_id']}.json", marker)]
            )
            self.assertNotEqual(parse_evidence(completed.stdout)["status"], "already_processed")

    def test_valid_marker_plus_extra_marker_needs_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, row = self._seed_success(tmp_path)
            # Add an extra unrelated marker artifact next to the valid one.
            write_marker_named(paths["processed"], "gate4a_fnv1a_cccccccc.json", marker_json("gate4a_fnv1a_cccccccc", payload_hash="fnv1a_cccccccc"))
            completed = self._rerun(paths, tmp_path)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertNotEqual(evidence["status"], "already_processed")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

    def test_post_success_state_has_single_processed_and_zero_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths, _ = self._seed_success(tmp_path)
            self.assertEqual(len(list(paths["processed"].glob("*"))), 1)
            self.assertEqual(len(list(paths["processed"].glob("*.json"))), 1)
            self.assertEqual(len(list(paths["processed"].glob("*.json.tmp"))), 0)
            self.assertFalse(paths["failed"].exists() and any(paths["failed"].iterdir()))

    # --- queue retry-contract invariants (this amendment) --------------

    def test_retry_contract_fields_must_be_canonical_before_lookup(self):
        cases = {
            "attempt_nonzero": {"attempt": 1},
            "attempt_boolean": {"attempt": True},
            "max_attempts_two": {"max_attempts": 2},
            "max_attempts_zero": {"max_attempts": 0},
            "consent_wrong": {"consent_status": "acknowledged"},
        }
        for label, override in cases.items():
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                paths = self.paths(tmp_path)
                write_jsonl(paths["queue"], [canonical_queue_row(**override)])
                completed = self.run_cli(self.args(paths, extra=self.fake_ps(tmp_path)))
                evidence = parse_evidence(completed.stdout)
                self.assertEqual(evidence["status"], "needs_fix", label)
                self.assertEqual(evidence["lookup_attempt_count"], "0", label)
                self.assertEqual(evidence["ac2_lookup_invoked"], "false", label)
                self.assertEqual(nonblank_lines(paths["results"]), 0, label)


if __name__ == "__main__":
    unittest.main()
