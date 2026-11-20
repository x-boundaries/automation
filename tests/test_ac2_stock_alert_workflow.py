"""Offline contract of the inactive AC2 stock export error-alert n8n export (#245, #288 slice 2).

No n8n instance, Gmail account or VM is contacted. The committed Code node is executed with the
local Node runtime against events built by the real exporter (build_alert_payload), and its
output is checked against the committed Code node of the shared XB Ops Alert Email sub-workflow.
Every value here is synthetic.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_stock_balance_hourly as hourly  # noqa: E402

WORKFLOW = ROOT / "n8n-workflows" / "ac2_stock_alert.workflow.json"
SUB_WORKFLOW = ROOT / "n8n-workflows" / "ops_alert_email.workflow.json"
README = ROOT / "n8n-workflows" / "README.md"
CODE_NODE = "Validate Alert Event"
SUB_VALIDATE_NODE = "Validate Alert Request"
CALL_NODE = "Send Alert Email"
CALL_FIELDS = ["system", "severity", "key", "short", "lines"]
ALLOWED_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.code",
    "n8n-nodes-base.if",
    "n8n-nodes-base.executeWorkflow",
    "n8n-nodes-base.respondToWebhook",
    "n8n-nodes-base.stickyNote",
}
EXPECTED_EDGES = {
    ("AC2 Stock Alert Webhook", 0, "Validate Alert Event"),
    ("Validate Alert Event", 0, "Event Is Valid"),
    ("Validate Alert Event", 1, "Respond Internal Error"),
    ("Event Is Valid", 0, CALL_NODE),
    ("Event Is Valid", 1, "Respond Event Rejected"),
    (CALL_NODE, 0, "Email Was Handled"),
    (CALL_NODE, 1, "Respond Internal Error"),
    ("Email Was Handled", 0, "Respond Alert Sent"),
    ("Email Was Handled", 1, "Email Send Failed"),
    ("Email Send Failed", 0, "Respond Email Failed"),
    ("Email Send Failed", 1, "Respond Internal Error"),
}
# The VM records these as alert_status / http_status; the map must not change (#245).
RESPONSE_CODES = {
    "Respond Alert Sent": 200,
    "Respond Event Rejected": 400,
    "Respond Internal Error": 500,
    "Respond Email Failed": 502,
}
STARTED = datetime(2026, 10, 29, 14, 0, 5, tzinfo=ZoneInfo("Asia/Singapore"))
RUN_ID = "0123456789abcdef0123456789abcdef"
CANARY_ITEM = "CANARY-ITEM-0201"
LINE_RE = re.compile(r"^[ -~]{0,300}$")

# Runs the AC2 Code node on each event, then hands a valid result to the sub-workflow's own
# Validate Alert Request Code node exactly as the call node maps it (system pinned to AC2-STOCK).
HARNESS = r"""
const fs = require('fs');
const [workflowPath, subPath, casesPath] = process.argv.slice(2);
const codeOf = (path, name) => JSON.parse(fs.readFileSync(path, 'utf8')).nodes.find((n) => n.name === name).parameters.jsCode;
const code = codeOf(workflowPath, 'Validate Alert Event');
const subCode = codeOf(subPath, 'Validate Alert Request');
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const results = cases.map((body) => {
  const $input = { all: () => [{ json: { body, headers: { 'x-xb-alert-token': 'FAKE-SECRET-NOT-READ' } } }] };
  try {
    const out = new Function('$input', code)($input);
    let sub = null;
    if (out.length === 1 && out[0].json.valid === true) {
      const j = out[0].json;
      const call = { system: 'AC2-STOCK', severity: j.severity, key: j.key, short: j.short, lines: j.lines };
      sub = new Function('$input', subCode)({ all: () => [{ json: call }] });
    }
    return { ok: true, out, sub };
  } catch (error) {
    return { ok: false, error: String(error.message) };
  }
});
process.stdout.write(JSON.stringify(results));
"""


def status(**overrides):
    value = {
        "job": hourly.JOB,
        "run_id": RUN_ID,
        "status": "success",
        "failure_class": None,
        "daily_close_status": "not_due",
        "delivery_status": "handed_to_drive_sync_folder",
        "feed_status": "published",
        "item_index_status": "published",
        "large_balance_count": 0,
        "late_change_day_count": None,
    }
    value.update(overrides)
    return value


def event(kind="new", **overrides):
    st = status(**overrides)
    return hourly.build_alert_payload(st, hourly.alert_conditions(st), kind, STARTED)


def failed_event(failure_class="ledger_mismatch"):
    return event(
        status="failed", failure_class=failure_class, daily_close_status="skipped_failed_run",
        delivery_status="skipped_failed_run", feed_status=None, item_index_status=None, large_balance_count=None,
    )


class WorkflowShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_inactive_mcp_hidden_unsaved_and_free_of_live_identity(self):
        self.assertIs(self.workflow["active"], False)
        settings = self.workflow["settings"]
        self.assertIs(settings["availableInMCP"], False)
        # The webhook item carries the request headers, so the shared secret, in every execution.
        self.assertEqual(settings["saveDataSuccessExecution"], "none")
        self.assertEqual(settings["saveDataErrorExecution"], "none")
        self.assertIs(settings["saveManualExecutions"], False)
        # Pinned so an instance that turns on save-on-progress still keeps in-flight data out.
        self.assertIs(settings["saveExecutionProgress"], False)
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES, node["name"])

    def test_edges_are_exactly_the_reviewed_graph(self):
        edges = {
            (source, index, link["node"])
            for source, outputs in self.workflow["connections"].items()
            for index, branch in enumerate(outputs["main"])
            for link in branch
        }
        self.assertEqual(edges, EXPECTED_EDGES)
        working = {name for name, node in self.nodes.items() if node["type"] != "n8n-nodes-base.stickyNote"}
        self.assertEqual(working, {e[0] for e in EXPECTED_EDGES} | {e[2] for e in EXPECTED_EDGES})

    def test_webhook_is_header_auth_and_answers_from_respond_nodes(self):
        params = self.nodes["AC2 Stock Alert Webhook"]["parameters"]
        self.assertEqual(params["httpMethod"], "POST")
        self.assertEqual(params["path"], "REPLACE_WITH_ALERT_WEBHOOK_PATH")
        self.assertEqual(params["authentication"], "headerAuth")
        self.assertEqual(params["responseMode"], "responseNode")

    def test_every_path_ends_in_a_status_coded_response(self):
        for name, code in RESPONSE_CODES.items():
            node = self.nodes[name]
            self.assertEqual(node["type"], "n8n-nodes-base.respondToWebhook", name)
            self.assertEqual(node["parameters"]["options"]["responseCode"], code, name)
            self.assertEqual(node["parameters"]["respondWith"], "json", name)
        sources = {e[0] for e in EXPECTED_EDGES}
        for name, node in self.nodes.items():
            if node["type"] not in ("n8n-nodes-base.stickyNote", "n8n-nodes-base.respondToWebhook"):
                self.assertIn(name, sources, "%s must lead somewhere" % name)
        # Fallible nodes route errors to a response instead of halting the webhook call.
        for name in ("Validate Alert Event", CALL_NODE):
            self.assertEqual(self.nodes[name].get("onError"), "continueErrorOutput", name)
        self.assertEqual(set(RESPONSE_CODES.values()), {200, 400, 500, 502})
        self.assertEqual(self.nodes["Respond Email Failed"]["parameters"]["responseBody"],
                         "={{ { error: 'upstream_error', message: 'email send failed' } }}")

    def test_telegram_is_gone(self):
        text = WORKFLOW.read_text(encoding="utf-8").lower()
        self.assertNotIn("telegram", text)
        self.assertNotIn("chat_id", text)
        self.assertNotIn("chatid", text)

    def test_call_payload_is_closed_and_matches_the_sub_workflow_input(self):
        node = self.nodes[CALL_NODE]
        params = node["parameters"]
        self.assertEqual(node["type"], "n8n-nodes-base.executeWorkflow")
        self.assertEqual(params["source"], "database")
        self.assertEqual(params["workflowId"],
                         {"__rl": True, "value": "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "mode": "id"})
        self.assertEqual(params["mode"], "once")
        self.assertIs(params["options"]["waitForSubWorkflow"], True)
        inputs = params["workflowInputs"]
        self.assertEqual(inputs["mappingMode"], "defineBelow")
        self.assertEqual(inputs["value"], {
            "system": "AC2-STOCK",
            "severity": "={{ $json.severity }}",
            "key": "={{ $json.key }}",
            "short": "={{ $json.short }}",
            "lines": "={{ $json.lines }}",
        })
        self.assertEqual([f["id"] for f in inputs["schema"]], CALL_FIELDS)
        self.assertFalse(any(f["removed"] for f in inputs["schema"]))
        self.assertIs(inputs["convertFieldsToString"], False, "lines must stay an array")
        sub = json.loads(SUB_WORKFLOW.read_text(encoding="utf-8"))
        trigger = next(n for n in sub["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflowTrigger")
        declared = [(v["name"], v["type"]) for v in trigger["parameters"]["workflowInputs"]["values"]]
        self.assertEqual(declared, [(f["id"], f["type"]) for f in inputs["schema"]])
        # No automatic retry: a retried sub-workflow call could send a second email.
        for key in ("retryOnFail", "maxTries", "waitBetweenTries", "continueOnFail", "alwaysOutputData"):
            self.assertNotIn(key, node)

    def test_only_a_handled_email_answers_200(self):
        handled = self.nodes["Email Was Handled"]["parameters"]["conditions"]["conditions"]
        self.assertEqual([c["leftValue"] for c in handled],
                         ["={{ ['sent', 'folded', 'folded_flood'].includes($json.outcome) }}"])
        failed = self.nodes["Email Send Failed"]["parameters"]["conditions"]["conditions"]
        self.assertEqual([c["leftValue"] for c in failed], ["={{ $json.outcome === 'send_failed' }}"])
        for name in ("Email Was Handled", "Email Send Failed"):
            (condition,) = self.nodes[name]["parameters"]["conditions"]["conditions"]
            self.assertIs(condition["rightValue"], True)
            self.assertEqual(condition["operator"], {"type": "boolean", "operation": "equals"})

    def test_rejected_answer_carries_only_the_fixed_reason_code(self):
        body = self.nodes["Respond Event Rejected"]["parameters"]["responseBody"]
        self.assertEqual(body, "={{ { error: 'validation_error', message: $json.reason } }}")

    def test_code_allowlist_mirrors_the_exporter_contract(self):
        code = self.nodes[CODE_NODE]["parameters"]["jsCode"]
        self.assertIn("'%s'" % hourly.ALERT_SCHEMA, code)
        self.assertIn("'%s'" % hourly.ALERT_EVENT, code)
        groups = {
            "KEYS": hourly.ALERT_PAYLOAD_KEYS,
            "CONDITIONS": hourly.ALERT_CONDITIONS,
            "KINDS": hourly.ALERT_KINDS,
            "FAILURE_CLASSES": hourly.ALERT_FAILURE_CLASSES + ("unknown",),
            "CLOSE_STATUSES": hourly.ALERT_CLOSE_STATUSES,
            "DELIVERY_STATUSES": hourly.ALERT_DELIVERY_STATUSES,
            "PUBLISH_STATUSES": hourly.ALERT_PUBLISH_STATUSES,
        }
        for name, values in groups.items():
            line = next(line for line in code.splitlines() if line.startswith("const %s = [" % name))
            listed = json.loads(line.split("=", 1)[1].strip().rstrip(";").replace("'", '"'))
            self.assertEqual(listed, list(values), name)
        self.assertIn("const COUNT_MAX = %d;" % hourly.ALERT_COUNT_MAX, code)
        for reader in (".headers", "['headers']", '["headers"]'):
            self.assertNotIn(reader, code, "the Code node must never read request headers")

    def test_notes_and_readme(self):
        notes = "\n".join(
            node["parameters"]["content"] for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.stickyNote"
        )
        self.assertIn("Required manual configuration", notes)
        self.assertIn("REPLACE_WITH_ALERT_WEBHOOK_PATH", notes)
        self.assertIn("REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", notes)
        self.assertIn("XB Ops Alert Email", notes)
        self.assertIn("ac2_stock_alert.workflow.json", README.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code node")
class WorkflowCodeExecutionTests(unittest.TestCase):
    def run_cases(self, cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            args = [shutil.which("node"), str(harness), str(WORKFLOW), str(SUB_WORKFLOW), str(cases_path)]
            done = subprocess.run(args, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def assert_accepted_by_sub_workflow(self, result):
        self.assertIsNotNone(result["sub"])
        (sub,) = result["sub"]
        self.assertIs(sub["json"]["valid"], True, sub)
        self.assertEqual(sub["json"]["system"], "AC2-STOCK")

    def test_real_exporter_events_render_a_closed_email_request(self):
        cases = [
            (failed_event(), "ERROR", "run_failed:ledger_mismatch",
             ["AC2 stock export: ERROR", "- Hourly export FAILED: ledger_mismatch", "Run: 01234567",
              "Hour: 2026-10-29 14:00 (UTC+08:00)"]),
            (event(large_balance_count=2), "WARN", "large_balance",
             ["AC2 stock export: WARN", "- Large balances: 2 row(s)"]),
            (event(daily_close_status="failed", late_change_day_count=3), "ERROR", "daily_close_failed+late_change",
             ["- Daily close file not written", "- Late ledger changes: 3 day(s)"]),
            (event(delivery_status="copy_failed", feed_status="failed", item_index_status="failed"), "ERROR",
             "delivery_failed+feed_failed+item_index_failed",
             ["- Drive delivery failed: copy_failed", "- Dashboard feed not published", "- Item index not published"]),
            (event("repeat", large_balance_count=1), "WARN", "large_balance",
             ["AC2 stock export: still not resolved (daily reminder)"]),
        ]
        results = self.run_cases([body for body, *_ in cases])
        for (body, severity, short, expected), result in zip(cases, results):
            with self.subTest(conditions=body["conditions"]):
                self.assertTrue(result["ok"], result)
                (item,) = result["out"]
                self.assertEqual(set(item["json"]), {"valid", "severity", "key", "short", "lines"})
                self.assertIs(item["json"]["valid"], True)
                self.assertEqual(item["json"]["severity"], severity)
                self.assertEqual(item["json"]["short"], short)
                self.assertEqual(item["json"]["key"], short, "the fold key is stable per condition set")
                text = "\n".join(item["json"]["lines"])
                for line in expected:
                    self.assertIn(line, text)
                self.assertNotIn("FAKE-SECRET", json.dumps(item))
                self.assertNotIn("Telegram", text)
                self.assert_accepted_by_sub_workflow(result)

    def test_every_closed_value_is_accepted_by_the_shared_email_sub_workflow(self):
        # The sub-workflow refuses anything but printable ASCII lines and subject-safe codes, so
        # every value the exporter can send must render inside those limits.
        cases = [failed_event(failure_class) for failure_class in hourly.ALERT_FAILURE_CLASSES + ("unknown",)]
        cases += [event(delivery_status=value, large_balance_count=1) for value in ("verify_failed", "copy_failed")]
        cases += [event("repeat", daily_close_status="failed", late_change_day_count=2, feed_status="failed",
                        item_index_status="failed", delivery_status="verify_failed",
                        large_balance_count=hourly.ALERT_COUNT_MAX)]
        for body, result in zip(cases, self.run_cases(cases)):
            with self.subTest(failure_class=body["failure_class"], delivery=body["delivery_status"]):
                self.assertTrue(result["ok"], result)
                (item,) = result["out"]
                self.assertIs(item["json"]["valid"], True, item)
                for line in item["json"]["lines"]:
                    self.assertRegex(line, LINE_RE)
                self.assertRegex(item["json"]["short"], r"^[A-Za-z0-9_:+.-]{1,120}$")
                self.assert_accepted_by_sub_workflow(result)

    def test_every_departure_from_the_contract_is_rejected(self):
        base = event(large_balance_count=2)
        bad_failed = failed_event()
        cases = [
            {**base, "item_code": CANARY_ITEM},
            {**base, "message": "free text " + CANARY_ITEM},
            {key: value for key, value in base.items() if key != "conditions"},
            {**base, "schema": "xb.ac2.stock_alert.v2"},
            {**base, "event": "other"},
            {**base, "run_id": "not-a-run-id"},
            {**base, "run_hour_local": "2026-10-29T14:30:00+08:00"},
            {**base, "run_hour_local": "yesterday"},
            {**base, "alert_kind": "cleared"},
            {**base, "status": "ok"},
            {**base, "failure_class": "ledger_mismatch"},
            {**bad_failed, "failure_class": None},
            {**bad_failed, "failure_class": "<b>x</b>"},
            {**base, "conditions": []},
            {**base, "conditions": ["large_balance", "large_balance"]},
            {**base, "conditions": ["stock_qty"]},
            {**base, "conditions": ["run_failed"]},
            {**bad_failed, "conditions": ["run_failed", "large_balance"]},
            {**base, "conditions": "large_balance"},
            {**base, "daily_close_status": "done"},
            {**base, "delivery_status": "uploaded"},
            {**base, "feed_status": "built"},
            {**base, "item_index_status": "built"},
            {**base, "large_balance_count": -1},
            {**base, "large_balance_count": "2"},
            {**base, "large_balance_count": True},
            {**base, "late_change_day_count": 1.5},
            {**base, "late_change_day_count": None},
            [],
            None,
            "text",
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=str(case)[:80]):
                self.assertTrue(result["ok"], result)
                (item,) = result["out"]
                self.assertEqual(item["json"]["valid"], False)
                self.assertRegex(item["json"]["reason"], r"^AC2_ALERT_[A-Z_]+$")
                self.assertEqual(set(item["json"]), {"valid", "reason"})
                self.assertIsNone(result["sub"], "a rejected event never reaches the email sub-workflow")


if __name__ == "__main__":
    unittest.main()
