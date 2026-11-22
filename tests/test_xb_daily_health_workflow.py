"""Offline contract of the inactive "XB Daily Health" n8n workflow (#325 H1), and the golden email.

No n8n instance, n8n API, Data Table or Gmail is contacted. The committed Code nodes are
executed with the local Node runtime against events made by the real checker code
(scripts/xb_daily_health.py) from fake observations, and against synthetic n8n reads. The golden
email is then passed through the committed XB Ops Alert Email Code nodes. Every value here is fake.
"""

import atexit
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import xb_daily_health as health  # noqa: E402
from test_xb_daily_health import healthy_obs, record, settings  # noqa: E402

WORKFLOW = ROOT / "n8n-workflows" / "xb_daily_health.workflow.json"
SENDER = ROOT / "n8n-workflows" / "ops_alert_email.workflow.json"
README = ROOT / "n8n-workflows" / "README.md"
RUNBOOK = ROOT / "docs" / "daily_health_check.md"
VALIDATE = "Validate Health Event"
JUDGE = "Judge And Build Email"
DEADMAN = "Decide Dead-Man"
BROKEN = "Build Broken-Check Email"
SGT = health.SGT
NOW = datetime(2026, 11, 19, 7, 30, tzinfo=SGT)
NOW_MS = int(NOW.timestamp() * 1000)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_TYPES = {
    "n8n-nodes-base.webhook", "n8n-nodes-base.if", "n8n-nodes-base.respondToWebhook", "n8n-nodes-base.code",
    "n8n-nodes-base.n8n", "n8n-nodes-base.dataTable", "n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.noOp",
    "n8n-nodes-base.executeWorkflow", "n8n-nodes-base.set", "n8n-nodes-base.stickyNote",
}
EXPECTED_EDGES = {
    ("Health Check Webhook", 0, "Request Came Through Proxy"),
    ("Request Came Through Proxy", 0, "Respond Not Found"),
    ("Request Came Through Proxy", 1, VALIDATE),
    (VALIDATE, 0, "Event Is Valid"),
    (VALIDATE, 1, "Respond Internal Error"),
    ("Event Is Valid", 0, "Respond Event Accepted"),
    ("Event Is Valid", 1, "Respond Event Rejected"),
    ("Respond Event Accepted", 0, "List Workflows"),
    ("List Workflows", 0, "List Error Runs"),
    ("List Error Runs", 0, "Read Alert Log"),
    ("Read Alert Log", 0, "Read Failed Alert Emails"),
    ("Read Failed Alert Emails", 0, "Read Monitor Error Log"),
    ("Read Monitor Error Log", 0, "Read Kit Re-tagger Errors"),
    ("Read Kit Re-tagger Errors", 0, "Read Health Runs"),
    ("Read Health Runs", 0, JUDGE),
    (JUDGE, 0, "Email Plan"),
    (JUDGE, 1, BROKEN),
    ("Dead-Man Check 08:15", 0, "Read Today Runs"),
    ("Read Today Runs", 0, DEADMAN),
    (DEADMAN, 0, "Email Plan"),
    (DEADMAN, 1, BROKEN),
    (BROKEN, 0, "Email Plan"),
    ("Email Plan", 0, "Email Is Due"),
    ("Email Is Due", 0, "Send Health Email"),
    ("Email Is Due", 1, "Collect Send Outcome"),
    ("Send Health Email", 0, "Collect Send Outcome"),
    ("Send Health Email", 1, "Collect Send Outcome"),
    ("Collect Send Outcome", 0, "Log Health Run"),
}
READ_NODES = ("List Workflows", "List Error Runs", "Read Alert Log", "Read Failed Alert Emails", "Read Monitor Error Log",
              "Read Kit Re-tagger Errors", "Read Health Runs", "Read Today Runs")
SEND_FAILURES = ["send_failed", "rejected", "error", "call_failed", "unknown"]
RUN_COLUMNS = ["date", "kind", "verdict", "watched", "problems", "fail_jobs", "warn_jobs", "email", "code", "logged_at"]

HARNESS = r"""
const fs = require('fs');
const [casesPath] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const cache = {};
const codeOf = (path, name) => {
  cache[path] = cache[path] || JSON.parse(fs.readFileSync(path, 'utf8'));
  return cache[path].nodes.find((n) => n.name === name).parameters.jsCode;
};
const asItems = (value) => (Array.isArray(value) ? value.map((json) => ({ json })) : value.raw);
const realNow = Date.now;
const results = cases.map((c) => {
  const $input = { all: () => asItems(c.items) };
  const $ = (name) => {
    if (!(name in (c.nodes || {}))) throw new Error('unexpected node reference: ' + name);
    const items = asItems(c.nodes[name]);
    return { first: () => items[0], all: () => items };
  };
  Date.now = () => c.now_ms;
  try {
    return { ok: true, out: new Function('$input', '$', codeOf(c.workflow, c.node))($input, $) };
  } catch (error) {
    return { ok: false, error: String(error.message) };
  } finally {
    Date.now = realNow;
  }
});
process.stdout.write(JSON.stringify(results));
"""

# The golden problem-day email (design section 6.1, rebuilt from fake records by the real code).
GOLDEN_SUBJECT = "[XB health] FAIL 3 jobs 2026-11-19"
GOLDEN_TEXT = None  # filled below the class definitions


def load(path=WORKFLOW):
    return json.loads(path.read_text(encoding="utf-8"))


def edges_of(workflow):
    return {(source, index, link["node"]) for source, outputs in workflow["connections"].items()
            for index, branch in enumerate(outputs["main"]) for link in branch}


def run_cases(cases):
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps(cases), encoding="utf-8")
        done = subprocess.run([shutil.which("node"), str(harness), str(path)], capture_output=True, text=True,
                              timeout=60)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def checker_event(obs=None, records=None, now=NOW, **overrides):
    return health.judge(settings(**overrides), obs or healthy_obs(), records or {}, now)


def validate(*bodies, now_ms=NOW_MS):
    results = run_cases([{"workflow": str(WORKFLOW), "node": VALIDATE, "items": [{"body": body}], "now_ms": now_ms}
                         for body in bodies])
    outs = []
    for result in results:
        assert result["ok"], result
        (item,) = result["out"]
        outs.append(item["json"])
    return outs


WORKFLOWS_OK = [{"id": "wfStockAlert01", "name": "fake", "active": True},
                {"id": "wfOpsEmail0001", "name": "fake", "active": True}]
# The n8n API list carries each workflow's settings (n8n 2.48.5 workflows.public.controller.ts, getWorkflows).
MONITOR_LINKED = {"errorWorkflow": "wfEgErrorLog01", "saveDataErrorExecution": "none",
                  "saveDataSuccessExecution": "none", "executionOrder": "v1"}
WORKFLOWS_ALL = WORKFLOWS_OK + [{"id": "wfEgMonitor001", "name": "fake", "active": True, "settings": MONITOR_LINKED},
                                {"id": "wfEgErrorLog01", "name": "fake", "active": True}]


def judge_reads(workflows=None, error_runs=None, alert_log=None, runs=None, failed_alerts=None, error_log=None,
                kit_errors=None):
    return {
        "List Workflows": WORKFLOWS_ALL if workflows is None else workflows,
        "List Error Runs": [{}] if error_runs is None else error_runs,
        "Read Alert Log": [{}] if alert_log is None else alert_log,
        "Read Failed Alert Emails": [{}] if failed_alerts is None else failed_alerts,
        "Read Monitor Error Log": [{}] if error_log is None else error_log,
        "Read Kit Re-tagger Errors": [{}] if kit_errors is None else kit_errors,
        "Read Health Runs": [{}] if runs is None else runs,
    }


def configured_workflow(watch_ids):
    """A copy of the export with the WATCH placeholders replaced, written to a temp file."""

    workflow = load()
    node = next(n for n in workflow["nodes"] if n["name"] == JUDGE)
    code = node["parameters"]["jsCode"]
    for placeholder, value in watch_ids.items():
        code = code.replace("'%s'" % placeholder, "'%s'" % value)
    node["parameters"]["jsCode"] = code
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(workflow, handle)
    return handle.name


CONFIGURED = {
    "REPLACE_WITH_STOCK_ALERT_WORKFLOW_ID": "wfStockAlert01",
    "REPLACE_WITH_ARAP_ALERT_WORKFLOW_ID": "",
    "REPLACE_WITH_ENERGYGRID_MONITOR_WORKFLOW_ID": "wfEgMonitor001",
    "REPLACE_WITH_ENERGYGRID_MONITOR_ERROR_LOG_WORKFLOW_ID": "wfEgErrorLog01",
    "REPLACE_WITH_ENERGYGRID_OWNER_EMAIL_WORKFLOW_ID": "",
    "REPLACE_WITH_ENERGYGRID_DRIVE_VERIFY_WORKFLOW_ID": "",
    "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID": "wfOpsEmail0001",
    "REPLACE_WITH_MEMBER_SYNC_SUMMARY_WORKFLOW_ID": "",
    "REPLACE_WITH_DAILY_HEALTH_WORKFLOW_ID": "",
    "REPLACE_WITH_SIMBA_INTAKE_WORKFLOW_ID": "wfSimbaIntake1",
    "REPLACE_WITH_SIMBA_ERROR_ALERT_WORKFLOW_ID": "",
}


_CONFIGURED_PATH = []


def configured_path():
    if not _CONFIGURED_PATH:
        path = configured_workflow(CONFIGURED)
        atexit.register(lambda: Path(path).unlink(missing_ok=True))
        _CONFIGURED_PATH.append(path)
    return _CONFIGURED_PATH[0]


