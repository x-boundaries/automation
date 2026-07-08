import base64
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "autocount2-automation"
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"
RUNBOOK = DOCS / "member_intake_n8n_gate4a_manual_queue_handoff_runbook.md"
SCRIPT = ROOT / "scripts" / "member_lookup_gate4a_evidence_summary.py"
PRECHECK_SCRIPT = ROOT / "scripts" / "member_lookup_gate4a_queue_precheck.py"

FORBIDDEN_WRITE_TOKENS = [
    "Save" + "Member",
    "New" + "Member",
    "Delete" + "Member",
    "GetNext" + "MemberNo",
]


def result_row(**overrides):
    row = {
        "job_id": "job-safe-001",
        "intake_source": "google_sheets_uat",
        "source_reference": "safe-ref-001",
        "source_row_ref": "safe-row-001",
        "row_number": 2,
        "state": "READY_FOR_CREATE_REVIEW",
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
        "error_code": None,
        "consent_status": "acknowledged",
        "pdpa_status": "yes",
        "attempt": 0,
        "dry_run_only": True,
        "final_write_automation": False,
        "result_created_at": "fixture-time",
        "result_applied_at": None,
    }
    row.update(overrides)
    return row


def queue_row(**overrides):
    encoded_value = base64.b64encode(b"approved-real-member-value").decode("ascii")
    row = {
        "job_id": "queue-safe-001",
        "intake_source": "google_sheets_uat",
        "source_reference": "safe-source-001",
        "source_row_ref": "safe-row-001",
        "row_number": 2,
        "intake_id": "intake-safe-001",
        "state": "PENDING_LOOKUP",
        "submitted_member_no_base64_utf8": encoded_value,
        "consent_status": "acknowledged",
        "pdpa_status": "yes",
        "payload_hash": "safe-hash-001",
        "attempt": 0,
        "max_attempts": 1,
        "created_at": "safe-created-at",
        "updated_at": "safe-updated-at",
        "timeout_at": "safe-timeout-at",
    }
    row.update(overrides)
    return row


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def parse_evidence(text):
    parsed = {}
    for line in text.splitlines():
        key, value = line.split(" = ", 1)
        parsed[key] = value
    return parsed


