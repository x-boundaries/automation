"""Offline contract of the inactive "Member Discount Check - Monthly Email" n8n export (#268 Q7).

No n8n instance, Gmail account or AutoCount server is contacted. The committed Code node is run
with the local Node runtime against events built by the real script (build_alert_payload), and a
valid result is handed to the committed Code node of the shared XB Ops Alert Email sub-workflow.
The workflow is a copy of the reviewed AR/AP alert's shape (ac2_arap_alert.workflow.json); the
tests pin that the graph, settings and responses stay the same. Every value here is synthetic.
"""

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import xb_member_pos_exceptions as xr  # noqa: E402

WORKFLOW = ROOT / "n8n-workflows" / "member_pos_exceptions_alert.workflow.json"
TEMPLATE = ROOT / "n8n-workflows" / "ac2_arap_alert.workflow.json"
SUB_WORKFLOW = ROOT / "n8n-workflows" / "ops_alert_email.workflow.json"
README = ROOT / "n8n-workflows" / "README.md"
RUNBOOK = ROOT / "docs" / "autocount2-automation" / "member_pos_exceptions_runbook.md"
WEBHOOK_NODE = "Member Discount Check Webhook"
CODE_NODE = "Validate Alert Event"
CALL_NODE = "Send Alert Email"
SYSTEM = "MEMBER-POS"
RUN_ID = "0123456789abcdef0123456789abcdef"
LINE_RE = re.compile(r"^[ -~]{0,300}$")
OTHER_SYSTEMS = ("AC2-STOCK", "AC2-ARAP", "ENERGYGRID", "MEMBER-SYNC", "HEALTH", "KIT-RETAG")
CANARY_MEMBER = "TEST000987"

HARNESS = r"""
const fs = require('fs');
const [workflowPath, subPath, casesPath, system] = process.argv.slice(2);
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
      const call = { system, severity: j.severity, key: j.key, short: j.short, lines: j.lines };
      sub = new Function('$input', subCode)({ all: () => [{ json: call }] });
    }
    return { ok: true, out, sub };
  } catch (error) {
    return { ok: false, error: String(error.message) };
  }
});
process.stdout.write(JSON.stringify(results));
"""


def counts(**values):
    base = dict.fromkeys(xr.COUNT_KEYS, 0)
    base.update(receipts_read=600, cancelled=4, not_positive=2, member_rate=150, member_valid=140)
    base.update(values)
    return base


def row(n=1, reason="guest", note="none", day="2026-11-10", rate="15%"):
    return {"doc_no": "CS-T%04d" % n, "outlet": "OUT01", "day": day, "rate": rate, "reason": reason,
            "member": "***987", "record_note": note}


def result(status="report", **overrides):
    value = {
        "run_id": RUN_ID, "month": "2026-11", "status": status, "failure_class": None,
        "counts": counts(guest=3, expired=1, same_receipt=1, no_member_on_receipt=8, changed_after_sale=2),
        "outlets": [{"outlet": "OUT01", "guest": 3, "expired": 1, "same_receipt": 0, "no_member_on_receipt": 7},
                    {"outlet": "OUT02", "guest": 0, "expired": 0, "same_receipt": 1, "no_member_on_receipt": 1}],
        "rows": [row(1, note="created_on_sale_day"), row(2), row(3, "expired", rate="20%"), row(4, "same_receipt"),
                 row(5, note="edited_on_or_after_sale_day")],
        "stale_outlets": [], "message": "free text " + CANARY_MEMBER, "finished_at": "2026-12-02T09:15:00+08:00",
    }
    value.update(overrides)
    return value


def event(max_listed=12, **overrides):
    return xr.build_alert_payload(result(**overrides), max_listed)


def stale_event():
    return event(status="pos_sync_stale", counts=None, outlets=[], rows=[], stale_outlets=["OUT01", "OUT02"])


