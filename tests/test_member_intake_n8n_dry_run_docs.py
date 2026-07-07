import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "autocount2-automation"
README = ROOT / "README.md"
WORKFLOW_DOC = DOCS / "member_intake_n8n_dry_run_workflow.md"
UAT_PLAN = DOCS / "member_intake_n8n_lookup_bridge_uat_plan.md"
NODE_CONTRACT = DOCS / "member_intake_n8n_node_contract.md"
DIRECT_RUNBOOK = DOCS / "member_intake_n8n_direct_lookup_runbook.md"
LOOKUP_RUNBOOK = DOCS / "member_lookup_review_runbook.md"
BRIDGE_DESIGN = DOCS / "member_intake_local_bridge_design.md"
BRIDGE_RUNBOOK = DOCS / "member_intake_local_lookup_bridge_runbook.md"
DECISION_RUNBOOK = DOCS / "member_intake_decision_review_runbook.md"
TEMPLATE_PATH = DOCS / "templates" / "member_intake_n8n_dry_run_lookup.reference.json"


DRY_RUN_DOCS = [WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, BRIDGE_RUNBOOK]
FORBIDDEN_WRITE_TOKENS = [
    "Save" + "Member",
    "New" + "Member",
    "Delete" + "Member",
    "GetNext" + "MemberNo",
]


