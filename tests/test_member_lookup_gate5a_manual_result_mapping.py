import base64
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "autocount2-automation"
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"
RUNBOOK = DOCS / "member_intake_n8n_gate5a_manual_result_mapping_runbook.md"
TEMPLATE = ROOT / "n8n-workflows" / "member_intake_gate5a_sanitized_result_mapping.workflow.json"
PRECHECK_SCRIPT = ROOT / "scripts" / "member_lookup_gate5a_result_precheck.py"

sys.path.insert(0, str(ROOT / "scripts"))
import member_lookup_gate4_real_queue_lookup as gate4  # noqa: E402

SOURCE_LABEL = "google_sheets_uat_gate4a"
CONTAINER_RESULT_FILE = "/home/node/.n8n-files/member_lookup_bridge_gate5a_result_copy.jsonl"

ALLOWED_NODE_TYPES = {
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.readWriteFile",
    "n8n-nodes-base.extractFromFile",
    "n8n-nodes-base.code",
    "n8n-nodes-base.googleSheets",
    "n8n-nodes-base.if",
    "n8n-nodes-base.stickyNote",
}

REVIEW_COLUMNS = [
    "uat_lookup_job_id",
    "uat_lookup_state",
    "uat_lookup_status",
    "uat_lookup_error_code",
    "uat_lookup_warning_count",
    "uat_lookup_attempt",
    "uat_lookup_completed_at",
    "reviewer_status",
]

FORBIDDEN_WRITE_TOKENS = [
    "Save" + "Member",
    "New" + "Member",
    "Delete" + "Member",
    "GetNext" + "MemberNo",
]

EXPECTED_PRECHECK_EVIDENCE_KEYS = [
    "status",
    "gate",
    "runtime_location",
    "execution_mode",
    "input_result_row_count",
    "result_schema_valid_count",
    "result_state_consistent_count",
    "forbidden_field_count",
    "unexpected_shape_count",
    "mapped_lookup_error_review_count",
    "mapped_manual_review_required_count",
    "mapped_existing_member_review_count",
    "mapped_ready_for_create_review_count",
    "original_gate4_artifacts_modified",
    "autocount_lookup_invoked",
    "member_create_or_update_invoked",
    "autocount_write_attempted",
    "direct_sql_write_attempted",
    "n8n_result_mapping_run",
    "workflow_activation",
    "scheduler_enabled",
    "public_inbound_to_ac2_host",
    "final_write_automation",
    "no_row_values_printed",
    "sanitized_note",
]

EXPECTED_WORKFLOW_EVIDENCE_KEYS = [
    "status",
    "gate",
    "runtime_location",
    "execution_mode",
    "input_result_row_count",
    "result_schema_valid_count",
    "source_rows_read_count",
    "source_row_match_count",
    "mapping_attempt_count",
    "mapping_success_count",
    "mapping_already_applied_count",
    "mapping_error_count",
    "mapped_lookup_error_review_count",
    "mapped_manual_review_required_count",
    "mapped_existing_member_review_count",
    "mapped_ready_for_create_review_count",
    "reviewer_status_unreviewed_count",
    "original_gate4_artifacts_modified",
    "autocount_lookup_invoked",
    "member_create_or_update_invoked",
    "autocount_write_attempted",
    "direct_sql_write_attempted",
    "workflow_activation",
    "scheduler_enabled",
    "public_inbound_to_ac2_host",
    "final_write_automation",
    "no_row_values_printed",
]


def canonical_member_base64(member_no="111111"):
    return base64.b64encode(member_no.encode("utf-8")).decode("ascii")


def expected_job_id(member_no="111111", row_number=2):
    payload_hash = gate4.canonical_payload_hash(
        {
            "intake_source": SOURCE_LABEL,
            "source_reference": SOURCE_LABEL,
            "source_row_ref": f"row_{row_number}",
            "row_number": row_number,
            "intake_id": f"gate4a_row_{row_number}",
            "state": "PENDING_LOOKUP",
            "submitted_member_no_base64_utf8": canonical_member_base64(member_no),
            "pdpa_status": "yes",
        }
    )
    return f"gate4a_{payload_hash}"


def result_row(**overrides):
    row = {
        "job_id": expected_job_id(),
        "intake_source": SOURCE_LABEL,
        "source_reference": SOURCE_LABEL,
        "source_row_ref": "row_2",
        "row_number": 2,
        "state": "READY_FOR_CREATE_REVIEW",
        "status": "ok",
        "authentication_success": True,
        "user_session_available": True,
        "member_command_found": True,
        "get_member_found": True,
        "submitted_member_no_status": "already_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": False,
        "member_found_by": None,
        "manual_review_required": False,
        "warning_count": 0,
        "error_code": None,
        "consent_status": "marketing_consent_not_queued",
        "pdpa_status": "yes",
        "attempt": 0,
        "dry_run_only": True,
        "final_write_automation": False,
        "result_created_at": "2026-07-14T10:00:00+00:00",
        "result_applied_at": None,
    }
    row.update(overrides)
    return row


def error_result_row(**overrides):
    row = result_row(
        state="LOOKUP_ERROR_REVIEW",
        status="error",
        authentication_success=False,
        user_session_available=False,
        member_command_found=False,
        get_member_found=False,
        error_code="runtimeexception",
    )
    row.update(overrides)
    return row


