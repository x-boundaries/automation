"""Offline contract of the inactive "XB Daily Health" n8n workflow (#325 H1), and the golden email.

No n8n instance, n8n API, Data Table, Gmail or Telegram is contacted. The committed Code nodes are
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
SGT = health.SGT
NOW = datetime(2026, 11, 19, 7, 30, tzinfo=SGT)
NOW_MS = int(NOW.timestamp() * 1000)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_TYPES = {
    "n8n-nodes-base.webhook", "n8n-nodes-base.if", "n8n-nodes-base.respondToWebhook", "n8n-nodes-base.code",
    "n8n-nodes-base.n8n", "n8n-nodes-base.dataTable", "n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.noOp",
    "n8n-nodes-base.executeWorkflow", "n8n-nodes-base.set", "n8n-nodes-base.telegram", "n8n-nodes-base.stickyNote",
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
    ("Read Alert Log", 0, "Read Health Runs"),
    ("Read Health Runs", 0, JUDGE),
    (JUDGE, 0, "Email Plan"),
    (JUDGE, 1, "Telegram Check Broken"),
    ("Dead-Man Check 08:15", 0, "Read Today Runs"),
    ("Read Today Runs", 0, DEADMAN),
    (DEADMAN, 0, "Email Plan"),
    (DEADMAN, 1, "Telegram Check Broken"),
    ("Email Plan", 0, "Email Is Due"),
    ("Email Is Due", 0, "Send Health Email"),
    ("Email Is Due", 1, "Collect Send Outcome"),
    ("Send Health Email", 0, "Collect Send Outcome"),
    ("Send Health Email", 1, "Collect Send Outcome"),
    ("Collect Send Outcome", 0, "Log Health Run"),
    ("Log Health Run", 0, "Email Failed"),
    ("Email Failed", 0, "Send Telegram Fallback"),
}
READ_NODES = ("List Workflows", "List Error Runs", "Read Alert Log", "Read Health Runs", "Read Today Runs")
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
WORKFLOWS_ALL = WORKFLOWS_OK + [{"id": "wfEgMonitor001", "name": "fake", "active": True}]


def judge_reads(workflows=None, error_runs=None, alert_log=None, runs=None):
    return {
        "List Workflows": WORKFLOWS_ALL if workflows is None else workflows,
        "List Error Runs": [{}] if error_runs is None else error_runs,
        "Read Alert Log": [{}] if alert_log is None else alert_log,
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
    "REPLACE_WITH_ENERGYGRID_MONITOR_WORKFLOW_ID": "wfEgMonitor001",
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
        for name in ("Send Telegram Fallback", "Telegram Check Broken"):
            self.assertEqual(self.nodes[name].get("onError"), "continueRegularOutput", name)
            self.assertIs(self.nodes[name].get("retryOnFail"), False, name)

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
        tables = {n["parameters"]["dataTableId"]["value"] for n in self.workflow["nodes"]
                  if n["type"] == "n8n-nodes-base.dataTable"}
        self.assertEqual(tables, {"REPLACE_WITH_ALERT_LOG_TABLE_ID", "REPLACE_WITH_HEALTH_RUNS_TABLE_ID"})
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

    def test_telegram_lines_are_fixed_and_go_to_the_placeholder_chat(self):
        for name in ("Send Telegram Fallback", "Telegram Check Broken"):
            params = self.nodes[name]["parameters"]
            self.assertEqual(params["chatId"], "REPLACE_WITH_OWNER_TELEGRAM_CHAT_ID")
            self.assertEqual(params["additionalFields"], {"appendAttribution": False})
            self.assertNotIn("$json.mail.lines", params["text"])
        self.assertIn("Check the n8n Gmail credential", self.nodes["Send Telegram Fallback"]["parameters"]["text"])
        self.assertNotIn("{{", self.nodes["Telegram Check Broken"]["parameters"]["text"])

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
        self.assertEqual(plan["row"], {"date": "2026-11-19", "kind": "check", "verdict": "FAIL", "watched": 11,
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
        self.assertEqual(plan["mail"]["short"], "OK 10 jobs 2026-11-19")
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
        self.assertEqual(plan["mail"]["short"], "OK 10 jobs 2026-11-23")
        self.assertEqual(plan["mail"]["lines"][0], "XB weekly health check, Mon 23 Nov 2026 07:30 SGT: all 10 watched "
                                                   "jobs OK. 7 of 7 daily checks ran this week; 1 send failures.")
        self.assertIn("Not watched: Member sync daily summary (not live yet), Member tags (not live yet), SIMBA intake "
                      "(not live yet).", plan["mail"]["lines"])
        self.assertEqual(plan["mail"]["lines"][-1],
                         "No [XB health] email on a Monday by 09:00 means the checker is down: tell Claude.")
        checked, decided = through_sender(plan)
        self.assertEqual(decided["mail"]["subject"], "[XB health] OK 10 jobs 2026-11-23")
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
        self.assertTrue(lines[0].startswith("FAIL  Member sync daily summary: no email this morning, since 19 Nov."))
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
                                            "monitor, since 19 Nov."), lines)
        errors = [{"id": "e%d" % n, "workflowId": "wfEgMonitor001", "status": "error",
                   "startedAt": "2026-11-18T%02d:00:00.000Z" % n} for n in (1, 2, 3)]
        errors += [{"id": "old", "workflowId": "wfEgMonitor001", "status": "error", "startedAt": "2026-11-17T20:00:00.000Z"},
                   {"id": "other", "workflowId": "wfStockAlert01", "status": "error", "startedAt": "2026-11-18T22:00:00.000Z"}]
        plan, lines = n8n_line(workflows=WORKFLOWS_OK + [{"id": "wfEgMonitor001", "active": True}], error_runs=errors)
        self.assertTrue(lines[0].startswith("WARN  n8n workflows: 3 failed run(s) in the last 24 h, since 19 Nov."),
                        lines)
        plan, lines = n8n_line(workflows={"raw": [{"json": {"error": "401 unauthorized"}}]})
        self.assertIn("the n8n API could not be read", lines[0])
        # The unconfigured export says so instead of guessing.
        plan = judge_plan(event, workflow=str(WORKFLOW))
        line = next(l for l in plan["mail"]["lines"] if "n8n workflows:" in l)
        self.assertTrue(line.startswith("WARN  n8n workflows: some workflow IDs are not set in XB Daily Health"), line)

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
        plan = judge_plan(event, judge_reads(workflows={"raw": [{"json": {"error": "x"}}]},
                                             alert_log={"raw": [{"json": {"error": "x"}}]}))
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
                       "workflow_missing", "error_runs", "n8n_api_failed", "not_configured"):
            self.assertRegex(code, r"\n  %s: " % reason, reason)
        for job in health.JOBS:
            self.assertIn(" %s: '" % job, code, job)


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
                             "asleep, or webhook), since 20 Nov. Next: check the server PC is on and Task Scheduler "
                             "\"XB-DailyHealth\" (runbook: daily_health_check.md).")
            self.assertEqual(plan["row"], {"date": "2026-11-20", "kind": "deadman", "verdict": "FAIL", "watched": 1,
                                           "problems": 1, "fail_jobs": "checker", "warn_jobs": ""})
            checked, decided = through_sender(plan)
            self.assertEqual(decided["mail"]["subject"], "[XB health] FAIL 1 jobs 2026-11-20")

    def test_an_unreadable_run_log_still_mails(self):
        (item,) = self.decide({"raw": [{"json": {"error": "Data table not found"}}]})
        self.assertIn("could not be read", item["json"]["mail"]["lines"][2])


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

    def evaluate_with_outcome(self, node, field, outcomes, path):
        """Evaluate one expression of `node` with $('Collect Send Outcome') and $('Email Plan') stubbed."""

        params = next(n for n in load()["nodes"] if n["name"] == node)["parameters"]
        expression = path(params)
        self.assertTrue(expression.startswith("={{ ") and expression.endswith(" }}"), field)
        plan = {"row": {"date": "2026-11-19"}, "mail": {"short": "FAIL 2 jobs 2026-11-19"}}
        script = ("const outcomes = %s; const plan = %s; process.stdout.write(JSON.stringify(outcomes.map((outcome) => {"
                  " const $ = (name) => ({ first: () => ({ json: name === 'Collect Send Outcome' ? { outcome, code: '' }"
                  " : (name === 'Email Plan' ? plan : (() => { throw new Error('unexpected ' + name); })()) }) });"
                  " return (%s); })));") % (json.dumps(outcomes), json.dumps(plan), expression[4:-3])
        done = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_telegram_fallback_fires_exactly_when_a_due_email_did_not_go_out(self):
        # G2 F1 (mutations M8, M16, M17): the Email Failed condition itself, not only its wiring.
        fires = ["send_failed", "rejected", "error", "call_failed", "unknown"]
        quiet = ["sent", "not_due", "folded", "folded_flood"]
        result = self.evaluate_with_outcome(
            "Email Failed", "condition", fires + quiet,
            lambda p: p["conditions"]["conditions"][0]["leftValue"])
        self.assertEqual(result, [True] * len(fires) + [False] * len(quiet))
        condition = next(n for n in load()["nodes"] if n["name"] == "Email Failed")["parameters"]["conditions"]
        self.assertEqual((condition["combinator"], len(condition["conditions"])), ("and", 1))
        self.assertEqual(condition["conditions"][0]["rightValue"], True)
        self.assertEqual(condition["conditions"][0]["operator"], {"type": "boolean", "operation": "equals"})
        # Its true output, and only that, reaches the fallback; the false output ends the run.
        self.assertEqual(load()["connections"]["Email Failed"],
                         {"main": [[{"node": "Send Telegram Fallback", "type": "main", "index": 0}]]})

    def test_end_to_end_outcomes_decide_the_fallback(self):
        # The real chain: what reaches Collect Send Outcome -> its outcome -> the Email Failed condition.
        inputs = [{"ok": True, "outcome": "sent", "code": ""},                       # email sent
                  {"ok": False, "outcome": "send_failed", "code": "gmail_error"},    # Gmail down
                  {"ok": False, "outcome": "rejected", "code": "OPS_ALERT_SHORT"},   # sender refused
                  {"error": "Workflow does not exist"},                              # sender call failed
                  {"send": False, "mail": {}, "row": {}}]                            # OK weekday: not due
        outcomes = self.evaluate("Collect Send Outcome", "outcome", inputs)
        self.assertEqual(outcomes, ["sent", "send_failed", "rejected", "call_failed", "not_due"])
        fired = self.evaluate_with_outcome("Email Failed", "condition", outcomes,
                                           lambda p: p["conditions"]["conditions"][0]["leftValue"])
        self.assertEqual(fired, [False, True, True, True, False])

    def test_fallback_text_names_gmail_only_for_send_failed(self):
        # G2 L6: a neutral line for the outcomes that are not a Gmail failure.
        outcomes = ["send_failed", "rejected", "error", "call_failed", "unknown"]
        texts = self.evaluate_with_outcome("Send Telegram Fallback", "text", outcomes, lambda p: p["text"])
        self.assertEqual(texts[0], "XB health email could not be sent on 2026-11-19 (n8n email step: send_failed). "
                                   "Worst: FAIL 2 jobs. Check the n8n Gmail credential, then XB Daily Health.")
        for outcome, text in zip(outcomes[1:], texts[1:]):
            self.assertEqual(text, "XB health email could not be sent on 2026-11-19 (n8n email step: %s). Worst: FAIL 2 "
                                   "jobs. Check XB Daily Health and XB Ops Alert Email in n8n." % outcome)
            self.assertNotIn("Gmail", text)
        for text in texts:
            self.assertRegex(text, r"^[\x20-\x7E]+$")
            self.assertLessEqual(len(text), 300)


GOLDEN_TEXT = "\n".join([
    "XB daily health check, Thu 19 Nov 2026 07:30 SGT: 3 of 11 watched jobs need a look (2 FAIL, 1 WARN).",
    "",
    "FAIL  Member sync daily summary: no email this morning, since 19 Nov. Next: check task "
    "XB-MemberSync-DailySummary on the VM (runbook: member_sync_phase1.md, \"Daily summary email\").",
    "FAIL  AC2 hourly stock export: no new run for 8 h (last 18 Nov 23:30), since 19 Nov. Next: check the AC2 VM is "
    "on and signed in, then Task Scheduler \"XB AC2 Hourly Stock Balance\" (runbook: stock_balance_hourly_runbook.md, "
    "\"Failure classes\").",
    "WARN  Drive mirror: the last weekly run could not reach GitHub (FETCH_FAILED), since 16 Nov. Next: nothing if the "
    "next Sunday run passes; otherwise see drive_mirror_runbook.md.",
    "",
    "OK    8 other jobs: Stock alert route, AR/AP export, EnergyGrid daily bills, EnergyGrid retention, n8n workflows, "
    "Server task list, Health checker, Drive folder.",
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
