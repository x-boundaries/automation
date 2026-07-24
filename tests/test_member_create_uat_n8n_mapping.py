import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows" / "member_create_uat_result_mapping.workflow.json"


class N8nMappingStaticTests(unittest.TestCase):
    """Static assertions (finding 5) proving operation_id is the single-use mapping
    key and the workflow stays inactive, credential-free, and mapping-only."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.wf = json.loads(cls.text)
        cls.nodes = {n["name"]: n for n in cls.wf["nodes"]}

    def test_inactive_and_credential_free(self):
        self.assertFalse(self.wf["active"])
        self.assertFalse(self.wf["meta"]["templateCredsSetupCompleted"])
        self.assertNotIn("webhookId", self.text)
        self.assertNotIn('"credentials"', self.text)

    def test_shared_yes_marker_is_gone(self):
        self.assertNotIn("MemberCreateUatApprovedForMapping", self.text)

    def test_read_node_filters_by_operation_id(self):
        read = self.nodes["Read Row By Operation Id"]
        flt = read["parameters"]["filtersUI"]["values"][0]
        self.assertEqual(flt["lookupColumn"], "uat_create_operation_id")
        self.assertIn("operation_id", flt["lookupValue"])

    def test_update_node_matches_on_operation_id(self):
        upd = self.nodes["Update Review Fields By Operation Id"]
        self.assertEqual(upd["parameters"]["columns"]["matchingColumns"], ["uat_create_operation_id"])
        self.assertEqual(upd["parameters"]["operation"], "update")
        self.assertEqual(upd["parameters"]["options"]["cellFormat"], "RAW")

    def test_no_forbidden_action_nodes(self):
        types = {n["type"] for n in self.wf["nodes"]}
        for forbidden in (
            "n8n-nodes-base.httpRequest",
            "n8n-nodes-base.executeCommand",
            "n8n-nodes-base.webhook",
            "n8n-nodes-base.scheduleTrigger",
            "@n8n/n8n-nodes-langchain.agent",
        ):
            self.assertNotIn(forbidden, types)

    def test_validate_node_recomputes_terminal_state(self):
        code = self.nodes["Validate Sanitized Terminal Result Strictly"]["parameters"]["jsCode"]
        # The canonical recompute-and-compare (finding 4) must be present.
        self.assertIn("recompute", code)
        self.assertIn("create_uat_terminal_code_does_not_match_recomputed", code)
        self.assertIn("create_uat_state_contradiction", code)
        # The ExpiryDate activation guards must be mirrored into the canonical table so
        # the n8n validator agrees with the Python and PowerShell contradiction checks.
        self.assertIn("expiry_date_not_assigned", code)
        self.assertIn("assigned_field_count_stale", code)
        self.assertIn("EXPECTED_ASSIGNED_FIELD_COUNT", code)

    def test_verify_node_rejects_blank_or_conflicting_operation_id(self):
        code = self.nodes["Verify Identity And Decide Mapping"]["parameters"]["jsCode"]
        self.assertIn("create_uat_row_operation_id_blank_or_malformed", code)
        self.assertIn("create_uat_row_operation_id_conflict", code)
        self.assertIn("create_uat_requires_exactly_one_row_for_operation_id", code)
        # Identity + fingerprint revalidation retained.
        self.assertIn("create_uat_source_record_id_mismatch", code)
        self.assertIn("create_uat_source_fingerprint_mismatch", code)

    def test_google_sheets_nodes_have_retry(self):
        for name in ("Read Row By Operation Id", "Update Review Fields By Operation Id"):
            self.assertTrue(self.nodes[name]["retryOnFail"])


if __name__ == "__main__":
    unittest.main()