class MemberIntakeN8nDryRunDocsTests(unittest.TestCase):
    def read(self, path):
        return path.read_text(encoding="utf-8")

    def combined(self, paths):
        return "\n".join(self.read(path) for path in paths)

    def test_new_docs_exist_and_are_linked_from_readme(self):
        readme = self.read(README)

        for path in [WORKFLOW_DOC, UAT_PLAN, NODE_CONTRACT, DIRECT_RUNBOOK, BRIDGE_RUNBOOK]:
            self.assertTrue(path.exists(), path)
            self.assertIn(path.name, readme)

    def test_docs_state_cloud_or_non_ac2_n8n_plus_windows_bridge_architecture(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)cloud/VPS/non-AC2 n8n|cloud, VPS, or another non-AC2")
        self.assertRegex(combined, r"(?i)Windows AC2 lookup bridge|local Windows AC2 lookup bridge")
        self.assertRegex(combined, r"(?i)only component allowed to load AutoCount assemblies")
        self.assertRegex(combined, r"(?i)not the final architecture|not the long-term n8n hosting architecture")
        self.assertNotRegex(combined, r"(?i)current runtime direction is local self-hosted n8n")
        self.assertNotRegex(combined, r"(?i)main dry-run runtime path for local self-hosted n8n")

    def test_cloud_n8n_direct_execute_command_to_local_ac2_is_blocked(self):
        combined = self.combined([WORKFLOW_DOC, UAT_PLAN, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)Cloud n8n cannot directly run local AC2 PowerShell")
        self.assertRegex(combined, r"(?i)n8n Execute Command runs on the n8n host/container")
        self.assertRegex(combined, r"(?i)invalid for local AC2 lookup")
        self.assertNotRegex(combined, r"(?i)cloud n8n can directly call local AC2 PowerShell")

    def test_outbound_polling_bridge_is_preferred_and_public_inbound_is_not(self):
        combined = self.combined([WORKFLOW_DOC, UAT_PLAN, NODE_CONTRACT, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)outbound polling|polls outbound|poll outbound")
        self.assertRegex(combined, r"(?i)preferred")
        self.assertRegex(combined, r"(?i)public inbound webhook.*not recommended|not recommended.*public inbound webhook")
        self.assertRegex(combined, r"(?i)separate approval|separately approved")

    def test_docs_require_base64_member_input_and_explain_it_is_not_secret(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_RUNBOOK])

        self.assertIn("MemberNoBase64Utf8", combined)
        self.assertIn("submitted_member_no_base64_utf8", combined)
        self.assertRegex(combined, r"(?i)not raw `MemberNo`")
        self.assertRegex(combined, r"(?i)Base64 is not encryption")
        self.assertRegex(combined, r"(?i)not secret")
        self.assertRegex(combined, r"(?i)interpolation risk|quoting")
        self.assertRegex(combined, r"\^\[A-Za-z0-9\+/]\+\=\{0,2\}\$")

    def test_docs_require_password_as_local_runtime_secret_not_queue_payload(self):
        combined = self.combined([NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertIn("AC2_PROBE_PASSWORD", combined)
        self.assertRegex(combined, r"(?i)runtime-only local configuration|local environment secret")
        self.assertRegex(combined, r"(?i)Do not add real server, database, user, password")
        self.assertRegex(combined, r"(?i)must not be passed as a command argument|Do not pass it in command arguments")

    def test_docs_define_node_by_node_queue_workflow_outline(self):
        workflow = self.read(WORKFLOW_DOC)

        for phrase in [
            "Google Sheets new-row trigger or poller",
            "UTF-8 base64 encode step",
            "Base64 allowlist step",
            "Queue request step",
            "Wait/poll result step",
            "JSON/schema guard",
            "Route decision step",
        ]:
            self.assertIn(phrase, workflow)

    def test_queue_contract_defines_allowed_fields_for_request_response_and_states(self):
        contract = self.read(NODE_CONTRACT)

        for field in [
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
            "normalized_member_no_length",
            "member_exists",
            "manual_review_required",
            "dry_run_only",
            "final_write_automation",
        ]:
            self.assertIn(field, contract)

        for state in [
            "PENDING_LOOKUP",
            "LOOKUP_IN_PROGRESS",
            "LOOKUP_ERROR_REVIEW",
            "MANUAL_REVIEW_REQUIRED",
            "EXISTING_MEMBER_REVIEW",
            "READY_FOR_CREATE_REVIEW",
        ]:
            self.assertIn(state, contract)

        self.assertRegex(contract, r"(?i)Retry exhaustion routes to `LOOKUP_ERROR_REVIEW`")
        self.assertRegex(contract, r"(?i)Same idempotency key plus same payload hash")

    def test_docs_route_sanitized_json_outcomes(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, BRIDGE_RUNBOOK])

        expected_routes = {
            "LOOKUP_ERROR_REVIEW": [
                "process failure",
                "invalid JSON",
                "missing fields",
                "status != ok",
                "unexpected shape",
            ],
            "MANUAL_REVIEW_REQUIRED": ["manual_review_required = true"],
            "EXISTING_MEMBER_REVIEW": ["member_exists = true"],
            "READY_FOR_CREATE_REVIEW": ["member_exists = false", "warning_count = 0"],
        }
        for review_code, phrases in expected_routes.items():
            self.assertIn(review_code, combined)
            for phrase in phrases:
                self.assertIn(phrase, combined)

    def test_ready_for_create_review_is_not_final_create_approval(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, DECISION_RUNBOOK, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)READY_FOR_CREATE_REVIEW.*not approval to create")
        self.assertRegex(combined, r"(?i)separate PR")
        self.assertRegex(combined, r"(?i)explicit business approval")
        self.assertRegex(combined, r"(?i)idempotency")
        self.assertRegex(combined, r"(?i)consent/audit")
        self.assertRegex(combined, r"(?i)write guardrails")

    def test_docs_state_no_autocount_writes_and_no_final_write_automation(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, DECISION_RUNBOOK, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)No AutoCount writes|AutoCount member writes")
        self.assertRegex(combined, r"(?i)not final write automation|does not authorize final write automation")
        self.assertRegex(combined, r"(?i)does not add an n8n workflow export|not a workflow export")
        self.assertRegex(combined, r"(?i)final write automation remains blocked|Real create/update automation remains blocked")

    def test_dry_run_docs_have_no_forbidden_write_api_tokens_or_sql_examples(self):
        combined = self.combined(DRY_RUN_DOCS)

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, combined, token)
        self.assertNotRegex(
            combined,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )

    def test_docs_avoid_logging_raw_inputs_encoded_values_commands_and_stderr(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, BRIDGE_RUNBOOK])

        for phrase in [
            "raw form values",
            "encoded values",
            "full command arguments",
            "credentials",
            "unsanitized stderr",
        ]:
            self.assertRegex(combined, re.escape(phrase), phrase)

    def test_template_is_intentionally_skipped(self):
        workflow = self.read(WORKFLOW_DOC)

        self.assertFalse(TEMPLATE_PATH.exists())
        self.assertIn("No n8n workflow template is included in this PR.", workflow)
        self.assertRegex(workflow, r"(?i)importable workflow artifact would create unnecessary activation risk")

    def test_uat_plan_documents_n8n_skills_and_official_references_checked(self):
        plan = self.read(UAT_PLAN)

        for skill in [
            "n8n-skills:using-n8n-skills",
            "n8n-skills:n8n-workflow-lifecycle",
            "n8n-skills:n8n-node-configuration",
            "n8n-skills:n8n-credentials-and-security",
            "n8n-skills:n8n-data-tables",
            "n8n-skills:n8n-error-handling",
            "n8n-skills:n8n-loops",
            "n8n-skills:n8n-expressions",
        ]:
            self.assertIn(skill, plan)

        for reference in [
            "Official n8n Google Sheets Trigger docs",
            "Official n8n Google Sheets node docs",
            "Official n8n Data Table docs",
            "Official n8n Wait node docs",
            "Official n8n Schedule Trigger docs",
            "Official n8n execution data and redaction docs",
            "n8n-skills plugin and official n8n documentation are the current planning source",
            "`n8n-skills` live tooling reference",
        ]:
            self.assertIn(reference, plan)

        self.assertRegex(plan, r"(?i)n8n-skills-backed")
        self.assertRegex(plan, r"(?i)not a blocker for this UAT-only plan")
        self.assertRegex(plan, r"(?i)future live-instance verification")
        self.assertRegex(plan, r"(?i)exact live n8n node parameter shapes")
        live_tooling_label = "M" + "CP"
        self.assertNotRegex(plan, rf"(?i)n8n {live_tooling_label}-backed")
        self.assertNotRegex(plan, rf"(?i)n8n {live_tooling_label} / skills")
        self.assertIn("get_node_types", plan)
        self.assertIn("validate_workflow", plan)
        self.assertIn("get_workflow_details", plan)

    def test_uat_plan_compares_queue_options_and_recommends_sheets_uat_only(self):
        plan = self.read(UAT_PLAN)

        for option in [
            "Google Sheets queue tab",
            "n8n Data Table / internal storage",
            "Lightweight external queue/API",
            "Local file drop only for fixture mode",
        ]:
            self.assertIn(option, plan)

        self.assertRegex(plan, r"(?i)Recommended UAT approach: use a Google Sheets queue tab")
        self.assertRegex(plan, r"(?i)explicitly UAT-only")
        self.assertRegex(plan, r"(?i)disabled or replaced before production activation")
        self.assertRegex(plan, r"(?i)Candidate for a later controlled UAT")

    def test_uat_plan_defines_exact_node_level_polling_workflow_shape(self):
        plan = self.read(UAT_PLAN)

        for phrase in [
            "Workflow A: Queue Lookup Jobs",
            "Schedule Trigger",
            "Google Sheets",
            "Read UAT Rows Marked For Lookup",
            "Apply Intake And PDPA Guards",
            "Build Allowed Queue Job",
            "Validate Encoded Member Value",
            "Append UAT Queue Job",
            "Workflow B: Apply Sanitized Results",
            "Read Bridge Results Ready For n8n",
            "Validate Result Schema",
            "Map Result To Review State",
            "Update Form Review Fields",
            "Workflow C: Timeout And Retry Sweep",
            "Sweep UAT Lookup Timeouts",
        ]:
            self.assertIn(phrase, plan)

        self.assertRegex(plan, r"(?i)Do not use the Wait node")
        self.assertRegex(plan, r"(?i)scheduled result polling")
        self.assertRegex(plan, r"(?i)no execution waits with raw form context")

    def test_uat_plan_defines_allowed_queue_result_and_reviewer_fields(self):
        plan = self.read(UAT_PLAN)

        for field in [
            "job_id",
            "row_number",
            "intake_id",
            "state",
            "submitted_member_no_base64_utf8",
            "payload_hash",
            "attempt",
            "max_attempts",
            "lease_owner",
            "lease_expires_at",
            "timeout_at",
            "normalized_member_no_length",
            "member_exists",
            "manual_review_required",
            "warning_count",
            "dry_run_only",
            "final_write_automation",
            "uat_lookup_job_id",
            "reviewer_status",
            "reviewer_decision_code",
        ]:
            self.assertIn(field, plan)

        for state in [
            "PENDING_LOOKUP",
            "LOOKUP_IN_PROGRESS",
            "LOOKUP_ERROR_REVIEW",
            "MANUAL_REVIEW_REQUIRED",
            "EXISTING_MEMBER_REVIEW",
            "READY_FOR_CREATE_REVIEW",
        ]:
            self.assertIn(state, plan)

        self.assertRegex(plan, r"(?i)Free-text reviewer notes are out of scope")
        self.assertRegex(plan, r"(?i)No state authorizes member creation")

    def test_uat_plan_forbids_pii_secrets_sheet_ids_urls_and_direct_write_paths(self):
        plan = self.read(UAT_PLAN)

        for phrase in [
            "raw member numbers",
            "normalized member numbers",
            "names",
            "emails",
            "raw phone numbers",
            "Sheet URLs",
            "Sheet IDs",
            "credentials",
            "connection strings",
            "stderr/stdout",
            "PII",
        ]:
            self.assertRegex(plan, re.escape(phrase), phrase)

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, plan, token)
        self.assertNotRegex(
            plan,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )
        self.assertNotRegex(plan, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(plan, r"(?i)(password|secret|token)\s*[:=]\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(plan, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertRegex(plan, r"(?i)No production activation is allowed")
        self.assertRegex(plan, r"(?i)Real create/update automation remains blocked")


if __name__ == "__main__":
    unittest.main()
