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
}
EXPECTED_EDGES = {
    (WEBHOOK_NODE, 0, CODE_NODE),
    (CODE_NODE, 0, "Event Is Valid"),
    (CODE_NODE, 1, "Respond Internal Error"),
    ("Event Is Valid", 0, CALL_NODE),
    ("Event Is Valid", 1, "Respond Event Rejected"),
    (CALL_NODE, 0, "Email Was Sent"),
    (CALL_NODE, 1, "Respond Internal Error"),
    ("Email Was Sent", 0, "Respond Summary Sent"),
    ("Email Was Sent", 1, "Email Send Failed"),
    ("Email Send Failed", 0, "Respond Email Failed"),
    ("Email Send Failed", 1, "Respond Internal Error"),
}
# The VM records these as http_status; send_failed for anything but 200.
RESPONSE_CODES = {
    "Respond Summary Sent": 200,
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


def build(task=None, config=None, digest=None, members=None, new_members=None, first_create=False):
    """An event exactly as the VM script builds it, from fake child answers."""

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
                                first_create=first_create, expected_member_type="GUEST")
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

    def assert_sent(self, result, subject):
        (item,) = result["out"]
        self.assertIs(item["json"]["valid"], True, item)
        self.assertEqual(set(item["json"]), {"valid", "severity", "short", "lines"})
        (sub,) = result["sub"]
        self.assertIs(sub["json"]["valid"], True, sub)
        (plan,) = result["plan"]
        self.assertIsNone(plan["json"]["held_outcome"])
        mail = plan["json"]["mail"]
        self.assertEqual(mail["subject"], subject)
        self.assertRegex(mail["subject"], SUBJECT_RE)
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


if __name__ == "__main__":
    unittest.main()