def failed_event(failure_class="identity_refused"):
    return event(status="failed", failure_class=failure_class, counts=None, outlets=[], rows=[])


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
        cls.template = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    def test_inactive_mcp_hidden_unsaved_and_free_of_live_identity(self):
        self.assertIs(self.workflow["active"], False)
        self.assertEqual(self.workflow["name"], "Member Discount Check - Monthly Email")
        settings = self.workflow["settings"]
        self.assertIs(settings["availableInMCP"], False)
        self.assertEqual((settings["saveDataSuccessExecution"], settings["saveDataErrorExecution"]), ("none", "none"))
        self.assertIs(settings["saveManualExecutions"], False)
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])

    def test_same_shape_as_the_reviewed_arap_alert(self):
        rename = {"AC2 ARAP Alert Webhook": WEBHOOK_NODE}
        self.assertEqual(edges_of(self.workflow),
                         {(rename.get(s, s), i, rename.get(t, t)) for s, i, t in edges_of(self.template)})
        self.assertEqual(self.workflow["settings"], self.template["settings"])
        other_nodes = {rename.get(name, name): node for name, node in nodes_of(self.template).items()}
        self.assertEqual(set(self.nodes), set(other_nodes))
        for name, node in self.nodes.items():
            other = other_nodes[name]
            self.assertEqual((node["type"], node["typeVersion"], node.get("onError")),
                             (other["type"], other["typeVersion"], other.get("onError")), name)
            if node["type"] in ("n8n-nodes-base.if", "n8n-nodes-base.respondToWebhook"):
                same = json.dumps(node["parameters"]).replace("-8268-", "-8321-")
                self.assertEqual(json.loads(same), other["parameters"], name)

    def test_ids_never_collide_with_the_template(self):
        self.assertNotEqual(self.workflow["id"], self.template["id"])
        self.assertNotEqual(self.workflow["versionId"], self.template["versionId"])
        mine = {node["id"] for node in self.workflow["nodes"]}
        self.assertEqual(len(mine), len(self.workflow["nodes"]))
        self.assertFalse(mine & {node["id"] for node in self.template["nodes"]})

    def test_webhook_and_call_node(self):
        params = self.nodes[WEBHOOK_NODE]["parameters"]
        self.assertEqual((params["httpMethod"], params["authentication"], params["responseMode"]),
                         ("POST", "headerAuth", "responseNode"))
        self.assertEqual(params["path"], "REPLACE_WITH_MEMBER_DISCOUNT_CHECK_WEBHOOK_PATH")
        call = self.nodes[CALL_NODE]["parameters"]
        self.assertEqual(call["workflowInputs"]["value"]["system"], SYSTEM)
        self.assertEqual(call["workflowId"]["value"], "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID")
        sub = nodes_of(json.loads(SUB_WORKFLOW.read_text(encoding="utf-8")))
        # The sender accepts the system wherever it sits in its closed list (#355 added XT-CODES after it).
        self.assertRegex(sub["Validate Alert Request"]["parameters"]["jsCode"],
                         r"\nconst SYSTEMS = \[[^\]\n]*'%s'[^\]\n]*\];\n" % re.escape(SYSTEM))
        self.assertIn(SYSTEM, sub["Start Here"]["parameters"]["content"])

    def test_code_allowlist_mirrors_the_script_contract(self):
        code = self.nodes[CODE_NODE]["parameters"]["jsCode"]
        self.assertIn("'%s'" % xr.ALERT_SCHEMA, code)
        self.assertIn("'%s'" % xr.ALERT_EVENT, code)
        groups = {"KEYS": xr.ALERT_PAYLOAD_KEYS, "COUNT_KEYS": xr.COUNT_KEYS, "OUTLET_KEYS": xr.OUTLET_KEYS,
                  "ROW_KEYS": xr.ROW_KEYS, "REASONS": xr.BUCKETS, "FAILURE_CLASSES": xr.FAILURE_CLASSES}
        for name, values in groups.items():
            line = next(line for line in code.splitlines() if line.startswith("const %s = [" % name))
            listed = json.loads(line.split("=", 1)[1].strip().rstrip(";").replace("'", '"'))
            self.assertEqual(listed, list(values), name)
        self.assertIn("const MAX_ROWS = 12;", code)
        for reader in (".headers", "['headers']", '["headers"]'):
            self.assertNotIn(reader, code)

    def test_notes_readme_and_runbook(self):
        notes = "\n".join(n["parameters"]["content"] for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.stickyNote")
        for text in ("Required manual configuration", "REPLACE_WITH_MEMBER_DISCOUNT_CHECK_WEBHOOK_PATH",
                     "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "XB Ops Alert Email", SYSTEM):
            self.assertIn(text, notes)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("### member_pos_exceptions_alert.workflow.json", readme)
        self.assertIn("`MEMBER-POS`", readme)
        self.assertIn("member_pos_exceptions_alert.workflow.json", RUNBOOK.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code node")
class WorkflowCodeExecutionTests(unittest.TestCase):
    def run_cases(self, cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            done = subprocess.run([shutil.which("node"), str(harness), str(WORKFLOW), str(SUB_WORKFLOW), str(cases_path), SYSTEM],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def assert_email(self, result, severity, short, expected):
        self.assertTrue(result["ok"], result)
        (item,) = result["out"]
        self.assertIs(item["json"]["valid"], True, item)
        self.assertEqual((item["json"]["severity"], item["json"]["short"]), (severity, short))
        lines = item["json"]["lines"]
        self.assertLessEqual(len(lines), 30)
        for line in lines:
            self.assertRegex(line, LINE_RE)
        text = "\n".join(lines)
        for needle in expected:
            self.assertIn(needle, text)
        rendered = json.dumps(item)
        for leak in ("FAKE-SECRET", CANARY_MEMBER, "free text"):
            self.assertNotIn(leak, rendered)
        for other in OTHER_SYSTEMS:
            self.assertNotIn(other, text)
        (sub,) = result["sub"]
        self.assertIs(sub["json"]["valid"], True, sub)
        self.assertEqual(sub["json"]["system"], SYSTEM)
        return lines

    def test_report_email(self):
        (res,) = self.run_cases([event()])
        lines = self.assert_email(res, "WARN", "exceptions_2026-11", [
            "In-store member discounts, Nov 2026: 5 receipt(s) to check",
            "- Registered, not qualified (GUEST): 3",
            "- Membership had expired: 1",
            "- May be on the receipt that made them a member (the rate starts from the next purchase): 1",
            "- CS-T0001 | OUT01 | 2026-11-10 | 15% | registered, not qualified | member ***987 | member record created on the sale day",
            "- CS-T0002 | OUT01 | 2026-11-10 | 15% | registered, not qualified | member ***987\n",
            "- CS-T0003 | OUT01 | 2026-11-10 | 20% | membership had expired | member ***987\n",
            "- CS-T0004 | OUT01 | 2026-11-10 | 15% | may be the receipt that made them a member | member ***987\n",
            "- CS-T0005 | OUT01 | 2026-11-10 | 15% | registered, not qualified | member ***987"
            " | member record edited on or after the sale day (check its history)",
            "By outlet (registered / expired / same receipt / rate with no member on the receipt): OUT01 3/1/0/7; OUT02 0/0/1/1",
            "rate with no member on the receipt 8",
            "Read 600 receipts (4 cancelled and 2 returns or zero receipts skipped); 150 had the member rate.",
            "Run: 01234567.",
        ])
        self.assertEqual(res["out"][0]["json"]["key"], "exceptions:2026-11")
        text = "\n".join(lines)
        self.assertNotIn("more, not listed", text)
        # The check knows only when the record was created or last edited, never that its type changed.
        self.assertNotIn("type changed", text)

    def test_guest_registered_on_the_sale_day_end_to_end(self):
        # A customer registers at the till (new GUEST record that day) and the member rate is keyed on
        # that same receipt: the script's own rule and payload, then the committed Code node.
        rows = [{"DocKey": 1, "DocNo": "CS-T0001", "DocDate": xr.datetime(2026, 11, 10), "Cancelled": "F", "IsPositive": 1,
                 "MemberNo": CANARY_MEMBER, "Outlet": "OUT01", "MemberFound": CANARY_MEMBER, "MemberType": "GUEST",
                 "ExpiryDate": None, "LastModified": xr.datetime(2026, 11, 10, 15, 2),
                 "MemberCreated": xr.datetime(2026, 11, 10, 15, 1), "IsActive": "T", "Discount": "15.00%"}]
        raw = json.loads((ROOT / "config" / "member_pos_exceptions.example.json").read_text(encoding="utf-8"))
        raw["output"]["record_dir"] = "C:\\EXAMPLE\\unused"
        found, outlets, listed = xr.classify(rows, xr.validate_settings(raw))
        body = xr.build_alert_payload(result(counts=found, outlets=outlets, rows=listed), 12)
        (res,) = self.run_cases([body])
        lines = self.assert_email(res, "WARN", "exceptions_2026-11", [
            "- CS-T0001 | OUT01 | 2026-11-10 | 15% | registered, not qualified | member ***987"
            " | member record created on the sale day",
        ])
        self.assertNotIn("type changed", "\n".join(lines))

    def test_full_list_fits_the_sender_limits(self):
        rows = [row(i, note="edited_on_or_after_sale_day") for i in range(40)]
        body = event(counts=counts(guest=40, changed_after_sale=40), rows=rows,
                     outlets=[{"outlet": "OUT%02d" % i, "guest": 4, "expired": 0, "same_receipt": 0,
                               "no_member_on_receipt": 999999} for i in range(10)])
        self.assertEqual((len(body["rows"]), body["rows_not_listed"]), (12, 28))
        (res,) = self.run_cases([body])
        self.assert_email(res, "WARN", "exceptions_2026-11", ["- (+28 more, not listed"])

    def test_stale_and_failed_emails(self):
        cases = [stale_event()] + [failed_event(c) for c in xr.FAILURE_CLASSES] + [failed_event("not_a_class")]
        results = self.run_cases(cases)
        self.assert_email(results[0], "WARN", "pos_sync_stale_2026-11",
                          ["is waiting: the till data is not fully synced",
                           "Outlets with no finished POS sync since the month ended: OUT01, OUT02"])
        for body, res in zip(cases[1:], results[1:]):
            with self.subTest(failure_class=body["failure_class"]):
                self.assert_email(res, "ERROR", "run_failed:" + body["failure_class"],
                                  ["check for Nov 2026 FAILED: " + body["failure_class"], "Next: forward to Claude."])
        self.assertEqual(cases[-1]["failure_class"], "internal_error")

    def test_every_departure_from_the_contract_is_rejected(self):
        base = event()
        stale = stale_event()
        failed = failed_event()

        def with_row(**change):
            body = copy.deepcopy(base)
            body["rows"][0].update(change)
            return body

        def with_row_at(index, **change):
            body = copy.deepcopy(base)
            body["rows"][index].update(change)
            return body

        with_old_mark = copy.deepcopy(base)
        del with_old_mark["rows"][0]["record_note"]
        with_old_mark["rows"][0]["changed_after_sale"] = True

        def with_counts(**change):
            body = copy.deepcopy(base)
            body["counts"].update(change)
            return body

        cases = [
            {**base, "member_no": CANARY_MEMBER},
            {**base, "message": "free text"},
            {k: v for k, v in base.items() if k != "rows"},
            {**base, "schema": "xb.member.pos_exceptions.v2"},
            {**base, "event": "ac2_arap_alert"},
            {**base, "run_id": "x"},
            {**base, "month": "2026-13"},
            {**base, "month": "2026-11-01"},
            {**base, "status": "ok"},
            {**base, "failure_class": "identity_refused"},
            {**failed, "failure_class": None},
            {**failed, "failure_class": "ledger_mismatch"},
            {**base, "stale_outlets": ["OUT01"]},
            {**stale, "stale_outlets": []},
            {**stale, "stale_outlets": ["OUT 01"]},
            {**stale, "counts": base["counts"]},
            {**failed, "rows": base["rows"]},
            with_row(member=CANARY_MEMBER),
            with_row(member="***98765"),
            with_row(doc_no="CS T0001"),
            with_row(doc_no="<b>x</b>"),
            with_row(day="2026-10-31"),
            with_row(rate="15"),
            with_row(reason="valid"),
            with_row(record_note="yes"),
            with_row(record_note=True),
            with_row_at(2, record_note="created_on_sale_day"),  # a note on an expired row
            with_row_at(3, record_note="edited_on_or_after_sale_day"),  # a note on a same-receipt row
            with_old_mark,
            with_row(amount=12.5),
            with_counts(guest=-1),
            with_counts(guest=1.5),
            with_counts(guest=4),  # list + not listed no longer matches the total
            {**base, "counts": dict.fromkeys(xr.COUNT_KEYS, 0), "rows": [], "outlets": []},  # nothing to act on
            {**base, "rows": base["rows"] * 4},
            {**base, "outlets": [dict(base["outlets"][0], name="x")]},
            {**base, "rows_not_listed": -1},
            [],
            None,
            "text",
        ]
        for case, res in zip(cases, self.run_cases(cases)):
            with self.subTest(case=json.dumps(case)[:120]):
                self.assertTrue(res["ok"], res)
                (item,) = res["out"]
                self.assertEqual(set(item["json"]), {"valid", "reason"})
                self.assertIs(item["json"]["valid"], False)
                self.assertRegex(item["json"]["reason"], r"^POS_XR_[A-Z_]+$")
                self.assertIsNone(res["sub"])


if __name__ == "__main__":
    unittest.main()
