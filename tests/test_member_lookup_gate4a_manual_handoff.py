import base64
import json
import re
import shutil
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
CONTRACT = DOCS / "member_intake_n8n_node_contract.md"
TEMPLATE = ROOT / "n8n-workflows" / "member_intake_gate4a_container_queue_write.workflow.json"
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


def allowed_queue_fields():
    return [
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
        "timeout_at",
    ]


def workflow_code():
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    code = next(
        node
        for node in template["nodes"]
        if node["name"] == "Validate And Build One Sanitized Queue Row"
    )
    return code["parameters"]["jsCode"]


def actual_source_row(**overrides):
    row = {
        "Date & Time": "safe-timestamp-marker",
        "Full Name": "source-name-present",
        "AutoCount MemberNo": "".join(["1"] * 6),
        "Email Address": "source-email-present",
        "Birthday Month": "June",
        "Marketing Consent": "optional-marketing-source-value",
        "PDPA Acknowledged": "Yes",
        "Gate4AApprovedForLookup": "YES",
        "row_number": 2,
    }
    row.update(overrides)
    return row


def run_workflow_code(row):
    node_exe = shutil.which("node")
    if not node_exe:
        raise unittest.SkipTest("node executable is required to execute the n8n Code node")
    wrapper = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const run = new Function('$input', 'Buffer', payload.code);
  const items = payload.rows.map((json) => ({ json }));
  const result = run({ all: () => items }, Buffer);
  console.log(JSON.stringify({ ok: true, result }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: String(error && error.message ? error.message : error) }));
}
"""
    completed = subprocess.run(
        [node_exe, "-e", wrapper],
        input=json.dumps({"code": workflow_code(), "rows": [row]}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def decode_queue_row(code_result):
    binary_data = code_result["result"][0]["binary"]["data"]["data"]
    jsonl = base64.b64decode(binary_data).decode("utf-8")
    rows = [json.loads(line) for line in jsonl.splitlines() if line.strip()]
    return rows[0]


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

    def test_precheck_rejects_extra_forbidden_fields_without_name_or_value_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "member_lookup_bridge_gate4a_pending_queue.jsonl"
            forbidden_values = {
                "name": "Forbidden Person",
                "email": "forbidden@example.test",
                "raw_phone": "61234567",
                "birthday": "2000-01-01",
                "normalized_member_no": "normalized-forbidden",
                "sheet_url": "forbidden-sheet-url",
                "credential_id": "forbidden-credential-id",
                "node_raw_input": "forbidden-node-raw-input",
            }
            write_jsonl(queue_path, [queue_row(**forbidden_values)])

            completed = self.run_precheck(queue_path)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(evidence["status"], "needs_fix")
            self.assertEqual(evidence["queue_row_count"], "1")
            self.assertEqual(evidence["queue_base64_decode_ok_count"], "1")
            self.assertGreater(int(evidence["unexpected_queue_shape_count"]), 0)
            self.assertEqual(evidence["bridge_handoff_approved"], "false")
            self.assertEqual(evidence["ac2_lookup_invoked"], "false")

            for forbidden_field in forbidden_values:
                self.assertNotRegex(completed.stdout, rf"\b{re.escape(forbidden_field)}\b")
            for forbidden_value in forbidden_values.values():
                self.assertNotIn(forbidden_value, completed.stdout)


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


class Gate4AN8nWorkflowCodeTests(unittest.TestCase):
    def test_actual_source_headers_pass_and_queue_schema_stays_exact(self):
        completed = run_workflow_code(actual_source_row())

        self.assertTrue(completed["ok"], completed)
        queue = decode_queue_row(completed)
        self.assertEqual(list(queue.keys()), allowed_queue_fields())
        self.assertEqual(queue["state"], "PENDING_LOOKUP")
        self.assertEqual(queue["pdpa_status"], "yes")
        self.assertEqual(queue["consent_status"], "marketing_consent_not_queued")
        self.assertEqual(queue["source_reference"], "google_sheets_uat_gate4a")
        self.assertEqual(queue["source_row_ref"], "row_2")
        self.assertEqual(queue["intake_id"], "gate4a_row_2")

        forbidden_fields = [
            "Date & Time",
            "Full Name",
            "AutoCount MemberNo",
            "Email Address",
            "Birthday Month",
            "Marketing Consent",
            "PDPA Acknowledged",
            "DOB",
            "derived_dob",
            "birthday",
            "raw_member_no",
            "normalized_member_no",
        ]
        for field in forbidden_fields:
            self.assertNotIn(field, queue)

        serialized = json.dumps(queue, sort_keys=True).lower()
        for forbidden_value in [
            "source-name-present",
            "source-email-present",
            "optional-marketing-source-value",
            "june",
            "2000-06-01",
            "birthday",
            "dob",
        ]:
            self.assertNotIn(forbidden_value, serialized)

    def test_birthday_month_case_and_spacing_passes(self):
        completed = run_workflow_code(actual_source_row(**{"Birthday Month": " june "}))

        self.assertTrue(completed["ok"], completed)
        queue = decode_queue_row(completed)
        self.assertEqual(list(queue.keys()), allowed_queue_fields())
        self.assertNotIn("2000-06-01", json.dumps(queue, sort_keys=True))

    def test_invalid_birthday_month_fails(self):
        completed = run_workflow_code(actual_source_row(**{"Birthday Month": "not-a-month"}))

        self.assertFalse(completed["ok"], completed)
        self.assertEqual(completed["error"], "gate4a_birthday_month_must_be_valid_month_name")

    def test_pdpa_yes_is_mandatory_and_imported_or_missing_is_blocked(self):
        for pdpa_value, expected_error in [
            ("Imported", "gate4a_imported_pdpa_marker_blocked"),
            ("No", "gate4a_pdpa_must_be_yes"),
            ("", "gate4a_pdpa_must_be_yes"),
        ]:
            completed = run_workflow_code(actual_source_row(**{"PDPA Acknowledged": pdpa_value}))
            self.assertFalse(completed["ok"], completed)
            self.assertEqual(completed["error"], expected_error)

    def test_marketing_consent_cannot_rescue_invalid_pdpa(self):
        completed = run_workflow_code(
            actual_source_row(
                **{
                    "Marketing Consent": "Yes",
                    "PDPA Acknowledged": "No",
                }
            )
        )

        self.assertFalse(completed["ok"], completed)
        self.assertEqual(completed["error"], "gate4a_pdpa_must_be_yes")

    def test_autocount_member_no_must_be_numeric_6_to_20_digits(self):
        invalid_values = [
            "".join(["1"] * 5),
            "".join(["1"] * 21),
            "ABC123",
            "123 456",
        ]
        for member_no in invalid_values:
            completed = run_workflow_code(actual_source_row(**{"AutoCount MemberNo": member_no}))
            self.assertFalse(completed["ok"], completed)
            self.assertEqual(completed["error"], "gate4a_autocount_member_no_must_be_6_to_20_digits")

    def test_dummy_source_markers_are_blocked(self):
        for field in ["Full Name", "Email Address", "Marketing Consent"]:
            completed = run_workflow_code(actual_source_row(**{field: "synthetic-source-marker"}))
            self.assertFalse(completed["ok"], completed)
            self.assertEqual(completed["error"], "gate4a_source_row_must_be_real_non_dummy")

    def test_safe_queue_helper_fields_are_derived_from_row_metadata(self):
        completed = run_workflow_code(actual_source_row())

        self.assertTrue(completed["ok"], completed)
        queue = decode_queue_row(completed)
        self.assertEqual(queue["source_reference"], "google_sheets_uat_gate4a")
        self.assertEqual(queue["source_row_ref"], "row_2")
        self.assertEqual(queue["intake_id"], "gate4a_row_2")
        self.assertNotIn("Gate4A Source Reference", workflow_code())
        self.assertNotIn("Gate4A Source Row Ref", workflow_code())


class Gate4ARunbookTests(unittest.TestCase):
    def read(self, path):
        return path.read_text(encoding="utf-8")

    def test_readme_gitignore_runbook_and_script_are_wired(self):
        readme = self.read(README)
        gitignore = self.read(GITIGNORE)

        self.assertTrue(RUNBOOK.exists())
        self.assertTrue(CONTRACT.exists())
        self.assertTrue(TEMPLATE.exists())
        self.assertTrue(SCRIPT.exists())
        self.assertTrue(PRECHECK_SCRIPT.exists())
        self.assertIn(RUNBOOK.name, readme)
        self.assertIn(TEMPLATE.name, readme)
        self.assertIn("scripts/member_lookup_gate4a_queue_precheck.py", readme)
        self.assertIn("scripts/member_lookup_gate4a_evidence_summary.py", readme)
        self.assertIn("member_lookup_bridge_gate4a_pending_queue.jsonl", gitignore)
        self.assertIn("member_lookup_bridge_gate4a_results.jsonl", gitignore)

    def test_runbook_defines_container_queue_write_only_scope(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "Gate 4A real n8n one-row queue-write proof only",
            "does not run Gate 4A",
            "does not run Gate 4",
            "does not activate n8n",
            "does not call AC2",
            "does not run PowerShell",
            "does not run the local bridge",
            "does not call any bridge endpoint",
            "does not map results",
            "writes exactly one sanitized PENDING_LOOKUP queue row to JSONL inside the n8n container",
            "docker compose cp from the n8n container",
            "/home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl",
            "C:\\Users\\xPass\\OneDrive\\Desktop\\X-Boundaries\\autocount_outputs\\review\\member_lookup_bridge\\member_lookup_bridge_gate4a_pending_queue.jsonl",
            "member_lookup_bridge_gate4a_pending_queue.jsonl",
            "scripts\\member_lookup_gate4a_queue_precheck.py",
            "operator stop",
        ]:
            self.assertIn(phrase, runbook)

        self.assertNotIn("--lookup-mode powershell", runbook)
        self.assertNotIn("--enable-powershell-lookup", runbook)
        self.assertNotIn("--allow-root-login", runbook)
        self.assertNotIn("python scripts\\ac2_member_lookup_bridge_worker.py", runbook)
        self.assertNotRegex(runbook, r"(?i)AC2 lookup execution")
        self.assertNotRegex(runbook, r"(?i)production queue poller is implemented")

    def test_runbook_defines_real_one_row_queue_write_preparation(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "The first Gate 4A run must match exactly one approved real non-dummy Google Form / Google Sheet row",
            "Do not rename the real Google Form questions or Google Sheet headers",
            "`Full Name` -> required source name",
            "`AutoCount MemberNo` -> submitted member number and AutoCount `MemberNo` lookup value",
            "`Email Address` -> required source email",
            "`Birthday Month` -> required birthday-month source field",
            "`Marketing Consent` -> optional marketing/consent source metadata only",
            "`PDPA Acknowledged = Yes`",
            "`Gate4AApprovedForLookup = YES`",
            "`PDPA Acknowledged = Imported` is blocked",
            "`Marketing Consent` and `consent_status` cannot rescue",
            "`AutoCount MemberNo` must be numeric only and match `^[0-9]{6,20}$`",
            "AutoCount `MobilePhone` is intentionally unused",
            "`Birthday Month` is required and must be one of the 12 month names",
            "user-facing `01/FORM_INPUT_MONTH/2000`",
            "ISO `2000-MM-01`",
            "`June` maps to `2000-06-01`",
            "does not write `Birthday Month`, DOB, derived DOB, raw month, or any birthday value to the Gate 4A queue row",
            "Manually execute the workflow once in n8n",
        ]:
            self.assertIn(phrase, runbook)

        for required_source_field in [
            "`Full Name`",
            "`AutoCount MemberNo`",
            "`Email Address`",
            "`Birthday Month`",
            "`PDPA Acknowledged = Yes`",
            "`row_number`",
        ]:
            self.assertIn(required_source_field, runbook)

        for derived_helper_phrase in [
            "Do not add or maintain `Gate4A Source Reference` or `Gate4A Source Row Ref` sheet columns",
            "The workflow derives queue `source_reference`",
            "queue `source_row_ref` from n8n Google Sheets row metadata",
        ]:
            self.assertIn(derived_helper_phrase, runbook)

    def test_node_contract_accepts_actual_headers_and_birthday_month_policy(self):
        contract = self.read(CONTRACT)

        for phrase in [
            "Do not rename the real Google Form questions or Google Sheet headers",
            "`Date & Time`",
            "`Full Name`",
            "`AutoCount MemberNo`",
            "`Email Address`",
            "`Birthday Month`",
            "`Marketing Consent`",
            "`PDPA Acknowledged`",
            "`Gate4AApprovedForLookup`",
            "`row_number`",
            "does not require `Gate4A Source Reference` or `Gate4A Source Row Ref` sheet columns",
            "queue `source_reference` and `source_row_ref` are derived internally",
            "`AutoCount MemberNo` must match `^[0-9]{6,20}$`",
            "AutoCount `MobilePhone` is intentionally unused",
            "`Birthday Month` must be one of the 12 month names",
            "`01/FORM_INPUT_MONTH/2000`",
            "`2000-MM-01`",
            "must not queue `Birthday Month`, DOB, derived DOB, raw month, or any birthday value",
            "later-stage policy decision and is not Gate 4A approval to write",
            "`Marketing Consent` is optional source metadata only",
            "must not rescue, override, or reinterpret invalid, missing, or imported `pdpa_status`",
        ]:
            self.assertIn(phrase, contract)

    def test_runbook_requires_pre_bridge_queue_checks_before_handoff(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "`queue_row_count = 1`",
            "`queue_base64_decode_ok_count = 1`",
            "`queue_base64_decode_fail_count = 0`",
            "`queue_decoded_blank_count = 0`",
            "`queue_decoded_looks_dummy_count = 0`",
            "`unexpected_queue_shape_count = 0`",
            "gate = gate4a_real_queue_write_pre_bridge_check",
            "bridge_handoff_approved = false",
            "ac2_lookup_invoked = false",
            "This successful precheck is queue-write evidence only",
            "does not approve local bridge handoff",
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
            "workflow is inactive/manual",
            "has no scheduler, webhook, HTTP Request, Execute Command, AC2, SQL, RDP, tunnel, bridge endpoint, result-mapping, member create/update/delete, or AutoCount write node",
            "The workflow is activated.",
            "A scheduler or webhook is enabled.",
            "Any AC2, bridge endpoint, PowerShell, SQL, RDP, tunnel, AutoCount write, member create/update/delete, result-mapping, or final automation path appears.",
            "AutoCount writes",
            "direct SQL writes",
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

    def test_template_defines_only_manual_container_queue_write_nodes(self):
        template = json.loads(self.read(TEMPLATE))
        self.assertFalse(template["active"])
        node_names = {node["name"] for node in template["nodes"]}
        node_types = {node["type"] for node in template["nodes"]}

        for node_name in [
            "Manual Gate 4A Run",
            "Read One Approved Real Source Row",
            "Validate And Build One Sanitized Queue Row",
            "Write Queue JSONL Inside n8n Container",
        ]:
            self.assertIn(node_name, node_names)

        self.assertEqual(
            node_types,
            {
                "n8n-nodes-base.manualTrigger",
                "n8n-nodes-base.googleSheets",
                "n8n-nodes-base.code",
                "n8n-nodes-base.readWriteFile",
                "n8n-nodes-base.stickyNote",
            },
        )
        self.assertNotIn("n8n-nodes-base.executeCommand", node_types)
        self.assertNotIn("n8n-nodes-base.httpRequest", node_types)
        self.assertNotIn("n8n-nodes-base.scheduleTrigger", node_types)
        self.assertNotIn("n8n-nodes-base.webhook", node_types)

        writer = next(node for node in template["nodes"] if node["name"] == "Write Queue JSONL Inside n8n Container")
        self.assertEqual(writer["parameters"]["operation"], "write")
        self.assertEqual(writer["parameters"]["fileName"], "/home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl")
        self.assertFalse(writer["parameters"]["options"]["append"])
        self.assertIn("stale rows", writer["notes"])

        code = next(node for node in template["nodes"] if node["name"] == "Validate And Build One Sanitized Queue Row")
        js_code = code["parameters"]["jsCode"]
        for phrase in [
            "Full Name",
            "AutoCount MemberNo",
            "Email Address",
            "Birthday Month",
            "Marketing Consent",
            "PDPA Acknowledged",
            "MONTH_TO_ISO_DOB",
            "2000-06-01",
            "gate4a_requires_exactly_one_approved_source_row",
            "gate4a_autocount_member_no_must_be_6_to_20_digits",
            "gate4a_birthday_month_must_be_valid_month_name",
            "gate4a_pdpa_must_be_yes",
            "gate4a_imported_pdpa_marker_blocked",
            "gate4a_source_row_must_be_real_non_dummy",
            "submitted_member_no_base64_utf8: submittedMemberNoBase64",
            "state: 'PENDING_LOOKUP'",
            "pdpa_status: 'yes'",
        ]:
            self.assertIn(phrase, js_code)

        self.assertNotIn("derivedDobIso", "".join(allowed_queue_fields()))

    def test_template_writer_uses_n8n_approved_path_and_no_stale_paths(self):
        template_text = self.read(TEMPLATE)

        self.assertIn(
            "/home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl",
            template_text,
        )
        self.assertNotIn("/tmp/member_lookup_bridge_gate4a_pending_queue.jsonl", template_text)
        self.assertNotIn("/home/node/.n8n/member_lookup_bridge_gate4a_pending_queue.jsonl", template_text)

    def test_template_google_sheets_node_is_unbound_with_safe_filter(self):
        template = json.loads(self.read(TEMPLATE))
        sheets = next(
            node for node in template["nodes"] if node["type"] == "n8n-nodes-base.googleSheets"
        )

        self.assertEqual(
            sheets["parameters"]["filtersUI"],
            {"values": [{"lookupColumn": "Gate4AApprovedForLookup", "lookupValue": "YES"}]},
        )
        self.assertEqual(sheets["parameters"]["documentId"], {"__rl": True, "value": "", "mode": "list"})
        self.assertEqual(sheets["parameters"]["sheetName"], {"__rl": True, "value": "", "mode": "list"})

    def test_template_has_no_credentials_bindings_or_execution_data(self):
        template_text = self.read(TEMPLATE)
        template = json.loads(template_text)

        for node in template["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
        for forbidden_key in ["pinData", "staticData", "shared", "tags", "triggerCount"]:
            self.assertNotIn(forbidden_key, template)
        for forbidden_text in [
            "cachedResultName",
            "cachedResultUrl",
            "docs.google.com",
            "googleSheetsOAuth2Api",
        ]:
            self.assertNotIn(forbidden_text, template_text)

    def test_no_raw_live_workflow_export_is_tracked(self):
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()

        for tracked_path in tracked:
            lowered = tracked_path.lower()
            self.assertNotIn(".live-export.json", lowered, tracked_path)
            self.assertNotIn(".live-import.json", lowered, tracked_path)
            self.assertNotIn("raw_local_only", lowered, tracked_path)

        workflow_files = [p for p in tracked if p.startswith("n8n-workflows/")]
        self.assertIn("n8n-workflows/member_intake_gate4a_container_queue_write.workflow.json", workflow_files)
        for workflow_path in workflow_files:
            text = (ROOT / workflow_path).read_text(encoding="utf-8")
            self.assertNotIn("cachedResultUrl", text, workflow_path)
            self.assertNotIn("docs.google.com", text, workflow_path)
            self.assertNotIn('"credentials"', text, workflow_path)

    def test_runbook_documents_directory_preparation_and_updated_copy_command(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "### Pre-Run Directory Preparation",
            "docker compose -p n8n-local exec -u node n8n sh -lc",
            "mkdir -p /home/node/.n8n-files && chmod 700 /home/node/.n8n-files",
            "n8n:/home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl",
            "No Docker Compose edit is required",
            "rejects `/tmp`",
            "node-level",
            "Append must remain disabled",
        ]:
            self.assertIn(phrase, runbook)

        self.assertNotIn("/tmp/member_lookup_bridge_gate4a_pending_queue.jsonl", runbook)
        self.assertNotIn("/home/node/.n8n/member_lookup_bridge_gate4a_pending_queue.jsonl", runbook)

    def test_runbook_documents_manual_filter_verification_after_binding(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "verify the Google Sheets filter is still present",
            "If the filter is absent, manually add:",
            "Column: `Gate4AApprovedForLookup`",
            "Value: `YES`",
            "Exactly one row may match the filter.",
            "Do not paste the raw exported live workflow or its configured Google selectors",
        ]:
            self.assertIn(phrase, runbook)

    def test_runbook_warns_against_physical_row_number_helper_columns(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "The only Gate 4A helper/admin column the operator manually maintains is",
            "Remove any physical Sheet columns with these headers before running Gate 4A:",
            "automatically supplies virtual `row_number` metadata",
            "A physical Sheet column named `row_number` overwrites that generated metadata",
            "Members and operators must not create or manually populate a physical `row_number` column",
            "delete the whole column so n8n's virtual `row_number` is used",
            "the Code node fails with `gate4a_safe_row_number_required`",
        ]:
            self.assertIn(phrase, runbook)

        # Stop condition against duplicate/physical row_number and derived-helper columns.
        stop_section = runbook.split("## Stop Conditions", 1)[1]
        for phrase in [
            "physical `row_number` column",
            "duplicate `row_number` header",
            "`Gate4A Source Reference` or `Gate4A Source Row Ref` column",
            "overrides n8n's virtual `row_number` metadata",
            "delete the physical column instead of typing a value into it",
        ]:
            self.assertIn(phrase, stop_section)

    def test_runbook_records_sanitized_technical_uat_evidence(self):
        runbook = self.read(RUNBOOK)

        # The earlier test-style technical UAT record is preserved as historical evidence.
        for phrase in [
            "Gate 4A technical n8n-to-container queue-write UAT: PASS",
            "This technical UAT record is retained as historical evidence",
            "Bridge handoff remains unapproved.",
            "AC2 lookup remains uninvoked.",
        ]:
            self.assertIn(phrase, runbook)

        # Current status must no longer say the final real-row evidence is pending.
        self.assertNotIn("Final real non-dummy source-row evidence remains pending.", runbook)
        self.assertNotIn("Final real non-dummy source-row evidence remains pending", runbook)

    def test_runbook_records_final_real_consenting_row_evidence(self):
        runbook = self.read(RUNBOOK)

        for phrase in [
            "Final real non-dummy consenting source-row Gate 4A evidence: PASS",
            "Gate 4A queue-write evidence: PASS.",
            "one genuine consented UAT Form response",
            "selected exactly one row through `Gate4AApprovedForLookup = YES`",
            "kept the workflow manual and inactive",
            "wrote exactly one sanitized queue row",
            "decoded that one queue row's Base64 lookup value exactly once, with no decode failure",
            "produced no blank decoded value",
            "produced no dummy-looking decoded value",
            "produced no unexpected queue shape",
            "stopped after the aggregate precheck",
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
        ]:
            self.assertIn(phrase, runbook)

        # Exact successful aggregate counters for the final real-row run.
        for counter in [
            "queue_row_count = 1",
            "queue_base64_decode_ok_count = 1",
            "queue_base64_decode_fail_count = 0",
            "queue_decoded_blank_count = 0",
            "queue_decoded_looks_dummy_count = 0",
            "unexpected_queue_shape_count = 0",
        ]:
            self.assertIn(counter, runbook)

        # The next bridge/AC2 lookup-only step still needs its own explicit approval.
        self.assertIn(
            "Gate 4A completion does not by itself approve the next bridge/AC2 lookup-only step.",
            runbook,
        )
        self.assertIn(
            "requires a separate explicit gate, review, and operator approval",
            runbook,
        )

        # Operator must clear the approval marker after the completed run; the repo did not.
        self.assertIn(
            "the operator should clear the `Gate4AApprovedForLookup = YES` value",
            runbook,
        )
        self.assertIn("this repository did not and cannot clear the Sheet value", runbook)
        self.assertIn(
            "`Gate4AApprovedForLookup` is the only manually maintained Gate 4A helper/admin column. It is not a Google Form question.",
            runbook,
        )

        # No sensitive row-level evidence appears in the final evidence section.
        final_section = runbook.split("## Final Real Consenting Source-Row Evidence", 1)[1]
        self.assertNotRegex(final_section, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(final_section, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(final_section, r"(?i)\b(?:\+?65)?[689]\d{7}\b")
        self.assertNotRegex(
            final_section,
            r"submitted_member_no_base64_utf8\"\s*:\s*\"[A-Za-z0-9+/]+=*\"",
        )

    def test_committed_gate4a_material_has_no_sensitive_literals_or_artifacts(self):
        combined = "\n".join(
            [
                self.read(RUNBOOK),
                self.read(TEMPLATE),
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
        self.assertNotRegex(combined, r"(?i)scheduler_enabled = true")
        self.assertNotRegex(combined, r"(?i)workflow_activation = active")


if __name__ == "__main__":
    unittest.main()
