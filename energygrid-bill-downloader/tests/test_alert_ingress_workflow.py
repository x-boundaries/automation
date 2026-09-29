"""Offline contract of the inactive EnergyGrid alert-ingress n8n export (DL-XB-199 G3-101).

No n8n instance is contacted. The committed Code node is executed with the
local Node runtime against payloads built by the real notifier.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from energygrid_bill_downloader.notify import ALERT_KEYS, build_alert_payload


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / "n8n-workflows" / "energygrid_alert_ingress.workflow.json"
NODE_CHAIN = ["EnergyGrid Alert Webhook", "Validate Alert Payload", "Send EnergyGrid Alert"]
ALLOWED_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.code",
    "n8n-nodes-base.telegram",
    "n8n-nodes-base.stickyNote",
}
RUN_ID = "0f8e7d6c-5b4a-4392-8a1b-0c9d8e7f6a5b"

HARNESS = r"""
const fs = require('fs');
const [workflowPath, casesPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const code = wf.nodes.find((n) => n.name === 'Validate Alert Payload').parameters.jsCode;
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const results = cases.map((body) => {
  const $input = { all: () => [{ json: { body, headers: {} } }] };
  try {
    const out = new Function('$input', code)($input);
    return { ok: true, out };
  } catch (error) {
    return { ok: false, error: String(error.message) };
  }
});
process.stdout.write(JSON.stringify(results));
"""


def valid_payload(**overrides):
    payload = build_alert_payload(
        run_id=RUN_ID,
        stage="list",
        status="PORTAL_LAYOUT_CHANGED",
        support_ref="EG_HTTP_LIST_TENANT_MISMATCH",
        exit_code=20,
        counts={"inventory": 4, "downloaded": 0, "present": 0, "failure": 0},
    )
    payload.update(overrides)
    return payload


class AlertIngressExportShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))

    def by_name(self, name: str) -> dict:
        return next(node for node in self.workflow["nodes"] if node["name"] == name)

    def test_inactive_mcp_hidden_and_free_of_live_identity(self) -> None:
        self.assertIs(False, self.workflow["active"])
        self.assertIs(False, self.workflow["settings"]["availableInMCP"])
        self.assertIsNone(self.workflow["staticData"])
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES)
        self.assertNotIn("pinData", self.workflow)

    def test_chain_and_placeholders(self) -> None:
        names = [node["name"] for node in self.workflow["nodes"] if node["type"] != "n8n-nodes-base.stickyNote"]
        self.assertEqual(NODE_CHAIN, names)
        edges = [
            (source, link["node"])
            for source, outputs in self.workflow["connections"].items()
            for branch in outputs["main"]
            for link in branch
        ]
        self.assertEqual(list(zip(NODE_CHAIN, NODE_CHAIN[1:])), edges)
        webhook = self.by_name("EnergyGrid Alert Webhook")["parameters"]
        self.assertEqual("POST", webhook["httpMethod"])
        self.assertEqual("REPLACE_WITH_ALERT_WEBHOOK_PATH", webhook["path"])
        self.assertEqual("headerAuth", webhook["authentication"])
        # Delivery failure must be visible to the runtime as a non-2xx answer.
        self.assertEqual("lastNode", webhook["responseMode"])
        telegram = self.by_name("Send EnergyGrid Alert")["parameters"]
        self.assertEqual("REPLACE_WITH_TELEGRAM_CHAT_ID", telegram["chatId"])
        self.assertEqual("={{ $json.alert_text }}", telegram["text"])
        self.assertNotIn("parse_mode", telegram["additionalFields"], "plain text: no markup injection surface")
        telegram_node = self.by_name("Send EnergyGrid Alert")
        for key in ("onError", "continueOnFail", "alwaysOutputData", "retryOnFail"):
            self.assertNotIn(key, telegram_node)

    def test_code_allowlist_matches_the_notifier_contract(self) -> None:
        code = self.by_name("Validate Alert Payload")["parameters"]["jsCode"]
        for key in ALERT_KEYS:
            self.assertIn(f"'{key}'", code)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code node")
class AlertIngressCodeExecution(unittest.TestCase):
    def run_cases(self, cases: list) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            completed = subprocess.run(
                [shutil.which("node"), str(harness), str(WORKFLOW), str(cases_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_a_real_notifier_payload_renders_only_validated_fields(self) -> None:
        (result,) = self.run_cases([valid_payload()])
        self.assertTrue(result["ok"], result)
        text = result["out"][0]["json"]["alert_text"]
        self.assertEqual({"alert_text"}, set(result["out"][0]["json"]))
        self.assertIn("EG_HTTP_LIST_TENANT_MISMATCH", text)
        self.assertIn("attention required", text)
        self.assertIn(RUN_ID, text)

    def test_every_departure_from_the_contract_is_rejected(self) -> None:
        base = valid_payload()
        cases = [
            {**base, "tenant_id": "CANARY-TENANT"},
            {**base, "filename": "CANARYBILL.pdf"},
            {key: value for key, value in base.items() if key != "counts"},
            {**base, "support_ref": "lower-case <b>x</b>"},
            {**base, "stage": "CANARY"},
            {**base, "status": "OK"},
            {**base, "run_id": "not-a-uuid"},
            {**base, "exit_code": "20"},
            {**base, "counts": {**base["counts"], "extra": 1}},
            {**base, "counts": {**base["counts"], "inventory": -1}},
            {**base, "schema": "energygrid.alert.v2"},
            {**base, "attention_required": "yes"},
            {**base, "timestamp": "yesterday"},
            [],
            None,
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=str(case)[:60]):
                self.assertFalse(result["ok"], result)
                self.assertTrue(result["error"].startswith("EG_ALERT_"), result)


if __name__ == "__main__":
    unittest.main()