def source_row(**overrides):
    row = {
        "Date & Time": "safe-timestamp-marker",
        "Full Name": "source-name-present",
        "AutoCount MemberNo": "111111",
        "Email Address": "source-email-present",
        "Birthday Month": "June",
        "Marketing Consent": "optional-marketing-source-value",
        "PDPA Acknowledged": "Yes",
        "Gate5AApprovedForMapping": "YES",
        "row_number": 2,
    }
    for column in REVIEW_COLUMNS:
        row[column] = ""
    row.update(overrides)
    return row


def applied_source_row(**overrides):
    row = source_row(
        uat_lookup_job_id=expected_job_id(),
        uat_lookup_state="READY_FOR_CREATE_REVIEW",
        uat_lookup_status="ok",
        uat_lookup_error_code="",
        uat_lookup_warning_count="0",
        uat_lookup_attempt="0",
        uat_lookup_completed_at="2026-07-14T10:00:00+00:00",
        reviewer_status="UNREVIEWED",
    )
    row.update(overrides)
    return row


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def parse_evidence(text):
    parsed = {}
    for line in text.splitlines():
        key, value = line.split(" = ", 1)
        parsed[key] = value
    return parsed


def load_template():
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def workflow_code(node_name):
    template = load_template()
    node = next(node for node in template["nodes"] if node["name"] == node_name)
    return node["parameters"]["jsCode"]


