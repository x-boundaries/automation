import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "autocount2-automation"
README = ROOT / "README.md"
WORKFLOW_DOC = DOCS / "member_intake_n8n_dry_run_workflow.md"
NODE_CONTRACT = DOCS / "member_intake_n8n_node_contract.md"
DIRECT_RUNBOOK = DOCS / "member_intake_n8n_direct_lookup_runbook.md"
LOOKUP_RUNBOOK = DOCS / "member_lookup_review_runbook.md"
BRIDGE_DESIGN = DOCS / "member_intake_local_bridge_design.md"
DECISION_RUNBOOK = DOCS / "member_intake_decision_review_runbook.md"
TEMPLATE_PATH = DOCS / "templates" / "member_intake_n8n_dry_run_lookup.reference.json"


DRY_RUN_DOCS = [WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK]
FORBIDDEN_WRITE_TOKENS = [
    "SaveMember",
    "NewMember",
    "DeleteMember",
    "GetNextMemberNo",
]


class MemberIntakeN8nDryRunDocsTests(unittest.TestCase):
    def read(self, path):
        return path.read_text(encoding="utf-8")

    def combined(self, paths):
        return "\n".join(self.read(path) for path in paths)

    def test_new_docs_exist_and_are_linked_from_readme(self):
        readme = self.read(README)

        for path in [WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK]:
            self.assertTrue(path.exists(), path)
            self.assertIn(path.name, readme)

    def test_docs_state_local_self_hosted_boundary_and_cloud_bridge_requirement(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_DESIGN])

        self.assertRegex(combined, r"(?i)local self-hosted n8n|self-hosted local n8n")
        self.assertRegex(combined, r"(?i)Windows AutoCount host|locked-down Windows host")
        self.assertRegex(combined, r"(?i)Cloud n8n cannot directly execute local AC2 PowerShell")
        self.assertRegex(combined, r"(?i)separately approved local bridge|private route|VPN")
        self.assertNotRegex(combined, r"(?i)cloud n8n can directly call local AC2 PowerShell")

    def test_docs_require_base64_member_input_and_explain_it_is_not_secret(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK])

        self.assertIn("MemberNoBase64Utf8", combined)
        self.assertRegex(combined, r"(?i)not raw `MemberNo`")
        self.assertRegex(combined, r"(?i)Base64 is not encryption")
        self.assertRegex(combined, r"(?i)not secret")
        self.assertRegex(combined, r"(?i)shell interpolation risk|quoting")
        self.assertRegex(combined, r"\^\[A-Za-z0-9\+/]\+\=\{0,2\}\$")

    def test_docs_require_password_as_local_environment_secret_not_argument(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_DESIGN])

        self.assertIn("AC2_PROBE_PASSWORD", combined)
        self.assertRegex(combined, r"(?i)AC2_PROBE_PASSWORD.*local environment secret")
        self.assertRegex(combined, r"(?i)must not be passed as a command argument|Do not pass it in command arguments")

    def test_docs_define_node_by_node_workflow_outline(self):
        workflow = self.read(WORKFLOW_DOC)

        for phrase in [
            "Google Sheets new-row trigger or poller",
            "UTF-8 base64 encode step",
            "Base64 allowlist step",
            "Local PowerShell lookup step",
            "JSON parse guard",
            "Route decision step",
        ]:
            self.assertIn(phrase, workflow)

    def test_docs_route_sanitized_json_outcomes(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK])

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
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, DECISION_RUNBOOK, BRIDGE_DESIGN])

        self.assertRegex(combined, r"(?i)READY_FOR_CREATE_REVIEW.*not approval to create")
        self.assertRegex(combined, r"(?i)separate PR")
        self.assertRegex(combined, r"(?i)explicit business approval")
        self.assertRegex(combined, r"(?i)idempotency")
        self.assertRegex(combined, r"(?i)consent/audit")
        self.assertRegex(combined, r"(?i)write guardrails")

    def test_docs_state_no_autocount_writes_and_no_final_write_automation(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, DECISION_RUNBOOK, BRIDGE_DESIGN])

        self.assertRegex(combined, r"(?i)No AutoCount writes|AutoCount member writes")
        self.assertRegex(combined, r"(?i)not final write automation|does not authorize final write automation")
        self.assertRegex(combined, r"(?i)does not add an n8n workflow export|does not create a production n8n workflow")
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
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK])

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


if __name__ == "__main__":
    unittest.main()