class Gate4AQueuePrecheckTests(unittest.TestCase):
    def run_precheck(self, queue_path):
        return subprocess.run(
            [
                sys.executable,
                str(PRECHECK_SCRIPT),
                "--queue-jsonl",
                str(queue_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_precheck_prints_only_aggregate_queue_write_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "member_lookup_bridge_gate4a_pending_queue.jsonl"
            write_jsonl(queue_path, [queue_row()])

            completed = self.run_precheck(queue_path)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "ok")
            self.assertEqual(evidence["gate"], "gate4a_real_queue_write_pre_bridge_check")
            self.assertEqual(evidence["runtime_location"], "local_operator_pc_non_ac2_n8n_stack")
            self.assertEqual(evidence["execution_mode"], "manual_inactive_queue_write_pre_bridge_check")
            self.assertEqual(evidence["queue_row_count"], "1")
            self.assertEqual(evidence["queue_base64_decode_ok_count"], "1")
            self.assertEqual(evidence["queue_base64_decode_fail_count"], "0")
            self.assertEqual(evidence["queue_decoded_blank_count"], "0")
            self.assertEqual(evidence["queue_decoded_looks_dummy_count"], "0")
            self.assertEqual(evidence["unexpected_queue_shape_count"], "0")
            self.assertEqual(evidence["bridge_handoff_approved"], "false")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertEqual(evidence["workflow_activation"], "inactive")
            self.assertEqual(evidence["scheduler_enabled"], "false")
            self.assertEqual(evidence["public_inbound_to_ac2_host"], "false")
            self.assertEqual(evidence["member_create_or_update_invoked"], "false")
            self.assertEqual(evidence["autocount_write_attempted"], "false")
            self.assertEqual(evidence["direct_sql_write_attempted"], "false")
            self.assertEqual(evidence["final_write_automation"], "false")
            self.assertEqual(evidence["no_row_values_printed"], "true")

            for forbidden in [
                "queue-safe",
                "safe-source",
                "safe-row",
                "row_number",
                "submitted_member_no_base64_utf8",
                "approved-real-member-value",
                queue_row()["submitted_member_no_base64_utf8"],
            ]:
                self.assertNotIn(forbidden, completed.stdout)

    def test_precheck_marks_needs_fix_for_dummy_or_bad_queue_without_value_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "member_lookup_bridge_gate4a_pending_queue.jsonl"
            dummy_encoded = base64.b64encode(b"dummy-rehearsal-value").decode("ascii")
            write_jsonl(
                queue_path,
                [
                    queue_row(submitted_member_no_base64_utf8=dummy_encoded),
                    queue_row(job_id="queue-safe-002", source_reference="safe-source-002"),
                ],
            )

            completed = self.run_precheck(queue_path)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["queue_row_count"], "2")
            self.assertEqual(evidence["queue_base64_decode_ok_count"], "2")
            self.assertEqual(evidence["queue_base64_decode_fail_count"], "0")
            self.assertEqual(evidence["queue_decoded_looks_dummy_count"], "1")
            self.assertEqual(evidence["bridge_handoff_approved"], "false")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")
            self.assertNotIn("dummy-rehearsal-value", completed.stdout)
            self.assertNotIn(dummy_encoded, completed.stdout)


class Gate4ASummarizerTests(unittest.TestCase):
    def run_summary(
        self,
        results_path,
        *,
        approved_batch_size=3,
        n8n_queue_rows_written_count=3,
        local_queue_rows_loaded_count=3,
    ):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--results-jsonl",
                str(results_path),
                "--approved-batch-size",
                str(approved_batch_size),
                "--n8n-queue-rows-written-count",
                str(n8n_queue_rows_written_count),
                "--local-queue-rows-loaded-count",
                str(local_queue_rows_loaded_count),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_summarizer_prints_only_aggregate_gate4a_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "member_lookup_bridge_gate4a_results.jsonl"
            write_jsonl(
                results_path,
                [
                    result_row(state="READY_FOR_CREATE_REVIEW"),
                    result_row(
                        job_id="job-safe-002",
                        source_reference="safe-ref-002",
                        source_row_ref="safe-row-002",
                        row_number=3,
                        state="EXISTING_MEMBER_REVIEW",
                        member_exists=True,
                    ),
                    result_row(
                        job_id="job-safe-003",
                        source_reference="safe-ref-003",
                        source_row_ref="safe-row-003",
                        row_number=4,
                        state="MANUAL_REVIEW_REQUIRED",
                        manual_review_required=True,
                        warning_count=1,
                    ),
                ],
            )

            completed = self.run_summary(results_path)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "ok")
            self.assertEqual(evidence["gate"], "gate4a_manual_queue_handoff_ac2_lookup_only")
            self.assertEqual(evidence["runtime_location"], "local_operator_pc_non_ac2_n8n_stack")
            self.assertEqual(evidence["execution_mode"], "manual_inactive_review_only_handoff")
            self.assertEqual(evidence["approved_batch_size"], "3")
            self.assertEqual(evidence["n8n_queue_rows_written_count"], "3")
            self.assertEqual(evidence["local_queue_rows_loaded_count"], "3")
            self.assertEqual(evidence["lookup_attempt_count"], "3")
            self.assertEqual(evidence["lookup_success_count"], "3")
            self.assertEqual(evidence["lookup_existing_member_review_count"], "1")
            self.assertEqual(evidence["lookup_manual_review_count"], "1")
            self.assertEqual(evidence["lookup_error_count"], "0")
            self.assertEqual(evidence["local_review_result_rows_written_count"], "3")
            self.assertEqual(evidence["n8n_result_mapping_run"], "false")
            self.assertEqual(evidence["member_create_or_update_invoked"], "false")
            self.assertEqual(evidence["autocount_write_attempted"], "false")
            self.assertEqual(evidence["direct_sql_write_attempted"], "false")
            self.assertEqual(evidence["workflow_activation"], "inactive")
            self.assertEqual(evidence["scheduler_enabled"], "false")
            self.assertEqual(evidence["public_inbound_to_ac2_host"], "false")
            self.assertEqual(evidence["final_write_automation"], "false")

            forbidden_output = [
                "job_id",
                "job-safe",
                "source_reference",
                "safe-ref",
                "source_row_ref",
                "safe-row",
                "row_number",
                "submitted_member_no_base64_utf8",
                "normalized_member_no",
                "member_found_by",
                "fixture-time",
            ]
            for forbidden in forbidden_output:
                self.assertNotIn(forbidden, completed.stdout)

    def test_summarizer_marks_needs_fix_for_lookup_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "member_lookup_bridge_gate4a_results.jsonl"
            write_jsonl(
                results_path,
                [
                    result_row(state="EXISTING_MEMBER_REVIEW", member_exists=True),
                    result_row(
                        job_id="job-safe-error",
                        state="LOOKUP_ERROR_REVIEW",
                        status="error",
                        error_code="lookup_error",
                    ),
                ],
            )

            completed = self.run_summary(
                results_path,
                approved_batch_size=2,
                n8n_queue_rows_written_count=2,
                local_queue_rows_loaded_count=2,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["lookup_attempt_count"], "2")
            self.assertEqual(evidence["lookup_success_count"], "1")
            self.assertEqual(evidence["lookup_error_count"], "1")
            self.assertEqual(evidence["final_write_automation"], "false")

    def test_summarizer_marks_needs_fix_for_missing_safe_fields_without_row_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "member_lookup_bridge_gate4a_results.jsonl"
            write_jsonl(results_path, [result_row(), {"state": "READY_FOR_CREATE_REVIEW"}])

            completed = self.run_summary(
                results_path,
                approved_batch_size=2,
                n8n_queue_rows_written_count=2,
                local_queue_rows_loaded_count=2,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["lookup_attempt_count"], "2")
            self.assertNotIn("READY_FOR_CREATE_REVIEW", completed.stdout)
            self.assertNotIn("job-safe", completed.stdout)

    def test_summarizer_marks_needs_fix_for_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = Path(tmp) / "member_lookup_bridge_gate4a_results.jsonl"
            write_jsonl(
                results_path,
                [
                    result_row(state="READY_FOR_CREATE_REVIEW"),
                    result_row(
                        job_id="job-safe-002",
                        source_reference="safe-ref-002",
                        source_row_ref="safe-row-002",
                        row_number=3,
                        state="EXISTING_MEMBER_REVIEW",
                        member_exists=True,
                    ),
                ],
            )

            completed = self.run_summary(
                results_path,
                approved_batch_size=2,
                n8n_queue_rows_written_count=3,
                local_queue_rows_loaded_count=2,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["approved_batch_size"], "2")
            self.assertEqual(evidence["n8n_queue_rows_written_count"], "3")
            self.assertEqual(evidence["local_queue_rows_loaded_count"], "2")
            self.assertEqual(evidence["lookup_attempt_count"], "2")
            self.assertEqual(evidence["lookup_error_count"], "0")
            for forbidden in [
                "job_id",
                "job-safe",
                "source_reference",
                "source_row_ref",
                "row_number",
            ]:
                self.assertNotIn(forbidden, completed.stdout)


