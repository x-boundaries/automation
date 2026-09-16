import hashlib
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows/member_forms_gateway_ingest.workflow.json"
FIXTURE = ROOT / "tests/fixtures/member_forms_google_v1.fixture"
PAGE_TOKEN_EXPRESSION = "={{ $json.resume_page_token || undefined }}"

NODE_RUNNER = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const localRequire = name => name === 'crypto' ? require('crypto') : require(name);
  const selector = name => ({first: () => ({json: name === 'Read durable source cursor' ? payload.cursor : {}})});
  const run = new Function('$input', '$json', 'require', '$', payload.code);
  const item = {body: payload.page};
  const result = run({all: () => [{json: item}]}, item, localRequire, selector);
  process.stdout.write(JSON.stringify({ok: true, result}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, error: String(error && error.message ? error.message : error)}));
}
"""

EXPRESSION_RUNNER = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const evaluate = new Function('$json', 'return (' + payload.expression + ');');
  const value = evaluate(payload.json);
  process.stdout.write(JSON.stringify({ok: true, type: typeof value, value: value === undefined ? null : value}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, error: String(error && error.message ? error.message : error)}));
}
"""


@unittest.skipUnless(shutil.which("node"), "node executable unavailable")
class MemberGatewayN8nTests(unittest.TestCase):
    def setUp(self):
        self.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        self.fixtures = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.mapper = next(node for node in self.workflow["nodes"] if node["name"] == "Canonicalize source page")
        self.forms_http = next(node for node in self.workflow["nodes"] if node["name"] == "Google Forms single page (configured outside repo)")

    def cursor(self, *, token=None, state_version=0, count=0):
        return {
            "schema_version": "xb.member.gateway.source_cursor.v1",
            "source_system": "google_forms", "form_alias": "member_registration",
            "mapping_version": "member-intake.v1", "watermark": "2026-08-30T00:00:00Z",
            "last_admitted_create_time": None, "last_admitted_response_id": None,
            "state_version": state_version, "scan_lower_bound": "2026-08-30T00:00:00Z",
            "resume_page_token": token, "initial_window_admission_count": count,
        }

    def run_page(self, name, index=0, *, cursor=None):
        payload = {
            "code": self.mapper["parameters"]["jsCode"],
            "page": self.fixtures[name]["pages"][index],
            "cursor": cursor or self.cursor(),
        }
        proc = subprocess.run(["node", "-e", NODE_RUNNER], input=json.dumps(payload), capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def assert_page_error(self, name, code, index=0, *, cursor=None):
        result = self.run_page(name, index, cursor=cursor)
        self.assertFalse(result["ok"])
        self.assertIn(code, result["error"])

    def test_export_is_inactive_credential_webhook_pin_and_static_data_free(self):
        raw = WORKFLOW.read_text(encoding="utf-8")
        self.assertFalse(self.workflow["active"])
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
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

    def test_manual_false_gate_and_durable_cursor_sequence(self):
        node_names = {node["name"] for node in self.workflow["nodes"]}
        triggers = {node["type"] for node in self.workflow["nodes"] if node["type"].endswith("Trigger")}
        self.assertEqual(triggers, {"n8n-nodes-base.manualTrigger"})
        for name in ("Read durable source cursor", "Google Forms single page (configured outside repo)", "Canonicalize source page", "Protected XB Gateway ingest (configured outside repo)", "Commit durable page checkpoint"):
            self.assertIn(name, node_names)
        self.assertEqual(self.workflow["connections"]["Controlled activation gate"]["main"][1][0]["node"], "Blocked until separate activation")
        self.assertEqual(self.workflow["connections"]["Protected XB Gateway ingest (configured outside repo)"]["main"][0][0]["node"], "Commit durable page checkpoint")
        checkpoint = next(node for node in self.workflow["nodes"] if node["name"] == "Commit durable page checkpoint")
        self.assertIn("$json.replayed", checkpoint["parameters"]["jsonBody"])

    def test_connections_target_declared_nodes(self):
        declared = {node["name"] for node in self.workflow["nodes"]}
        for branches in self.workflow["connections"].values():
            for branch in branches.get("main", []):
                for edge in branch:
                    self.assertIn(edge["node"], declared)

    def test_forms_query_is_inclusive_single_response_resume(self):
        parameters = self.forms_http["parameters"]["queryParameters"]["parameters"]
        by_name = {item["name"]: item["value"] for item in parameters}
        self.assertEqual(by_name["pageSize"], "1")
        self.assertIn("timestamp >=", by_name["filter"])
        self.assertIn("scan_lower_bound", by_name["filter"])
        self.assertIn("resume_page_token", by_name["pageToken"])
        self.assertNotIn("pagination", self.forms_http["parameters"]["options"])

    def test_one_page_preserves_identity_hash_and_checkpoint(self):
        result = self.run_page("one_page")
        self.assertTrue(result["ok"], result)
        item = result["result"][0]["json"]
        event = item["source_event"]
        self.assertEqual(event["response_id"], "forms-resp-one-001")
        self.assertEqual(event["create_time"], "2026-08-30T01:02:03.000Z")
        canonical = json.dumps(event["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(event["payload_hash"], "sha256:" + hashlib.sha256(canonical.encode()).hexdigest())
        self.assertEqual(item["page_checkpoint"]["responses"], [{"response_id": event["response_id"], "create_time": event["create_time"], "payload_hash": event["payload_hash"]}])
        self.assertTrue(item["page_checkpoint"]["terminal"])

    def test_multi_run_resume_uses_current_and_next_tokens(self):
        first = self.run_page("multiple_pages", 0)
        self.assertTrue(first["ok"], first)
        first_item = first["result"][0]["json"]
        self.assertEqual(first_item["page_checkpoint"]["next_page_token"], "page-token-002")
        second_cursor = self.cursor(token="page-token-002", state_version=2)
        second = self.run_page("multiple_pages", 1, cursor=second_cursor)
        self.assertTrue(second["ok"], second)
        second_item = second["result"][0]["json"]
        self.assertEqual(second_item["page_checkpoint"]["current_page_token"], "page-token-002")
        self.assertTrue(second_item["page_checkpoint"]["terminal"])

    def test_repeated_and_malformed_tokens_fail_closed_but_initial_count_does_not_deadlock(self):
        self.assert_page_error("repeated_token", "forms_page_token_repeated", 1, cursor=self.cursor(token="repeat-token"))
        self.assert_page_error("malformed_token", "forms_page_token_invalid")
        result = self.run_page("one_page", cursor=self.cursor(count=1))
        self.assertTrue(result["ok"], result)

    def test_mapping_is_closed_and_real_shape_only(self):
        self.assert_page_error("missing_mapping", "forms_answer_value_invalid")
        self.assert_page_error("unknown_mapping", "forms_question_mapping_unknown")
        code = self.mapper["parameters"]["jsCode"]
        for text in ("QUESTION_ID_NAME_PLACEHOLDER", "answers", "responseId", "createTime", "nextPageToken", "forms_response_before_watermark"):
            self.assertIn(text, code)
        self.assertNotIn("source.phone", code)
        self.assertNotIn("lastSubmittedTime", code)

    def test_page_token_expression_resolves_first_page_and_continuation(self):
        page_token = next(item for item in self.forms_http["parameters"]["queryParameters"]["parameters"] if item["name"] == "pageToken")
        self.assertEqual(page_token["value"], PAGE_TOKEN_EXPRESSION)
        expression = page_token["value"][len("={{"):-len("}}")]
        for token, expected_type, expected_value in ((None, "undefined", None), ("", "undefined", None), ("page-token-002", "string", "page-token-002")):
            with self.subTest(token=token):
                payload = {"expression": expression, "json": {"resume_page_token": token}}
                proc = subprocess.run(["node", "-e", EXPRESSION_RUNNER], input=json.dumps(payload), capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["type"], expected_type)
                self.assertEqual(result["value"], expected_value)


class MemberGatewayN8nCanonicalContractTests(unittest.TestCase):
    """Canonical timeout, retry, MCP-exposure, description, and fail-closed regressions. No node runtime required."""

    HTTP_NODES = (
        "Read durable source cursor",
        "Google Forms single page (configured outside repo)",
        "Protected XB Gateway ingest (configured outside repo)",
        "Commit durable page checkpoint",
    )
    RETRY_POLICY = {
        "Read durable source cursor": (3, 1000),
        "Google Forms single page (configured outside repo)": (3, 2000),
        "Protected XB Gateway ingest (configured outside repo)": (2, 2000),
    }
    RETRY_FIELDS = ("retryOnFail", "maxTries", "waitBetweenTries")
    DOMAIN_PATTERN = re.compile(r"\b[a-z0-9-]+\.(?:com|sg|net|org|io|dev|app|googleapis)\b", re.IGNORECASE)
    OPAQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{25,}")

    def setUp(self):
        self.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        self.nodes = {node["name"]: node for node in self.workflow["nodes"]}

    def test_every_http_node_declares_an_explicit_thirty_second_timeout(self):
        http_names = {node["name"] for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"}
        self.assertEqual(http_names, set(self.HTTP_NODES))
        for name in self.HTTP_NODES:
            with self.subTest(node=name):
                options = self.nodes[name]["parameters"]["options"]
                self.assertEqual(options["timeout"], 30000)
                self.assertIn("response", options)

    def test_bounded_retry_only_on_reads_and_idempotent_ingest(self):
        for name, (max_tries, wait) in self.RETRY_POLICY.items():
            with self.subTest(node=name):
                node = self.nodes[name]
                self.assertIs(node["retryOnFail"], True)
                self.assertEqual(node["maxTries"], max_tries)
                self.assertEqual(node["waitBetweenTries"], wait)

    def test_cas_page_checkpoint_declares_no_automatic_retry(self):
        checkpoint = self.nodes["Commit durable page checkpoint"]
        for field in self.RETRY_FIELDS:
            self.assertNotIn(field, checkpoint)

    def test_settings_disable_mcp_exposure_and_preserve_execution_policy(self):
        self.assertEqual(self.workflow["settings"], {
            "executionOrder": "v1",
            "saveManualExecutions": False,
            "saveDataErrorExecution": "none",
            "saveDataSuccessExecution": "none",
            "availableInMCP": False,
        })

    def test_description_states_do_not_activate_without_leaking_live_identifiers(self):
        description = self.workflow["description"]
        self.assertTrue(description.strip())
        self.assertIn("DO NOT ACTIVATE", description)
        self.assertNotIn("http", description.lower())
        self.assertNotIn("@", description)
        self.assertEqual(self.DOMAIN_PATTERN.findall(description), [])
        self.assertEqual(self.OPAQUE_ID_PATTERN.findall(description), [])
        for marker in ("bearer", "api_key", "apikey", "password", "private key", "token="):
            self.assertNotIn(marker, description.lower())

    def test_page_token_parameter_is_declared_exactly_once_and_unchanged(self):
        forms = self.nodes["Google Forms single page (configured outside repo)"]
        parameters = forms["parameters"]["queryParameters"]["parameters"]
        page_tokens = [item for item in parameters if item["name"] == "pageToken"]
        self.assertEqual(len(page_tokens), 1)
        self.assertEqual(page_tokens[0]["value"], PAGE_TOKEN_EXPRESSION)
        self.assertNotIn("jsonQuery", forms["parameters"])
        self.assertNotIn("pagination", forms["parameters"]["options"])

    def test_inactive_manual_only_posture_carries_no_webhook_or_credential_binding(self):
        self.assertIs(self.workflow["active"], False)
        triggers = {node["type"] for node in self.workflow["nodes"] if node["type"].endswith("Trigger")}
        self.assertEqual(triggers, {"n8n-nodes-base.manualTrigger"})
        activation = next(item for item in self.nodes["Repository-safe source configuration"]["parameters"]["assignments"]["assignments"] if item["name"] == "activation_enabled")
        self.assertIs(activation["value"], False)

        def walk(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        for item in walk(self.workflow):
            self.assertNotIn("webhookId", item)
            self.assertNotIn("credentials", item)
            self.assertNotIn("authentication", item)
        raw = WORKFLOW.read_text(encoding="utf-8")
        for placeholder in ("GOOGLE_FORM_ID_PLACEHOLDER", "QUESTION_ID_NAME_PLACEHOLDER", "QUESTION_ID_PHONE_PLACEHOLDER"):
            self.assertIn(placeholder, raw)

    def test_blocked_activation_branch_reaches_no_network_node(self):
        blocked = self.workflow["connections"]["Controlled activation gate"]["main"][1]
        self.assertEqual([edge["node"] for edge in blocked], ["Blocked until separate activation"])
        reachable = set()
        frontier = [edge["node"] for edge in blocked]
        while frontier:
            name = frontier.pop()
            if name in reachable:
                continue
            reachable.add(name)
            for branch in self.workflow["connections"].get(name, {}).get("main", []):
                frontier.extend(edge["node"] for edge in branch)
        for name in reachable:
            node = self.nodes[name]
            self.assertNotEqual(node["type"], "n8n-nodes-base.httpRequest")
            self.assertNotIn("url", node["parameters"])

    def test_ineligible_page_checkpoints_without_gateway_ingest(self):
        eligible = self.workflow["connections"]["Eligible source response present"]["main"]
        self.assertEqual([edge["node"] for edge in eligible[0]], ["Protected XB Gateway ingest (configured outside repo)"])
        self.assertEqual([edge["node"] for edge in eligible[1]], ["Commit durable page checkpoint"])
        self.assertEqual([edge["node"] for edge in self.workflow["connections"]["Protected XB Gateway ingest (configured outside repo)"]["main"][0]], ["Commit durable page checkpoint"])


if __name__ == "__main__":
    unittest.main()
