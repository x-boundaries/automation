import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows/member_forms_gateway_ingest.workflow.json"
FIXTURE = ROOT / "tests/fixtures/member_forms_google_v1.fixture"

NODE_RUNNER = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const localRequire = name => name === 'crypto' ? require('crypto') : require(name);
  const run = new Function('$input', '$json', 'require', payload.code);
  const items = payload.items.map(json => ({ json }));
  const result = run({ all: () => items }, items[0] && items[0].json, localRequire);
  process.stdout.write(JSON.stringify({ ok: true, result }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: String(error && error.message ? error.message : error)
  }));
}
"""


@unittest.skipUnless(shutil.which("node"), "node executable unavailable")
class MemberGatewayN8nTests(unittest.TestCase):
    def setUp(self):
        self.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        self.fixtures = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.mapper = next(
            node for node in self.workflow["nodes"] if node["name"] == "Canonicalize source response"
        )
        self.forms_http = next(
            node for node in self.workflow["nodes"]
            if node["name"] == "Google Forms API page (configured outside repo)"
        )

    def run_mapper(self, name):
        pages = self.fixtures[name]["pages"]
        payload = {
            "code": self.mapper["parameters"]["jsCode"],
            # fullResponse=true is represented by n8n as {body: <Forms page>}.
            "items": [{"body": page} for page in pages],
        }
        proc = subprocess.run(
            ["node", "-e", NODE_RUNNER],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def assert_mapper_error(self, name, code):
        result = self.run_mapper(name)
        self.assertFalse(result["ok"])
        self.assertIn(code, result["error"])

    def test_export_is_inactive_credential_free_and_placeholder_only(self):
        raw = WORKFLOW.read_text(encoding="utf-8")
        self.assertFalse(self.workflow["active"])
        self.assertIsNone(self.workflow["staticData"])
        self.assertEqual(self.workflow["pinData"], {})
        self.assertNotIn("webhookId", self.workflow)

        def walk(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        objects = list(walk(self.workflow))
        self.assertFalse(any("credentials" in item for item in objects))
        self.assertFalse(any("authentication" in item for item in objects))
        self.assertIn("GOOGLE_FORM_ID_PLACEHOLDER", raw)
        self.assertIn("activation_enabled", raw)
        self.assertNotIn("81234567", raw)
        self.assertNotIn("@gmail", raw)
        self.assertNotIn("Alice", raw)

    def test_workflow_has_manual_trigger_and_false_activation_branch(self):
        node_names = {node["name"] for node in self.workflow["nodes"]}
        trigger_types = {
            node["type"]
            for node in self.workflow["nodes"]
            if node["type"].endswith("Trigger")
        }
        self.assertEqual(trigger_types, {"n8n-nodes-base.manualTrigger"})
        self.assertIn("Controlled activation gate", node_names)
        self.assertIn("Blocked until separate activation", node_names)
        self.assertEqual(
            self.workflow["connections"]["Controlled activation gate"]["main"][1][0]["node"],
            "Blocked until separate activation",
        )

    def test_connections_target_declared_nodes(self):
        declared = {node["name"] for node in self.workflow["nodes"]}
        for branches in self.workflow["connections"].values():
            for branch in branches.get("main", []):
                for edge in branch:
                    self.assertIn(edge["node"], declared)

    def test_forms_http_node_uses_real_bounded_page_pagination(self):
        options = self.forms_http["parameters"]["options"]
        response = options["response"]["response"]
        pagination = options["pagination"]["pagination"]
        parameter = pagination["parameters"]["parameters"][0]
        self.assertTrue(response["fullResponse"])
        self.assertEqual(response["responseFormat"], "json")
        self.assertEqual(pagination["paginationMode"], "updateAParameterInEachRequest")
        self.assertEqual(parameter["name"], "pageToken")
        self.assertEqual(parameter["type"], "qs")
        self.assertIn("$response.body.nextPageToken", parameter["value"])
        self.assertTrue(pagination["limitPagesFetched"])
        self.assertEqual(pagination["maxRequests"], 50)
        self.assertIn("completeExpression", pagination)
        self.assertIn("$response.body.nextPageToken", pagination["completeExpression"])

    def test_one_page_preserves_forms_identity_and_hash_contract(self):
        result = self.run_mapper("one_page")
        self.assertTrue(result["ok"], result)
        events = result["result"]
        self.assertEqual(len(events), 1)
        event = events[0]["json"]
        self.assertEqual(event["response_id"], "forms-resp-one-001")
        self.assertEqual(event["create_time"], "2026-08-30T01:02:03.000Z")
        self.assertNotIn("lastSubmittedTime", event)
        self.assertEqual(event["payload"]["phone"], "6581234567")
        self.assertEqual(event["payload"]["email"], "alpha@example.test")
        canonical = json.dumps(
            event["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(
            event["payload_hash"],
            "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def test_multiple_pages_follow_next_token_and_emit_both_events(self):
        result = self.run_mapper("multiple_pages")
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            {item["json"]["response_id"] for item in result["result"]},
            {"forms-resp-page-001", "forms-resp-page-002"},
        )

    def test_overlapping_response_ids_are_idempotently_deduplicated(self):
        result = self.run_mapper("overlap")
        self.assertTrue(result["ok"], result)
        events = result["result"]
        self.assertEqual(
            [item["json"]["response_id"] for item in events],
            ["forms-resp-overlap-001", "forms-resp-overlap-002"],
        )

    def test_repeated_and_malformed_page_tokens_fail_closed(self):
        self.assert_mapper_error("repeated_token", "forms_page_token_repeated")
        self.assert_mapper_error("malformed_token", "forms_page_token_invalid")

    def test_missing_and_unknown_question_mappings_fail_closed(self):
        self.assert_mapper_error("missing_mapping", "forms_question_mapping_missing")
        self.assert_mapper_error("unknown_mapping", "forms_question_mapping_unknown")

    def test_mapper_uses_allowlisted_answers_not_flat_or_text_similarity_fields(self):
        code = self.mapper["parameters"]["jsCode"]
        self.assertIn("QUESTION_ID_NAME_PLACEHOLDER", code)
        self.assertIn("answers", code)
        self.assertIn("responseId", code)
        self.assertIn("createTime", code)
        self.assertIn("nextPageToken", code)
        self.assertIn("forms_response_id_conflict", code)
        self.assertNotIn("source.phone", code)
        self.assertNotIn("lastSubmittedTime", code)


if __name__ == "__main__":
    unittest.main()
