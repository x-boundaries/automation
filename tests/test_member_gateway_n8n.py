import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows/member_forms_gateway_ingest.workflow.json"


class MemberGatewayN8nTests(unittest.TestCase):
    def test_export_is_inactive_credential_free_and_placeholder_only(self):
        raw = WORKFLOW.read_text(encoding="utf-8")
        workflow = json.loads(raw)
        self.assertFalse(workflow["active"])
        self.assertIsNone(workflow["staticData"])
        self.assertEqual(workflow["pinData"], {})
        self.assertNotIn("webhookId", workflow)

        def walk(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        objects = list(walk(workflow))
        self.assertFalse(any("credentials" in item for item in objects))
        self.assertFalse(any("authentication" in item for item in objects))
        self.assertIn("GOOGLE_FORM_ID_PLACEHOLDER", raw)
        self.assertIn("activation_enabled", raw)
        self.assertNotIn("81234567", raw)
        self.assertNotIn("@gmail", raw)
        self.assertNotIn("Alice", raw)

    def test_workflow_has_manual_trigger_and_false_activation_branch(self):
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        node_names = {node["name"] for node in workflow["nodes"]}
        trigger_types = {
            node["type"]
            for node in workflow["nodes"]
            if node["type"].endswith("Trigger")
        }
        self.assertEqual(trigger_types, {"n8n-nodes-base.manualTrigger"})
        self.assertIn("Controlled activation gate", node_names)
        self.assertIn("Blocked until separate activation", node_names)
        self.assertEqual(
            workflow["connections"]["Controlled activation gate"]["main"][1][0]["node"],
            "Blocked until separate activation",
        )

    def test_connections_target_declared_nodes(self):
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        declared = {node["name"] for node in workflow["nodes"]}
        for branches in workflow["connections"].values():
            for branch in branches.get("main", []):
                for edge in branch:
                    self.assertIn(edge["node"], declared)


if __name__ == "__main__":
    unittest.main()