def run_node_code(node_name, items, node_outputs=None):
    node_exe = shutil.which("node")
    if not node_exe:
        raise unittest.SkipTest("node executable is required to execute the n8n Code node")
    wrapper = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const run = new Function('$input', '$', 'Buffer', payload.code);
  const items = payload.items.map((json) => ({ json }));
  const nodeOutputs = payload.nodeOutputs || {};
  const dollar = (name) => {
    if (!Object.prototype.hasOwnProperty.call(nodeOutputs, name)) {
      throw new Error('missing_node_output_' + name);
    }
    return {
      first: () => ({ json: nodeOutputs[name] }),
      all: () => [{ json: nodeOutputs[name] }],
    };
  };
  const result = run({ all: () => items, first: () => items[0] }, dollar, Buffer);
  console.log(JSON.stringify({ ok: true, result }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: String(error && error.message ? error.message : error) }));
}
"""
    completed = subprocess.run(
        [node_exe, "-e", wrapper],
        input=json.dumps(
            {
                "code": workflow_code(node_name),
                "items": items,
                "nodeOutputs": node_outputs or {},
            }
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def run_validate_code(text):
    return run_node_code("Validate Copied Sanitized Result Strictly", [{"data": text}])


def run_verify_code(rows, result):
    return run_node_code(
        "Verify Identity And Decide Mapping",
        rows,
        node_outputs={"Validate Copied Sanitized Result Strictly": result},
    )


def run_evidence_code(verify_output):
    return run_node_code(
        "Emit Aggregate Evidence Only",
        [{}],
        node_outputs={"Verify Identity And Decide Mapping": verify_output},
    )


class Gate5APrecheckTests(unittest.TestCase):
    def run_precheck(self, result_path):
        return subprocess.run(
            [sys.executable, str(PRECHECK_SCRIPT), "--result-jsonl", str(result_path)],
            text=True,
            capture_output=True,
            check=False,
        )

    def precheck_for_rows(self, rows, raw_text=None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "member_lookup_bridge_gate5a_result_copy.jsonl"
            if raw_text is not None:
                write_text(result_path, raw_text)
            else:
                write_text(result_path, "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
            return self.run_precheck(result_path)

    def test_precheck_accepts_exactly_one_canonical_result_with_exact_aggregate_shape(self):
        completed = self.precheck_for_rows([result_row()])

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(list(evidence.keys()), EXPECTED_PRECHECK_EVIDENCE_KEYS)
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["gate"], "gate5a_manual_inactive_n8n_result_mapping")
        self.assertEqual(evidence["runtime_location"], "local_operator_pc_non_ac2_n8n_stack")
        self.assertEqual(evidence["execution_mode"], "manual_inactive_single_result_copy_precheck")
        self.assertEqual(evidence["input_result_row_count"], "1")
        self.assertEqual(evidence["result_schema_valid_count"], "1")
        self.assertEqual(evidence["result_state_consistent_count"], "1")
        self.assertEqual(evidence["forbidden_field_count"], "0")
        self.assertEqual(evidence["unexpected_shape_count"], "0")
        self.assertEqual(evidence["mapped_ready_for_create_review_count"], "1")
        self.assertEqual(evidence["original_gate4_artifacts_modified"], "false")
        self.assertEqual(evidence["n8n_result_mapping_run"], "false")
        self.assertEqual(evidence["workflow_activation"], "inactive")
        self.assertEqual(evidence["final_write_automation"], "false")
        self.assertEqual(evidence["no_row_values_printed"], "true")

        for forbidden in [
            expected_job_id(),
            "gate4a_fnv1a",
            "row_2",
            "2026-07-14",
            "MTExMTEx",
            "111111",
        ]:
            self.assertNotIn(forbidden, completed.stdout)

    def test_precheck_reports_all_valid_review_states_as_mapped_counts(self):
        cases = [
            (result_row(), "mapped_ready_for_create_review_count"),
            (error_result_row(), "mapped_lookup_error_review_count"),
            (
                result_row(
                    state="MANUAL_REVIEW_REQUIRED",
                    manual_review_required=True,
                    warning_count=1,
                ),
                "mapped_manual_review_required_count",
            ),
            (
                result_row(
                    state="EXISTING_MEMBER_REVIEW",
                    member_exists=True,
                    member_found_by="MemberCommand.GetMember",
                ),
                "mapped_existing_member_review_count",
            ),
        ]
        for row, expected_counter in cases:
            completed = self.precheck_for_rows([row])
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(evidence["status"], "ok")
            self.assertEqual(evidence[expected_counter], "1")

    def test_precheck_fails_closed_for_missing_empty_multiple_and_malformed_inputs(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            missing = self.run_precheck(Path(tmp) / "does_not_exist.jsonl")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(parse_evidence(missing.stdout)["status"], "needs_fix")

        cases = [
            ("", "0"),
            ("   \n\n", "0"),
            (json.dumps(result_row()) + "\n" + json.dumps(result_row()) + "\n", "2"),
            ("{not-json}\n", "1"),
            (json.dumps([result_row()]) + "\n", "1"),
            ('"just-a-string"\n', "1"),
        ]
        for raw_text, expected_rows in cases:
            completed = self.precheck_for_rows([], raw_text=raw_text)
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(completed.returncode, 2, raw_text)
            self.assertEqual(evidence["status"], "needs_fix", raw_text)
            self.assertEqual(evidence["input_result_row_count"], expected_rows, raw_text)

    def test_precheck_fails_closed_for_missing_unexpected_and_forbidden_fields(self):
        missing_field = result_row()
        missing_field.pop("warning_count")
        completed = self.precheck_for_rows([missing_field])
        self.assertEqual(parse_evidence(completed.stdout)["status"], "needs_fix")

        unexpected = result_row()
        unexpected["surprise_field"] = "surprise-value"
        completed = self.precheck_for_rows([unexpected])
        evidence = parse_evidence(completed.stdout)
        self.assertEqual(evidence["status"], "needs_fix")
        self.assertNotIn("surprise-value", completed.stdout)

        for forbidden_field in [
            "member_no",
            "submitted_member_no_base64_utf8",
            "payload_hash",
            "name",
            "email",
            "phone",
            "dob",
            "stdout",
            "sheet_url",
        ]:
            row = result_row()
            row[forbidden_field] = "forbidden-value-present"
            completed = self.precheck_for_rows([row])
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(completed.returncode, 2, forbidden_field)
            self.assertEqual(evidence["status"], "needs_fix", forbidden_field)
            self.assertEqual(evidence["forbidden_field_count"], "1", forbidden_field)
            self.assertNotIn("forbidden-value-present", completed.stdout)

    def test_precheck_fails_closed_for_invalid_types_and_flags(self):
        invalid_rows = [
            result_row(job_id="not-a-canonical-job-id"),
            result_row(job_id=None),
            result_row(intake_source="google_sheets_uat"),
            result_row(source_row_ref="row_3"),
            result_row(row_number="2"),
            result_row(row_number=True),
            result_row(state="PENDING_LOOKUP"),
            result_row(state="CREATED"),
            result_row(status="unknown"),
            result_row(authentication_success="yes"),
            result_row(normalized_member_no_length="10"),
            result_row(submitted_member_no_status="unexpected_label"),
            result_row(warning_count=-1),
            result_row(warning_count=True),
            result_row(error_code="Bad Code With Spaces"),
            result_row(consent_status="acknowledged"),
            result_row(pdpa_status="Imported"),
            result_row(attempt=1),
            result_row(result_created_at=None),
            result_row(result_created_at="not a timestamp !!"),
            result_row(dry_run_only=False),
            result_row(dry_run_only="true"),
            result_row(final_write_automation=True),
            result_row(result_applied_at="2026-07-14T11:00:00+00:00"),
        ]
        for row in invalid_rows:
            completed = self.precheck_for_rows([row])
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(completed.returncode, 2, row)
            self.assertEqual(evidence["status"], "needs_fix", row)
            self.assertEqual(evidence["result_schema_valid_count"], "0", row)

    def test_precheck_fails_closed_for_state_inconsistent_results(self):
        inconsistent_rows = [
            # Stored state disagrees with the recomputed precedence.
            result_row(state="LOOKUP_ERROR_REVIEW"),
            result_row(state="EXISTING_MEMBER_REVIEW"),
            result_row(state="MANUAL_REVIEW_REQUIRED"),
            result_row(state="READY_FOR_CREATE_REVIEW", warning_count=1),
            result_row(state="READY_FOR_CREATE_REVIEW", member_exists=True),
            result_row(
                state="EXISTING_MEMBER_REVIEW",
                member_exists=True,
                warning_count=2,
            ),
            # ok results must carry no error_code and all-true lookup flags.
            result_row(error_code="runtimeexception"),
            result_row(authentication_success=False),
            # error results must carry an error_code and the error review state.
            error_result_row(error_code=None),
            error_result_row(state="READY_FOR_CREATE_REVIEW"),
        ]
        for row in inconsistent_rows:
            completed = self.precheck_for_rows([row])
            evidence = parse_evidence(completed.stdout)
            self.assertEqual(completed.returncode, 2, row)
            self.assertEqual(evidence["status"], "needs_fix", row)
            self.assertEqual(evidence["result_state_consistent_count"], "0", row)

    def test_precheck_never_modifies_the_staging_copy(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "member_lookup_bridge_gate5a_result_copy.jsonl"
            original_text = json.dumps(result_row(), sort_keys=True) + "\n"
            write_text(result_path, original_text)
            before = result_path.read_bytes()
            self.run_precheck(result_path)
            self.assertEqual(result_path.read_bytes(), before)


class Gate5AWorkflowTemplateTests(unittest.TestCase):
    def test_template_parses_and_is_inactive(self):
        template = load_template()
        self.assertIs(template["active"], False)

    def test_template_uses_only_approved_node_types_and_versions(self):
        template = load_template()
        types = {node["type"] for node in template["nodes"]}
        self.assertEqual(types, ALLOWED_NODE_TYPES)

        expected_versions = {
            "n8n-nodes-base.manualTrigger": 1,
            "n8n-nodes-base.readWriteFile": 1.1,
            "n8n-nodes-base.extractFromFile": 1.1,
            "n8n-nodes-base.code": 2,
            "n8n-nodes-base.googleSheets": 4.7,
            "n8n-nodes-base.if": 2.3,
            "n8n-nodes-base.stickyNote": 1,
        }
        for node in template["nodes"]:
            self.assertEqual(node["typeVersion"], expected_versions[node["type"]], node["name"])

    def test_template_has_exactly_one_manual_trigger_and_no_other_trigger(self):
        template = load_template()
        trigger_nodes = [
            node for node in template["nodes"] if "trigger" in node["type"].lower()
        ]
        self.assertEqual(len(trigger_nodes), 1)
        self.assertEqual(trigger_nodes[0]["type"], "n8n-nodes-base.manualTrigger")

        template_text = TEMPLATE.read_text(encoding="utf-8")
        for forbidden_type in [
            "scheduleTrigger",
            "n8n-nodes-base.webhook",
            "n8n-nodes-base.wait",
            "executeCommand",
            "n8n-nodes-base.ssh",
            "httpRequest",
            "microsoftSql",
            "postgres",
            "mySql",
            "emailSend",
            "gmail",
            "slack",
            "executeWorkflow",
        ]:
            self.assertNotIn(forbidden_type, template_text)

    def test_template_has_no_credentials_pinned_data_or_execution_data(self):
        template_text = TEMPLATE.read_text(encoding="utf-8")
        template = load_template()

        for node in template["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
        for forbidden_key in ["pinData", "staticData", "shared", "tags", "triggerCount"]:
            self.assertNotIn(forbidden_key, template)
        for forbidden_text in [
            "cachedResultName",
            "cachedResultUrl",
            "docs.google.com",
            "googleSheetsOAuth2Api",
            '"credentials"',
        ]:
            self.assertNotIn(forbidden_text, template_text)

    def test_template_execution_data_settings_are_most_restrictive(self):
        template = load_template()
        settings = template["settings"]
        self.assertEqual(settings["saveDataErrorExecution"], "none")
        self.assertEqual(settings["saveDataSuccessExecution"], "none")
        self.assertIs(settings["saveManualExecutions"], False)
        self.assertIs(settings["saveExecutionProgress"], False)

    def test_template_reader_uses_approved_container_path_only(self):
        template = load_template()
        reader = next(
            node for node in template["nodes"] if node["type"] == "n8n-nodes-base.readWriteFile"
        )
        self.assertEqual(reader["parameters"]["fileSelector"], CONTAINER_RESULT_FILE)
        self.assertTrue(CONTAINER_RESULT_FILE.startswith("/home/node/.n8n-files/"))
        # The reader must never be a writer: read is the node default operation and no
        # write/append parameters may be present.
        self.assertNotIn("operation", reader["parameters"])
        self.assertNotIn("fileName", reader["parameters"])
        self.assertNotIn("append", json.dumps(reader["parameters"]))

    def test_template_google_sheets_nodes_are_unbound_with_safe_filter(self):
        template = load_template()
        sheets_nodes = [
            node for node in template["nodes"] if node["type"] == "n8n-nodes-base.googleSheets"
        ]
        self.assertEqual(len(sheets_nodes), 2)
        for node in sheets_nodes:
            self.assertEqual(node["parameters"]["documentId"], {"__rl": True, "value": "", "mode": "list"})
            self.assertEqual(node["parameters"]["sheetName"], {"__rl": True, "value": "", "mode": "list"})

        read_node = next(node for node in sheets_nodes if "operation" not in node["parameters"])
        self.assertEqual(
            read_node["parameters"]["filtersUI"],
            {"values": [{"lookupColumn": "Gate5AApprovedForMapping", "lookupValue": "YES"}]},
        )

    def test_template_update_node_writes_only_controlled_review_columns(self):
        template = load_template()
        update_node = next(
            node
            for node in template["nodes"]
            if node["type"] == "n8n-nodes-base.googleSheets"
            and node["parameters"].get("operation") == "update"
        )
        columns = update_node["parameters"]["columns"]
        self.assertEqual(columns["mappingMode"], "defineBelow")
        self.assertEqual(columns["matchingColumns"], ["row_number"])
        self.assertEqual(columns["schema"], [])
        self.assertEqual(update_node["parameters"]["options"], {"cellFormat": "RAW"})

        written_columns = set(columns["value"].keys())
        self.assertEqual(written_columns, set(REVIEW_COLUMNS) | {"row_number"})

        for forbidden_column in [
            "Full Name",
            "AutoCount MemberNo",
            "Email Address",
            "Birthday Month",
            "Marketing Consent",
            "PDPA Acknowledged",
            "uat_lookup_queued_at",
            "reviewer_decision_code",
        ]:
            self.assertNotIn(forbidden_column, written_columns)

        self.assertEqual(columns["value"]["reviewer_status"], "={{ $json.reviewer_status }}")

    def test_template_connections_match_designed_flow_exactly(self):
        template = load_template()
        expected = {
            "Manual Gate 5A Run": {
                "main": [[{"node": "Read Copied Sanitized Recovery Result", "type": "main", "index": 0}]]
            },
            "Read Copied Sanitized Recovery Result": {
                "main": [[{"node": "Extract Result Text", "type": "main", "index": 0}]]
            },
            "Extract Result Text": {
                "main": [[{"node": "Validate Copied Sanitized Result Strictly", "type": "main", "index": 0}]]
            },
            "Validate Copied Sanitized Result Strictly": {
                "main": [[{"node": "Read One Approved Source Row", "type": "main", "index": 0}]]
            },
            "Read One Approved Source Row": {
                "main": [[{"node": "Verify Identity And Decide Mapping", "type": "main", "index": 0}]]
            },
            "Verify Identity And Decide Mapping": {
                "main": [[{"node": "Apply Only Fresh Mapping", "type": "main", "index": 0}]]
            },
            "Apply Only Fresh Mapping": {
                "main": [
                    [{"node": "Update Approved Review Fields Only", "type": "main", "index": 0}],
                    [{"node": "Emit Aggregate Evidence Only", "type": "main", "index": 0}],
                ]
            },
            "Update Approved Review Fields Only": {
                "main": [[{"node": "Emit Aggregate Evidence Only", "type": "main", "index": 0}]]
            },
        }
        self.assertEqual(template["connections"], expected)

    def test_template_has_no_forbidden_write_tokens_or_ac2_reach(self):
        template_text = TEMPLATE.read_text(encoding="utf-8")
        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, template_text)
        for forbidden in ["AllowRootLogin", "AC2_PROBE", "192.168.", "rdp", "cloudflare"]:
            self.assertNotIn(forbidden, template_text)


class Gate5AValidateCodeTests(unittest.TestCase):
    def test_valid_single_result_passes_and_returns_the_result_unchanged(self):
        row = result_row()
        completed = run_validate_code(json.dumps(row) + "\n")
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(completed["result"][0]["json"], row)

    def test_all_four_valid_states_pass_validation(self):
        for row in [
            result_row(),
            error_result_row(),
            result_row(state="MANUAL_REVIEW_REQUIRED", manual_review_required=True, warning_count=1),
            result_row(
                state="EXISTING_MEMBER_REVIEW",
                member_exists=True,
                member_found_by="MemberCommand.GetMember",
            ),
        ]:
            completed = run_validate_code(json.dumps(row) + "\n")
            self.assertTrue(completed["ok"], completed)

    def test_empty_multiple_malformed_and_non_object_inputs_fail_closed(self):
        cases = [
            ("", "gate5a_result_copy_empty"),
            ("   \n \n", "gate5a_result_copy_empty"),
            (
                json.dumps(result_row()) + "\n" + json.dumps(result_row()) + "\n",
                "gate5a_result_copy_must_contain_exactly_one_row",
            ),
            ("{not json}\n", "gate5a_result_copy_malformed_json"),
            (json.dumps([result_row()]) + "\n", "gate5a_result_copy_not_a_single_object"),
            ("null\n", "gate5a_result_copy_not_a_single_object"),
        ]
        for text, expected_error in cases:
            completed = run_validate_code(text)
            self.assertFalse(completed["ok"], completed)
            self.assertEqual(completed["error"], expected_error, text)

    def test_missing_unexpected_and_forbidden_fields_fail_closed(self):
        missing = result_row()
        missing.pop("state")
        completed = run_validate_code(json.dumps(missing) + "\n")
        self.assertFalse(completed["ok"])
        self.assertEqual(completed["error"], "gate5a_result_required_field_missing")

        unexpected = result_row()
        unexpected["surprise_field"] = "surprise-value"
        completed = run_validate_code(json.dumps(unexpected) + "\n")
        self.assertFalse(completed["ok"])
        self.assertEqual(completed["error"], "gate5a_result_unexpected_field_present")

        for forbidden_field in [
            "member_no",
            "submitted_member_no_base64_utf8",
            "payload_hash",
            "name",
            "email",
            "phone",
            "dob",
            "stderr",
            "sheet_url",
            "token",
        ]:
            row = result_row()
            row[forbidden_field] = "forbidden-value-present"
            completed = run_validate_code(json.dumps(row) + "\n")
            self.assertFalse(completed["ok"], forbidden_field)
            self.assertEqual(completed["error"], "gate5a_result_forbidden_field_present", forbidden_field)
            self.assertNotIn("forbidden-value-present", completed["error"])

    def test_flag_state_and_consistency_violations_fail_closed(self):
        cases = [
            (result_row(dry_run_only=False), "gate5a_result_dry_run_only_must_be_true"),
            (result_row(final_write_automation=True), "gate5a_result_final_write_automation_must_be_false"),
            (
                result_row(result_applied_at="2026-07-14T11:00:00+00:00"),
                "gate5a_result_applied_at_must_be_null",
            ),
            (result_row(state="UNKNOWN_STATE"), "gate5a_result_state_unrecognised"),
            (result_row(status="unknown"), "gate5a_result_status_unrecognised"),
            (result_row(state="LOOKUP_ERROR_REVIEW"), "gate5a_result_state_inconsistent"),
            (result_row(warning_count=1), "gate5a_result_state_inconsistent"),
            (result_row(member_exists=True), "gate5a_result_state_inconsistent"),
            (result_row(error_code="runtimeexception"), "gate5a_result_state_inconsistent"),
            (error_result_row(error_code=None), "gate5a_result_state_inconsistent"),
            (error_result_row(state="READY_FOR_CREATE_REVIEW"), "gate5a_result_state_inconsistent"),
            (result_row(attempt=1), "gate5a_result_field_types_invalid"),
            (result_row(row_number="2"), "gate5a_result_field_types_invalid"),
            (result_row(job_id="not-canonical"), "gate5a_result_field_types_invalid"),
            (result_row(consent_status="acknowledged"), "gate5a_result_field_types_invalid"),
        ]
        for row, expected_error in cases:
            completed = run_validate_code(json.dumps(row) + "\n")
            self.assertFalse(completed["ok"], row)
            self.assertEqual(completed["error"], expected_error, row)


class Gate5AVerifyCodeTests(unittest.TestCase):
    def test_fresh_blank_row_with_matching_identity_decides_apply(self):
        completed = run_verify_code([source_row()], result_row())
        self.assertTrue(completed["ok"], completed)
        output = completed["result"][0]["json"]

        self.assertEqual(output["gate5a_decision"], "apply")
        self.assertEqual(output["gate5a_mapped_state"], "READY_FOR_CREATE_REVIEW")
        self.assertEqual(output["row_number"], 2)
        self.assertEqual(output["uat_lookup_job_id"], expected_job_id())
        self.assertEqual(output["uat_lookup_state"], "READY_FOR_CREATE_REVIEW")
        self.assertEqual(output["uat_lookup_status"], "ok")
        self.assertEqual(output["uat_lookup_error_code"], "")
        self.assertEqual(output["uat_lookup_warning_count"], "0")
        self.assertEqual(output["uat_lookup_attempt"], "0")
        self.assertEqual(output["uat_lookup_completed_at"], "2026-07-14T10:00:00+00:00")
        self.assertEqual(output["reviewer_status"], "UNREVIEWED")

        expected_keys = {"gate5a_decision", "gate5a_mapped_state", "row_number", *REVIEW_COLUMNS}
        self.assertEqual(set(output.keys()), expected_keys)

        serialized = json.dumps(output)
        for forbidden in ["source-name-present", "source-email-present", "111111", "June", "MTExMTEx"]:
            self.assertNotIn(forbidden, serialized)

    def test_reviewer_status_is_never_a_create_approval(self):
        completed = run_verify_code([source_row()], result_row())
        output = completed["result"][0]["json"]
        self.assertEqual(output["reviewer_status"], "UNREVIEWED")
        serialized = json.dumps(output).lower()
        for forbidden in ["approved", "create_member", "member_create", "approval"]:
            self.assertNotIn(forbidden, serialized)

    def test_zero_or_multiple_source_rows_refuse(self):
        for rows in [[], [source_row(), source_row(row_number=3)]]:
            completed = run_verify_code(rows, result_row())
            self.assertFalse(completed["ok"], completed)
            self.assertEqual(completed["error"], "gate5a_requires_exactly_one_approved_source_row")

    def test_missing_required_review_column_fails_with_setup_guidance(self):
        for column in REVIEW_COLUMNS:
            row = source_row()
            row.pop(column)
            completed = run_verify_code([row], result_row())
            self.assertFalse(completed["ok"], column)
            self.assertEqual(
                completed["error"],
                "gate5a_required_review_column_missing_add_review_columns_before_run",
                column,
            )

    def test_row_number_alone_is_insufficient_identity(self):
        # Same row number, different submitted member value: recomputed canonical
        # job_id no longer matches the sanitized result, so the mapping refuses.
        completed = run_verify_code(
            [source_row(**{"AutoCount MemberNo": "222222"})],
            result_row(),
        )
        self.assertFalse(completed["ok"], completed)
        self.assertEqual(completed["error"], "gate5a_result_does_not_match_approved_source_row")

    def test_identity_mismatches_refuse(self):
        mismatch_results = [
            result_row(job_id=expected_job_id(member_no="222222")),
            result_row(source_row_ref="row_3", row_number=3),
            result_row(intake_source="google_sheets_uat"),
            result_row(consent_status="acknowledged"),
            result_row(attempt=1),
        ]
        for result in mismatch_results:
            completed = run_verify_code([source_row()], result)
            self.assertFalse(completed["ok"], result)
            self.assertEqual(
                completed["error"], "gate5a_result_does_not_match_approved_source_row", result
            )

    def test_source_row_form_guards_still_apply(self):
        cases = [
            (source_row(**{"PDPA Acknowledged": "Imported"}), "gate5a_imported_pdpa_marker_blocked"),
            (source_row(**{"PDPA Acknowledged": "No"}), "gate5a_pdpa_must_be_yes"),
            (source_row(**{"Full Name": ""}), "gate5a_full_name_required"),
            (source_row(**{"Email Address": ""}), "gate5a_email_address_required"),
            (source_row(**{"Birthday Month": "not-a-month"}), "gate5a_birthday_month_must_be_valid_month_name"),
            (source_row(**{"AutoCount MemberNo": "ABC123"}), "gate5a_autocount_member_no_must_be_6_to_20_digits"),
            (source_row(**{"Full Name": "synthetic-source-marker"}), "gate5a_source_row_must_be_real_non_dummy"),
            (source_row(row_number=""), "gate5a_safe_row_number_required"),
        ]
        for row, expected_error in cases:
            completed = run_verify_code([row], result_row())
            self.assertFalse(completed["ok"], expected_error)
            self.assertEqual(completed["error"], expected_error)

    def test_mapping_precedence_is_strict(self):
        manual_over_existing = result_row(
            state="MANUAL_REVIEW_REQUIRED",
            manual_review_required=True,
            warning_count=1,
            member_exists=True,
            member_found_by="MemberCommand.GetMember",
        )
        completed = run_verify_code([source_row()], manual_over_existing)
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(completed["result"][0]["json"]["gate5a_mapped_state"], "MANUAL_REVIEW_REQUIRED")

        warning_only = result_row(
            state="MANUAL_REVIEW_REQUIRED",
            manual_review_required=False,
            warning_count=2,
        )
        completed = run_verify_code([source_row()], warning_only)
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(completed["result"][0]["json"]["gate5a_mapped_state"], "MANUAL_REVIEW_REQUIRED")

        existing = result_row(
            state="EXISTING_MEMBER_REVIEW",
            member_exists=True,
            member_found_by="MemberCommand.GetMember",
        )
        completed = run_verify_code([source_row()], existing)
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(completed["result"][0]["json"]["gate5a_mapped_state"], "EXISTING_MEMBER_REVIEW")

        error_result = error_result_row()
        completed = run_verify_code([source_row()], error_result)
        self.assertTrue(completed["ok"], completed)
        output = completed["result"][0]["json"]
        self.assertEqual(output["gate5a_mapped_state"], "LOOKUP_ERROR_REVIEW")
        self.assertEqual(output["uat_lookup_error_code"], "runtimeexception")

        inconsistent = result_row(state="EXISTING_MEMBER_REVIEW", member_exists=False)
        completed = run_verify_code([source_row()], inconsistent)
        self.assertFalse(completed["ok"], completed)
        self.assertEqual(completed["error"], "gate5a_result_state_inconsistent")

    def test_exact_rerun_returns_already_applied(self):
        completed = run_verify_code([applied_source_row()], result_row())
        self.assertTrue(completed["ok"], completed)
        output = completed["result"][0]["json"]
        self.assertEqual(output["gate5a_decision"], "already_applied")
        self.assertEqual(output["gate5a_mapped_state"], "READY_FOR_CREATE_REVIEW")

    def test_conflicting_prior_mapping_refuses(self):
        conflict_rows = [
            applied_source_row(uat_lookup_state="EXISTING_MEMBER_REVIEW"),
            applied_source_row(uat_lookup_job_id="gate4a_fnv1a_deadbeef"),
            applied_source_row(reviewer_status="REVIEWED_NO_CREATE_APPROVAL"),
            applied_source_row(uat_lookup_completed_at="2026-07-14T12:34:56+00:00"),
            # Partial fill: some review cells populated, others blank.
            source_row(uat_lookup_state="READY_FOR_CREATE_REVIEW"),
            source_row(reviewer_status="UNREVIEWED"),
        ]
        for row in conflict_rows:
            completed = run_verify_code([row], result_row())
            self.assertFalse(completed["ok"], row)
            self.assertEqual(completed["error"], "gate5a_conflicting_prior_mapping_refused", row)

    def test_applying_result_to_a_different_row_never_happens(self):
        # The approved row is row 3, but the result belongs to row 2's identity.
        moved_row = source_row(row_number=3)
        completed = run_verify_code([moved_row], result_row())
        self.assertFalse(completed["ok"], completed)
        self.assertEqual(completed["error"], "gate5a_result_does_not_match_approved_source_row")


class Gate5AEvidenceCodeTests(unittest.TestCase):
    def verify_output(self, decision="apply", mapped_state="READY_FOR_CREATE_REVIEW"):
        return {
            "gate5a_decision": decision,
            "gate5a_mapped_state": mapped_state,
            "row_number": 2,
            "uat_lookup_job_id": expected_job_id(),
            "uat_lookup_state": mapped_state,
            "uat_lookup_status": "ok",
            "uat_lookup_error_code": "",
            "uat_lookup_warning_count": "0",
            "uat_lookup_attempt": "0",
            "uat_lookup_completed_at": "2026-07-14T10:00:00+00:00",
            "reviewer_status": "UNREVIEWED",
        }

    def test_fresh_apply_evidence_shape_is_exact_and_aggregate_only(self):
        completed = run_evidence_code(self.verify_output())
        self.assertTrue(completed["ok"], completed)
        evidence = completed["result"][0]["json"]

        self.assertEqual(list(evidence.keys()), EXPECTED_WORKFLOW_EVIDENCE_KEYS)
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["gate"], "gate5a_manual_inactive_n8n_result_mapping")
        self.assertEqual(evidence["runtime_location"], "local_operator_pc_non_ac2_n8n_stack")
        self.assertEqual(
            evidence["execution_mode"], "manual_inactive_single_result_review_mapping_only"
        )
        self.assertEqual(evidence["mapping_attempt_count"], 1)
        self.assertEqual(evidence["mapping_success_count"], 1)
        self.assertEqual(evidence["mapping_already_applied_count"], 0)
        self.assertEqual(evidence["mapping_error_count"], 0)
        self.assertEqual(evidence["mapped_ready_for_create_review_count"], 1)
        self.assertEqual(evidence["reviewer_status_unreviewed_count"], 1)
        self.assertIs(evidence["original_gate4_artifacts_modified"], False)
        self.assertIs(evidence["autocount_lookup_invoked"], False)
        self.assertIs(evidence["member_create_or_update_invoked"], False)
        self.assertIs(evidence["autocount_write_attempted"], False)
        self.assertIs(evidence["direct_sql_write_attempted"], False)
        self.assertEqual(evidence["workflow_activation"], "inactive")
        self.assertIs(evidence["scheduler_enabled"], False)
        self.assertIs(evidence["public_inbound_to_ac2_host"], False)
        self.assertIs(evidence["final_write_automation"], False)
        self.assertIs(evidence["no_row_values_printed"], True)

        serialized = json.dumps(evidence)
        for forbidden in [expected_job_id(), "gate4a_fnv1a", "2026-07-14", "UNREVIEWED", "row_number"]:
            self.assertNotIn(forbidden, serialized)

    def test_already_applied_evidence_reports_zero_writes(self):
        completed = run_evidence_code(self.verify_output(decision="already_applied"))
        self.assertTrue(completed["ok"], completed)
        evidence = completed["result"][0]["json"]
        self.assertEqual(evidence["mapping_success_count"], 0)
        self.assertEqual(evidence["mapping_already_applied_count"], 1)
        self.assertEqual(evidence["mapping_error_count"], 0)

    def test_each_mapped_state_reports_exactly_one_counter(self):
        counters = {
            "LOOKUP_ERROR_REVIEW": "mapped_lookup_error_review_count",
            "MANUAL_REVIEW_REQUIRED": "mapped_manual_review_required_count",
            "EXISTING_MEMBER_REVIEW": "mapped_existing_member_review_count",
            "READY_FOR_CREATE_REVIEW": "mapped_ready_for_create_review_count",
        }
        for state, counter in counters.items():
            completed = run_evidence_code(self.verify_output(mapped_state=state))
            evidence = completed["result"][0]["json"]
            for other_state, other_counter in counters.items():
                self.assertEqual(
                    evidence[other_counter], 1 if other_counter == counter else 0, state
                )

    def test_unexpected_decision_or_state_fails_closed(self):
        completed = run_evidence_code(self.verify_output(decision="create_member"))
        self.assertFalse(completed["ok"], completed)
        self.assertEqual(completed["error"], "gate5a_unexpected_mapping_decision")

        completed = run_evidence_code(self.verify_output(mapped_state="CREATED"))
        self.assertFalse(completed["ok"], completed)
        self.assertEqual(completed["error"], "gate5a_unexpected_mapped_state")


class Gate5AWiringAndDocsTests(unittest.TestCase):
    def read(self, path):
        return path.read_text(encoding="utf-8")

    def test_readme_gitignore_runbook_workflow_and_script_are_wired(self):
        readme = self.read(README)
        self.assertIn("scripts/member_lookup_gate5a_result_precheck.py", readme)
        self.assertIn(
            "docs/autocount2-automation/member_intake_n8n_gate5a_manual_result_mapping_runbook.md",
            readme,
        )
        self.assertIn(
            "n8n-workflows/member_intake_gate5a_sanitized_result_mapping.workflow.json", readme
        )

        gitignore = self.read(GITIGNORE)
        self.assertIn("member_lookup_bridge_gate5a_result_copy.jsonl", gitignore)
        self.assertIn(
            "autocount_outputs/**/member_lookup_bridge_gate5a_result_copy.jsonl", gitignore
        )

        self.assertTrue(RUNBOOK.exists())
        self.assertTrue(TEMPLATE.exists())
        self.assertTrue(PRECHECK_SCRIPT.exists())

    def test_runbook_defines_copy_only_manual_handoff_and_no_ac2_inbound(self):
        runbook = self.read(RUNBOOK)
        for phrase in [
            "Copy — never move —",
            "The AutoCount host receives no inbound network connection, webhook, callback, tunnel, or n8n command",
            "member_lookup_bridge_gate4_recovery1_results.jsonl",
            "member_lookup_bridge_gate5a_result_copy.jsonl",
            "n8n:/home/node/.n8n-files/member_lookup_bridge_gate5a_result_copy.jsonl",
            "mkdir -p /home/node/.n8n-files && chmod 700 /home/node/.n8n-files",
            "member_lookup_gate5a_result_precheck.py",
            "Keep the workflow inactive",
            "Manually execute the workflow exactly once",
            "Never edit, delete, truncate, rename, move, overwrite, reset, or \"repair\" any of them",
        ]:
            self.assertIn(phrase, runbook)

    def test_runbook_documents_uat_plan_differences_and_expected_state(self):
        runbook = self.read(RUNBOOK)
        self.assertIn("## Differences From The Older n8n Lookup-Bridge UAT Plan", runbook)
        self.assertIn("`result_applied_at` must remain `null`", runbook)
        self.assertIn("all 25 allowed keys", runbook)
        self.assertIn("does not write either field", runbook)
        self.assertIn(
            "For the current recorded recovery evidence the expected mapped state is `READY_FOR_CREATE_REVIEW`.",
            runbook,
        )
        self.assertIn("`reviewer_status = UNREVIEWED`", runbook)

    def test_runbook_keeps_review_only_boundaries_and_stop_conditions(self):
        runbook = self.read(RUNBOOK)
        self.assertIn("## Stop Conditions", runbook)
        stop_section = runbook.split("## Stop Conditions", 1)[1]
        for phrase in [
            "The workflow is activated",
            "inbound connection, callback, or n8n command reaches the AutoCount host",
            "edited, deleted, renamed, moved, truncated, overwritten",
            "No state authorizes member creation",
        ]:
            self.assertIn(phrase, runbook)
        self.assertIn("No state authorizes member creation", runbook)
        self.assertIn("never member-creation approval", stop_section + runbook)

    def test_committed_gate5a_material_has_no_sensitive_literals(self):
        for path in [PRECHECK_SCRIPT, TEMPLATE, RUNBOOK]:
            text = self.read(path)
            for token in FORBIDDEN_WRITE_TOKENS:
                self.assertNotIn(token, text, path.name)
            for forbidden in [
                "AllowRootLogin",
                "docs.google.com",
                "cachedResultUrl",
                "BEGIN PRIVATE KEY",
                "password=",
            ]:
                self.assertNotIn(forbidden, text, path.name)


if __name__ == "__main__":
    unittest.main()
