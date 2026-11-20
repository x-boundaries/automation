"""Offline contract of the inactive AC2 AR/AP aging export error-alert n8n export (#321 slice A3).

No n8n instance, Gmail account or VM is contacted. The committed Code node is executed with the
local Node runtime against events built by the real exporter (build_alert_payload), and its
output is checked against the committed Code node of the shared XB Ops Alert Email sub-workflow.
The workflow is a copy of the stock alert's shape (ac2_stock_alert.workflow.json); the tests pin
that the graph, settings and responses stay the same. Every value here is synthetic.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import autocount_arap_aging as arap  # noqa: E402

WORKFLOW = ROOT / "n8n-workflows" / "ac2_arap_alert.workflow.json"
STOCK_WORKFLOW = ROOT / "n8n-workflows" / "ac2_stock_alert.workflow.json"
SUB_WORKFLOW = ROOT / "n8n-workflows" / "ops_alert_email.workflow.json"
README = ROOT / "n8n-workflows" / "README.md"
RUNBOOK = ROOT / "docs" / "autocount2-automation" / "arap_aging_runbook.md"
WEBHOOK_NODE = "AC2 ARAP Alert Webhook"
CODE_NODE = "Validate Alert Event"
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
    (WEBHOOK_NODE, 0, CODE_NODE),
    (CODE_NODE, 0, "Event Is Valid"),
    (CODE_NODE, 1, "Respond Internal Error"),
    ("Event Is Valid", 0, CALL_NODE),
    ("Event Is Valid", 1, "Respond Event Rejected"),
    (CALL_NODE, 0, "Email Was Handled"),
    (CALL_NODE, 1, "Respond Internal Error"),
    ("Email Was Handled", 0, "Respond Alert Sent"),
    ("Email Was Handled", 1, "Email Send Failed"),
    ("Email Send Failed", 0, "Respond Email Failed"),
    ("Email Send Failed", 1, "Respond Internal Error"),
}
# The VM records these as alert_status / alert_http_status; same map as the stock alert.
RESPONSE_CODES = {
    "Respond Alert Sent": 200,
    "Respond Event Rejected": 400,
    "Respond Internal Error": 500,
    "Respond Email Failed": 502,
}
SGT = timezone(timedelta(hours=8))
STARTED = datetime(2026, 11, 19, 14, 30, 5, tzinfo=SGT)
RUN_ID = "0123456789abcdef0123456789abcdef"
CANARY_PARTY = "DEBT-CANARY-01"
LINE_RE = re.compile(r"^[ -~]{0,300}$")
OTHER_SYSTEMS = ("AC2-STOCK", "ENERGYGRID", "MEMBER-SYNC", "HEALTH")

# Runs the ARAP Code node on each event, then hands a valid result to the sub-workflow's own
# Validate Alert Request Code node exactly as the call node maps it (system pinned to AC2-ARAP).
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
      const call = { system: 'AC2-ARAP', severity: j.severity, key: j.key, short: j.short, lines: j.lines };
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
        "job": arap.JOB,
        "run_id": RUN_ID,
        "status": "success",
        "failure_class": None,
        "message": "free text " + CANARY_PARTY,
        "delivery_status": "handed_to_drive_sync_folder",
        "ap_payments_recorded": False,
        "docs_open": {"ar": 12, "ap": 7},
    }
    value.update(overrides)
    return value


def event(kind="new", **overrides):
    st = status(**overrides)
    return arap.build_alert_payload(st, arap.alert_conditions(st), kind, STARTED)


def failed_event(failure_class="outstanding_mismatch"):
    return event(status="failed", failure_class=failure_class, delivery_status="skipped_failed_run",
                 ap_payments_recorded=None, docs_open=None)


def nodes_of(workflow):
    return {node["name"]: node for node in workflow["nodes"]}


def edges_of(workflow):
    return {
        (source, index, link["node"])
        for source, outputs in workflow["connections"].items()
        for index, branch in enumerate(outputs["main"])
        for link in branch
    }


class WorkflowShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        cls.nodes = nodes_of(cls.workflow)

    def test_inactive_mcp_hidden_unsaved_and_free_of_live_identity(self):
        self.assertIs(self.workflow["active"], False)
        self.assertEqual(self.workflow["name"], "AC2 ARAP Export - Error Alert")
        settings = self.workflow["settings"]
        self.assertIs(settings["availableInMCP"], False)
        # The webhook item carries the request headers, so the shared secret, in every execution.
        self.assertEqual(settings["saveDataSuccessExecution"], "none")
        self.assertEqual(settings["saveDataErrorExecution"], "none")
        self.assertIs(settings["saveManualExecutions"], False)
        self.assertIs(settings["saveExecutionProgress"], False)
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES, node["name"])

    def test_ids_never_collide_with_the_stock_alert(self):
        stock = json.loads(STOCK_WORKFLOW.read_text(encoding="utf-8"))
        self.assertNotEqual(self.workflow["id"], stock["id"])
        self.assertNotEqual(self.workflow["versionId"], stock["versionId"])
        mine = {node["id"] for node in self.workflow["nodes"]}
        self.assertEqual(len(mine), len(self.workflow["nodes"]))
        self.assertFalse(mine & {node["id"] for node in stock["nodes"]})

    def test_same_shape_as_the_stock_alert(self):
        # A copy of the reviewed stock alert: same graph, node types, settings and responses; only
        # the webhook's name and path, the Code node, the pinned system and the notes differ.
        stock = json.loads(STOCK_WORKFLOW.read_text(encoding="utf-8"))
        rename = {"AC2 Stock Alert Webhook": WEBHOOK_NODE}
        self.assertEqual(edges_of(self.workflow), {(rename.get(s, s), i, rename.get(t, t)) for s, i, t in edges_of(stock)})
        self.assertEqual(self.workflow["settings"], stock["settings"])
        stock_nodes = {rename.get(name, name): node for name, node in nodes_of(stock).items()}
        self.assertEqual(set(self.nodes), set(stock_nodes))
        for name, node in self.nodes.items():
            other = stock_nodes[name]
            self.assertEqual((node["type"], node["typeVersion"]), (other["type"], other["typeVersion"]), name)
            self.assertEqual(node.get("onError"), other.get("onError"), name)
            if node["type"] in ("n8n-nodes-base.if", "n8n-nodes-base.respondToWebhook"):
                # Condition ids carry each workflow's own issue number (-8321- here, -8245- there).
                same = json.dumps(node["parameters"]).replace("-8321-", "-8245-")
                self.assertEqual(json.loads(same), other["parameters"], name)

    def test_edges_are_exactly_the_reviewed_graph(self):
        self.assertEqual(edges_of(self.workflow), EXPECTED_EDGES)
        working = {name for name, node in self.nodes.items() if node["type"] != "n8n-nodes-base.stickyNote"}
        self.assertEqual(working, {e[0] for e in EXPECTED_EDGES} | {e[2] for e in EXPECTED_EDGES})

    def test_webhook_is_header_auth_with_its_own_path_placeholder(self):
        params = self.nodes[WEBHOOK_NODE]["parameters"]
        self.assertEqual(params["httpMethod"], "POST")
        self.assertEqual(params["path"], "REPLACE_WITH_ARAP_ALERT_WEBHOOK_PATH")
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
        # Error outputs wired: fallible nodes answer 500 instead of halting the webhook call.
        for name in (CODE_NODE, CALL_NODE):
            self.assertEqual(self.nodes[name].get("onError"), "continueErrorOutput", name)
        self.assertEqual(self.nodes["Respond Event Rejected"]["parameters"]["responseBody"],
                         "={{ { error: 'validation_error', message: $json.reason } }}")

    def test_call_payload_is_closed_pins_ac2_arap_and_matches_the_sub_workflow_input(self):
        node = self.nodes[CALL_NODE]
        params = node["parameters"]
        self.assertEqual(params["workflowId"],
                         {"__rl": True, "value": "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "mode": "id"})
        self.assertEqual(params["mode"], "once")
        self.assertIs(params["options"]["waitForSubWorkflow"], True)
        inputs = params["workflowInputs"]
        self.assertEqual(inputs["value"], {
            "system": "AC2-ARAP",
            "severity": "={{ $json.severity }}",
            "key": "={{ $json.key }}",
            "short": "={{ $json.short }}",
            "lines": "={{ $json.lines }}",
        })
        self.assertEqual([f["id"] for f in inputs["schema"]], CALL_FIELDS)
        self.assertIs(inputs["convertFieldsToString"], False, "lines must stay an array")
        sub = json.loads(SUB_WORKFLOW.read_text(encoding="utf-8"))
        trigger = next(n for n in sub["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflowTrigger")
        declared = [(v["name"], v["type"]) for v in trigger["parameters"]["workflowInputs"]["values"]]
        self.assertEqual(declared, [(f["id"], f["type"]) for f in inputs["schema"]])
        # No automatic retry: a retried sub-workflow call could send a second email.
        for key in ("retryOnFail", "maxTries", "waitBetweenTries", "continueOnFail", "alwaysOutputData"):
            self.assertNotIn(key, node)
        # The sender knows the system this workflow pins.
        self.assertIn("'AC2-ARAP'", next(n for n in sub["nodes"] if n["name"] == "Validate Alert Request")
                      ["parameters"]["jsCode"])

    def test_code_allowlist_mirrors_the_exporter_contract(self):
        code = self.nodes[CODE_NODE]["parameters"]["jsCode"]
        self.assertIn("'%s'" % arap.ALERT_SCHEMA, code)
        self.assertIn("'%s'" % arap.ALERT_EVENT, code)
        groups = {
            "KEYS": arap.ALERT_PAYLOAD_KEYS,
            "CONDITIONS": arap.ALERT_CONDITIONS,
            "KINDS": arap.ALERT_KINDS,
            "FAILURE_CLASSES": arap.ALERT_FAILURE_CLASSES + ("unknown",),
            "DELIVERY_STATUSES": arap.ALERT_DELIVERY_STATUSES,
            "DELIVERY_FAILURES": arap.ALERT_DELIVERY_FAILURES,
            "WARN_CONDITIONS": arap.ALERT_NOTICE_CONDITIONS,
        }
        for name, values in groups.items():
            line = next(line for line in code.splitlines() if line.startswith("const %s = [" % name))
            listed = json.loads(line.split("=", 1)[1].strip().rstrip(";").replace("'", '"'))
            self.assertEqual(listed, list(values), name)
        for reader in (".headers", "['headers']", '["headers"]'):
            self.assertNotIn(reader, code, "the Code node must never read request headers")

    def test_notes_readme_and_runbook(self):
        notes = "\n".join(
            node["parameters"]["content"] for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.stickyNote"
        )
        for text in ("Required manual configuration", "REPLACE_WITH_ARAP_ALERT_WEBHOOK_PATH",
                     "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "XB Ops Alert Email", "AC2-ARAP"):
            self.assertIn(text, notes)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("### ac2_arap_alert.workflow.json", readme)
        self.assertIn("AC2 ARAP Export - Error Alert", readme)
        runbook = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("## A3: error alerts", runbook)
        self.assertIn("ac2_arap_alert.workflow.json", runbook)


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
        self.assertEqual(sub["json"]["system"], "AC2-ARAP")

    def test_real_exporter_events_render_a_closed_email_request(self):
        cases = [
            (failed_event(), "ERROR", "run_failed:outstanding_mismatch",
             ["AC2 AR/AP export: ERROR", "- Hourly AR/AP export FAILED: outstanding_mismatch", "Run: 01234567",
              "Hour: 2026-11-19 14:00 (UTC+08:00)", "arap_aging_runbook.md"]),
            (event(delivery_status="copy_failed"), "ERROR", "delivery_failed",
             ["- Drive delivery failed: copy_failed"]),
            (event(ap_payments_recorded=True), "WARN", "ap_payments_recorded",
             ["AC2 AR/AP export: WARN", "- AP payments are now recorded in AutoCount", "knockoff_target_types.ap"]),
            (event(delivery_status="verify_failed", ap_payments_recorded=True), "ERROR",
             "delivery_failed+ap_payments_recorded",
             ["- Drive delivery failed: verify_failed", "- AP payments are now recorded"]),
            (event("repeat", delivery_status="copy_failed"), "ERROR", "delivery_failed",
             ["AC2 AR/AP export: still not resolved (daily reminder)"]),
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
                rendered = json.dumps(item)
                for leak in ("FAKE-SECRET", CANARY_PARTY, "free text"):
                    self.assertNotIn(leak, rendered)
                for other in OTHER_SYSTEMS:
                    self.assertNotIn(other, text)
                self.assert_accepted_by_sub_workflow(result)

    def test_every_closed_value_is_accepted_by_the_shared_email_sub_workflow(self):
        cases = [failed_event(failure_class) for failure_class in arap.ALERT_FAILURE_CLASSES + ("unknown", "not_a_class")]
        cases += [event(kind, delivery_status=value, ap_payments_recorded=ap)
                  for kind in arap.ALERT_KINDS for value in arap.ALERT_DELIVERY_FAILURES for ap in (True, False)]
        cases += [event(kind, ap_payments_recorded=True) for kind in arap.ALERT_KINDS]
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
        base = event(delivery_status="copy_failed")
        notice = event(ap_payments_recorded=True)
        bad_failed = failed_event()
        cases = [
            {**base, "party_code": CANARY_PARTY},
            {**base, "message": "free text " + CANARY_PARTY},
            {**base, "amount": 1200},
            {**base, "docs_open": {"ar": 1, "ap": 2}},
            {key: value for key, value in base.items() if key != "conditions"},
            {key: value for key, value in base.items() if key != "ap_payments_recorded"},
            {**base, "schema": "xb.ac2.arap_alert.v2"},
            {**base, "schema": "xb.ac2.stock_alert.v1"},
            {**base, "event": "ac2_stock_alert"},
            {**base, "run_id": "not-a-run-id"},
            {**base, "run_id": RUN_ID.upper()},
            {**base, "run_hour_local": "2026-11-19T14:30:00+08:00"},
            {**base, "run_hour_local": "yesterday"},
            {**base, "alert_kind": "cleared"},
            {**base, "status": "ok"},
            {**base, "failure_class": "outstanding_mismatch"},
            {**bad_failed, "failure_class": None},
            {**bad_failed, "failure_class": "ledger_mismatch"},
            {**bad_failed, "failure_class": "<b>x</b>"},
            {**base, "conditions": []},
            {**base, "conditions": ["delivery_failed", "delivery_failed"]},
            {**base, "conditions": ["large_balance"]},
            {**base, "conditions": ["run_failed"]},
            {**bad_failed, "conditions": ["run_failed", "delivery_failed"]},
            {**base, "conditions": "delivery_failed"},
            {**base, "delivery_status": "uploaded"},
            # The condition and its status field must agree.
            {**base, "delivery_status": "handed_to_drive_sync_folder"},
            {**notice, "delivery_status": "copy_failed"},
            {**notice, "ap_payments_recorded": False},
            {**base, "ap_payments_recorded": True},
            {**base, "ap_payments_recorded": None},
            {**base, "ap_payments_recorded": "true"},
            {**bad_failed, "ap_payments_recorded": False},
            [],
            None,
            "text",
        ]
        for case, result in zip(cases, self.run_cases(cases)):
            with self.subTest(case=str(case)[:100]):
                self.assertTrue(result["ok"], result)
                (item,) = result["out"]
                self.assertEqual(item["json"]["valid"], False)
                self.assertRegex(item["json"]["reason"], r"^ARAP_ALERT_[A-Z_]+$")
                self.assertEqual(set(item["json"]), {"valid", "reason"})
                self.assertIsNone(result["sub"], "a rejected event never reaches the email sub-workflow")


if __name__ == "__main__":
    unittest.main()
