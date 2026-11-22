"""Offline contract of the inactive "Member Sync - Daily Summary" n8n export (#288 slice 3).

No n8n instance, Gmail account, Data Table or VM is contacted. The committed Code node is run with
the local Node runtime against events built by the real VM script (xb_member_sync_daily_summary),
and its output is handed to the committed Code nodes of the shared XB Ops Alert Email sub-workflow
(validate, then fold-or-send). Every value here is synthetic.
"""

import copy
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
sys.path.insert(0, str(ROOT / "directlocal"))

import xb_member_sync_daily_summary as summary  # noqa: E402

WORKFLOW = ROOT / "n8n-workflows" / "member_sync_daily_summary.workflow.json"
SUB_WORKFLOW = ROOT / "n8n-workflows" / "ops_alert_email.workflow.json"
README = ROOT / "n8n-workflows" / "README.md"
CODE_NODE = "Validate Summary Event"
CALL_NODE = "Send Summary Email"
WEBHOOK_NODE = "Member Sync Summary Webhook"
CALL_FIELDS = ["system", "severity", "key", "short", "lines"]
ALLOWED_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.code",
    "n8n-nodes-base.if",
    "n8n-nodes-base.executeWorkflow",
    "n8n-nodes-base.respondToWebhook",
    "n8n-nodes-base.stickyNote",
    "n8n-nodes-base.dataTable",  # #288 slice 3 errors only: Log Clean Summary, the one insert
}
EXPECTED_EDGES = {
    (WEBHOOK_NODE, 0, CODE_NODE),
    (CODE_NODE, 0, "Event Is Valid"),
    (CODE_NODE, 1, "Respond Internal Error"),
    ("Event Is Valid", 0, "Email Is Due"),
    ("Email Is Due", 0, CALL_NODE),
    ("Email Is Due", 1, "Log Clean Summary"),
    ("Log Clean Summary", 0, "Respond Clean Logged"),
    ("Log Clean Summary", 1, "Respond Internal Error"),
    ("Event Is Valid", 1, "Respond Event Rejected"),
    (CALL_NODE, 0, "Email Was Sent"),
    (CALL_NODE, 1, "Respond Internal Error"),
    ("Email Was Sent", 0, "Respond Summary Sent"),
    ("Email Was Sent", 1, "Email Send Failed"),
    ("Email Send Failed", 0, "Respond Email Failed"),
    ("Email Send Failed", 1, "Respond Internal Error"),
}
# The VM records these as http_status: 200 sent, 202 logged_clean, send_failed for anything else.
RESPONSE_CODES = {
    "Respond Summary Sent": 200,
    "Respond Clean Logged": 202,
    "Respond Event Rejected": 400,
    "Respond Internal Error": 500,
    "Respond Email Failed": 502,
}
NOW = datetime(2026, 11, 6, 22, 40, 0, tzinfo=timezone.utc)  # 7 Nov 2026 06:40 SGT
SUBJECT_RE = re.compile(r"^\[XB daily\] MEMBER-SYNC (CLEAN|STOP) \d{4}-\d{2}-\d{2}$")
FAKE_PHONE = "65" + "8000" + "0042"

# Runs the summary Code node on each body; a valid result is handed to the sub-workflow's
# Validate Alert Request exactly as the call node maps it, then to Decide Fold Or Send.
HARNESS = r"""
const fs = require('fs');
const [workflowPath, subPath, casesPath] = process.argv.slice(2);
const codeOf = (path, name) => JSON.parse(fs.readFileSync(path, 'utf8')).nodes.find((n) => n.name === name).parameters.jsCode;
const code = codeOf(workflowPath, 'Validate Summary Event');
const subValidate = codeOf(subPath, 'Validate Alert Request');
const subDecide = codeOf(subPath, 'Decide Fold Or Send');
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const realNow = Date.now;
const results = cases.map((c) => {
  const $input = { all: () => [{ json: { body: c.body, headers: { 'x-xb-summary-token': 'FAKE-SECRET-NOT-READ' } } }] };
  try {
    const out = new Function('$input', code)($input);
    let sub = null;
    let plan = null;
    if (out.length === 1 && out[0].json.valid === true) {
      const j = out[0].json;
      const call = { system: 'MEMBER-SYNC', severity: j.severity, key: 'daily_summary', short: j.short, lines: j.lines };
      sub = new Function('$input', subValidate)({ all: () => [{ json: call }] });
      if (sub[0].json.valid === true) {
        Date.now = () => c.now_ms;
        const $ = (name) => ({ first: () => ({ json: sub[0].json }) });
        plan = new Function('$input', '$', subDecide)({ all: () => (c.rows.length ? c.rows : [{}]).map((json) => ({ json })) }, $);
        Date.now = realNow;
      }
    }
    return { ok: true, out, sub, plan };
  } catch (error) {
    Date.now = realNow;
    return { ok: false, error: String(error.message) };
  }
});
process.stdout.write(JSON.stringify(results));
"""


def build(task=None, config=None, digest=None, members=None, new_members=None, first_create=False, member_tools=None):
    """An event exactly as the VM script builds it, from fake child answers. ``member_tools``
    (#288 wiring): None gives the v1 event; else the two tool sections (None: that tool off)."""

    task = summary.task_section({"state": "Ready", "last_run": "2026-11-07T06:30", "last_result": 0,
                                 "next_run": "2026-11-07T06:45", "missed_runs": 0} if task is None else task)
    config = summary.config_section({"status": "ok", "autocount_book": "AED_XBOUNDARIES", "ac2_create_enabled": True,
                                     "shopify_create_enabled": False, "max_writes_per_run": 1, "max_creates_per_hour": 6,
                                     "ac2_create_member_type": "GUEST", "immediate_create_enabled": False} if config is None else config,
                                    True)
    raw_digest = {"runs": {"completed": 96, "unfinished": 0, "duration_seconds_max": 41, "duration_seconds_median": 12},
                  "counts": {"ac2_member_created": 1, "created_after_wait": 1},
                  "alerts": {"created_after_wait": 1, "waiting_for_staff": 2},
                  "alert_refs": [{"ref": "m-0a1b", "code": "memberno_conflict", "member_no_masked": "*******042"}],
                  "open": {"shopify_pending": 0, "ac2_pending": 5, "ac2_pending_would_create": 4, "uncertain_writes": 0, "links": 1234},
                  "creates": {"writes_per_hour_max": 1}}
    if digest is not None:
        raw_digest = digest
    digest = summary.digest_section(raw_digest, 1230)
    members = members or summary.members_section([("GUEST", 12), ("MEMBER", 2000), (None, 1)], None)
    new_members = new_members or summary.new_members_section(3, None, None)
    event = summary.build_event(now=NOW, task=task, config=config, digest=digest, members=members, new_members=new_members,
                                first_create=first_create, expected_member_type="GUEST", member_tools=member_tools)
    summary.check_event(event)
    return json.loads(json.dumps(event))


def daily_row(minutes_ago, key="daily_summary", outcome="sent", system="MEMBER-SYNC"):
    at = NOW - timedelta(minutes=minutes_ago)
    return {"id": int(10000 - minutes_ago * 10), "system": system, "key": key, "severity": "INFO", "outcome": outcome, "code": "",
            "folded_count": 0, "sent_at": at.isoformat().replace("+00:00", "Z")}


class WorkflowShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_inactive_mcp_hidden_unsaved_and_free_of_live_identity(self):
        self.assertEqual(self.workflow["name"], "Member Sync - Daily Summary")
        self.assertIs(self.workflow["active"], False)
        settings = self.workflow["settings"]
        self.assertIs(settings["availableInMCP"], False)
        # The webhook item carries the request headers, so the shared secret, in every execution.
        self.assertEqual(settings["saveDataSuccessExecution"], "none")
        self.assertEqual(settings["saveDataErrorExecution"], "none")
        self.assertIs(settings["saveManualExecutions"], False)
        self.assertIs(settings["saveExecutionProgress"], False)
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text), "no email address")
        self.assertNotRegex(text.lower(), r"telegram|chat_?id|credentialid|https?://")
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES, node["name"])

    def test_edges_are_exactly_the_reviewed_graph(self):
        edges = {(source, index, link["node"]) for source, outputs in self.workflow["connections"].items()
                 for index, branch in enumerate(outputs["main"]) for link in branch}
        self.assertEqual(edges, EXPECTED_EDGES)
        working = {name for name, node in self.nodes.items() if node["type"] != "n8n-nodes-base.stickyNote"}
        self.assertEqual(working, {e[0] for e in EXPECTED_EDGES} | {e[2] for e in EXPECTED_EDGES})

    def test_webhook_is_header_auth_post_with_a_placeholder_path(self):
        params = self.nodes[WEBHOOK_NODE]["parameters"]
        self.assertEqual(params, {"httpMethod": "POST", "path": "REPLACE_WITH_MEMBER_SYNC_SUMMARY_WEBHOOK_PATH",
                                  "authentication": "headerAuth", "responseMode": "responseNode", "options": {}})

    def test_response_codes_map_as_designed(self):
        for name, code in RESPONSE_CODES.items():
            node = self.nodes[name]
            self.assertEqual(node["type"], "n8n-nodes-base.respondToWebhook", name)
            self.assertEqual(node["parameters"]["options"], {"responseCode": code}, name)
            self.assertEqual(node["parameters"]["respondWith"], "json", name)
        for name in (CODE_NODE, CALL_NODE):
            self.assertEqual(self.nodes[name].get("onError"), "continueErrorOutput", name)
        self.assertEqual(self.nodes["Respond Email Failed"]["parameters"]["responseBody"],
                         "={{ { error: 'upstream_error', message: 'email send failed' } }}")
        self.assertEqual(self.nodes["Respond Event Rejected"]["parameters"]["responseBody"],
                         "={{ { error: 'validation_error', message: $json.reason } }}")
        # Only a sent email answers 200: a daily summary is never folded, so "folded" is not success.
        sent = self.nodes["Email Was Sent"]["parameters"]["conditions"]["conditions"]
        self.assertEqual([c["leftValue"] for c in sent], ["={{ $json.outcome === 'sent' }}"])
        failed = self.nodes["Email Send Failed"]["parameters"]["conditions"]["conditions"]
        self.assertEqual([c["leftValue"] for c in failed], ["={{ $json.outcome === 'send_failed' }}"])

    def test_call_payload_is_closed_and_pins_member_sync_and_the_daily_key(self):
        node = self.nodes[CALL_NODE]
        params = node["parameters"]
        self.assertEqual(node["type"], "n8n-nodes-base.executeWorkflow")
        self.assertEqual(params["workflowId"], {"__rl": True, "value": "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "mode": "id"})
        self.assertEqual((params["source"], params["mode"], params["options"]), ("database", "once", {"waitForSubWorkflow": True}))
        inputs = params["workflowInputs"]
        self.assertEqual(inputs["mappingMode"], "defineBelow")
        self.assertEqual(inputs["value"], {"system": "MEMBER-SYNC", "severity": "={{ $json.severity }}", "key": "daily_summary",
                                           "short": "={{ $json.short }}", "lines": "={{ $json.lines }}"})
        self.assertEqual([f["id"] for f in inputs["schema"]], CALL_FIELDS)
        self.assertIs(inputs["convertFieldsToString"], False)
        sub = json.loads(SUB_WORKFLOW.read_text(encoding="utf-8"))
        trigger = next(n for n in sub["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflowTrigger")
        self.assertEqual([(v["name"], v["type"]) for v in trigger["parameters"]["workflowInputs"]["values"]],
                         [(f["id"], f["type"]) for f in inputs["schema"]])
        # No automatic retry: a retried call could send a second email.
        for key in ("retryOnFail", "maxTries", "waitBetweenTries", "continueOnFail", "alwaysOutputData"):
            self.assertNotIn(key, node)

    def test_code_never_reads_headers_and_lists_match_the_vm_script(self):
        code = self.nodes[CODE_NODE]["parameters"]["jsCode"]
        for reader in (".headers", "['headers']", '["headers"]'):
            self.assertNotIn(reader, code)
        self.assertIn("'%s'" % summary.EVENT_SCHEMA, code)
        self.assertIn("'%s'" % summary.EVENT_NAME, code)
        line = next(line for line in code.splitlines() if line.startswith("const KEYS = ["))
        self.assertEqual(json.loads(line.split("=", 1)[1].strip().rstrip(";").replace("'", '"')), list(summary.EVENT_KEYS))

    def test_notes_and_readme(self):
        notes = "\n".join(n["parameters"]["content"] for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.stickyNote")
        for text in ("Required manual configuration", "REPLACE_WITH_MEMBER_SYNC_SUMMARY_WEBHOOK_PATH",
                     "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "XB Ops Alert Email", "[XB daily] MEMBER-SYNC CLEAN", "observe mode"):
            self.assertIn(text, notes)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("member_sync_daily_summary.workflow.json", readme)
        self.assertIn("Member Sync - Daily Summary", readme)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class WorkflowCodeExecutionTests(unittest.TestCase):
    def run_cases(self, bodies, rows=None):
        cases = [{"body": body, "rows": rows or [], "now_ms": int(NOW.timestamp() * 1000)} for body in bodies]
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            done = subprocess.run([shutil.which("node"), str(harness), str(WORKFLOW), str(SUB_WORKFLOW), str(cases_path)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        results = json.loads(done.stdout)
        for result in results:
            self.assertTrue(result["ok"], result)
        return results

    def assert_sent(self, result, subject, subject_re=SUBJECT_RE):
        (item,) = result["out"]
        self.assertIs(item["json"]["valid"], True, item)
        self.assertEqual(set(item["json"]), {"valid", "email", "severity", "short", "lines"})
        (sub,) = result["sub"]
        self.assertIs(sub["json"]["valid"], True, sub)
        (plan,) = result["plan"]
        self.assertIsNone(plan["json"]["held_outcome"])
        mail = plan["json"]["mail"]
        self.assertEqual(mail["subject"], subject)
        self.assertRegex(mail["subject"], subject_re)
        self.assertEqual(plan["json"]["mail_row"]["key"], "daily_summary")
        self.assertNotRegex(mail["text"], r"AC2-STOCK|ENERGYGRID", "one app per email")
        self.assertNotIn("FAKE-SECRET", json.dumps(result))
        return mail["text"]

    def test_clean_event_renders_a_daily_clean_email(self):
        (result,) = self.run_cases([build()])
        text = self.assert_sent(result, "[XB daily] MEMBER-SYNC CLEAN 2026-11-07")
        for line in ("MEMBER-SYNC daily summary 2026-11-07: CLEAN", "No stop condition found.",
                     "Auto off (observe mode, nothing was switched): no action",
                     "Sync task: state=Ready last_run=2026-11-07 06:30 last_result=0", "member_type=GUEST",
                     "Writes 24h: AC2 creates=1", "Shopify writes=0", "Alert refs: memberno_conflict ***042",
                     "Open: links=1234 (yesterday 1230)", "Members per type: -=1, GUEST=12, MEMBER=2000",
                     "New AC2 members 24h: total=3 by sync=1 by staff (till/HQ)=2",
                     "Owner AutoCount session: not checked", "Daily summary email (#288 slice 3)", "System: MEMBER-SYNC | Daily summary"):
            self.assertIn(line, text)
        self.assertEqual(result["out"][0]["json"]["severity"], "INFO")

    def test_every_stop_code_renders_a_daily_stop_email(self):
        bodies = []
        expected = []
        # One event per stop code, built by the VM script from fake answers.
        task_ok = {"state": "Ready", "last_run": "2026-11-07T06:30", "last_result": 0, "next_run": "2026-11-07T06:45", "missed_runs": 0}
        config_ok = {"status": "ok", "autocount_book": "AED_XBOUNDARIES", "ac2_create_enabled": True, "shopify_create_enabled": False,
                     "max_writes_per_run": 1, "max_creates_per_hour": 6, "ac2_create_member_type": "GUEST", "immediate_create_enabled": False}

        def digest(**changes):
            value = {"runs": {"completed": 96, "unfinished": 0}, "counts": {}, "alerts": {}, "alert_refs": [],
                     "open": {"uncertain_writes": 0, "links": 10}, "creates": {"writes_per_hour_max": 1}}
            for key, item in changes.items():
                value[key] = {**value[key], **item}
            return value

        per_code = {
            "task_state_unavailable": dict(task=summary.task_section(None)),
            "task_last_result": dict(task=summary.task_section({**task_ok, "last_result": 2})),
            "task_disabled": dict(task=summary.task_section({**task_ok, "state": "Disabled"})),
            "check_config_failed": dict(config=summary.config_section(None, True)),
            "config_unexpected": dict(config=summary.config_section({**config_ok, "shopify_create_enabled": True}, True)),
            "digest_failed": dict(digest=None),
            "no_runs": dict(digest=digest(runs={"completed": 0})),
            "unfinished_runs": dict(digest=digest(runs={"unfinished": 2})),
            "shopify_write": dict(digest=digest(counts={"shopify_customer_created": 1})),
            "uncertain_writes": dict(digest=digest(open={"uncertain_writes": 1})),
            "possible_duplicate_member": dict(digest=digest(alerts={"possible_duplicate_member": 1})),
            "member_type_unknown": dict(digest=digest(alerts={"member_type_unknown": 1})),
            "login_refused": dict(digest=digest(alerts={"autocount_login_refused": 1})),
            "writes_over_cap": dict(digest=digest(creates={"writes_per_hour_max": 9})),
            "alert_unexpected": dict(digest=digest(alerts={"customer_held_by_open_write": 1})),
        }
        self.assertEqual(tuple(per_code), summary.STOP_CODES)
        for code, parts in per_code.items():
            task = parts.get("task", summary.task_section(task_ok))
            config = parts.get("config", summary.config_section(config_ok, True))
            raw = parts["digest"] if "digest" in parts else digest()
            dig = summary.digest_section(raw, None)
            members = summary.members_section(None, "permission_denied")
            new = summary.new_members_section(None, None, "permission_denied")
            event = summary.build_event(now=NOW, task=task, config=config, digest=dig, members=members, new_members=new,
                                        first_create=False, expected_member_type="GUEST")
            summary.check_event(event)
            self.assertIn(code, event["stop_reasons"], code)
            bodies.append(json.loads(json.dumps(event)))
            expected.append(code)
        for code, result in zip(expected, self.run_cases(bodies)):
            with self.subTest(code=code):
                text = self.assert_sent(result, "[XB daily] MEMBER-SYNC STOP 2026-11-07")
                self.assertIn("STOP reasons: ", text)
                self.assertIn(code, text.splitlines()[1])
                self.assertIn("Members per type: not available (permission_denied)", text)
                self.assertEqual(result["out"][0]["json"]["severity"], "WARN")
                # A dead sync: the email also shows why.
                if code == "task_disabled":
                    self.assertIn("Sync task: state=Disabled ", text)
                if code == "no_runs":
                    self.assertIn("Runs 24h: completed=0 ", text)

    def test_state_root_mismatch_renders_a_stop_email_that_says_so(self):
        clean = build()
        self.assertIs(clean["config"]["state_root_ok"], True)
        body = copy.deepcopy(clean)
        config = summary.config_section({"status": "ok", "autocount_book": "AED_XBOUNDARIES", "ac2_create_enabled": True,
                                         "shopify_create_enabled": False, "max_writes_per_run": 1, "max_creates_per_hour": 6,
                                         "ac2_create_member_type": "GUEST", "immediate_create_enabled": False}, False)
        event = summary.build_event(now=NOW, task=clean["task"], config=config, digest=summary.digest_section(
            {"runs": {"completed": 96, "unfinished": 0}, "counts": {}, "alerts": {}, "alert_refs": [],
             "open": {"uncertain_writes": 0, "links": 10}, "creates": {"writes_per_hour_max": 1}}, None),
            members=clean["members_per_type"], new_members=summary.new_members_section(3, None, None), first_create=False,
            expected_member_type="GUEST")
        summary.check_event(event)
        self.assertEqual(event["stop_reasons"], ["config_unexpected"])
        clean_result, stop_result = self.run_cases([body, json.loads(json.dumps(event))])
        self.assertIn(" state_root_as_in_settings=yes", self.assert_sent(clean_result, "[XB daily] MEMBER-SYNC CLEAN 2026-11-07"))
        text = self.assert_sent(stop_result, "[XB daily] MEMBER-SYNC STOP 2026-11-07")
        self.assertIn("STOP reasons: config_unexpected ", text)
        self.assertIn(" state_root_as_in_settings=no", text)

    def test_would_switch_is_reported_and_nothing_else(self):
        body = build(digest={"runs": {"completed": 4, "unfinished": 0}, "counts": {"shopify_customer_created": 1}, "alerts": {"possible_duplicate_member": 1},
                             "alert_refs": [], "open": {"uncertain_writes": 1}, "creates": {"writes_per_hour_max": 1}})
        (result,) = self.run_cases([body])
        text = self.assert_sent(result, "[XB daily] MEMBER-SYNC STOP 2026-11-07")
        self.assertIn("Auto off (observe mode, nothing was switched): would switch creates off (shopify_write_while_disabled, "
                      "uncertain_writes, possible_duplicate_member)", text)

    def test_first_create_and_failed_parts(self):
        body = build(first_create=True)
        (result,) = self.run_cases([body])
        self.assertIn("first create=yes (FIRST CREATE: run S5 Part B)", self.assert_sent(result, "[XB daily] MEMBER-SYNC CLEAN 2026-11-07"))

    def test_a_daily_summary_is_never_folded_and_never_held_by_a_flood(self):
        # A summary sent minutes ago (a manual re-run) and a full alert flood of this system.
        rows = [daily_row(5)] + [daily_row(m, key="possible_duplicate_member") for m in range(1, 12)]
        (result,) = self.run_cases([build()], rows=rows)
        self.assert_sent(result, "[XB daily] MEMBER-SYNC CLEAN 2026-11-07")

    def test_worst_case_event_fits_the_sub_workflow_limits(self):
        # Every list at its bound with codes at their 64-character maximum and counts at COUNT_MAX.
        alerts = {("memberno_" + "x" * 53 + "%02d" % n): summary.COUNT_MAX for n in range(60)}
        alerts.update({"possible_duplicate_member": 1, "member_type_unknown": 1, "autocount_" + "z" * 54: 1, "q" * 64: 1})
        refs = [{"ref": "m-%02x" % n, "code": "memberno_" + "y" * 55, "member_no_masked": "*******%03d" % n} for n in range(40)]
        # Every stop code that a readable task, config and digest can give, at once.
        body = build(task={"state": "Disabled", "last_run": "2026-11-07T06:30", "last_result": 2147942402, "next_run": "2026-11-07T06:45",
                           "missed_runs": summary.COUNT_MAX},
                     config={"status": "ok", "autocount_book": "AED_XBOUNDARIES", "ac2_create_enabled": True, "shopify_create_enabled": False,
                             "max_writes_per_run": 1, "max_creates_per_hour": 6, "ac2_create_member_type": "X" * 20,
                             "immediate_create_enabled": True},
                     digest={"runs": {"completed": 0, "unfinished": summary.COUNT_MAX, "duration_seconds_max": summary.COUNT_MAX,
                                      "duration_seconds_median": summary.COUNT_MAX},
                             "counts": {"ac2_member_created": summary.COUNT_MAX, "created_immediately": summary.COUNT_MAX,
                                        "created_after_wait": summary.COUNT_MAX, "shopify_customer_created": summary.COUNT_MAX},
                             "alerts": alerts, "alert_refs": refs,
                             "open": {"uncertain_writes": summary.COUNT_MAX, "links": summary.COUNT_MAX, "ac2_pending": summary.COUNT_MAX,
                                      "ac2_pending_would_create": summary.COUNT_MAX, "shopify_pending": summary.COUNT_MAX},
                             "creates": {"writes_per_hour_max": summary.COUNT_MAX}},
                     members=summary.members_section([("T" + "z" * 17 + "%02d" % n, summary.COUNT_MAX) for n in range(30)], None),
                     new_members=summary.new_members_section(summary.COUNT_MAX, None, None), first_create=True)
        self.assertEqual(len(body["digest"]["alerts"]), summary.MAX_ALERT_CODES)
        self.assertEqual(len(body["digest"]["alert_refs"]), summary.MAX_ALERT_REFS)
        self.assertEqual(len(body["members_per_type"]["counts"]), summary.MAX_MEMBER_TYPES)
        self.assertEqual(body["stop_reasons"], [c for c in summary.STOP_CODES if not c.endswith(("_unavailable", "_failed"))])
        (result,) = self.run_cases([body])
        lines = result["out"][0]["json"]["lines"]
        self.assertLessEqual(len(lines), 30)
        self.assertLessEqual(len("\n".join(lines)), 4000)
        self.assertTrue(all(len(line) <= 300 for line in lines))
        self.assert_sent(result, "[XB daily] MEMBER-SYNC STOP 2026-11-07")

    def test_schema_rejects_any_extra_or_unknown_field(self):
        base = build()
        cases = {
            "MS_SUMMARY_KEYS": [{**base, "note": "x"}, {k: v for k, v in base.items() if k != "owner_session"}],
            "MS_SUMMARY_SCHEMA": [{**base, "schema": "xb.member_sync.daily_summary.v2"}],
            "MS_SUMMARY_EVENT": [{**base, "event": "other"}],
            "MS_SUMMARY_DATE": [{**base, "summary_date": "07/11/2026"}, {**base, "generated_at": "2026-11-08T06:40+08:00"},
                                {**base, "generated_at": "2026-11-07T06:40Z"}],
            "MS_SUMMARY_RESULT": [{**base, "result": "OK"}],
            "MS_SUMMARY_STOP_REASONS": [{**base, "result": "STOP"}, {**base, "stop_reasons": ["uncertain_writes"]},
                                        {**base, "result": "STOP", "stop_reasons": ["not_a_code"]},
                                        {**base, "result": "STOP", "stop_reasons": ["uncertain_writes", "task_last_result"]},
                                        {**base, "result": "STOP", "stop_reasons": ["uncertain_writes", "uncertain_writes"]}],
            "MS_SUMMARY_AUTO_OFF": [{**base, "auto_off": "switched"}, {**base, "auto_off": "would_switch"},
                                    {**base, "auto_off_reasons": ["uncertain_writes"]}],
            "MS_SUMMARY_FIRST_CREATE": [{**base, "first_create": "no"}],
            "MS_SUMMARY_OWNER_SESSION": [{**base, "owner_session": "yes"}],
            "MS_SUMMARY_PERSONAL_DATA": [],
        }
        sections = {
            "MS_SUMMARY_TASK": ("task", [{"extra": 1}, {"state": "Gone"}, {"last_result": -1}, {"last_run": "06:30"},
                                         {"available": False}]),
            "MS_SUMMARY_CONFIG": ("config", [{"extra": 1}, {"autocount_book": "OTHER_BOOK"}, {"ac2_create_enabled": "yes"},
                                             {"ac2_create_member_type": "<b>"}, {"state_root_ok": "yes"}, {"state_root_ok": 1}]),
            "MS_SUMMARY_DIGEST": ("digest", [{"extra": 1}, {"links": -1}, {"links": "1234"}, {"available": False}]),
            "MS_SUMMARY_ALERTS": ("digest", [{"alerts": {"Bad Code": 1}}, {"alerts": {"x": -1}}, {"alerts": []}]),
            "MS_SUMMARY_ALERT_REFS": ("digest", [{"alert_refs": [{"code": "memberno_conflict", "member_no": "042"}]},
                                                 {"alert_refs": [{"code": "memberno_conflict", "member_no": "***042", "ref": "m-1"}]},
                                                 {"alert_refs": [{"code": "memberno_conflict", "member_no": "****0042"}]},
                                                 # G2 B3 (S4): three stars plus 4 characters is one too many.
                                                 {"alert_refs": [{"code": "memberno_conflict", "member_no": "***0042"}]},
                                                 {"alert_refs": [{"code": "memberno_conflict", "member_no": "**042"}]}]),
            "MS_SUMMARY_MEMBERS": ("members_per_type", [{"extra": 1}, {"reason": "disabled"}, {"counts": {"Alpha Tester": 1}},
                                                        {"available": False, "counts": None, "reason": "because"}]),
            "MS_SUMMARY_NEW_MEMBERS": ("new_members_24h", [{"extra": 1}, {"total": None}, {"available": False}]),
        }
        for reason, (section, changes) in sections.items():
            for change in changes:
                body = copy.deepcopy(base)
                body[section] = {**body[section], **change}
                cases.setdefault(reason, []).append(body)
        # Phone- or email-shaped text anywhere is refused, whatever field it hides in.
        for path, value in ((("digest", "alerts"), {"phone_" + FAKE_PHONE: 1}), (("members_per_type", "counts"), {"GUEST": 1, "a@example.com": 1})):
            body = copy.deepcopy(base)
            body[path[0]][path[1]] = value
            cases["MS_SUMMARY_PERSONAL_DATA"].append(body)
        cases["MS_SUMMARY_BODY"] = [None, [], "text"]
        flat = [(reason, body) for reason, bodies in cases.items() for body in bodies]
        for (reason, body), result in zip(flat, self.run_cases([body for _, body in flat])):
            with self.subTest(reason=reason, body=json.dumps(body)[:120]):
                (item,) = result["out"]
                self.assertEqual(item["json"], {"valid": False, "reason": reason})
                self.assertIsNone(result["sub"], "a rejected event never reaches the email sub-workflow")


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class Bf2bWorkflowTests(unittest.TestCase):
    """#155 BF2b: the unchanged n8n validator accepts the BF2b event (creates expected, a backfill
    window in effect) and the email shows the backfill's creates apart, in the alert line."""

    run_cases = WorkflowCodeExecutionTests.run_cases
    assert_sent = WorkflowCodeExecutionTests.assert_sent

    def test_backfill_creates_are_shown_in_a_clean_email_with_creates_on(self):
        raw = {"runs": {"completed": 96, "unfinished": 0, "duration_seconds_max": 41, "duration_seconds_median": 12},
               "counts": {"ac2_member_created": 1, "shopify_customer_created": 41},
               "alerts": {"waiting_for_staff": 2},
               "alert_refs": [],
               "open": {"shopify_pending": 0, "ac2_pending": 5, "ac2_pending_would_create": 0, "uncertain_writes": 0, "links": 1300},
               "creates": {"writes_per_hour_max": 41},
               "backfill_members": {"calls": 3, "creates": 40, "uncertain": 0, "rejected": 0, "writes": 40, "writes_per_call_max": 20,
                                    "writes_per_hour_max": 40, "live_writes_per_hour_max": 1}}
        window = summary._window({"from": "2026-11-06", "to": "2026-11-07", "max_per_call": 25, "max_per_hour": 60})
        creates = summary.creates_section(raw, expected=True, window=window)
        config = summary.config_section({"status": "ok", "autocount_book": "AED_XBOUNDARIES", "ac2_create_enabled": True,
                                         "shopify_create_enabled": True, "max_writes_per_run": 1, "max_creates_per_hour": 6,
                                         "ac2_create_member_type": "GUEST", "immediate_create_enabled": False}, True)
        task = summary.task_section({"state": "Ready", "last_run": "2026-11-07T06:30", "last_result": 0, "next_run": "2026-11-07T06:45", "missed_runs": 0})
        event = summary.build_event(now=NOW, task=task, config=config, digest=summary.digest_section(raw, 1230),
                                    members=summary.members_section([("MEMBER", 2000)], None), new_members=summary.new_members_section(3, None, None),
                                    first_create=False, expected_member_type="GUEST", creates=creates)
        summary.check_event(event)
        (result,) = self.run_cases([json.loads(json.dumps(event))])
        text = self.assert_sent(result, "[XB daily] MEMBER-SYNC CLEAN 2026-11-07")
        for line in ("shopify_create=yes", "Shopify writes=41", "backfill_members_creates=40", "No stop condition found."):
            self.assertIn(line, text)


# ------------------------------------------------------------------------------------------------
# #288 member email wiring: the v2 event (member core and tags) in the same Code node
# ------------------------------------------------------------------------------------------------

SUBJECT_V2_RE = re.compile(r"^\[XB daily\] MEMBER-SYNC (CLEAN|WARN|STOP) \d{4}-\d{2}-\d{2}$")
FAKE_AT = "x@example.com"
MORE_RE = re.compile(r"\+\d+ more \(see the VM record\)")
# SHA-256 of the Code node's output for every body of v1_golden_bodies(), made with the
# main 8222aed export of this workflow (scripted, #288 wiring G1): a v1 event renders exactly as before.
V1_LINES_GOLDEN_SHA256 = "da9c0ef40da2d42eea8b80932df7598784057c5b55e408864cb40a7fd328057b"
# SHA-256 of this export with the Validate Summary Event jsCode blanked. Made on main 8222aed
# (8a77d55f...), then re-made at fix 1 (G2 LATER 1) after the Start Here sticky's Input and Subject
# bullets and its height (400 -> 460) changed: no other node, connection, setting or note changed.
# #288 slice 3 errors only (22 Nov 2026): re-pinned to the graph with Email Is Due, Log Clean Summary and
# Respond Clean Logged (and the moved email path); the Code node itself is pinned by the golden digests.
WORKFLOW_REST_SHA256 = "6a9884327666e5fb26227766a6412ef0d16c766070ea8159b8aa5cc24ec3a3bd"


def core_raw(alerts=None, runs=None, opened=None, **overrides):
    value = {"schema": summary.CORE_DIGEST_SCHEMA, "status": "ok", "rule_settings_match": True,
             "runs": {"completed": 96, "unfinished": 0, **(runs or {})}, "alerts": dict(alerts or {}),
             "shadow": {"would_upgrade": 3, "would_extend": 1},
             "open": {"uncertain_writes": 0, "held_writes": 0, "waiting_member": 2, **(opened or {})}}
    value.update(overrides)
    return value


def tags_raw(**email):
    block = {"runs_completed": 12, "runs_unfinished": 0, "tagged_now": 812, "bday_tagged_now": 40, "would_tag": 3, "would_untag": 0,
             "would_bday_tag": 2, "would_bday_untag": 0, "alerts": [], "stale_month": False, "open_writes": 0}
    block.update(email)
    return {"status": "ok", "email": block}


CORE_CASES = {
    "CLEAN": lambda: summary.core_section(core_raw(alerts={"orders_read_incomplete": 1})),
    "WARN": lambda: summary.core_section(core_raw(opened={"held_writes": 1})),
    "STOP": lambda: summary.core_section(core_raw(rule_settings_match=False)),
    "failed": lambda: summary.tool_failed("member_core", "benefits_ledger_missing"),
}
TAGS_CASES = {
    "CLEAN": lambda: summary.tags_section(tags_raw()),
    "WARN": lambda: summary.tags_section(tags_raw(stale_month=True)),
    "STOP": lambda: summary.tags_section(tags_raw(alerts=["write_uncertain"])),
    "failed": lambda: summary.tool_failed("tags", "tags_ledger_missing"),
}


def stop_sync_task():
    return {"state": "Disabled", "last_run": "2026-11-07T06:30", "last_result": 1, "next_run": "2026-11-07T06:45", "missed_runs": 0}


def worst_v1_parts():
    """The sync's worst case (every list at its bound, codes at 64 characters, counts at COUNT_MAX)."""

    alerts = {("memberno_" + "x" * 53 + "%02d" % n): summary.COUNT_MAX for n in range(60)}
    alerts.update({"possible_duplicate_member": 1, "member_type_unknown": 1, "autocount_" + "z" * 54: 1, "q" * 64: 1})
    refs = [{"ref": "m-%02x" % n, "code": "memberno_" + "y" * 55, "member_no_masked": "*******%03d" % n} for n in range(40)]
    return dict(task={"state": "Disabled", "last_run": "2026-11-07T06:30", "last_result": 2147942402, "next_run": "2026-11-07T06:45",
                      "missed_runs": summary.COUNT_MAX},
                config={"status": "ok", "autocount_book": "AED_XBOUNDARIES", "ac2_create_enabled": True, "shopify_create_enabled": False,
                        "max_writes_per_run": 1, "max_creates_per_hour": 6, "ac2_create_member_type": "X" * 20, "immediate_create_enabled": True},
                digest={"runs": {"completed": 0, "unfinished": summary.COUNT_MAX, "duration_seconds_max": summary.COUNT_MAX,
                                 "duration_seconds_median": summary.COUNT_MAX},
                        "counts": {"ac2_member_created": summary.COUNT_MAX, "created_immediately": summary.COUNT_MAX,
                                   "created_after_wait": summary.COUNT_MAX, "shopify_customer_created": summary.COUNT_MAX},
                        "alerts": alerts, "alert_refs": refs,
                        "open": {"uncertain_writes": summary.COUNT_MAX, "links": summary.COUNT_MAX, "ac2_pending": summary.COUNT_MAX,
                                 "ac2_pending_would_create": summary.COUNT_MAX, "shopify_pending": summary.COUNT_MAX},
                        "creates": {"writes_per_hour_max": summary.COUNT_MAX}},
                members=summary.members_section([("T" + "z" * 17 + "%02d" % n, summary.COUNT_MAX) for n in range(30)], None),
                new_members=summary.new_members_section(summary.COUNT_MAX, None, None), first_create=True)


def worst_tools():
    """Each tool's worst valid section: every reason it can show at once, counts at COUNT_MAX and 20
    alert codes of 64 characters (accepted by the VM's own check_event)."""

    core = {"status": "STOP", "reasons": [r for r in summary.CORE_STOP_REASONS + summary.CORE_WARN_REASONS if r != "digest_failed"],
            "available": True, "refusal": None, "rule_settings_match": False,
            **{f: summary.COUNT_MAX for f in summary.CORE_FIELDS if f not in ("rule_settings_match", "alerts")},
            "alerts": {("c" * 62 + "%02d" % n): summary.COUNT_MAX for n in range(20)}}
    tags = {"status": "STOP", "reasons": [r for r in summary.TAGS_STOP_REASONS + summary.TAGS_WARN_REASONS if r != "digest_failed"],
            "available": True, "refusal": None, **{f: summary.COUNT_MAX for f in summary.TAGS_FIELDS if f not in ("stale_month", "alerts")},
            "stale_month": True, "alerts": ["t" * 62 + "%02d" % n for n in range(20)]}
    core = {k: core[k] for k in summary.TOOL_SECTION_HEAD + summary.CORE_FIELDS}
    tags = {k: tags[k] for k in summary.TOOL_SECTION_HEAD + summary.TAGS_FIELDS}
    return core, tags


def v1_golden_bodies():
    """v1 bodies for the golden email lines: clean, first create, each sync stop shape, the worst case."""

    stop_digest = {"runs": {"completed": 4, "unfinished": 0}, "counts": {"shopify_customer_created": 1}, "alerts": {"possible_duplicate_member": 1},
                   "alert_refs": [], "open": {"uncertain_writes": 1}, "creates": {"writes_per_hour_max": 1}}
    return [
        build(), build(first_create=True), build(digest=stop_digest), build(task=stop_sync_task()),
        build(task=None, config=None, digest=None, members=summary.members_section(None, "permission_denied"),
              new_members=summary.new_members_section(None, None, "permission_denied")),
        json.loads(json.dumps(_digest_failed_event())), build(**worst_v1_parts()),
    ]


def _digest_failed_event():
    event = summary.build_event(now=NOW, task=summary.task_section(None), config=summary.config_section(None, None),
                                digest=summary.digest_section(None, None), members=summary.members_section(None, "config_unavailable"),
                                new_members=summary.new_members_section(None, None, "config_unavailable"), first_create=False,
                                expected_member_type="GUEST")
    summary.check_event(event)
    return event


def lines_digest(results):
    import hashlib  # noqa: PLC0415
    # #288 slice 3 errors only: the `email` flag is left out, so the golden digests still pin the unchanged text.
    out = [[{**o, "json": {k: v for k, v in o["json"].items() if k != "email"}} for o in r["out"]] for r in results]
    return hashlib.sha256(json.dumps(out, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def workflow_rest_digest(path):
    import hashlib  # noqa: PLC0415
    workflow = json.loads(Path(path).read_text(encoding="utf-8"))
    for node in workflow["nodes"]:
        if node["name"] == CODE_NODE:
            node["parameters"]["jsCode"] = ""
    return hashlib.sha256(json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class WiringWorkflowShapeTests(unittest.TestCase):
    """W5: only the Code node changed (the existing structural tests above still pin the graph)."""

    def test_nothing_but_the_code_node_changed_since_main(self):
        self.assertEqual(workflow_rest_digest(WORKFLOW), WORKFLOW_REST_SHA256)

    def test_the_start_here_note_describes_v1_v2_and_the_warn_subject(self):
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        note = next(n for n in workflow["nodes"] if n["name"] == "Start Here")
        content = note["parameters"]["content"]
        self.assertTrue(content.isascii())
        for text in ("`xb.member_sync.daily_summary.v1` (both member tools off on the VM)", "`xb.member_sync.daily_summary.v2`",
                     "`member_tools`", "`... WARN <date>`", "WARN comes only from a tool"):
            self.assertIn(text, content)
        self.assertEqual(note["parameters"]["height"], 600)

    def test_the_code_node_lists_match_the_vm_script(self):
        code = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        code = next(n for n in code["nodes"] if n["name"] == CODE_NODE)["parameters"]["jsCode"]
        self.assertIn("'%s'" % summary.EVENT_SCHEMA_V2, code)
        for name in summary.TOOL_NAMES:
            stop, warn, fields = summary.TOOL_RULES[name]
            for values in (stop, warn, fields):
                self.assertIn(json.dumps(list(values)).replace('"', "'"), code)
        self.assertIn("const TOOL_NAMES = %s;" % json.dumps(list(summary.TOOL_NAMES)).replace('"', "'"), code)
        self.assertIn("const TOOL_HEAD = %s;" % json.dumps(list(summary.TOOL_SECTION_HEAD)).replace('"', "'"), code)
        for reader in (".headers", "['headers']", '["headers"]'):
            self.assertNotIn(reader, code)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class WiringWorkflowTests(unittest.TestCase):
    run_cases = WorkflowCodeExecutionTests.run_cases
    assert_sent = WorkflowCodeExecutionTests.assert_sent

    def sent_v2(self, result, word):
        text = self.assert_sent(result, "[XB daily] MEMBER-SYNC %s 2026-11-07" % word, SUBJECT_V2_RE)
        self.assertEqual(result["out"][0]["json"]["severity"], "INFO" if word == "CLEAN" else "WARN")
        return text

    # -- W1 -----------------------------------------------------------------------------------
    def test_v1_events_give_exactly_mains_email_lines(self):
        results = self.run_cases(v1_golden_bodies())
        self.assertEqual(lines_digest(results), V1_LINES_GOLDEN_SHA256)
        for result in results:
            self.assertNotRegex("\n".join(result["out"][0]["json"]["lines"]), MORE_RE)

    # -- W2 -----------------------------------------------------------------------------------
    def test_the_subject_word_is_the_worst_of_sync_and_tools(self):
        bodies, expected = [], []
        for sync_word, task in (("CLEAN", None), ("STOP", stop_sync_task())):
            for core_name in [None, *CORE_CASES]:
                for tags_name in [None, *TAGS_CASES]:
                    if core_name is None and tags_name is None:
                        continue
                    core = CORE_CASES[core_name]() if core_name else None
                    tags = TAGS_CASES[tags_name]() if tags_name else None
                    body = build(task=task, member_tools={"member_core": core, "tags": tags})
                    self.assertEqual(body["result"], sync_word)
                    words = [sync_word] + [s["status"] for s in (core, tags) if s is not None]
                    bodies.append(body)
                    expected.append((max(words, key=summary.TOOL_STATUSES.index), sync_word, core, tags))
        self.assertEqual(len(bodies), 48)
        for (word, sync_word, core, tags), result in zip(expected, self.run_cases(bodies)):
            with self.subTest(word=word, sync=sync_word, core=core and core["status"], tags=tags and tags["status"]):
                text = self.sent_v2(result, word)
                # #288 slice 3 errors only: emailed exactly when the subject word is not CLEAN.
                self.assertIs(result["out"][0]["json"]["email"], word != "CLEAN")
                lines = text.splitlines()
                parts = ["sync " + sync_word] + (["member core " + core["status"]] if core else []) + (["tags " + tags["status"]] if tags else [])
                self.assertIn("MEMBER-SYNC daily summary 2026-11-07: %s (%s)" % (word, ", ".join(parts)), lines)
                if sync_word == "STOP":
                    self.assertIn("Sync STOP: task_last_result, task_disabled (check the sync now; the off switch is in the runbook)", lines)
                else:
                    self.assertIn("Sync: no stop condition found.", lines)
                for label, section, failed in (("Member core", core, "benefits_ledger_missing"), ("Tags", tags, "tags_ledger_missing")):
                    if section is None:
                        self.assertFalse(any(line.startswith(label + ":") for line in lines), label)
                    elif not section["available"]:
                        self.assertIn("%s: STOP (digest_failed: %s)" % (label, failed), lines)
                        self.assertFalse(any(line.startswith(label + " alerts") for line in lines))
                    else:
                        head = label + ": " + section["status"] + (" (%s)" % ", ".join(section["reasons"]) if section["reasons"] else "") + " runs="
                        self.assertTrue(any(line.startswith(head) for line in lines), head)
                # A tool's problem never reads as the sync's, and the sync part is the v1 text.
                self.assertIn("Owner AutoCount session: not checked (manual glance)", lines)

    def test_the_member_no_unsupported_core_alert_passes_the_code_node(self):
        # Slice NF2 (#268): the member core's new hold alert is accepted by the
        # n8n Code node (no MS_SUMMARY_TOOLS refusal) and listed as any alert.
        core = summary.core_section(core_raw(alerts={"member_no_unsupported": 1}))
        self.assertEqual((core["status"], core["reasons"]), ("WARN", ["alerts"]))
        (result,) = self.run_cases([build(member_tools={"member_core": core, "tags": None})])
        lines = self.sent_v2(result, "WARN").splitlines()
        self.assertIn("Member core: WARN (alerts) runs=96 unfinished=0 rule_settings_match=yes", lines)
        self.assertIn("Member core alerts 24h (runs): member_no_unsupported=1 (refs: digest on the VM)", lines)

    def test_the_v2_layout(self):
        core = summary.core_section(core_raw(alerts={"claim_needs_review": 2, "orders_read_incomplete": 1}))
        tags = summary.tags_section(tags_raw(stale_month=True))
        (result,) = self.run_cases([build(member_tools={"member_core": core, "tags": tags})])
        lines = self.sent_v2(result, "WARN").splitlines()
        start = lines.index("Owner AutoCount session: not checked (manual glance)")
        self.assertEqual(lines[start + 1:start + 9], [
            "",
            "Member core: WARN (alerts) runs=96 unfinished=0 rule_settings_match=yes",
            "Member core open: uncertain_writes=0 held_writes=0 waiting_member=2 would_upgrade=3 would_extend=1 (would_* = ready receipts, all time)",
            "Member core alerts 24h (runs): claim_needs_review=2, orders_read_incomplete=1 (refs: digest on the VM)",
            "Tags: WARN (stale_month) runs=12 unfinished=0 tagged_now=812 bday_tagged_now=40 stale_month=yes open_writes=0",
            "Tags latest run: would_tag=3 would_untag=0 would_bday_tag=2 would_bday_untag=0",
            "Tags alerts 24h: none",
            "",
        ])
        self.assertEqual(lines[0], "MEMBER-SYNC daily summary 2026-11-07: WARN (sync CLEAN, member core WARN, tags WARN)")
        self.assertEqual(lines[1], "Sync: no stop condition found.")
        self.assertTrue(result["out"][0]["json"]["lines"][-1].startswith("Runbook: "))

    # -- W3 -----------------------------------------------------------------------------------
    def test_validator_rejections(self):
        base = build(member_tools={"member_core": CORE_CASES["WARN"](), "tags": TAGS_CASES["WARN"]()})
        v1 = build()

        def changed(fn, body=None):
            value = copy.deepcopy(base if body is None else body)
            fn(value)
            return value

        def tool(name, **changes):
            return changed(lambda b: b["member_tools"][name].update(changes))

        cases = {
            "MS_SUMMARY_SCHEMA": [changed(lambda b: b.update(member_tools=copy.deepcopy(base["member_tools"])), v1),
                                  changed(lambda b: b.pop("member_tools"))],
            "MS_SUMMARY_KEYS": [changed(lambda b: b.update(note="x"))],
            "MS_SUMMARY_TOOLS": [
                changed(lambda b: b.update(member_tools=None)), changed(lambda b: b.update(member_tools=[])),
                changed(lambda b: b.update(member_tools={"member_core": None, "tags": None})),
                changed(lambda b: b["member_tools"].update(sync=None)), changed(lambda b: b["member_tools"].pop("tags")),
                tool("tags", status="CLEAN"), tool("tags", status="STOP"), tool("tags", status="OK"), tool("member_core", status="WARN ") ,
                tool("tags", reasons=["alerts", "stale_month"]), tool("tags", reasons=["stale_month", "stale_month"]),
                tool("tags", reasons=["made_up"]), tool("tags", reasons=["held_writes"]), tool("tags", reasons="stale_month"),
                tool("tags", open_writes=-1), tool("tags", open_writes=summary.COUNT_MAX + 1), tool("tags", open_writes=1.5),
                tool("tags", open_writes=None), tool("tags", open_writes=True), tool("tags", stale_month="no"),
                tool("member_core", held_writes=-1), tool("member_core", rule_settings_match=None),
                tool("tags", alerts=["code_%02d" % n for n in range(21)]), tool("tags", alerts=["b_code", "a_code"]),
                tool("tags", alerts=["a_code", "a_code"]), tool("tags", alerts=["Bad Code"]), tool("tags", alerts={"a_code": 1}),
                tool("member_core", alerts={"code_%02d" % n: 1 for n in range(21)}), tool("member_core", alerts={"Bad Code": 1}),
                tool("member_core", alerts={"a_code": -1}), tool("member_core", alerts=["a_code"]),
                tool("member_core", available=False), tool("member_core", refusal="timed_out"), tool("member_core", available="yes"),
                tool("member_core", status="STOP", reasons=["digest_failed", "held_writes"]),
                tool("tags", extra=1),
                changed(lambda b: b["member_tools"]["tags"].pop("alerts")),
                # available false with figures present, or with no refusal, or another reason
                changed(lambda b: b["member_tools"].update(tags={**summary.tool_failed("tags", "timed_out"), "runs_completed": 3})),
                changed(lambda b: b["member_tools"].update(tags={**summary.tool_failed("tags", "timed_out"), "alerts": []})),
                changed(lambda b: b["member_tools"].update(tags={**summary.tool_failed("tags", "timed_out"), "refusal": None})),
                changed(lambda b: b["member_tools"].update(tags={**summary.tool_failed("tags", "timed_out"), "refusal": "Bad Code"})),
                changed(lambda b: b["member_tools"].update(tags={**summary.tool_failed("tags", "timed_out"),
                                                                 "reasons": ["digest_failed", "stale_month"]})),
            ],
            "MS_SUMMARY_PERSONAL_DATA": [tool("tags", alerts=["x_" + FAKE_PHONE]), tool("member_core", alerts={FAKE_AT: 1}),
                                         changed(lambda b: b["member_tools"].update(tags=summary.tool_failed("tags", FAKE_AT))),
                                         changed(lambda b: b["member_tools"]["tags"].update({"x_" + FAKE_PHONE: 1}))],
        }
        flat = [(reason, body) for reason, bodies in cases.items() for body in bodies]
        for (reason, body), result in zip(flat, self.run_cases([body for _, body in flat])):
            with self.subTest(reason=reason, tools=json.dumps(body.get("member_tools"))[:160]):
                (item,) = result["out"]
                self.assertEqual(item["json"], {"valid": False, "reason": reason})
                self.assertIsNone(result["sub"])
        # And the base itself, and a v1 body, pass.
        for result in self.run_cases([base, v1]):
            self.assertIs(result["out"][0]["json"]["valid"], True)

    # -- W4 -----------------------------------------------------------------------------------
    def test_the_worst_case_fits_for_all_four_switch_combinations(self):
        core, tags = worst_tools()
        combos = {"off/off": None, "core/off": {"member_core": core, "tags": None}, "off/tags": {"member_core": None, "tags": tags},
                  "core/tags": {"member_core": core, "tags": tags}}
        bodies = [build(**worst_v1_parts(), member_tools=tools) for tools in combos.values()]
        self.measured = {}
        for (name, tools), result in zip(combos.items(), self.run_cases(bodies)):
            with self.subTest(combo=name):
                lines = result["out"][0]["json"]["lines"]
                self.assertLessEqual(len(lines), 30)
                self.assertTrue(all(len(line) <= 300 for line in lines), max(len(line) for line in lines))
                self.assertLessEqual(len("\n".join(lines)), 4000)
                word = "STOP"
                text = self.assert_sent(result, "[XB daily] MEMBER-SYNC %s 2026-11-07" % word, SUBJECT_V2_RE)
                # The never-cut lines are whole.
                self.assertTrue(lines[0].startswith("MEMBER-SYNC daily summary 2026-11-07: STOP"))
                self.assertTrue(lines[1].startswith("STOP reasons: " if tools is None else "Sync STOP: "))
                self.assertTrue(lines[1].endswith("(check the sync now; the off switch is in the runbook)"))
                self.assertTrue(lines[2].startswith("Auto off (observe mode, nothing was switched): would switch"))
                for prefix in ("Sync task: ", "Config: ", "Runs 24h: ", "Writes 24h: ", "Possible duplicates=", "Open: ", "New AC2 members 24h: ",
                               "Owner AutoCount session", "Generated ", "Runbook: "):
                    self.assertEqual(sum(line.startswith(prefix) for line in lines), 1, prefix)
                if tools and tools["member_core"]:
                    self.assertIn("Member core: STOP (rule_settings_mismatch, no_runs, unfinished_runs, uncertain_writes, "
                                  "uncertain_writes_run_in_progress, held_writes, alerts) runs=1000000000 unfinished=1000000000 "
                                  "rule_settings_match=no", lines)
                    self.assertTrue(any(line.startswith("Member core open: ") and line.endswith("(would_* = ready receipts, all time)") for line in lines))
                    self.assertEqual(sum(line.startswith("Member core alerts 24h (runs): ") for line in lines), 1)
                if tools and tools["tags"]:
                    self.assertTrue(any(line.startswith("Tags: STOP (no_runs, unfinished_runs, open_writes, write_alert, alerts_at_cap, "
                                                        "open_writes_run_in_progress, stale_month, alerts) runs=") for line in lines))
                    self.assertTrue(any(line.startswith("Tags latest run: would_tag=") for line in lines))
                    self.assertEqual(sum(line.startswith("Tags alerts 24h: ") for line in lines), 1)
                if tools is not None:
                    self.assertRegex(text, MORE_RE)  # something was cut, and says so
                self.measured[name] = (len(lines), len("\n".join(lines)), max(len(line) for line in lines))
        # G1 measurement (lines, characters, longest line): off/off 25, 3773, 281; core/off 27, 3952, 273;
        # off/tags 27, 3936, 273; core/tags 30, 3928, 273.
        self.assertEqual(set(self.measured), set(combos))

    def test_a_normal_day_is_never_cut(self):
        core = summary.core_section(core_raw(alerts={"claim_needs_review": 2, "orders_read_incomplete": 1}))
        tags = summary.tags_section(tags_raw(alerts=["tag_removes_capped", "untag_volume_unexpected"]))
        (result,) = self.run_cases([build(member_tools={"member_core": core, "tags": tags})])
        text = self.sent_v2(result, "WARN")
        self.assertNotRegex(text, MORE_RE)
        self.assertIn("Tags alerts 24h: tag_removes_capped, untag_volume_unexpected", text)
        self.assertIn("Alert refs: memberno_conflict ***042", text)

    def test_a_long_tool_alert_list_alone_cuts_only_itself(self):
        """G2 LATER 5 (kills G2-J4): only one tool's alert list is long, everything else is a normal
        day. The one-line list keeps what fits on its line; no other list is cut."""

        long_tags = summary.tags_section(tags_raw(alerts=["t" * 62 + "%02d" % n for n in range(19)]))
        long_core = summary.core_section(core_raw(alerts={"c" * 62 + "%02d" % n: 3 for n in range(20)}))
        bodies = [build(member_tools={"member_core": summary.core_section(core_raw()), "tags": long_tags}),
                  build(member_tools={"member_core": long_core, "tags": summary.tags_section(tags_raw())})]
        for (label, other), result in zip((("Tags alerts 24h: ", "Member core alerts 24h (runs): "),
                                           ("Member core alerts 24h (runs): ", "Tags alerts 24h: ")), self.run_cases(bodies)):
            with self.subTest(long=label):
                lines = result["out"][0]["json"]["lines"]
                self.assertIn("Members per type: -=1, GUEST=12, MEMBER=2000", lines)
                self.assertIn("Alert refs: memberno_conflict ***042", lines)
                self.assertIn("Alerts 24h (runs): created_after_wait=1, waiting_for_staff=2", lines)
                cut = [line for line in lines if MORE_RE.search(line)]
                self.assertEqual(len(cut), 1, cut)
                self.assertTrue(cut[0].startswith(label) and cut[0].endswith(" more (see the VM record)" + (
                    " (refs: digest on the VM)" if label.startswith("Member core") else "")), cut[0])
                self.assertLessEqual(len(cut[0]), 300)
                self.assertFalse(any(line.startswith(other) and MORE_RE.search(line) for line in lines))
                self.assert_sent(result, "[XB daily] MEMBER-SYNC WARN 2026-11-07", SUBJECT_V2_RE)

    def test_the_cut_order_is_members_refs_tags_core_then_sync_alerts(self):
        core, tags = worst_tools()
        (result,) = self.run_cases([build(**worst_v1_parts(), member_tools={"member_core": core, "tags": tags})])
        lines = result["out"][0]["json"]["lines"]
        members = next(line for line in lines if line.startswith("Members per type: "))
        refs = next(line for line in lines if line.startswith("Alert refs: "))
        self.assertRegex(members, r"^Members per type: \+12 more \(see the VM record\)$")
        self.assertRegex(refs, r"^Alert refs: \+5 more \(see the VM record\)$")
        # A one-line tool list keeps what fits on its line first.
        for prefix in ("Tags alerts 24h: ", "Member core alerts 24h (runs): "):
            line = next(line for line in lines if line.startswith(prefix))
            self.assertRegex(line, MORE_RE)
            self.assertLessEqual(len(line), 300)


class ErrorsOnlyShapeTests(unittest.TestCase):
    """#288 slice 3, owner 22 Nov 2026 ("Yes, errors only"): no email on a CLEAN morning, but a logged row."""

    @classmethod
    def setUpClass(cls):
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_email_is_due_reads_only_the_flag(self):
        node = self.nodes["Email Is Due"]
        self.assertEqual(node["type"], "n8n-nodes-base.if")
        conditions = node["parameters"]["conditions"]
        self.assertEqual(conditions["options"]["typeValidation"], "strict")
        self.assertEqual([(c["leftValue"], c["rightValue"], c["operator"]) for c in conditions["conditions"]],
                         [("={{ $json.email === true }}", True, {"type": "boolean", "operation": "equals"})])

    def test_log_clean_summary_writes_one_closed_row(self):
        node = self.nodes["Log Clean Summary"]
        params = node["parameters"]
        self.assertEqual((node["type"], node["typeVersion"]), ("n8n-nodes-base.dataTable", 1.1))
        self.assertEqual((params["resource"], params["operation"]), ("row", "insert"))
        self.assertEqual(params["dataTableId"], {"__rl": True, "value": "REPLACE_WITH_ALERT_LOG_TABLE_ID", "mode": "id"})
        columns = params["columns"]
        self.assertEqual(columns["mappingMode"], "defineBelow")
        self.assertEqual(columns["value"], {"system": "MEMBER-SYNC", "key": "daily_summary", "severity": "INFO",
                                            "outcome": "clean_not_sent", "code": "", "folded_count": "={{ 0 }}",
                                            "sent_at": "={{ new Date().toISOString() }}"})
        # The same columns, in the same order and types, as the sender's own insert into xb_alert_email_log.
        sub = json.loads(SUB_WORKFLOW.read_text(encoding="utf-8"))
        sender = next(n for n in sub["nodes"] if n["name"] == "Log Email Sent")["parameters"]
        self.assertEqual(params["dataTableId"], sender["dataTableId"])
        self.assertEqual(columns["schema"], sender["columns"]["schema"])
        # A failed insert must answer 500 (the VM records send_failed, the health check sees no row): never 202.
        self.assertEqual(node["onError"], "continueErrorOutput")
        for key in ("alwaysOutputData", "retryOnFail", "executeOnce", "continueOnFail"):
            self.assertNotIn(key, node)
        respond = self.nodes["Respond Clean Logged"]["parameters"]
        self.assertEqual(respond, {"respondWith": "json", "responseBody": "={{ { ok: true, outcome: 'clean_not_sent' } }}",
                                   "options": {"responseCode": 202}})
        self.assertEqual(sorted(n["type"] for n in self.workflow["nodes"]).count("n8n-nodes-base.dataTable"), 1)

    def test_notes_name_the_rule_and_the_new_placeholder(self):
        notes = "\n".join(n["parameters"]["content"] for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.stickyNote")
        for text in ("**Errors only (#288 slice 3, owner 22 Nov 2026):**", "`clean_not_sent`", "answers 202",
                     "FIRST CREATE", "`forms_flow_silent`", "REPLACE_WITH_ALERT_LOG_TABLE_ID", "- **Log Clean Summary**"):
            self.assertIn(text, notes)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class ErrorsOnlyCodeTests(unittest.TestCase):
    run_cases = WorkflowCodeExecutionTests.run_cases

    def flags(self, bodies, rows=None):
        return [result["out"][0]["json"]["email"] for result in self.run_cases(bodies, rows)]

    def test_only_a_morning_that_needs_a_look_is_emailed(self):
        clean = build()
        stop = build(task=stop_sync_task())
        first = build(first_create=True)
        silent = copy.deepcopy(clean)
        silent["digest"]["alerts"]["forms_flow_silent"] = 1
        counts_only = copy.deepcopy(clean)
        counts_only["digest"]["alerts"].update({"forms_held": 2, "forms_would_heal": 1})
        warn = build(member_tools={"member_core": None, "tags": TAGS_CASES["WARN"]()})
        self.assertEqual((clean["result"], stop["result"], first["result"], warn["result"]), ("CLEAN", "STOP", "CLEAN", "CLEAN"))
        self.assertEqual(self.flags([clean, stop, first, silent, counts_only, warn]), [False, True, True, True, False, True])

    def test_a_clean_row_never_changes_the_senders_decision(self):
        # Yesterday's and today's clean_not_sent rows: the STOP summary is still sent, never folded or held.
        rows = [daily_row(5, outcome="clean_not_sent"), daily_row(60 * 24, outcome="clean_not_sent")]
        rows += [daily_row(m, key="possible_duplicate_member") for m in range(1, 12)]
        (result,) = self.run_cases([build(task=stop_sync_task())], rows=rows)
        (plan,) = result["plan"]
        self.assertIsNone(plan["json"]["held_outcome"])
        self.assertEqual(plan["json"]["mail"]["subject"], "[XB daily] MEMBER-SYNC STOP 2026-11-07")


if __name__ == "__main__":
    unittest.main()