def judge_plan(event, reads=None, workflow=None, now_ms=NOW_MS):
    """The Judge node's plan. By default the export with its WATCH IDs set (CONFIGURED), as at L3."""

    (validated,) = validate(event, now_ms=now_ms)
    assert validated["valid"] is True, validated
    nodes = dict(reads or judge_reads())
    nodes[VALIDATE] = [validated]
    (result,) = run_cases([{"workflow": workflow or configured_path(), "node": JUDGE,
                            "items": nodes["Read Health Runs"], "nodes": nodes, "now_ms": now_ms}])
    assert result["ok"], result
    (item,) = result["out"]
    return item["json"]


def through_sender(plan):
    """The plan's request through the committed sender's Validate and Decide Code nodes."""

    request = {"system": "HEALTH", "severity": plan["mail"]["severity"], "key": "daily_health",
               "short": plan["mail"]["short"], "lines": plan["mail"]["lines"]}
    (validated,) = run_cases([{"workflow": str(SENDER), "node": "Validate Alert Request", "items": [request],
                               "now_ms": NOW_MS}])
    assert validated["ok"], validated
    checked = validated["out"][0]["json"]
    if not checked["valid"]:
        return checked, None
    (decided,) = run_cases([{"workflow": str(SENDER), "node": "Decide Fold Or Send", "items": [{}],
                             "nodes": {"Validate Alert Request": [checked]}, "now_ms": NOW_MS}])
    assert decided["ok"], decided
    return checked, decided["out"][0]["json"]


def golden_inputs():
    """Problem day, Thu 19 Nov 2026 07:30 SGT: stock stopped at 23:30, no member-sync summary this
    morning, Drive mirror FETCH_FAILED since Monday; everything else fine."""

    obs = healthy_obs()
    obs["stock_status"] = dict(obs["stock_status"], started_at="2026-11-18T23:27:00+08:00",
                               finished_at="2026-11-18T23:30:00+08:00")
    obs["tasks"] = [dict(t, last_result=4) if t["name"].startswith("XB Drive Mirror") else t for t in obs["tasks"]]
    records = {d: record(d, {"drive_mirror": "WARN"}) for d in ("2026-11-16", "2026-11-17", "2026-11-18")}
    expected = dict(json.loads((ROOT / "config" / "daily_health.example.json").read_text(encoding="utf-8"))["expected"],
                    member_sync_summary="on")
    event = health.judge(settings(expected=expected), obs, records, NOW)
    return event


class ShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load()
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_inactive_mcp_hidden_no_saved_data_and_free_of_live_identity(self):
        self.assertEqual(self.workflow["name"], "XB Daily Health")
        self.assertIs(self.workflow["active"], False)
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        s = self.workflow["settings"]
        self.assertIs(s["availableInMCP"], False)
        self.assertEqual(s["timezone"], "Asia/Singapore")
        # The webhook item holds the Header Auth secret: nothing is saved, success or error.
        self.assertEqual((s["saveDataSuccessExecution"], s["saveDataErrorExecution"]), ("none", "none"))
        self.assertIs(s["saveManualExecutions"], False)
        self.assertIs(s["saveExecutionProgress"], False)
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES, node["name"])
        types = {n["type"] for n in self.workflow["nodes"]}
        for forbidden in ("n8n-nodes-base.gmail", "n8n-nodes-base.httpRequest", "n8n-nodes-base.executeCommand",
                          "n8n-nodes-base.readWriteFile"):
            self.assertNotIn(forbidden, types)

    def test_no_address_token_or_live_id_anywhere(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(EMAIL_RE.findall(text), [])
        self.assertNotRegex(text, r"bot[0-9]{6,}:|[0-9]{8,}:[A-Za-z0-9_-]{30,}")
        self.assertNotRegex(text, r'"chatId": "-?[0-9]')
        for path in (README, RUNBOOK):
            emails = [e for e in EMAIL_RE.findall(path.read_text(encoding="utf-8")) if not e.endswith("example.com")]
            self.assertEqual(emails, [], path.name)

    def test_edges_are_exactly_the_reviewed_graph(self):
        self.assertEqual(edges_of(self.workflow), EXPECTED_EDGES)
        working = {name for name, node in self.nodes.items() if node["type"] != "n8n-nodes-base.stickyNote"}
        self.assertEqual(working, {e[0] for e in EXPECTED_EDGES} | {e[2] for e in EXPECTED_EDGES})
        triggers = sorted(n["name"] for n in self.workflow["nodes"]
                          if n["type"] in ("n8n-nodes-base.webhook", "n8n-nodes-base.scheduleTrigger"))
        self.assertEqual(triggers, ["Dead-Man Check 08:15", "Health Check Webhook"])

    def test_every_fallible_node_has_its_error_path(self):
        for name in (VALIDATE, JUDGE, DEADMAN, "Send Health Email"):
            self.assertEqual(self.nodes[name].get("onError"), "continueErrorOutput", name)
        # Reads continue on their one regular output (the sender's G2 H1 lesson: a wired error output
        # too would run the next node twice). The judge turns a failed read into a WARN line.
        for name in READ_NODES:
            node = self.nodes[name]
            self.assertEqual((node.get("onError"), node.get("alwaysOutputData"), node.get("executeOnce")),
                             ("continueRegularOutput", True, True), name)
            self.assertEqual(len(self.workflow["connections"][name]["main"]), 1, name)
        log = self.nodes["Log Health Run"]
        self.assertEqual((log.get("onError"), log.get("alwaysOutputData")), ("continueRegularOutput", True))
        self.assertNotIn("Log Health Run", self.workflow["connections"], "the row is the last step")
        # #288 T2: a broken Judge or Dead-Man step ends in the fixed broken-check email, not a Telegram line.
        for name in (JUDGE, DEADMAN):
            self.assertEqual(self.workflow["connections"][name]["main"][1],
                             [{"node": BROKEN, "type": "main", "index": 0}], name)
        broken = self.nodes[BROKEN]
        self.assertEqual((broken["type"], broken["typeVersion"]), ("n8n-nodes-base.set", 3.4))
        for key in ("onError", "retryOnFail", "alwaysOutputData", "executeOnce"):
            self.assertNotIn(key, broken)

    def test_webhook_is_header_auth_with_a_placeholder_path_and_refuses_proxied_requests(self):
        params = self.nodes["Health Check Webhook"]["parameters"]
        self.assertEqual((params["httpMethod"], params["authentication"], params["responseMode"]),
                         ("POST", "headerAuth", "responseNode"))
        self.assertEqual(params["path"], "REPLACE_WITH_DAILY_HEALTH_WEBHOOK_PATH")
        condition = self.nodes["Request Came Through Proxy"]["parameters"]["conditions"]["conditions"][0]["leftValue"]
        for header in ("cf-ray", "cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
            self.assertIn("'%s'" % header, condition)
        self.assertEqual(self.nodes["Respond Not Found"]["parameters"],
                         {"respondWith": "noData", "options": {"responseCode": 404}})
        # The Code nodes never read request headers.
        for name in (VALIDATE, JUDGE, DEADMAN):
            self.assertNotIn("headers", self.nodes[name]["parameters"]["jsCode"], name)
        codes = {name: self.nodes[name]["parameters"]["options"]["responseCode"] for name in
                 ("Respond Event Accepted", "Respond Event Rejected", "Respond Internal Error")}
        self.assertEqual(codes, {"Respond Event Accepted": 200, "Respond Event Rejected": 400,
                                 "Respond Internal Error": 500})

    def test_n8n_api_nodes_only_read(self):
        workflows = self.nodes["List Workflows"]["parameters"]
        self.assertEqual((workflows["resource"], workflows["operation"]), ("workflow", "getAll"))
        runs = self.nodes["List Error Runs"]["parameters"]
        self.assertEqual((runs["resource"], runs["operation"], runs["filters"]), ("execution", "getAll",
                                                                                  {"status": "error"}))
        api_nodes = [n["name"] for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.n8n"]
        self.assertEqual(sorted(api_nodes), ["List Error Runs", "List Workflows"])

    def test_data_table_nodes(self):
        log_read = self.nodes["Read Alert Log"]["parameters"]
        self.assertEqual(log_read["dataTableId"]["value"], "REPLACE_WITH_ALERT_LOG_TABLE_ID")
        self.assertEqual(log_read["filters"]["conditions"], [
            {"keyName": "system", "condition": "eq", "keyValue": "MEMBER-SYNC"},
            {"keyName": "key", "condition": "eq", "keyValue": "daily_summary"}])
        for name in ("Read Health Runs", "Read Today Runs", "Log Health Run"):
            self.assertEqual(self.nodes[name]["parameters"]["dataTableId"],
                             {"__rl": True, "value": "REPLACE_WITH_HEALTH_RUNS_TABLE_ID", "mode": "id"}, name)
        insert = self.nodes["Log Health Run"]["parameters"]
        self.assertEqual((insert["resource"], insert["operation"]), ("row", "insert"))
        self.assertEqual([c["id"] for c in insert["columns"]["schema"]], RUN_COLUMNS)
        self.assertEqual(set(insert["columns"]["value"]), set(RUN_COLUMNS))
        failed = self.nodes["Read Failed Alert Emails"]["parameters"]
        self.assertEqual((failed["resource"], failed["operation"], failed["dataTableId"]["value"]),
                         ("row", "get", "REPLACE_WITH_ALERT_LOG_TABLE_ID"))
        self.assertEqual(failed["filters"]["conditions"], [
            {"keyName": "outcome", "condition": "eq", "keyValue": "send_failed"}])
        self.assertEqual((failed["orderByColumn"], failed["orderByDirection"], failed["limit"]), ("id", "DESC", 250))
        tables = {n["parameters"]["dataTableId"]["value"] for n in self.workflow["nodes"]
                  if n["type"] == "n8n-nodes-base.dataTable"}
        self.assertEqual(tables, {"REPLACE_WITH_ALERT_LOG_TABLE_ID", "REPLACE_WITH_HEALTH_RUNS_TABLE_ID",
                                  "REPLACE_WITH_MONITOR_ERROR_LOG_TABLE_ID"})
        # #288 option (c): the run monitor's failure rows, newest first, read only; the judge filters by workflow.
        error_log = self.nodes["Read Monitor Error Log"]["parameters"]
        self.assertEqual(error_log, {"resource": "row", "operation": "get", "dataTableId": {
            "__rl": True, "value": "REPLACE_WITH_MONITOR_ERROR_LOG_TABLE_ID", "mode": "id"}, "returnAll": False,
            "limit": 250, "orderBy": True, "orderByColumn": "id", "orderByDirection": "DESC"})
        # #341 fix1 LATER (b): KIT-RETAG ERROR rows of xb_alert_email_log, newest first, read only.
        kit = self.nodes["Read Kit Re-tagger Errors"]["parameters"]
        self.assertEqual(kit, dict(failed, filters={"conditions": [
            {"keyName": "system", "condition": "eq", "keyValue": "KIT-RETAG"},
            {"keyName": "severity", "condition": "eq", "keyValue": "ERROR"}]}))
        writes = [n["name"] for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.dataTable"
                  and n["parameters"]["operation"] != "get"]
        self.assertEqual(writes, ["Log Health Run"], "the alert log is only read")

    def test_sender_call_pins_health_and_never_retries(self):
        node = self.nodes["Send Health Email"]
        params = node["parameters"]
        self.assertEqual(params["workflowId"], {"__rl": True, "value": "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID",
                                                "mode": "id"})
        value = params["workflowInputs"]["value"]
        self.assertEqual((value["system"], value["key"]), ("HEALTH", "daily_health"))
        sender = load(SENDER)
        trigger = next(n for n in sender["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflowTrigger")
        self.assertEqual([(v["name"], v["type"]) for v in trigger["parameters"]["workflowInputs"]["values"]],
                         [(f["id"], f["type"]) for f in params["workflowInputs"]["schema"]])
        for key in ("retryOnFail", "maxTries", "waitBetweenTries", "continueOnFail", "alwaysOutputData"):
            self.assertNotIn(key, node)
        self.assertIs(params["options"]["waitForSubWorkflow"], True)

    def test_email_only_no_telegram_node_credential_or_placeholder(self):
        # #288 T2 (owner 19 Nov 2026: email only). The repo-wide guard is tests/test_no_telegram_nodes.py.
        self.assertNotIn("n8n-nodes-base.telegram", {n["type"] for n in self.workflow["nodes"]})
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("telegramApi", text)
        self.assertNotIn("TELEGRAM", text)
        self.assertNotIn("chatId", text)
        self.assertEqual([n for n in self.nodes if "telegram" in n.lower()], [])

    def test_dead_man_runs_at_0815_sgt(self):
        rule = self.nodes["Dead-Man Check 08:15"]["parameters"]["rule"]["interval"]
        self.assertEqual(rule, [{"field": "days", "daysInterval": 1, "triggerAtHour": 8, "triggerAtMinute": 15}])

    def test_notes_and_readme(self):
        notes = "\n".join(n["parameters"]["content"] for n in self.workflow["nodes"]
                          if n["type"] == "n8n-nodes-base.stickyNote")
        self.assertIn("Required manual configuration", notes)
        for placeholder in sorted(set(re.findall(r"REPLACE_WITH_[A-Z_]+", WORKFLOW.read_text(encoding="utf-8")))):
            if placeholder.endswith("_WORKFLOW_ID") and placeholder != "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID":
                continue  # the WATCH IDs are covered by one line naming REPLACE_WITH_..._WORKFLOW_ID
            self.assertIn(placeholder, notes)
        self.assertIn("REPLACE_WITH_..._WORKFLOW_ID", notes)
        self.assertIn("Read Failed Alert Emails", notes)
        self.assertNotIn("Telegram check", notes)
        watcher = self.nodes["Who Watches The Watcher"]["parameters"]["content"]
        for phrase in ("Monday only", "retried once", "nothing arrives at all", "up to 7 days"):
            self.assertIn(phrase, watcher)
        # #340 G2 L8 (folded into #341 fix2): the runbook text of fix1 L2, L3 and the M5 rollout note.
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for phrase in ("**Link check (fix1 L2).**",
                       "the judge checks that the monitor has `errorWorkflow` = the error log's `WATCH` ID and "
                       "`saveDataErrorExecution` = `none`",
                       "FAIL `n8n workflows: EnergyGrid monitor is no longer linked to its error log; ask Claude to check`",
                       "so it cannot fire before M5",
                       "Fix: re-apply M4 (the monitor's two settings), then read back.",
                       "a count of **250 means 250 or more** (fix1 L3)",
                       "Read back that the live monitor already has the M4 settings, or the first check will FAIL "
                       "`error_log_unlinked`."):
            self.assertIn(phrase, runbook)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("### xb_daily_health.workflow.json", readme)
        self.assertIn("[XB health]", readme)
        self.assertIn("daily_health_check.md", readme)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class ValidateTests(unittest.TestCase):
    def test_events_made_by_the_real_checker_are_accepted(self):
        events = [checker_event(), golden_inputs()]
        obs = healthy_obs()
        obs["tasks"] = None
        obs["drive_root_ok"] = False
        obs["stock_status"] = None
        events.append(checker_event(obs, pauses=[{"job": "arap_export", "until": "2026-11-20T18:00",
                                                 "reason_code": "vm_work"}]))
        obs = healthy_obs()
        obs["stock_status"]["finished_at"] = "2026-11-18T23:30:00+08:00"
        obs["arap_status"]["finished_at"] = "2026-11-18T23:01:00+08:00"
        events.append(checker_event(obs))
        self.assertEqual(events[-1]["notes"], ["vm_or_drive_down"])
        for event, out in zip(events, validate(*events)):
            self.assertIs(out["valid"], True, out)
            self.assertEqual(out["event"], event)

    def test_every_departure_from_the_contract_is_rejected(self):
        base = checker_event()

        def changed(mutate):
            event = json.loads(json.dumps(base))
            mutate(event)
            return event

        def job(event, name):
            return next(e for e in event["jobs"] if e["job"] == name)

        cases = {
            "HEALTH_BODY": [None, [], "text"],
            "HEALTH_KEYS": [changed(lambda e: e.update(host="EXAMPLE-PC")), changed(lambda e: e.pop("notes"))],
            "HEALTH_SCHEMA": [changed(lambda e: e.update(schema="xb.daily_health.v2"))],
            "HEALTH_DATE": [changed(lambda e: e.update(date="2026-02-30"))],
            "HEALTH_CHECKED_AT": [changed(lambda e: e.update(checked_at="2026-11-19T07:30:00+08:00")),
                                  changed(lambda e: e.update(checked_at="2026-11-18T07:30+08:00"))],
            "HEALTH_NOTES": [changed(lambda e: e.update(notes=["free text"])),
                             changed(lambda e: e.update(notes=["vm_or_drive_down", "vm_or_drive_down"]))],
            "HEALTH_JOBS": [changed(lambda e: e["jobs"].pop()), changed(lambda e: e.update(jobs={}))],
            "HEALTH_ENTRY_KEYS": [changed(lambda e: job(e, "checker").update(task_name="XB-DailyHealth")),
                                  changed(lambda e: job(e, "checker").pop("count"))],
            "HEALTH_JOB": [changed(lambda e: e["jobs"].reverse()),
                           changed(lambda e: e["jobs"][0].update(job="shopify_sync"))],
            "HEALTH_STATE": [changed(lambda e: job(e, "checker").update(state="ON"))],
            "HEALTH_FIGURES": [changed(lambda e: job(e, "checker").update(age_min=-1)),
                               changed(lambda e: job(e, "checker").update(exit=1.5)),
                               changed(lambda e: job(e, "checker").update(count="2")),
                               changed(lambda e: job(e, "checker").update(exit=2 ** 32)),
                               # G2 F3 (mutation M7): each upper bound, one past it.
                               changed(lambda e: job(e, "checker").update(count=10001)),
                               changed(lambda e: job(e, "checker").update(age_min=1000001))],
            "HEALTH_REASONS": [changed(lambda e: job(e, "checker").update(reasons=["free text note"],
                                                                           verdict="WARN", since="2026-11-19")),
                               changed(lambda e: job(e, "checker").update(reasons=["folder_missing"],
                                                                           verdict="FAIL", since="2026-11-19")),
                               changed(lambda e: job(e, "checker").update(reasons="previous_send_failed"))],
            "HEALTH_VERDICT": [changed(lambda e: job(e, "checker").update(verdict="WARN")),
                               changed(lambda e: job(e, "checker").update(reasons=["previous_send_failed"],
                                                                           verdict="FAIL", since="2026-11-19")),
                               changed(lambda e: job(e, "n8n_workflows").update(verdict="OK")),
                               changed(lambda e: job(e, "simba_intake").update(verdict="OK")),
                               changed(lambda e: job(e, "member_tags").update(reasons=["x"]))],
            "HEALTH_SINCE": [changed(lambda e: job(e, "checker").update(reasons=["previous_send_failed"],
                                                                         verdict="WARN", since=None)),
                             changed(lambda e: job(e, "checker").update(reasons=["previous_send_failed"],
                                                                         verdict="WARN", since="2026-11-20")),
                             changed(lambda e: job(e, "checker").update(since="2026-11-19"))],
            "HEALTH_PAUSE": [changed(lambda e: job(e, "checker").update(until="2026-11-20T18:00")),
                             changed(lambda e: job(e, "checker").update(state="paused", verdict="NA",
                                                                         until="2026-11-30T18:00",
                                                                         pause_reason="sitting")),
                             changed(lambda e: job(e, "checker").update(state="paused", verdict="NA",
                                                                         until="2026-11-19T07:00",
                                                                         pause_reason="sitting")),
                             changed(lambda e: job(e, "checker").update(state="paused", verdict="NA",
                                                                         until="2026-11-20T18:00",
                                                                         pause_reason="because"))],
        }
        flat = [(reason, body) for reason, bodies in cases.items() for body in bodies]
        for (reason, body), out in zip(flat, validate(*[b for _, b in flat])):
            with self.subTest(reason=reason, body=str(body)[:80]):
                self.assertEqual(out, {"valid": False, "reason": reason})
        # A replayed or delayed event is refused.
        (late,) = validate(base, now_ms=NOW_MS + 61 * 60 * 1000)
        self.assertEqual(late, {"valid": False, "reason": "HEALTH_STALE"})
        (results,) = run_cases([{"workflow": str(WORKFLOW), "node": VALIDATE, "now_ms": NOW_MS,
                                 "items": [{"body": base}, {"body": base}]}])
        self.assertEqual(results["out"][0]["json"], {"valid": False, "reason": "HEALTH_ITEM_COUNT"})


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class JudgeTests(unittest.TestCase):
    def test_golden_problem_day_email(self):
        plan = judge_plan(golden_inputs())
        self.assertIs(plan["send"], True)
        self.assertEqual(plan["row"], {"date": "2026-11-19", "kind": "check", "verdict": "FAIL", "watched": 12,
                                       "problems": 3, "fail_jobs": "member_sync_summary,ac2_stock_export",
                                       "warn_jobs": "drive_mirror"})
        checked, decided = through_sender(plan)
        self.assertIs(checked["valid"], True, checked)
        self.assertEqual(decided["mail"]["subject"], GOLDEN_SUBJECT)
        self.assertEqual(decided["mail"]["text"], GOLDEN_TEXT)
        self.assertRegex(decided["mail"]["text"], r"^[\x20-\x7E\n]+$")
        self.assertEqual(EMAIL_RE.findall(decided["mail"]["text"]), [])

    def test_ok_weekday_sends_nothing_and_monday_sends_the_heartbeat(self):
        plan = judge_plan(checker_event(), judge_reads())
        self.assertIs(plan["send"], False)
        self.assertEqual(plan["mail"]["short"], "OK 11 jobs 2026-11-19")
        self.assertEqual(plan["row"]["verdict"], "OK")
        monday = datetime(2026, 11, 23, 7, 30, tzinfo=SGT)
        monday_ms = int(monday.timestamp() * 1000)
        obs = healthy_obs()
        for status in ("stock_status", "arap_status"):
            for key in ("started_at", "finished_at"):
                obs[status][key] = obs[status][key].replace("2026-11-19", "2026-11-23")
        obs["stock_manifest"]["finished_at"] = obs["stock_status"]["finished_at"]
        obs["tasks"] = [dict(t, last_run=t["last_run"].replace("2026-11-18", "2026-11-22").replace("2026-11-19",
                        "2026-11-23").replace("2026-11-15", "2026-11-22")) for t in obs["tasks"]]
        event = health.judge(settings(), obs, {}, monday)
        runs = [{"date": "2026-11-%d" % d, "kind": "check", "verdict": "OK", "email": "not_due" if d != 20 else
                 "send_failed", "fail_jobs": "", "warn_jobs": ""} for d in (17, 18, 19, 20, 21, 22)]
        plan = judge_plan(event, judge_reads(runs=runs), now_ms=monday_ms)
        self.assertIs(plan["send"], True)
        self.assertEqual(plan["mail"]["severity"], "INFO")
        self.assertEqual(plan["mail"]["short"], "OK 11 jobs 2026-11-23")
        self.assertEqual(plan["mail"]["lines"][0], "XB weekly health check, Mon 23 Nov 2026 07:30 SGT: all 11 watched "
                                                   "jobs OK. 7 of 7 daily checks ran this week; 1 send failures.")
        self.assertIn("Not watched: Member sync daily summary (not live yet), Member tags (not live yet), SIMBA intake "
                      "(not live yet).", plan["mail"]["lines"])
        self.assertEqual(plan["mail"]["lines"][-1],
                         "No [XB health] email on a Monday by 09:00 means the checker is down: tell Claude.")
        checked, decided = through_sender(plan)
        self.assertEqual(decided["mail"]["subject"], "[XB health] OK 11 jobs 2026-11-23")
        # Two missed days: 5 of 7.
        plan = judge_plan(event, judge_reads(runs=[r for r in runs if r["date"] not in ("2026-11-18", "2026-11-21")]),
                          now_ms=monday_ms)
        self.assertIn("5 of 7 daily checks ran this week", plan["mail"]["lines"][0])

    def summary_event(self):
        expected = dict(json.loads((ROOT / "config" / "daily_health.example.json").read_text(encoding="utf-8"))
                        ["expected"], member_sync_summary="on")
        return checker_event(expected=expected)

    def summary_line(self, alert_log):
        plan = judge_plan(self.summary_event(), judge_reads(alert_log=alert_log))
        return plan, [line for line in plan["mail"]["lines"] if "Member sync daily summary:" in line]

    def test_member_sync_summary_rules(self):
        today_sent = {"system": "MEMBER-SYNC", "key": "daily_summary", "outcome": "sent",
                      "sent_at": "2026-11-18T22:40:05.000Z"}  # 06:40 SGT on the 19th
        plan, lines = self.summary_line([today_sent])
        self.assertEqual((plan["row"]["verdict"], lines), ("OK", []))
        yesterday = dict(today_sent, sent_at="2026-11-17T22:40:05.000Z")
        plan, lines = self.summary_line([yesterday])
        self.assertTrue(lines[0].startswith("FAIL  Member sync daily summary: the summary did not report this morning, "
                                            "since 19 Nov."))
        # #288 slice 3 errors only (owner 22 Nov 2026): a CLEAN morning is logged, not emailed. Today's
        # clean_not_sent row proves the summary ran; yesterday's does not; no other outcome counts.
        plan, lines = self.summary_line([dict(today_sent, outcome="clean_not_sent")])
        self.assertEqual((plan["row"]["verdict"], lines), ("OK", []))
        plan, lines = self.summary_line([dict(yesterday, outcome="clean_not_sent")])
        self.assertIn("the summary did not report this morning", lines[0])
        self.assertEqual(plan["row"]["fail_jobs"], "member_sync_summary")
        for other in ("folded", "folded_flood", "clean", "not_sent", "CLEAN_NOT_SENT"):
            plan, lines = self.summary_line([dict(today_sent, outcome=other)])
            self.assertIn("the summary did not report this morning", lines[0], other)
        plan, lines = self.summary_line([dict(today_sent, outcome="clean_not_sent", system="AC2-STOCK")])
        self.assertIn("the summary did not report this morning", lines[0])
        plan, lines = self.summary_line([dict(today_sent, outcome="clean_not_sent", key="run_failed")])
        self.assertIn("the summary did not report this morning", lines[0])
        plan, lines = self.summary_line([dict(today_sent, outcome="send_failed")])
        self.assertIn("the email could not be sent this morning", lines[0])
        self.assertEqual(plan["row"]["fail_jobs"], "member_sync_summary")
        plan, lines = self.summary_line({"raw": [{"json": {"error": "Data table not found"}}]})
        self.assertTrue(lines[0].startswith("WARN  Member sync daily summary: the email log could not be read"))
        # The bare echo of the previous node (a Data Table router failure) is a failed read too.
        plan, lines = self.summary_line([{"id": "fakeExecution1", "status": "error"}])
        self.assertIn("the email log could not be read", lines[0])

    def test_n8n_workflow_rules(self):
        workflow = configured_path()
        event = checker_event()

        def n8n_line(**reads):
            plan = judge_plan(event, judge_reads(**reads), workflow=workflow)
            return plan, [line for line in plan["mail"]["lines"] if "n8n workflows:" in line]

        plan, lines = n8n_line()
        self.assertEqual((plan["row"]["verdict"], lines), ("OK", []))
        plan, lines = n8n_line(workflows=[{"id": "wfStockAlert01", "active": False}, {"id": "wfOpsEmail0001",
                                                                                      "active": True}])
        self.assertTrue(lines[0].startswith("FAIL  n8n workflows: inactive: stock alert; not found: EnergyGrid run "
                                            "monitor, EnergyGrid monitor error log, since 19 Nov."), lines)
        # #288 option (c): the run monitor saves no failed executions, so saved error runs are not counted for
        # it (`errors: false`); its error workflow's rows in energygrid_monitor_error_log are.
        errors = [{"id": "e%d" % n, "workflowId": "wfEgMonitor001", "status": "error",
                   "startedAt": "2026-11-18T%02d:00:00.000Z" % n} for n in (1, 2, 3)]
        errors.append({"id": "other", "workflowId": "wfStockAlert01", "status": "error",
                       "startedAt": "2026-11-18T22:00:00.000Z"})
        plan, lines = n8n_line(error_runs=errors)
        self.assertEqual((plan["row"]["verdict"], lines), ("OK", []))

        def row(at, workflow_id="wfEgMonitor001", code="EG_MONITOR_SCHEMA"):
            return {"id": 1, "workflow_id": workflow_id, "execution_id": "4711", "failed_node": "Validate Both Streams "
                    "and Classify", "code": code, "mode": "webhook", "failed_at": at, "createdAt": at, "updatedAt": at}

        log = [row("2026-11-18T%02d:00:00.000Z" % n) for n in (1, 2)]
        log += [row("2026-11-18T03:05:00.000Z", code="EG_MONITOR_TRIGGER_FAILED"),
                row("2026-11-17T20:00:00.000Z"),  # older than 24 h before the 07:30 check
                # L1: the table is the monitor's alone, so every row in the window counts, whatever its workflow_id
                # (the reducer writes 'unknown' when the ID failed its shape check).
                row("2026-11-18T22:00:00.000Z", workflow_id="unknown"),
                row("2026-11-18T22:30:00.000Z", workflow_id="wfStockAlert01"),
                row("not a date")]
        plan, lines = n8n_line(error_log=log, error_runs=errors)
        self.assertTrue(lines[0].startswith("WARN  n8n workflows: 5 failed run(s) in the last 24 h, since 19 Nov."),
                        lines)
        self.assertEqual(plan["row"]["warn_jobs"], "n8n_workflows")
        self.assertNotIn("EG_MONITOR", "\n".join(plan["mail"]["lines"]), "only a count reaches the email")
        plan, lines = n8n_line(error_log={"raw": [{"json": {"error": "table not found"}}]})
        self.assertTrue(lines[0].startswith("WARN  n8n workflows: the monitor error log could not be read, since 19 Nov."),
                        lines)
        # SIMBA keeps its failed executions (`errors: true`): those are still counted from the n8n API.
        simba = [{"id": "s1", "workflowId": "wfSimbaIntake1", "status": "error", "startedAt": "2026-11-18T22:00:00.000Z"}]
        plan = judge_plan(checker_event(expected=dict(json.loads((ROOT / "config" / "daily_health.example.json")
                                                             .read_text(encoding="utf-8"))["expected"],
                                                      simba_intake="on")),
                          judge_reads(workflows=WORKFLOWS_ALL + [{"id": "wfSimbaIntake1", "active": True}],
                                      error_runs=simba), workflow=workflow)
        line = next(l for l in plan["mail"]["lines"] if "SIMBA intake:" in l)
        self.assertTrue(line.startswith("WARN  SIMBA intake: 1 failed run(s) in the last 24 h"), line)
        plan, lines = n8n_line(workflows={"raw": [{"json": {"error": "401 unauthorized"}}]})
        self.assertIn("the n8n API could not be read", lines[0])
        # The unconfigured export says so instead of guessing.
        plan = judge_plan(event, workflow=str(WORKFLOW))
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertTrue(line.startswith("WARN  n8n workflows: some workflow IDs are not set in XB Daily Health"), line)
        self.assertNotIn("no longer linked", line)  # L2 is gated on the error log's WATCH ID being set
        # 250 rows (the read's limit) are shown as 250: the runbook says that means 250 or more (L3).
        many = [row("2026-11-18T%02d:%02d:00.000Z" % (n // 60, n % 60)) for n in range(250)]
        plan, lines = n8n_line(error_log=many)
        self.assertTrue(lines[0].startswith("WARN  n8n workflows: 250 failed run(s) in the last 24 h"), lines)

    def test_monitor_link_drift_is_an_error_once_the_error_log_is_bound(self):
        # G2 L2 (gap 3(c)): a re-import or an editor change can clear the monitor's errorWorkflow, or turn
        # failed-run saving back on; then failures leave no row and health would stay OK. The n8n API list
        # health already reads carries each workflow's settings, so the Judge checks them.
        event = checker_event()
        unlinked = "FAIL  n8n workflows: EnergyGrid monitor is no longer linked to its error log; ask Claude to check"

        def plan_for(settings, workflow=None, **reads):
            monitor = {"id": "wfEgMonitor001", "name": "fake", "active": True}
            if settings is not None:
                monitor["settings"] = settings
            workflows = WORKFLOWS_OK + [monitor, {"id": "wfEgErrorLog01", "name": "fake", "active": True}]
            return judge_plan(event, judge_reads(workflows=workflows, **reads), workflow=workflow or configured_path())

        plan = plan_for(MONITOR_LINKED)
        self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", False))
        drifts = {
            "errorWorkflow cleared": {k: v for k, v in MONITOR_LINKED.items() if k != "errorWorkflow"},
            "errorWorkflow elsewhere": dict(MONITOR_LINKED, errorWorkflow="wfSomethingEl1"),
            "failed runs saved again": dict(MONITOR_LINKED, saveDataErrorExecution="all"),
            "save setting cleared (n8n default all)": {k: v for k, v in MONITOR_LINKED.items()
                                                       if k != "saveDataErrorExecution"},
            "no settings at all": None,
            "settings not an object": "x",
        }
        for name, settings in drifts.items():
            with self.subTest(name):
                plan = plan_for(settings)
                lines = [line for line in plan["mail"]["lines"] if "n8n workflows:" in line]
                self.assertEqual((plan["row"]["verdict"], plan["row"]["fail_jobs"], plan["send"]),
                                 ("FAIL", "n8n_workflows", True))
                self.assertTrue(lines[0].startswith(unlinked + ", since 19 Nov."), lines)
                self.assertEqual(plan["mail"]["severity"], "ERROR")
                self.assertIs(through_sender(plan)[0]["valid"], True)
        # Gated: nothing fires while the error log's WATCH ID is '' or still its placeholder (before M5) ...
        for value in ("", "REPLACE_WITH_ENERGYGRID_MONITOR_ERROR_LOG_WORKFLOW_ID"):
            with self.subTest(error_log_id=value):
                path = configured_workflow(dict(CONFIGURED, REPLACE_WITH_ENERGYGRID_MONITOR_ERROR_LOG_WORKFLOW_ID=value))
                self.addCleanup(Path(path).unlink, missing_ok=True)
                plan = plan_for(None, workflow=path)
                self.assertNotIn("no longer linked", "\n".join(plan["mail"]["lines"]))
                self.assertNotIn("n8n_workflows", plan["row"]["fail_jobs"])
        # ... nor when the monitor itself is missing (that is workflow_missing) or the API failed.
        plan = judge_plan(event, judge_reads(workflows=WORKFLOWS_OK + [{"id": "wfEgErrorLog01", "active": True}]))
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertTrue(line.startswith("FAIL  n8n workflows: not found: EnergyGrid run monitor, since"), line)
        self.assertNotIn("no longer linked", line)
        plan = judge_plan(event, judge_reads(workflows={"raw": [{"json": {"error": "401"}}]}))
        self.assertNotIn("no longer linked", "\n".join(plan["mail"]["lines"]))

    def test_kit_retagger_watch_waits_for_its_id_then_watches_it(self):
        # #341 fix1 LATER (a): the entry ships as a placeholder that is skipped, not reported, until the owner
        # binds it at the kits live step; then it is watched like any other workflow.
        code = next(n for n in load()["nodes"] if n["name"] == JUDGE)["parameters"]["jsCode"]
        self.assertIn("{ id: 'REPLACE_WITH_KIT_RETAGGER_WORKFLOW_ID', label: 'kit re-tagger', errors: false, "
                      "bind_later: true },", code)
        self.assertNotIn("REPLACE_WITH_KIT_RETAGGER_WORKFLOW_ID", CONFIGURED)
        event = checker_event()
        plan = judge_plan(event)  # every other ID set, the kit re-tagger still its placeholder
        self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", False))
        path = configured_workflow(dict(CONFIGURED, REPLACE_WITH_KIT_RETAGGER_WORKFLOW_ID="wfKitRetag0001"))
        self.addCleanup(Path(path).unlink, missing_ok=True)
        plan = judge_plan(event, workflow=path)
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertTrue(line.startswith("FAIL  n8n workflows: not found: kit re-tagger, since 19 Nov."), line)
        plan = judge_plan(event, judge_reads(workflows=WORKFLOWS_ALL + [{"id": "wfKitRetag0001", "active": False}]),
                          workflow=path)
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertTrue(line.startswith("FAIL  n8n workflows: inactive: kit re-tagger, since 19 Nov."), line)
        plan = judge_plan(event, judge_reads(workflows=WORKFLOWS_ALL + [{"id": "wfKitRetag0001", "active": True}]),
                          workflow=path)
        self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", False))
        # bind_later covers the placeholder only: any other entry left as a placeholder still says not_configured.
        path = configured_workflow(dict(CONFIGURED, REPLACE_WITH_STOCK_ALERT_WORKFLOW_ID="REPLACE_WITH_STOCK_ALERT_WORKFLOW_ID"))
        self.addCleanup(Path(path).unlink, missing_ok=True)
        line = next(l for l in judge_plan(event, workflow=path)["mail"]["lines"] if "n8n workflows:" in l)
        self.assertIn("some workflow IDs are not set in XB Daily Health", line)

    def test_kit_retagger_error_alerts_are_counted_as_a_warn(self):
        # #341 fix1 LATER (b): each kit re-tagger error path sends exactly one KIT-RETAG ERROR alert; the rows of
        # the 24 h before the check are counted under the n8n workflows job, as a WARN, never named.
        def row(at, system="KIT-RETAG", severity="ERROR", outcome="sent"):
            return {"system": system, "key": "could_not_sign_in", "severity": severity, "outcome": outcome,
                    "code": "", "folded_count": 0, "sent_at": at}

        rows = [row("2026-11-18T23:00:00.000Z"),                       # 07:00 SGT today: counted
                row("2026-11-18T00:00:00.000Z", outcome="send_failed"),  # an error that could not be mailed: counted
                row("2026-11-17T23:29:00.000Z"),                       # 24 h 1 min before: not counted
                row("2026-11-18T22:00:00.000Z", severity="WARN"),      # not an error
                row("2026-11-18T22:00:00.000Z", system="AC2-STOCK"),   # not the kit re-tagger
                row("not a date")]
        plan = judge_plan(checker_event(), judge_reads(kit_errors=rows))
        self.assertEqual((plan["row"]["verdict"], plan["row"]["warn_jobs"], plan["send"]), ("WARN", "n8n_workflows", True))
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertTrue(line.startswith("WARN  n8n workflows: 2 kit re-tagger error alert(s) in the last 24 h (see "
                                        "xb_alert_email_log), since 19 Nov."), line)
        self.assertNotIn("could_not_sign_in", "\n".join(plan["mail"]["lines"]), "only a count reaches the email")
        self.assertIs(through_sender(plan)[0]["valid"], True)
        # Nothing in the window: OK; an unreadable log: the alert-log WARN, never a silent OK, and only once.
        plan = judge_plan(checker_event(), judge_reads(kit_errors=[row("2026-11-17T20:00:00.000Z")]))
        self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", False))
        broken = {"raw": [{"json": {"error": "Data table not found"}}]}
        plan = judge_plan(checker_event(), judge_reads(kit_errors=broken))
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertTrue(line.startswith("WARN  n8n workflows: the alert email log could not be read, since"), line)
        plan = judge_plan(checker_event(), judge_reads(kit_errors=broken, failed_alerts=broken))
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertEqual(line.count("the alert email log could not be read"), 1, line)
        self.assertNotIn("more)", line)
        # Only under n8n_workflows: with that job off, kit errors change nothing.
        expected = dict(json.loads((ROOT / "config" / "daily_health.example.json").read_text(encoding="utf-8"))
                        ["expected"], n8n_workflows="off")
        plan = judge_plan(checker_event(expected=expected), judge_reads(kit_errors=rows))
        self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", False))

    def test_since_for_n8n_jobs_comes_from_the_run_log(self):
        runs = [{"date": d, "kind": "check", "verdict": "FAIL", "fail_jobs": "member_sync_summary,checker",
                 "warn_jobs": "", "email": "sent"} for d in ("2026-11-17", "2026-11-18")]
        runs.append({"date": "2026-11-16", "kind": "deadman", "verdict": "FAIL", "fail_jobs": "member_sync_summary",
                     "warn_jobs": "", "email": "sent"})
        plan = judge_plan(self.summary_event(), judge_reads(runs=runs))
        line = next(l for l in plan["mail"]["lines"] if "Member sync daily summary:" in l)
        self.assertIn(", since 17 Nov.", line)

    def test_paused_and_vm_down_lines(self):
        obs = healthy_obs()
        obs["stock_status"]["finished_at"] = "2026-11-18T23:30:00+08:00"
        obs["arap_status"]["finished_at"] = "2026-11-18T23:01:00+08:00"
        event = checker_event(obs, pauses=[{"job": "drive_mirror", "until": "2026-11-20T18:00",
                                            "reason_code": "sitting"}])
        plan = judge_plan(event)
        lines = plan["mail"]["lines"]
        self.assertIn("      Together: the AC2 VM or its Google Drive sync looks down (stock and AR/AP exports both "
                      "stopped).", lines)
        self.assertIn("Paused: Drive mirror until 20 Nov 18:00 (sitting).", lines)
        self.assertEqual(plan["mail"]["short"], "FAIL 2 jobs 2026-11-19")
        self.assertTrue(through_sender(plan)[0]["valid"])

    def test_every_reason_renders_inside_the_sender_limits(self):
        # Every checker-side reason at once, at its largest figures, on every job: still one valid request.
        event = checker_event()
        for e in event["jobs"]:
            if e["job"] in health.REASONS and e["state"] == "on":
                e["reasons"] = list(health.REASONS[e["job"]])[:6]
                e["verdict"] = "FAIL" if any(health.REASONS[e["job"]][r] == "FAIL" for r in e["reasons"]) else "WARN"
                e["since"] = "2026-11-05"
                e.update(age_min=1000000, exit=4294967295, count=10000)
        event["notes"] = ["vm_or_drive_down"]
        runs = [{"date": "2026-11-18", "kind": "check", "verdict": "FAIL", "email": "send_failed", "fail_jobs": "",
                 "warn_jobs": ""}, {"date": "2026-11-18", "kind": "retry", "verdict": "FAIL", "email": "call_failed",
                                    "fail_jobs": "", "warn_jobs": ""}]
        plan = judge_plan(event, judge_reads(workflows={"raw": [{"json": {"error": "x"}}]},
                                             alert_log={"raw": [{"json": {"error": "x"}}]},
                                             failed_alerts={"raw": [{"json": {"error": "x"}}]},
                                             error_log={"raw": [{"json": {"error": "x"}}]},
                                             kit_errors={"raw": [{"json": {"error": "x"}}]}, runs=runs))
        self.assertIn("Yesterday's health email could not be sent (send_failed at 07:30, call_failed at 08:15).",
                      plan["mail"]["lines"])
        self.assertLessEqual(len(plan["mail"]["lines"]), 30)
        for line in plan["mail"]["lines"]:
            self.assertLessEqual(len(line), 300)
            self.assertRegex(line, r"^[\x20-\x7E]*$")
        checked, decided = through_sender(plan)
        self.assertIs(checked["valid"], True, checked)
        self.assertTrue(decided["mail"]["subject"].startswith("[XB health] FAIL "))

    def test_every_reason_has_a_sentence_and_every_job_a_label(self):
        code = next(n for n in load()["nodes"] if n["name"] == JUDGE)["parameters"]["jsCode"]
        for job, reasons in health.REASONS.items():
            for reason in reasons:
                self.assertRegex(code, r"\n  %s: " % reason, reason)
        for reason in ("summary_missing", "summary_send_failed", "log_read_failed", "workflow_inactive",
                       "workflow_missing", "error_runs", "n8n_api_failed", "not_configured", "alert_email_failed",
                       "alert_log_read_failed", "error_log_read_failed", "error_log_unlinked", "kit_retag_errors"):
            self.assertRegex(code, r"\n  %s: " % reason, reason)
        for job in health.JOBS:
            self.assertIn(" %s: '" % job, code, job)


    def test_validate_and_checker_hold_the_same_closed_job_and_reason_lists(self):
        # #321 L3: the checker event changed, so both closed lists change together.
        code = next(n for n in load()["nodes"] if n["name"] == VALIDATE)["parameters"]["jsCode"]
        jobs = code[code.index("const JOBS = ["):code.index("];", code.index("const JOBS = ["))]
        self.assertEqual(re.findall(r"'([a-z0-9_]+)'", jobs), list(health.JOBS))
        n8n_jobs = code[code.index("const N8N_JOBS = ["):code.index("];", code.index("const N8N_JOBS = ["))]
        self.assertEqual(re.findall(r"'([a-z0-9_]+)'", n8n_jobs), list(health.N8N_JOBS))
        block = code[code.index("const REASONS = {"):code.index("};", code.index("const REASONS = {"))]
        found = {}
        for job, body in re.findall(r"\n  ([a-z0-9_]+): \{([^}]*)\}", block):
            found[job] = {reason: {"F": "FAIL", "W": "WARN"}[level] for reason, level in re.findall(r"([a-z_]+): ([FW])", body)}
        self.assertEqual(found, health.REASONS)

    def test_ap_alert_route_line(self):
        obs = healthy_obs()
        obs["arap_status"]["alert_status"] = "send_failed"
        plan = judge_plan(checker_event(obs))
        self.assertIs(plan["send"], True)
        self.assertEqual(plan["row"]["warn_jobs"], "arap_alert_route")
        line = next(l for l in plan["mail"]["lines"] if "AR/AP alert route:" in l)
        self.assertEqual(line, "WARN  AR/AP alert route: the last alert could not be delivered to n8n, since 19 Nov. Next: "
                               "forward this email to Claude.")
        checked, _ = through_sender(plan)
        self.assertIs(checked["valid"], True, checked)
        self.assertIn("label: 'AR/AP alert'", next(n for n in load()["nodes"] if n["name"] == JUDGE)["parameters"]["jsCode"])

    def test_alert_email_failed_counts_send_failed_rows_of_every_system_in_the_last_24_h(self):
        # #288 Q4: under the n8n workflows job; counted, never named (no system name reaches the line).
        def row(system, at, outcome="send_failed"):
            return {"system": system, "key": "k", "outcome": outcome, "code": "gmail_error", "sent_at": at}

        rows = [row("AC2-STOCK", "2026-11-18T23:00:00.000Z"),          # 07:00 SGT today: counted
                row("AC2-ARAP", "2026-11-18T00:00:00.000Z"),           # 08:00 SGT yesterday: counted
                row("ENERGYGRID", "2026-11-17T23:29:00.000Z"),         # 24 h 1 min before: not counted
                row("HEALTH", "2026-11-18T08:00:00.000Z"),             # yesterday's failed health email: counted
                row("MEMBER-SYNC", "2026-11-18T22:40:00.000Z", "sent"),  # not a failure
                row("AC2-STOCK", "not a date")]
        plan = judge_plan(checker_event(), judge_reads(failed_alerts=rows))
        self.assertIs(plan["send"], True)
        self.assertEqual((plan["row"]["verdict"], plan["row"]["warn_jobs"]), ("WARN", "n8n_workflows"))
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertTrue(line.startswith("WARN  n8n workflows: 3 alert email(s) could not be sent in the last 24 h (see "
                                        "xb_alert_email_log), since 19 Nov."), line)
        checked, _ = through_sender(plan)
        self.assertIs(checked["valid"], True, checked)
        # No failed row in the window: nothing; an unreadable log: its own WARN, never a silent OK.
        plan = judge_plan(checker_event(), judge_reads(failed_alerts=[row("AC2-STOCK", "2026-11-17T20:00:00.000Z")]))
        self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", False))
        plan = judge_plan(checker_event(), judge_reads(failed_alerts={"raw": [{"json": {"error": "Data table not found"}}]}))
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertIn("the alert email log could not be read", line)
        # Only under n8n_workflows: with that job off, a failed alert changes nothing.
        expected = dict(json.loads((ROOT / "config" / "daily_health.example.json").read_text(encoding="utf-8"))
                        ["expected"], n8n_workflows="off")
        plan = judge_plan(checker_event(expected=expected), judge_reads(failed_alerts=rows))
        self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", False))

    def yesterday_plan(self, rows, event=None):
        return judge_plan(event or checker_event(), judge_reads(runs=rows))

    def test_yesterdays_unsent_email_forces_todays_email_with_one_line(self):
        def r(kind, email, day="2026-11-18"):
            return {"date": day, "kind": kind, "verdict": "WARN", "email": email, "fail_jobs": "", "warn_jobs": "drive_mirror"}

        cases = [
            ([r("check", "send_failed"), r("retry", "send_failed")],
             "Yesterday's health email could not be sent (send_failed at 07:30 and 08:15)."),
            ([r("check", "send_failed")],
             "Yesterday's health email could not be sent (send_failed at 07:30; the 08:15 retry did not run)."),
            ([r("check", "rejected"), r("retry", "send_failed")],
             "Yesterday's health email could not be sent (rejected at 07:30, send_failed at 08:15)."),
            ([r("check", "call_failed"), r("retry", "Free <b>text</b>")],
             "Yesterday's health email could not be sent (call_failed at 07:30, unknown at 08:15)."),
        ]
        for rows, line in cases:
            with self.subTest(line=line):
                plan = self.yesterday_plan(rows)
                # An OK Thursday: sent anyway, and the line comes right after the first line.
                self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", True))
                self.assertEqual(plan["mail"]["lines"][1], line)
                self.assertEqual(sum(1 for l in plan["mail"]["lines"] if l.startswith("Yesterday")), 1)
                self.assertTrue(through_sender(plan)[0]["valid"])
        # A problem day carries it under the first line too.
        plan = self.yesterday_plan(cases[0][0], golden_inputs())
        self.assertEqual(plan["mail"]["lines"][1], cases[0][1])
        self.assertEqual(plan["mail"]["lines"][2], "")

    def test_no_yesterday_line_when_it_went_out_or_was_not_due(self):
        def r(kind, email, day="2026-11-18"):
            return {"date": day, "kind": kind, "verdict": "OK", "email": email, "fail_jobs": "", "warn_jobs": ""}

        quiet = [
            [],                                                    # no row for yesterday
            [r("check", "not_due")],                               # an OK weekday
            [r("check", "sent")],
            [r("check", "send_failed"), r("retry", "sent")],       # the retry worked
            [r("check", "send_failed", day="2026-11-17")],         # the day before yesterday
            [r("deadman", "send_failed")],                         # not the due 07:30 email
        ]
        for rows in quiet:
            with self.subTest(rows=rows):
                plan = self.yesterday_plan(rows)
                self.assertEqual((plan["row"]["verdict"], plan["send"]), ("OK", False))
                self.assertFalse(any(l.startswith("Yesterday") for l in plan["mail"]["lines"]))
        # An unreadable run log gives no line (and no guess).
        plan = self.yesterday_plan({"raw": [{"json": {"error": "x"}}]})
        self.assertFalse(any(l.startswith("Yesterday") for l in plan["mail"]["lines"]))

    def test_ok_email_stays_monday_only(self):
        # Owner Q3 = (i), 20 Nov 2026: no daily OK heartbeat. Every weekday of a healthy week but Monday is silent.
        sends = {}
        for day in range(17, 24):
            now = datetime(2026, 11, day, 7, 30, tzinfo=SGT)
            obs = healthy_obs()
            for status in ("stock_status", "arap_status"):
                for key in ("started_at", "finished_at"):
                    obs[status][key] = obs[status][key].replace("2026-11-19", now.strftime("%Y-%m-%d"))
            obs["stock_manifest"]["finished_at"] = obs["stock_status"]["finished_at"]
            obs["arap_manifest"]["finished_at"] = obs["arap_status"]["finished_at"]
            previous = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            obs["tasks"] = [dict(t, last_run=previous + t["last_run"][10:]) if t["name"] != "XB-DailyHealth"
                            else dict(t, last_run=now.strftime("%Y-%m-%d") + "T07:30") for t in obs["tasks"]]
            event = health.judge(settings(), obs, {}, now)
            plan = judge_plan(event, now_ms=int(now.timestamp() * 1000))
            self.assertEqual(plan["row"]["verdict"], "OK", (day, plan["mail"]["lines"]))
            sends[now.strftime("%a")] = plan["send"]
        self.assertEqual(sends, {"Tue": False, "Wed": False, "Thu": False, "Fri": False, "Sat": False, "Sun": False,
                                 "Mon": True})


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class DeadManTests(unittest.TestCase):
    AT = int(datetime(2026, 11, 20, 8, 15, tzinfo=SGT).timestamp() * 1000)

    def decide(self, items):
        (result,) = run_cases([{"workflow": str(WORKFLOW), "node": DEADMAN, "items": items, "now_ms": self.AT}])
        self.assertTrue(result["ok"], result)
        return result["out"]

    def test_a_check_row_for_today_means_silence(self):
        self.assertEqual(self.decide([{"date": "2026-11-20", "kind": "check", "verdict": "OK"}]), [])

    def test_no_report_today_mails_one_fail(self):
        for items in ([{}], [{"date": "2026-11-19", "kind": "check"}], [{"date": "2026-11-20", "kind": "deadman"}]):
            (item,) = self.decide(items)
            plan = item["json"]
            self.assertEqual(plan["mail"]["short"], "FAIL 1 jobs 2026-11-20")
            self.assertEqual(plan["mail"]["lines"][2], "FAIL  Health checker: it did not report today (server task, PC "
                             "asleep, or webhook), since 20 Nov. Next: forward this email to Claude.")
            self.assertEqual(plan["row"], {"date": "2026-11-20", "kind": "deadman", "verdict": "FAIL", "watched": 1,
                                           "problems": 1, "fail_jobs": "checker", "warn_jobs": ""})
            checked, decided = through_sender(plan)
            self.assertEqual(decided["mail"]["subject"], "[XB health] FAIL 1 jobs 2026-11-20")

    def test_an_unreadable_run_log_still_mails(self):
        (item,) = self.decide({"raw": [{"json": {"error": "Data table not found"}}]})
        self.assertIn("could not be read", item["json"]["mail"]["lines"][2])

    @staticmethod
    def check(email, **changes):
        row = {"date": "2026-11-20", "kind": "check", "verdict": "FAIL", "watched": 12, "problems": 3,
               "fail_jobs": "member_sync_summary,ac2_stock_export", "warn_jobs": "drive_mirror", "email": email}
        row.update(changes)
        return row

    def test_a_failed_0730_email_is_retried_once_at_0815(self):
        # #288 T2: the retry fires exactly when today's due email did not go out.
        for email in SEND_FAILURES:
            with self.subTest(email=email):
                (item,) = self.decide([self.check(email)])
                plan = item["json"]
                self.assertIs(plan["send"], True)
                self.assertEqual(plan["mail"], {"severity": "ERROR", "short": "FAIL 3 jobs 2026-11-20", "lines": [
                    "The 07:30 health email could not be sent (%s). This is the 08:15 retry." % email,
                    "",
                    "FAIL  Member sync daily summary, AC2 hourly stock export.",
                    "WARN  Drive mirror.",
                    "",
                    "Full detail: health_2026-11-20.json on the server PC.",
                    "No [XB health] email on a Monday by 09:00 means the checker is down: tell Claude.",
                ]})
                self.assertEqual(plan["row"], {"date": "2026-11-20", "kind": "retry", "verdict": "FAIL", "watched": 12,
                                               "problems": 3, "fail_jobs": "member_sync_summary,ac2_stock_export",
                                               "warn_jobs": "drive_mirror"})
                checked, decided = through_sender(plan)
                self.assertIs(checked["valid"], True, checked)
                self.assertEqual(decided["mail"]["subject"], "[XB health] FAIL 3 jobs 2026-11-20")

    def test_never_for_sent_or_not_due_and_never_twice(self):
        quiet = [
            [self.check("sent")],
            [self.check("not_due", verdict="OK", problems=0, fail_jobs="", warn_jobs="")],
            [self.check("folded")],
            # A re-run that went out after a failed one.
            [self.check("sent"), self.check("send_failed")],
            # Already retried today (whatever its outcome): never twice.
            [{"date": "2026-11-20", "kind": "retry", "email": "send_failed"}, self.check("send_failed")],
            [{"date": "2026-11-20", "kind": "retry", "email": "sent"}, self.check("send_failed")],
        ]
        for rows in quiet:
            with self.subTest(rows=[(r["kind"], r["email"]) for r in rows]):
                self.assertEqual(self.decide(rows), [])
        # Yesterday's failed email is the Judge's next-morning line, not an 08:15 retry today.
        (item,) = self.decide([self.check("send_failed", date="2026-11-19")])
        self.assertEqual(item["json"]["row"]["kind"], "deadman")

    def test_the_retry_of_a_monday_heartbeat_and_of_a_broken_check(self):
        (item,) = self.decide([self.check("send_failed", verdict="OK", watched=11, problems=0, fail_jobs="",
                                          warn_jobs="")])
        plan = item["json"]
        self.assertEqual((plan["mail"]["severity"], plan["mail"]["short"]), ("INFO", "OK 11 jobs 2026-11-20"))
        self.assertEqual(plan["mail"]["lines"][2], "OK    all 11 watched jobs, Fri 20 Nov 2026 08:15 SGT.")
        self.assertTrue(through_sender(plan)[0]["valid"])
        (item,) = self.decide([self.check("send_failed", watched=1, problems=1, fail_jobs="checker", warn_jobs="")])
        self.assertEqual(item["json"]["mail"]["short"], "FAIL 1 jobs 2026-11-20")
        self.assertIn("FAIL  Health checker.", item["json"]["mail"]["lines"])

    def test_row_values_reach_the_retry_only_as_closed_labels_and_counts(self):
        (item,) = self.decide([self.check("send_failed", verdict="<b>BAD</b>", watched="12", problems=-4,
                                          fail_jobs="ac2_stock_export,Free Text Here", warn_jobs=None)])
        plan = item["json"]
        self.assertEqual(plan["mail"]["short"], "FAIL 1 jobs 2026-11-20")
        self.assertEqual(plan["row"], {"date": "2026-11-20", "kind": "retry", "verdict": "FAIL", "watched": 0,
                                       "problems": 0, "fail_jobs": "ac2_stock_export", "warn_jobs": ""})
        text = "\n".join(plan["mail"]["lines"])
        self.assertNotIn("Free Text", text)
        self.assertNotIn("<b>", text)
        self.assertTrue(through_sender(plan)[0]["valid"])
        # A verdict with no readable job ids still says how many.
        (item,) = self.decide([self.check("send_failed", fail_jobs="", warn_jobs="")])
        self.assertIn("FAIL  3 job(s); see the record.", item["json"]["mail"]["lines"])


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to evaluate the expressions")
class ExpressionTests(unittest.TestCase):
    def evaluate(self, node, name, cases):
        workflow = load()
        params = next(n for n in workflow["nodes"] if n["name"] == node)["parameters"]
        expression = next(a["value"] for a in params["assignments"]["assignments"] if a["name"] == name)
        self.assertTrue(expression.startswith("={{ ") and expression.endswith(" }}"))
        script = "const cases = %s; process.stdout.write(JSON.stringify(cases.map(($json) => (%s))));" % (
            json.dumps(cases), expression[4:-3])
        done = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_collect_send_outcome_is_closed(self):
        cases = [{"ok": True, "outcome": "sent", "code": ""}, {"ok": False, "outcome": "send_failed", "code": "gmail_error"},
                 {"ok": False, "outcome": "rejected", "code": "OPS_ALERT_SHORT"}, {"error": "Workflow does not exist"},
                 {"send": False, "mail": {}, "row": {}}, {"outcome": "Something <b>odd</b>", "code": "Free Text!"},
                 {"ok": False, "outcome": "error", "code": "x" * 41}]
        self.assertEqual(self.evaluate("Collect Send Outcome", "outcome", cases),
                         ["sent", "send_failed", "rejected", "call_failed", "not_due", "unknown", "error"])
        # G2 L1: the sender's uppercase OPS_ALERT_* codes are kept; free text and over-long codes are not.
        self.assertEqual(self.evaluate("Collect Send Outcome", "code", cases),
                         ["", "gmail_error", "OPS_ALERT_SHORT", "", "", "", ""])

    def broken_check(self, previous, now):
        """Evaluate the Build Broken-Check Email assignments with $prevNode and the clock stubbed."""

        params = next(n for n in load()["nodes"] if n["name"] == BROKEN)["parameters"]
        values = {a["name"]: a for a in params["assignments"]["assignments"]}
        self.assertEqual({name: a["type"] for name, a in values.items()},
                         {"send": "boolean", "mail": "object", "row": "object"})
        self.assertIs(values["send"]["value"], True)
        out = {"send": True}
        for name in ("mail", "row"):
            expression = values[name]["value"]
            self.assertTrue(expression.startswith("={{ ") and expression.endswith(" }}"), name)
            self.assertNotIn("$json", expression)
            script = ("Date.now = () => %d; const $prevNode = { name: %s };"
                      " process.stdout.write(JSON.stringify((%s)));") % (
                int(now.timestamp() * 1000), json.dumps(previous), expression[4:-3])
            done = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            out[name] = json.loads(done.stdout)
        return out

    def test_broken_check_email_is_fixed_and_passes_the_sender(self):
        # #288 T2: replaces the old "Telegram Check Broken" line. Row kind `check` from the 07:30 path (so the
        # 08:15 dead-man does not mail twice), `deadman` from the 08:15 path.
        at_0730 = datetime(2026, 11, 20, 7, 30, tzinfo=SGT)
        plan = self.broken_check(JUDGE, at_0730)
        self.assertEqual(plan["mail"], {"severity": "ERROR", "short": "FAIL 1 jobs 2026-11-20", "lines": [
            "XB daily health check, Fri 20 Nov 2026: the health check could not be built today.",
            "",
            "FAIL  Health checker: n8n XB Daily Health failed before the email. Next: forward this email to Claude.",
            "",
            "No [XB health] email on a Monday by 09:00 means the checker is down: tell Claude.",
        ]})
        self.assertEqual(plan["row"], {"date": "2026-11-20", "kind": "check", "verdict": "FAIL", "watched": 1,
                                       "problems": 1, "fail_jobs": "checker", "warn_jobs": ""})
        checked, decided = through_sender(plan)
        self.assertIs(checked["valid"], True, checked)
        self.assertEqual(decided["mail"]["subject"], "[XB health] FAIL 1 jobs 2026-11-20")
        plan = self.broken_check(DEADMAN, datetime(2026, 11, 2, 8, 15, tzinfo=SGT))
        self.assertEqual(plan["row"]["kind"], "deadman")
        self.assertEqual(plan["mail"]["short"], "FAIL 1 jobs 2026-11-02")
        self.assertEqual(plan["mail"]["lines"][0],
                         "XB daily health check, Mon 2 Nov 2026: the health check could not be built today.")
        # The SGT date, not the UTC one, just after midnight SGT.
        plan = self.broken_check(JUDGE, datetime(2026, 11, 21, 0, 5, tzinfo=SGT))
        self.assertEqual(plan["row"]["date"], "2026-11-21")

    def test_the_run_ends_at_the_row_for_every_outcome(self):
        # No fallback node after the row: a failed email is retried by the 08:15 path instead.
        inputs = [{"ok": True, "outcome": "sent", "code": ""}, {"ok": False, "outcome": "send_failed", "code": "gmail_error"},
                  {"error": "Workflow does not exist"}, {"send": False, "mail": {}, "row": {}}]
        self.assertEqual(self.evaluate("Collect Send Outcome", "outcome", inputs),
                         ["sent", "send_failed", "call_failed", "not_due"])
        self.assertEqual(load()["connections"]["Collect Send Outcome"],
                         {"main": [[{"node": "Log Health Run", "type": "main", "index": 0}]]})
        self.assertNotIn("Log Health Run", load()["connections"])


class OwnerNextTests(unittest.TestCase):
    """#288 (22 Nov 2026): the owner's only action is to tell Claude. Every WARN and FAIL line the owner reads
    ends with the same fixed Next; no line sends the owner to n8n, the server PC, the VM or a runbook."""

    FORWARD = "Next: forward this email to Claude."

    def test_every_problem_line_ends_with_forward_to_claude(self):
        obs = healthy_obs()
        obs["arap_status"]["alert_status"] = "send_failed"
        lines = judge_plan(checker_event(obs))["mail"]["lines"]
        lines += GOLDEN_TEXT.splitlines()
        problems = [line for line in lines if line.startswith(("WARN  ", "FAIL  "))]
        self.assertGreaterEqual(len(problems), 4)
        for line in problems:
            self.assertTrue(line.endswith(". " + self.FORWARD), line)
            self.assertEqual(line.count("Next:"), 1, line)

    def test_every_job_gets_the_same_next(self):
        code = next(n for n in load()["nodes"] if n["name"] == JUDGE)["parameters"]["jsCode"]
        self.assertIn("const NEXT = 'forward this email to Claude.';", code)
        self.assertIn("+ '. Next: ' + NEXT);", code)
        self.assertNotIn("NEXT[", code)

    def test_no_next_sends_the_owner_anywhere_else(self):
        text = json.dumps(load())
        nexts = re.findall(r"Next: [^'\\\"]*", text)
        self.assertEqual(sorted(set(nexts)), ["Next: ", self.FORWARD])
        self.assertEqual(text.count(self.FORWARD), 2)  # Decide Dead-Man and Build Broken-Check Email
        for stale in ("open n8n", "Next: check", "Next: nothing", "Next: see", "Next: add"):
            self.assertNotIn(stale, text)

    def test_runbook_keeps_claudes_first_look(self):
        doc = (ROOT / "docs" / "daily_health_check.md").read_text(encoding="utf-8")
        start = doc.index("## When the owner forwards a health email")
        section = doc[start:doc.index("\n## ", start + 1)]
        self.assertIn("`Next: forward this email to Claude.`", section)
        code = next(n for n in load()["nodes"] if n["name"] == JUDGE)["parameters"]["jsCode"]
        labels = code[code.index("const LABELS = {"):code.index("};", code.index("const LABELS = {"))]
        jobs = re.findall(r"(\w+): '", labels)
        self.assertEqual(len(jobs), 14)
        for job in jobs:
            self.assertIn("| `%s` |" % job, section)


GOLDEN_TEXT = "\n".join([
    "XB daily health check, Thu 19 Nov 2026 07:30 SGT: 3 of 12 watched jobs need a look (2 FAIL, 1 WARN).",
    "",
    "FAIL  Member sync daily summary: the summary did not report this morning, since 19 Nov. Next: forward this "
    "email to Claude.",
    "FAIL  AC2 hourly stock export: no new run for 8 h (last 18 Nov 23:30), since 19 Nov. Next: forward this email "
    "to Claude.",
    "WARN  Drive mirror: the last weekly run could not reach GitHub (FETCH_FAILED), since 16 Nov. Next: forward this "
    "email to Claude.",
    "",
    "OK    9 other jobs: Stock alert route, AR/AP export, AR/AP alert route, EnergyGrid daily bills, EnergyGrid "
    "retention, n8n workflows, Server task list, Health checker, Drive folder.",
    "Paused: none.",
    "Not watched: Member tags (not live yet), SIMBA intake (not live yet).",
    "",
    "Codes and counts only. Record: health_2026-11-19.json on the server PC.",
    "No [XB health] email on a Monday by 09:00 means the checker is down: tell Claude.",
    "",
    "--",
    "System: HEALTH | Daily health check",
    "Times are SGT (UTC+08:00). Daily health emails are never folded and do not count toward the alert flood cap.",
    "Log: n8n Data Table xb_alert_email_log. Contract: n8n-workflows/README.md, ops_alert_email.workflow.json.",
])


if __name__ == "__main__":
    unittest.main()
