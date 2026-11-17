"""Offline contract of the inactive shared "XB Ops Alert Email" n8n sub-workflow (#288, slice 1).

No n8n instance, Gmail account or Data Table is contacted. The committed Code nodes are executed
with the local Node runtime against synthetic requests and synthetic log rows. Every value here is
fake.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows" / "ops_alert_email.workflow.json"
CALLER = ROOT / "n8n-workflows" / "ac2_stock_alert.workflow.json"
README = ROOT / "n8n-workflows" / "README.md"
TABLE_DOC = ROOT / "docs" / "ops_alert_email_log.md"
VALIDATE = "Validate Alert Request"
DECIDE = "Decide Fold Or Send"
GMAIL = "Send Alert Email"
RECIPIENT_PLACEHOLDER = "REPLACE_WITH_OPERATIONS_INBOX"
TABLE_PLACEHOLDER = "REPLACE_WITH_ALERT_LOG_TABLE_ID"
INPUT_FIELDS = [("system", "string"), ("severity", "string"), ("key", "string"), ("short", "string"),
                ("lines", "array")]
LOG_COLUMNS = ["system", "key", "severity", "outcome", "code", "folded_count", "sent_at"]
LOG_NODES = ("Log Held Back Alert", "Log Email Sent", "Log Send Failed")
RETURN_NODES = ("Return Sent", "Return Send Failed", "Return Held Back", "Return Rejected", "Return Internal Error")
ALLOWED_TYPES = {
    "n8n-nodes-base.executeWorkflowTrigger",
    "n8n-nodes-base.code",
    "n8n-nodes-base.if",
    "n8n-nodes-base.dataTable",
    "n8n-nodes-base.gmail",
    "n8n-nodes-base.set",
    "n8n-nodes-base.stickyNote",
}
EXPECTED_EDGES = {
    ("When Called By Another Workflow", 0, VALIDATE),
    (VALIDATE, 0, "Request Is Valid"),
    (VALIDATE, 1, "Return Internal Error"),
    ("Request Is Valid", 0, "Read Recent Log"),
    ("Request Is Valid", 1, "Return Rejected"),
    ("Read Recent Log", 0, DECIDE),
    (DECIDE, 0, "Hold Back Alert"),
    (DECIDE, 1, "Return Internal Error"),
    ("Hold Back Alert", 0, "Log Held Back Alert"),
    ("Hold Back Alert", 1, GMAIL),
    ("Log Held Back Alert", 0, "Flood Notice Due"),
    ("Flood Notice Due", 0, GMAIL),
    ("Flood Notice Due", 1, "Return Held Back"),
    (GMAIL, 0, "Log Email Sent"),
    (GMAIL, 1, "Classify Send Error"),
    ("Log Email Sent", 0, "Return Sent"),
    ("Classify Send Error", 0, "Log Send Failed"),
    ("Log Send Failed", 0, "Return Send Failed"),
}
SYSTEMS = ("AC2-STOCK", "ENERGYGRID", "MEMBER-SYNC")
SUBJECT_RE = re.compile(r"^\[XB alert\] (AC2-STOCK|ENERGYGRID|MEMBER-SYNC) (ERROR|WARN|INFO) [A-Za-z0-9_:+.-]{1,120}$")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CANARY = "CANARY-NOTE-0001-XYZ"
# 6 Nov 2026 14:30:00 SGT, a fixed clock for every Decide run.
NOW = datetime(2026, 11, 6, 6, 30, 0, tzinfo=timezone.utc)

HARNESS = r"""
const fs = require('fs');
const [workflowPath, casesPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const codeOf = (name) => wf.nodes.find((n) => n.name === name).parameters.jsCode;
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const realNow = Date.now;
const results = cases.map((c) => {
  const $input = { all: () => c.raw || c.items.map((json) => ({ json })) };
  const $ = (name) => {
    if (!(name in (c.nodes || {}))) throw new Error('unexpected node reference: ' + name);
    return { first: () => ({ json: c.nodes[name] }) };
  };
  Date.now = () => (c.now_ms === undefined ? realNow() : c.now_ms);
  try {
    return { ok: true, out: new Function('$input', '$', codeOf(c.node))($input, $) };
  } catch (error) {
    return { ok: false, error: String(error.message) };
  } finally {
    Date.now = realNow;
  }
});
process.stdout.write(JSON.stringify(results));
"""


def request(**overrides):
    value = {
        "system": "AC2-STOCK",
        "severity": "ERROR",
        "key": "run_failed:ledger_mismatch",
        "short": "run_failed:ledger_mismatch",
        "lines": ["AC2 stock export: ERROR", "", "- Hourly export FAILED: ledger_mismatch", "Run: 01234567"],
    }
    value.update(overrides)
    return value


def validated(**overrides):
    return {"valid": True, **request(**overrides)}


def row(minutes_ago, outcome="sent", system="AC2-STOCK", key="run_failed:ledger_mismatch", row_id=None, **extra):
    at = NOW - timedelta(minutes=minutes_ago)
    value = {"id": row_id if row_id is not None else int(10000 - minutes_ago * 10), "system": system, "key": key,
             "severity": "ERROR", "outcome": outcome, "code": "", "folded_count": 0,
             "sent_at": at.isoformat().replace("+00:00", "Z"),
             "createdAt": at.isoformat(), "updatedAt": at.isoformat()}
    value.update(extra)
    return value


def load_workflow():
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


def walk_keys(value, trail=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield trail + (key,), key, child
            yield from walk_keys(child, trail + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_keys(child, trail + (index,))


def edges_of(workflow):
    return {
        (source, index, link["node"])
        for source, outputs in workflow["connections"].items()
        for index, branch in enumerate(outputs["main"])
        for link in branch
    }


class OpsAlertEmailShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load_workflow()
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_inactive_mcp_hidden_and_free_of_live_identity(self):
        self.assertEqual(self.workflow["name"], "XB Ops Alert Email")
        self.assertIs(self.workflow["active"], False)
        settings = self.workflow["settings"]
        self.assertIs(settings["availableInMCP"], False)
        self.assertEqual(settings["callerPolicy"], "workflowsFromSameOwner")
        self.assertEqual(settings["saveDataSuccessExecution"], "none")
        self.assertIs(settings["saveManualExecutions"], False)
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES, node["name"])

    def test_no_credential_id_or_address_anywhere(self):
        for path in (WORKFLOW, CALLER):
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            for trail, key, _ in walk_keys(data):
                self.assertNotIn(key.lower(), ("credentials", "credentialid", "credential_id"), (path.name, trail))
            self.assertEqual(EMAIL_RE.findall(text), [], path.name)
            self.assertNotRegex(text.lower(), r"telegram|chat_?id|bot[0-9]{6,}:", path.name)
        for path in (README, TABLE_DOC):
            emails = [e for e in EMAIL_RE.findall(path.read_text(encoding="utf-8")) if not e.endswith("example.com")]
            self.assertEqual(emails, [], path.name)

    def test_trigger_declares_exactly_the_closed_input(self):
        trigger = self.nodes["When Called By Another Workflow"]
        self.assertEqual(trigger["type"], "n8n-nodes-base.executeWorkflowTrigger")
        params = trigger["parameters"]
        self.assertEqual(params["inputSource"], "workflowInputs")
        declared = [(v["name"], v["type"]) for v in params["workflowInputs"]["values"]]
        self.assertEqual(declared, INPUT_FIELDS)
        triggers = [n for n in self.workflow["nodes"] if "trigger" in n["type"].lower() or n["type"].endswith("webhook")]
        self.assertEqual([n["name"] for n in triggers], ["When Called By Another Workflow"])

    def test_edges_are_exactly_the_reviewed_graph(self):
        self.assertEqual(edges_of(self.workflow), EXPECTED_EDGES)
        working = {name for name, node in self.nodes.items() if node["type"] != "n8n-nodes-base.stickyNote"}
        self.assertEqual(working, {e[0] for e in EXPECTED_EDGES} | {e[2] for e in EXPECTED_EDGES})
        # Every path ends in a Return node; nothing stops the caller with an error.
        sinks = working - {e[0] for e in EXPECTED_EDGES}
        self.assertEqual(sinks, set(RETURN_NODES))
        for name in (VALIDATE, DECIDE, GMAIL):
            self.assertEqual(self.nodes[name].get("onError"), "continueErrorOutput", name)

    def test_a_failed_log_read_reaches_decide_once(self):
        # G2 H1: with an error output wired too, a failed read ran Decide twice (once on the empty
        # always-output item, once on the error item) and sent two emails. One regular output only.
        read = self.nodes["Read Recent Log"]
        self.assertEqual(read.get("onError"), "continueRegularOutput")
        self.assertIs(read.get("alwaysOutputData"), True)
        self.assertEqual(self.workflow["connections"]["Read Recent Log"],
                         {"main": [[{"node": DECIDE, "type": "main", "index": 0}]]})
        feeding_decide = [(source, index) for source, index, target in edges_of(self.workflow) if target == DECIDE]
        self.assertEqual(feeding_decide, [("Read Recent Log", 0)])

    def test_gmail_is_one_plain_text_send_to_the_placeholder_only(self):
        node = self.nodes[GMAIL]
        params = node["parameters"]
        self.assertEqual((params["resource"], params["operation"]), ("message", "send"))
        self.assertEqual(params["sendTo"], RECIPIENT_PLACEHOLDER)
        self.assertEqual(params["emailType"], "text")
        self.assertEqual(params["subject"], "={{ $('Decide Fold Or Send').first().json.mail.subject }}")
        self.assertEqual(params["message"], "={{ $('Decide Fold Or Send').first().json.mail.text }}")
        self.assertIs(params["options"]["appendAttribution"], False)
        self.assertEqual(set(params["options"]), {"appendAttribution"}, "no cc, bcc, reply-to or sender name")
        # No automatic retry: a retry after a timeout can double-send (same rule as Owner Email).
        self.assertIs(node.get("retryOnFail"), False)
        for key in ("maxTries", "waitBetweenTries", "continueOnFail", "alwaysOutputData"):
            self.assertNotIn(key, node)
        senders = [n["name"] for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.gmail"]
        self.assertEqual(senders, [GMAIL])
        # The placeholder is the only recipient value in the whole export.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count('"sendTo"'), 1)

    def test_every_outcome_is_logged_and_a_log_failure_never_blocks(self):
        outcomes = {}
        for name in LOG_NODES:
            node = self.nodes[name]
            params = node["parameters"]
            self.assertEqual((params["resource"], params["operation"]), ("row", "insert"), name)
            self.assertEqual(params["dataTableId"], {"__rl": True, "value": TABLE_PLACEHOLDER, "mode": "id"}, name)
            columns = params["columns"]
            self.assertEqual(columns["mappingMode"], "defineBelow", name)
            self.assertEqual(set(columns["value"]), set(LOG_COLUMNS), name)
            self.assertEqual([c["id"] for c in columns["schema"]], LOG_COLUMNS, name)
            self.assertEqual(node.get("onError"), "continueRegularOutput", name)
            self.assertIs(node.get("alwaysOutputData"), True, name)
            outcomes[name] = columns["value"]["outcome"]
        self.assertEqual(outcomes["Log Email Sent"], "sent")
        self.assertEqual(outcomes["Log Send Failed"], "send_failed")
        self.assertEqual(outcomes["Log Held Back Alert"], "={{ $('Decide Fold Or Send').first().json.held_row.outcome }}")
        self.assertEqual(self.nodes["Log Send Failed"]["parameters"]["columns"]["value"]["code"],
                         "={{ $('Classify Send Error').first().json.send_code }}")
        read = self.nodes["Read Recent Log"]["parameters"]
        self.assertEqual((read["operation"], read["orderBy"], read["orderByColumn"], read["orderByDirection"]),
                         ("get", True, "id", "DESC"))
        self.assertEqual(read["dataTableId"]["value"], TABLE_PLACEHOLDER)
        self.assertIs(self.nodes["Read Recent Log"].get("alwaysOutputData"), True)
        # G2 L1: the 500-row bound is per system; Decide filters again as a second guard.
        self.assertEqual(read["matchType"], "allConditions")
        self.assertEqual(read["filters"], {"conditions": [
            {"keyName": "system", "condition": "eq", "keyValue": "={{ $json.system }}"}]})
        self.assertFalse(read["returnAll"])
        self.assertEqual(read["limit"], 500)
        # G2 L3: a send_failed row carries the same k as the send it stands for; the doc says so.
        self.assertEqual(self.nodes["Log Send Failed"]["parameters"]["columns"]["value"]["folded_count"],
                         "={{ $('Decide Fold Or Send').first().json.mail_row.folded_count }}")
        self.assertIn("On a `sent` or `send_failed` row of an alert", TABLE_DOC.read_text(encoding="utf-8"))

    def test_send_failed_is_returned_never_thrown(self):
        failed = {a["name"]: a["value"] for a in
                  self.nodes["Return Send Failed"]["parameters"]["assignments"]["assignments"]}
        self.assertEqual(failed["outcome"], "={{ $('Decide Fold Or Send').first().json.held_outcome || 'send_failed' }}")
        self.assertEqual(failed["code"], "={{ $('Classify Send Error').first().json.send_code }}")
        for name in RETURN_NODES:
            fields = [a["name"] for a in self.nodes[name]["parameters"]["assignments"]["assignments"]]
            self.assertEqual(fields, ["ok", "outcome", "code"], name)
            self.assertFalse(self.nodes[name]["parameters"].get("includeOtherFields", False), name)
        types = {n["type"] for n in self.workflow["nodes"]}
        self.assertNotIn("n8n-nodes-base.stopAndError", types)

    def test_no_global_all_system_remains(self):
        # Owner, 6 Nov 2026: each app's alerts stay separate; there is no cross-app flood mail.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("'ALL'", text)
        self.assertNotIn("ALL WARN", text)
        for path in (README, TABLE_DOC):
            self.assertNotIn("ALL WARN", path.read_text(encoding="utf-8"), path.name)

    def test_fold_window_and_flood_cap_are_settings(self):
        code = self.nodes[DECIDE]["parameters"]["jsCode"]
        self.assertIn("const FOLD_MINUTES = 60;", code)
        self.assertIn("const FLOOD_CAP = 10;", code)
        self.assertIn("FOLD_MINUTES", self.nodes["Setup"]["parameters"]["content"])

    def test_notes_and_readme(self):
        notes = "\n".join(
            n["parameters"]["content"] for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.stickyNote")
        self.assertIn("Required manual configuration", notes)
        self.assertIn(RECIPIENT_PLACEHOLDER, notes)
        self.assertIn(TABLE_PLACEHOLDER, notes)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("ops_alert_email.workflow.json", readme)
        self.assertIn(RECIPIENT_PLACEHOLDER, readme)
        self.assertIn("[XB daily]", readme)
        doc = TABLE_DOC.read_text(encoding="utf-8")
        for column in LOG_COLUMNS + ["xb_alert_email_log", "folded_flood", "send_failed"]:
            self.assertIn(column, doc)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class OpsAlertEmailCodeTests(unittest.TestCase):
    def run_cases(self, cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            done = subprocess.run([shutil.which("node"), str(harness), str(WORKFLOW), str(cases_path)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        results = json.loads(done.stdout)
        for result in results:
            self.assertTrue(result["ok"], result)
        return [result["out"] for result in results]

    def validate(self, *bodies):
        return [out[0]["json"] for out in self.run_cases([{"node": VALIDATE, "items": [b]} for b in bodies])]

    def decide(self, rows, req=None, read_error=False, raw=None):
        items = [{"error": "Data table not found"}] if read_error else (rows or [{}])
        case = {"node": DECIDE, "items": items, "now_ms": int(NOW.timestamp() * 1000),
                "nodes": {VALIDATE: req or validated()}}
        if raw is not None:
            case["raw"] = raw
        (out,) = self.run_cases([case])
        (item,) = out
        return item["json"]

    # ---- closed input ---- #
    def test_valid_request_passes_only_the_allowlisted_fields(self):
        (out,) = self.validate(request())
        self.assertEqual(out, validated())
        for system in ("AC2-STOCK", "ENERGYGRID", "MEMBER-SYNC"):
            for severity in ("ERROR", "WARN", "INFO"):
                (out,) = self.validate(request(system=system, severity=severity))
                self.assertIs(out["valid"], True, (system, severity))

    def test_unknown_system_severity_or_extra_field_is_rejected(self):
        cases = {
            "OPS_ALERT_SYSTEM": [request(system="ALL"), request(system="ac2-stock"), request(system="SHOPIFY"),
                                 request(system=None)],
            "OPS_ALERT_SEVERITY": [request(severity="CRITICAL"), request(severity="error"), request(severity=None)],
            "OPS_ALERT_KEYS": [{**request(), "recipient": "someone@example.com"}, {**request(), "note": CANARY},
                               {k: v for k, v in request().items() if k != "short"}],
            "OPS_ALERT_KEY": [request(key=""), request(key="a b"), request(key="x" * 121), request(key=7)],
            "OPS_ALERT_SHORT": [request(short="<b>x</b>"), request(short="line\nbreak")],
            "OPS_ALERT_LINES": [request(lines=[]), request(lines="one line"), request(lines=[1]),
                                request(lines=["tab\there"]), request(lines=["new\nline"]),
                                request(lines=["x" * 301]), request(lines=["ok"] * 31), request(lines=["caf\u00e9"]),
                                request(lines=["x" * 300] * 14)],
            "OPS_ALERT_INPUT": [None, [], "text"],
            # The flood notice key is reserved, so a caller's alert can never match a flood row.
            "OPS_ALERT_KEY_RESERVED": [request(key="alert_flood")],
            # A request belongs to one system: no field may name another one.
            "OPS_ALERT_OTHER_SYSTEM": [request(lines=["see ENERGYGRID"]), request(key="MEMBER-SYNC:x"),
                                       request(short="ENERGYGRID"),
                                       request(system="ENERGYGRID", key="k", short="k", lines=["AC2-STOCK too"])],
        }
        for reason, bodies in cases.items():
            for body, out in zip(bodies, self.validate(*bodies)):
                with self.subTest(reason=reason, body=str(body)[:60]):
                    expected = "OPS_ALERT_KEY" if reason == "OPS_ALERT_KEY_RESERVED" else reason
                    self.assertEqual(out, {"valid": False, "reason": expected})
        (two,) = self.run_cases([{"node": VALIDATE, "items": [request(), request()]}])
        self.assertEqual(two[0]["json"], {"valid": False, "reason": "OPS_ALERT_ITEM_COUNT"})

    # ---- subject and body ---- #
    def test_subject_format(self):
        plan = self.decide([])
        self.assertIsNone(plan["held_outcome"])
        self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK ERROR run_failed:ledger_mismatch")
        for system, severity, short in (("ENERGYGRID", "WARN", "bill_pending"),
                                        ("MEMBER-SYNC", "INFO", "possible_duplicate_member"),
                                        ("AC2-STOCK", "WARN", "large_balance+late_change")):
            plan = self.decide([], validated(system=system, severity=severity, key=short, short=short))
            self.assertEqual(plan["mail"]["subject"], "[XB alert] %s %s %s" % (system, severity, short))
            self.assertRegex(plan["mail"]["subject"], SUBJECT_RE)
            self.assertNotRegex(plan["mail"]["subject"], r"\d\d:\d\d|2026", "no time in the subject")

    def test_no_field_outside_the_allowlist_reaches_the_body_or_the_log(self):
        req = {**validated(), "recipient": "someone@example.com", "note": CANARY, "token": "FAKE-TOKEN-0001"}
        plan = self.decide([row(30, key="other_key", note=CANARY, extra_secret="FAKE-TOKEN-0001")], req)
        rendered = json.dumps(plan)
        for leak in ("someone@example.com", CANARY, "FAKE-TOKEN"):
            self.assertNotIn(leak, rendered)
        text = plan["mail"]["text"]
        self.assertTrue(text.startswith("\n".join(request()["lines"]) + "\n\n--\n"), text)
        self.assertEqual(set(plan["mail_row"]), {"system", "key", "severity", "folded_count", "code", "sent_at"})
        self.assertNotRegex(text, EMAIL_RE)
        self.assertRegex(text, r"^[\x20-\x7E\n]+$")

    # ---- fold ---- #
    def test_same_key_within_60_minutes_is_folded(self):
        for minutes in (1, 30, 59.9):
            plan = self.decide([row(minutes)])
            self.assertEqual(plan["held_outcome"], "folded", minutes)
            self.assertIsNone(plan["mail"])
            self.assertEqual(plan["held_row"]["outcome"], "folded")
            self.assertEqual(plan["held_row"]["system"], "AC2-STOCK")
            self.assertEqual(plan["held_row"]["sent_at"], "2026-11-06T06:30:00.000Z")

    def test_other_keys_old_sends_and_failed_sends_do_not_fold(self):
        for rows in ([row(10, key="large_balance")], [row(10, system="ENERGYGRID")], [row(60)], [row(61)],
                     [row(5, outcome="send_failed")], [row(5, outcome="folded")]):
            plan = self.decide(rows)
            self.assertIsNone(plan["held_outcome"], rows)
            self.assertIsNotNone(plan["mail"], rows)

    def test_next_real_send_says_how_many_were_folded_since_when(self):
        # Sent 13:00 SGT, folded at 13:20 and 13:50 (both inside the window), real send at 14:30 SGT.
        rows = [row(90, row_id=1), row(70, outcome="folded", row_id=2), row(40, outcome="folded", row_id=3),
                row(35, outcome="folded", key="large_balance", row_id=4)]
        plan = self.decide(rows)
        self.assertIsNone(plan["held_outcome"])
        self.assertIn("\n\n(+2 folded since 13:00)\n", plan["mail"]["text"])
        self.assertEqual(plan["mail_row"]["folded_count"], 2)
        # Nothing folded since the last send: no note at all.
        plan = self.decide([row(70, outcome="folded", row_id=1), row(65, row_id=2)])
        self.assertNotIn("folded since", plan["mail"]["text"])
        self.assertEqual(plan["mail_row"]["folded_count"], 0)

    # ---- flood ---- #
    def test_flood_cap_holds_back_and_sends_one_flood_mail(self):
        busy = [row(5 * i + 1, key="k%d" % i, row_id=100 + i) for i in range(10)]
        plan = self.decide(busy)
        self.assertEqual(plan["held_outcome"], "folded_flood")
        self.assertEqual(plan["held_row"]["outcome"], "folded_flood")
        self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK WARN alert_flood")
        self.assertRegex(plan["mail"]["subject"], SUBJECT_RE)
        self.assertIn("First held back: AC2-STOCK ERROR run_failed:ledger_mismatch", plan["mail"]["text"])
        self.assertEqual({k: plan["mail_row"][k] for k in ("system", "key", "severity")},
                         {"system": "AC2-STOCK", "key": "alert_flood", "severity": "WARN"})
        # A flood notice already sent in the last 60 minutes: held back, no second notice.
        plan = self.decide(busy + [row(20, key="alert_flood", row_id=200)])
        self.assertEqual(plan["held_outcome"], "folded_flood")
        self.assertIsNone(plan["mail"])
        # The previous notice is older than 60 minutes: one new notice.
        plan = self.decide(busy + [row(61, key="alert_flood", row_id=1)])
        self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK WARN alert_flood")
        # Another system's recent flood notice does not stand in for this system's notice.
        plan = self.decide(busy + [row(20, system="ENERGYGRID", key="alert_flood", row_id=201)])
        self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK WARN alert_flood")

    def test_flood_mail_names_the_flooding_system_never_all(self):
        for system in SYSTEMS:
            busy = [row(5 * i + 1, system=system, key="k%d" % i) for i in range(10)]
            plan = self.decide(busy, validated(system=system, key="k99", short="k99", lines=["fake alert"]))
            subject = plan["mail"]["subject"]
            self.assertEqual(subject, "[XB alert] %s WARN alert_flood" % system)
            self.assertNotIn("ALL", subject.split())
            self.assertEqual(plan["mail_row"]["system"], system)
            self.assertEqual(plan["held_row"]["system"], system)

    def test_a_flood_in_one_system_never_holds_back_another(self):
        # 11 ENERGYGRID sends in the last hour plus its flood notice: an AC2-STOCK alert, even with the
        # same key as an ENERGYGRID alert sent 5 minutes ago, is sent at once with no fold note.
        flood = [row(5 * i + 1, system="ENERGYGRID", key="k%d" % i) for i in range(11)]
        flood += [row(4, system="ENERGYGRID", key="alert_flood"), row(5, system="ENERGYGRID"),
                  row(3, system="ENERGYGRID", outcome="folded_flood"), row(2, system="ENERGYGRID", outcome="folded")]
        plan = self.decide(flood)
        self.assertIsNone(plan["held_outcome"])
        self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK ERROR run_failed:ledger_mismatch")
        self.assertNotIn("folded since", plan["mail"]["text"])
        self.assertEqual(plan["mail_row"]["folded_count"], 0)
        # And the other way round: AC2-STOCK at its cap never delays a MEMBER-SYNC alert.
        ac2 = [row(5 * i + 1, key="k%d" % i) for i in range(11)]
        plan = self.decide(ac2, validated(system="MEMBER-SYNC", severity="WARN", key="possible_duplicate_member",
                                          short="possible_duplicate_member", lines=["fake member-sync alert"]))
        self.assertIsNone(plan["held_outcome"])
        self.assertEqual(plan["mail"]["subject"], "[XB alert] MEMBER-SYNC WARN possible_duplicate_member")

    def test_no_email_ever_names_two_systems(self):
        others = lambda own: [s for s in SYSTEMS if s != own]
        for own in SYSTEMS:
            req = validated(system=own, severity="ERROR", key="shared_key", short="shared_key", lines=["fake line"])
            noise = []
            for other in others(own):
                noise += [row(5 * i + 1, system=other, key="k%d" % i) for i in range(11)]
                noise += [row(30, system=other, key="shared_key"), row(20, system=other, key="shared_key", outcome="folded"),
                          row(10, system=other, key="alert_flood")]
            scenarios = {
                "send": noise,
                "send with fold note": noise + [row(90, system=own, key="shared_key", row_id=1),
                                                row(70, system=own, key="shared_key", outcome="folded", row_id=2)],
                "fold": noise + [row(10, system=own, key="shared_key")],
                "flood": noise + [row(5 * i + 1, system=own, key="own%d" % i) for i in range(10)],
            }
            for name, rows in scenarios.items():
                with self.subTest(system=own, scenario=name):
                    plan = self.decide(rows, req)
                    for part in ("held_row", "mail_row"):
                        if plan[part]:
                            self.assertEqual(plan[part]["system"], own)
                    if plan["mail"]:
                        rendered = plan["mail"]["subject"] + "\n" + plan["mail"]["text"]
                        named = {s for s in SYSTEMS if s in rendered}
                        self.assertEqual(named, {own}, rendered)
                    if name == "send with fold note":
                        # Only its own folded row counts; the other systems' rows add nothing.
                        self.assertIn("(+1 folded since 13:00)", plan["mail"]["text"])
                        self.assertEqual(plan["mail_row"]["folded_count"], 1)

    def test_below_the_cap_or_outside_the_hour_sends_normally(self):
        nine = [row(5 * i + 1, key="k%d" % i) for i in range(9)]
        for rows in (nine, nine + [row(61, key="k9")], nine + [row(3, key="k9", outcome="send_failed")],
                     nine + [row(3, key="alert_flood")], nine + [row(3, system="ENERGYGRID", key="k9")]):
            plan = self.decide(rows)
            self.assertIsNone(plan["held_outcome"])
            self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK ERROR run_failed:ledger_mismatch")

    def test_fold_wins_over_flood_and_a_held_key_reports_its_flood_folds_later(self):
        busy = [row(5 * i + 1, key="k%d" % i) for i in range(10)]
        plan = self.decide(busy + [row(2)])
        self.assertEqual(plan["held_outcome"], "folded")
        plan = self.decide([row(30, outcome="folded_flood", row_id=1)])
        self.assertIn("(+1 folded since 14:00)", plan["mail"]["text"])

    # ---- failure handling ---- #
    def test_log_read_failure_still_sends_and_is_coded(self):
        plan = self.decide(None, read_error=True)
        self.assertIsNone(plan["held_outcome"])
        self.assertIsNotNone(plan["mail"])
        self.assertEqual(plan["mail_row"]["code"], "log_read_failed")

    def test_each_read_shape_gives_one_decision_and_at_most_one_send(self):
        # The shapes Read Recent Log can hand Decide on n8n 2.39.10 with continueRegularOutput and
        # alwaysOutputData: rows, the empty item of an empty table, an item carrying `error` (on the
        # json or on the item), and the bare request echo the Data Table router returns for some
        # failures (G2 L4). Each is one Decide run; each must give one plan and at most one email.
        busy_same_key = [row(10)]
        echo = validated()
        shapes = {
            "rows": ({"rows": busy_same_key}, ""),
            "empty table": ({"rows": [{}]}, ""),
            "error on json": ({"raw": [{"json": {"error": "Data table with ID x could not be found"}}]}, "log_read_failed"),
            "error on item": ({"raw": [{"json": {}, "error": {"message": "boom"}}]}, "log_read_failed"),
            "error with echo": ({"raw": [{"json": {**echo, "error": "boom"}}]}, "log_read_failed"),
            "bare request echo": ({"raw": [{"json": echo}]}, "log_read_failed"),
        }
        for name, (shape, code) in shapes.items():
            with self.subTest(shape=name):
                case = {"node": DECIDE, "items": shape.get("rows", []), "now_ms": int(NOW.timestamp() * 1000),
                        "nodes": {VALIDATE: validated()}}
                if "raw" in shape:
                    case["raw"] = shape["raw"]
                (out,) = self.run_cases([case])
                self.assertEqual(len(out), 1, "exactly one decision")
                plan = out[0]["json"]
                self.assertIsInstance(plan["mail"], (dict, type(None)), "at most one email")
                row_written = plan["mail_row"] or plan["held_row"]
                self.assertEqual(row_written["code"], code)
                if name == "rows":
                    self.assertEqual(plan["held_outcome"], "folded")
                else:
                    # No usable history: decided as new, so exactly one email goes out.
                    self.assertIsNone(plan["held_outcome"])
                    self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK ERROR run_failed:ledger_mismatch")
                    self.assertNotIn("folded since", plan["mail"]["text"])

    def test_failed_sends_are_not_counted_in_the_fold_note(self):
        # G2 L2, mutant M4: a send_failed row is an attempt, not a held-back alert.
        rows = [row(90, row_id=1), row(70, outcome="folded", row_id=2),
                row(40, outcome="send_failed", row_id=3), row(30, outcome="send_failed", row_id=4)]
        plan = self.decide(rows)
        self.assertIsNone(plan["held_outcome"])
        self.assertIn("(+1 folded since 13:00)", plan["mail"]["text"])
        self.assertEqual(plan["mail_row"]["folded_count"], 1)
        plan = self.decide([row(90, row_id=1), row(30, outcome="send_failed", row_id=2)])
        self.assertNotIn("folded since", plan["mail"]["text"])
        self.assertEqual(plan["mail_row"]["folded_count"], 0)

    def test_a_failed_flood_notice_is_retried(self):
        # G2 L2, mutant M8: only a flood notice that was sent stops the next one.
        busy = [row(5 * i + 1, key="k%d" % i) for i in range(10)]
        plan = self.decide(busy + [row(10, key="alert_flood", outcome="send_failed")])
        self.assertEqual(plan["held_outcome"], "folded_flood")
        self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK WARN alert_flood")

    def test_future_dated_rows_never_count(self):
        # G2 L2, mutant M9: a row dated after now (a clock that ran ahead) neither folds nor floods.
        plan = self.decide([row(-5)])
        self.assertIsNone(plan["held_outcome"])
        self.assertIsNotNone(plan["mail"])
        plan = self.decide([row(-5 - i, key="k%d" % i) for i in range(10)])
        self.assertIsNone(plan["held_outcome"])
        self.assertEqual(plan["mail"]["subject"], "[XB alert] AC2-STOCK ERROR run_failed:ledger_mismatch")
        # A future-dated send must not hide a real send 10 minutes ago: still folded.
        plan = self.decide([row(10, row_id=1), row(-5, row_id=2)])
        self.assertEqual(plan["held_outcome"], "folded")
        self.assertIsNone(plan["mail"])
        # A future-dated fold is not counted in the note either.
        plan = self.decide([row(90, row_id=1), row(-5, outcome="folded", row_id=2)])
        self.assertNotIn("folded since", plan["mail"]["text"])

    def test_send_error_code_is_closed(self):
        node = next(n for n in load_workflow()["nodes"] if n["name"] == "Classify Send Error")
        (assignment,) = node["parameters"]["assignments"]["assignments"]
        expression = assignment["value"]
        self.assertTrue(expression.startswith("={{ ") and expression.endswith(" }}"))
        body = expression[4:-3]
        script = ("const cases = %s; process.stdout.write(JSON.stringify(cases.map(($json) => (%s))));"
                  % (json.dumps([
                      # The Gmail 2.1 error item: { json: { error: <message> } } (G2 L6). Its NodeApiError
                      # messages carry no status number, so these are gmail_error.
                      {"error": "Forbidden - perhaps check your credentials?"},
                      {"error": "The resource you are requesting could not be found"},
                      {"error": "Invalid recipient someone@example.com"},
                      # Only a message that itself carries a 4xx or 5xx status gives http_<status>.
                      {"error": "Request failed with status code 429"},
                      # Defensive: an object shape, or nothing at all, still maps to a closed code.
                      {"error": {"message": "socket hang up", "code": "ECONNRESET"}},
                      {"error": {"message": "Forbidden", "httpCode": "403"}},
                      {},
                  ]), body))
        done = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        codes = json.loads(done.stdout)
        self.assertEqual(codes, ["gmail_error", "gmail_error", "gmail_error", "http_429", "gmail_error", "http_403",
                                 "gmail_error"])
        for code in codes:
            self.assertRegex(code, r"^(gmail_error|http_[45][0-9][0-9])$")

    # ---- daily summary path (#288 slice 3) ---- #
    def daily(self, **overrides):
        value = {"system": "MEMBER-SYNC", "severity": "INFO", "key": "daily_summary", "short": "CLEAN 2026-11-07",
                 "lines": ["MEMBER-SYNC daily summary 2026-11-07: CLEAN", "No stop condition found."]}
        value.update(overrides)
        return {"valid": True, **value}

    def test_daily_summary_subject_and_body(self):
        plan = self.decide([], self.daily())
        self.assertIsNone(plan["held_outcome"])
        self.assertEqual(plan["mail"]["subject"], "[XB daily] MEMBER-SYNC CLEAN 2026-11-07")
        self.assertEqual(plan["mail_row"]["key"], "daily_summary")
        self.assertEqual(plan["mail_row"]["folded_count"], 0)
        text = plan["mail"]["text"]
        self.assertTrue(text.startswith("MEMBER-SYNC daily summary 2026-11-07: CLEAN\nNo stop condition found.\n\n--\n"), text)
        self.assertIn("System: MEMBER-SYNC | Daily summary", text)
        self.assertIn("Daily summaries are never folded", text)
        self.assertNotIn("folded since", text)
        plan = self.decide([], self.daily(severity="WARN", short="STOP 2026-11-07"))
        self.assertEqual(plan["mail"]["subject"], "[XB daily] MEMBER-SYNC STOP 2026-11-07")
        self.assertEqual(plan["mail_row"]["severity"], "WARN")

    def test_daily_summary_is_never_folded(self):
        rows = [row(m, system="MEMBER-SYNC", key="daily_summary") for m in (1, 5, 30)]
        rows.append(row(2, system="MEMBER-SYNC", key="daily_summary", outcome="folded"))
        plan = self.decide(rows, self.daily())
        self.assertIsNone(plan["held_outcome"])
        self.assertEqual(plan["mail"]["subject"], "[XB daily] MEMBER-SYNC CLEAN 2026-11-07")
        self.assertEqual(plan["mail_row"]["folded_count"], 0)

    def test_daily_summary_is_never_held_by_a_flood_and_never_counts_toward_one(self):
        flood = [row(m, system="MEMBER-SYNC", key="alert_%d" % m) for m in range(1, 12)]
        for rows in (flood, flood + [row(3, system="MEMBER-SYNC", key="alert_flood")]):
            plan = self.decide(rows, self.daily())
            self.assertIsNone(plan["held_outcome"])
            self.assertEqual(plan["mail"]["subject"], "[XB daily] MEMBER-SYNC CLEAN 2026-11-07")
        # The flood still holds back this system's alerts: the daily path changes nothing for them.
        alert = validated(system="MEMBER-SYNC", severity="ERROR", key="uncertain_writes", short="uncertain_writes", lines=["x"])
        self.assertEqual(self.decide(flood, alert)["held_outcome"], "folded_flood")
        # Eleven daily rows in the hour (manual re-runs) never make an alert a flood.
        dailies = [row(m, system="MEMBER-SYNC", key="daily_summary") for m in range(1, 12)]
        plan = self.decide(dailies, alert)
        self.assertIsNone(plan["held_outcome"])
        self.assertEqual(plan["mail"]["subject"], "[XB alert] MEMBER-SYNC ERROR uncertain_writes")

    def test_daily_key_and_short_are_closed(self):
        bad = [request(system="MEMBER-SYNC", key="daily_summary", short=short, lines=["x"])
               for short in ("CLEAN", "clean 2026-11-07", "CLEAN 2026-11-7", "CLEAN  2026-11-07", "CLEAN 2026-11-07 x",
                             "CLEAN_2026-11-07", "A 2026-11-07", "CLEAN\n2026-11-07", "run_failed:ledger_mismatch")]
        # A space is still refused in an alert's short: only the daily key has the status-and-date form.
        bad.append(request(short="CLEAN 2026-11-07"))
        for body, out in zip(bad, self.validate(*bad)):
            with self.subTest(short=body["short"], key=body["key"]):
                self.assertEqual(out, {"valid": False, "reason": "OPS_ALERT_SHORT"})
        (ok,) = self.validate(request(system="MEMBER-SYNC", key="daily_summary", short="STOP 2026-11-07", lines=["x"]))
        self.assertIs(ok["valid"], True)
        # A daily summary is one app's email too.
        (other,) = self.validate(request(system="MEMBER-SYNC", key="daily_summary", short="CLEAN 2026-11-07",
                                         lines=["see AC2-STOCK"]))
        self.assertEqual(other, {"valid": False, "reason": "OPS_ALERT_OTHER_SYSTEM"})


if __name__ == "__main__":
    unittest.main()