class Gate4ARunbookTests(unittest.TestCase):
    def read(self, path):
        return path.read_text(encoding="utf-8")

    def test_readme_gitignore_runbook_and_script_are_wired(self):
        readme = self.read(README)
        gitignore = self.read(GITIGNORE)

        self.assertTrue(RUNBOOK.exists())
        self.assertTrue(SCRIPT.exists())
        self.assertTrue(PRECHECK_SCRIPT.exists())
        self.assertIn(RUNBOOK.name, readme)
        self.assertIn("scripts/member_lookup_gate4a_queue_precheck.py", readme)
        self.assertIn("scripts/member_lookup_gate4a_evidence_summary.py", readme)
        self.assertIn("member_lookup_bridge_gate4a_pending_queue.jsonl", gitignore)
        self.assertIn("member_lookup_bridge_gate4a_results.jsonl", gitignore)

    def test_runbook_defines_manual_handoff_not_real_poller_or_execution(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "Gate 4A queue-write preparation package only",
            "does not run Gate 4A",
            "does not run Gate 4",
            "does not activate n8n",
            "does not touch real Google Sheets queue data from repo work",
            "does not call AC2 from Codex",
            "does not run PowerShell from Codex",
            "manual handoff path",
            "no real Google Sheets poller exists",
            "does not claim a real Google Sheets poller",
            "production queue poller",
            "n8n result mapping run",
            "member_lookup_bridge_gate4a_pending_queue.jsonl",
            "member_lookup_bridge_gate4a_results.jsonl",
            "--queue-mode fixture",
            "--lookup-mode powershell",
            "--enable-powershell-lookup",
            "--allow-root-login",
            "carries forward the Gate 3 local lookup auth setting",
            "is still read-only",
            "scripts\\member_lookup_gate4a_evidence_summary.py",
            "scripts\\member_lookup_gate4a_queue_precheck.py",
            "n8n result mapping is deferred",
            "This PR stops before bridge handoff",
            "The local bridge lookup step is deferred unless the operator explicitly approves the next step",
        ]:
            self.assertIn(phrase, runbook)

        self.assertNotRegex(runbook, r"(?i)real Google Sheets poller is implemented")
        self.assertNotRegex(runbook, r"(?i)production queue poller is implemented")

    def test_runbook_stops_when_queue_contains_only_dummy_rehearsal_rows(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "The current lookup queue tab contains only dummy Gate 2 rehearsal rows",
            "proved TSV-to-JSONL handoff mechanics only",
            "must not be passed to AC2 lookup",
            "must not be recorded as Gate 4A pass evidence",
            "Stop before bridge handoff if the lookup queue tab contains only dummy Gate 2 rehearsal rows",
            "The lookup queue tab does not contain only dummy Gate 2 rehearsal rows",
            "Do not include dummy Gate 2 rehearsal rows",
            "The lookup queue contains only dummy Gate 2 rehearsal rows",
        ]:
            self.assertIn(phrase, runbook)

    def test_runbook_defines_real_one_row_queue_write_preparation(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "Gate 4A lookup may not start until the operator has first produced exactly one real, non-dummy, sanitized `PENDING_LOOKUP` queue row",
            "This preparation gate is queue-write-only",
            "does not run Gate 4A lookup",
            "does not call AC2",
            "does not run PowerShell",
            "does not run the local bridge",
            "does not run n8n result mapping",
            "does not authorize any AutoCount write path",
            "Initially this must be exactly one source row",
            "n8n manually reads exactly one approved real Google Form/Sheet source row",
            "n8n validates required fields and `PDPA Acknowledged = Yes`",
            "Write only one sanitized non-dummy `PENDING_LOOKUP` queue row",
            "Stop before bridge handoff after the aggregate pre-bridge check",
            "AutoCount `MobilePhone` is intentionally unused",
            "maps to AutoCount `MemberNo`",
        ]:
            self.assertIn(phrase, runbook)

        for required_source_field in [
            "`Name`",
            "phone/member number field submitted by the user",
            "`Email`",
            "birthday field only when the current source includes birthday",
            "`PDPA Acknowledged = Yes`",
        ]:
            self.assertIn(required_source_field, runbook)

    def test_runbook_requires_pre_bridge_queue_checks_before_handoff(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "`queue_row_count = 1`",
            "`queue_base64_decode_ok_count = 1`",
            "`queue_base64_decode_fail_count = 0`",
            "`queue_decoded_blank_count = 0`",
            "`queue_decoded_looks_dummy_count = 0`",
            "not as Gate 4A lookup evidence or Gate 4A pass evidence",
            "one successful base64 decode",
            "no decode failures",
            "no blank decoded value",
            "no dummy-looking decoded value",
            "gate = gate4a_real_queue_write_pre_bridge_check",
            "bridge_handoff_approved = false",
            "ac2_lookup_invoked = false",
            "This successful precheck does not approve local bridge handoff",
        ]:
            self.assertIn(phrase, runbook)

    def test_runbook_queue_row_contract_contains_required_real_fields(self):
        runbook = self.read(RUNBOOK)

        for field in [
            "`job_id`",
            "`intake_source`",
            "`source_reference`",
            "`source_row_ref`",
            "`row_number`",
            "`intake_id`",
            "`state = PENDING_LOOKUP`",
            "`submitted_member_no_base64_utf8`",
            "`consent_status`",
            "`pdpa_status = yes`",
            "`payload_hash`",
            "`attempt`",
            "`max_attempts`",
            "`created_at`",
            "`updated_at`",
            "`timeout_at`",
        ]:
            self.assertIn(field, runbook)

        self.assertIn("must decode to the submitted phone/member number", runbook)
        self.assertIn("raw, encoded, decoded, and normalized values must never be committed", runbook)

    def test_runbook_keeps_read_only_review_only_and_no_activation_boundaries(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "workflow is inactive and manual",
            "Scheduler is disabled",
            "No public inbound webhook, callback, tunnel, or reverse proxy reaches the AC2 host",
            "No Execute Command node is used for AC2 lookup",
            "read-only lookup mode",
            "must not create, update, delete, or write AutoCount members",
            "must not perform direct SQL writes",
            "must not invoke final write automation",
            "Do not map result rows back to Google Sheets in Gate 4A",
            "Do not treat `READY_FOR_CREATE_REVIEW` as create approval",
            "Gate 4A passing still does not authorize member create/update",
            "AutoCount writes",
            "direct SQL writes",
            "production activation",
            "scheduler activation",
            "final write automation",
        ]:
            self.assertIn(phrase, runbook)

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, runbook, token)
        self.assertNotRegex(
            runbook,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )

    def test_runbook_pre_bridge_queue_evidence_shape_is_exact_and_aggregate_only(self):
        runbook = self.read(RUNBOOK)
        match = re.search(
            r"Required pre-bridge paste-back shape:\n\n```text\n(?P<body>status = <ok/needs_fix>.*?PII are pasted\.)\n```",
            runbook,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")

        expected_lines = [
            "status = <ok/needs_fix>",
            "gate = gate4a_real_queue_write_pre_bridge_check",
            "runtime_location = local_operator_pc_non_ac2_n8n_stack",
            "execution_mode = manual_inactive_queue_write_pre_bridge_check",
            "queue_row_count = <aggregate-count-only>",
            "queue_base64_decode_ok_count = <aggregate-count-only>",
            "queue_base64_decode_fail_count = <aggregate-count-only>",
            "queue_decoded_blank_count = <aggregate-count-only>",
            "queue_decoded_looks_dummy_count = <aggregate-count-only>",
            "unexpected_queue_shape_count = <aggregate-count-only>",
            "bridge_handoff_approved = false",
            "ac2_lookup_invoked = false",
            "n8n_result_mapping_run = false",
            "workflow_activation = inactive",
            "scheduler_enabled = false",
            "public_inbound_to_ac2_host = false",
            "member_create_or_update_invoked = false",
            "autocount_write_attempted = false",
            "direct_sql_write_attempted = false",
            "final_write_automation = false",
            "no_row_values_printed = true",
            "sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/decoded/normalized member values, names, emails, phone numbers, birthdays, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, screenshots, or PII are pasted.",
        ]
        self.assertEqual(body.splitlines(), expected_lines)

        for forbidden in [
            "job_id",
            "source_reference",
            "source_row_ref",
            "row_number",
            "submitted_member_no_base64_utf8",
        ]:
            self.assertNotIn(forbidden, body)

    def test_deferred_lookup_evidence_shape_is_exact_and_aggregate_only(self):
        runbook = self.read(RUNBOOK)
        match = re.search(
            r"Deferred Lookup Paste-Back Evidence.*?```text\n(?P<body>status = <ok/needs_fix>.*?PII are pasted\.)\n```",
            runbook,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")

        expected_lines = [
            "status = <ok/needs_fix>",
            "gate = gate4a_manual_queue_handoff_ac2_lookup_only",
            "runtime_location = local_operator_pc_non_ac2_n8n_stack",
            "execution_mode = manual_inactive_review_only_handoff",
            "approved_batch_size = <aggregate-count-only>",
            "n8n_queue_rows_written_count = <aggregate-count-only>",
            "local_queue_rows_loaded_count = <aggregate-count-only>",
            "lookup_attempt_count = <aggregate-count-only>",
            "lookup_success_count = <aggregate-count-only>",
            "lookup_existing_member_review_count = <aggregate-count-only>",
            "lookup_manual_review_count = <aggregate-count-only>",
            "lookup_error_count = <aggregate-count-only>",
            "local_review_result_rows_written_count = <aggregate-count-only>",
            "n8n_result_mapping_run = false",
            "member_create_or_update_invoked = false",
            "autocount_write_attempted = false",
            "direct_sql_write_attempted = false",
            "workflow_activation = inactive",
            "scheduler_enabled = false",
            "public_inbound_to_ac2_host = false",
            "final_write_automation = false",
            "sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, or PII are pasted.",
        ]
        self.assertEqual(body.splitlines(), expected_lines)

        for forbidden in [
            "job_id",
            "source_reference",
            "source_row_ref",
            "row_number",
            "submitted_member_no_base64_utf8",
        ]:
            self.assertNotIn(forbidden, body)

    def test_committed_gate4a_material_has_no_sensitive_literals_or_artifacts(self):
        combined = "\n".join(
            [
                self.read(RUNBOOK),
                self.read(SCRIPT),
                self.read(PRECHECK_SCRIPT),
                self.read(README),
                self.read(GITIGNORE),
            ]
        )

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, combined, token)
        self.assertNotRegex(combined, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(combined, r"(?i)(client_email|private_key|service_account|-----BEGIN PRIVATE KEY-----)")
        self.assertNotRegex(combined, r"(?i)(password|secret|token)\s*[:=]\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(combined, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(combined, r"submitted_member_no_base64_utf8\"\s*:\s*\"[A-Za-z0-9+/]+=*\"")
        self.assertNotRegex(combined, r"(?i)workflow export file")
        self.assertNotRegex(combined, r"(?i)scheduler_enabled = true")
        self.assertNotRegex(combined, r"(?i)workflow_activation = active")


if __name__ == "__main__":
    unittest.main()
