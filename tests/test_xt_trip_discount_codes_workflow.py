"""Offline contract of the inactive "XT Trip Discount Codes" n8n workflow (#355).

No n8n instance, Shopify store, Google Sheet or email account is contacted. The committed Code nodes,
the IF expressions, the unexpected-alert expression and the shared sender's own `Validate Alert Request`
node are executed with the local Node runtime against fake sheet values and fake Shopify answers.
Every name, email (example.com), invoice number and id here is fake.
"""

import copy
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows" / "xt_trip_discount_codes.workflow.json"
SENDER = ROOT / "n8n-workflows" / "ops_alert_email.workflow.json"
HEALTH = ROOT / "n8n-workflows" / "xb_daily_health.workflow.json"
README = ROOT / "n8n-workflows" / "README.md"
ROOT_README = ROOT / "README.md"
RUNBOOK = ROOT / "docs" / "xt_trip_discount_codes.md"
CI = ROOT / ".github" / "workflows" / "xt-trip-discount-codes-tests.yml"

SETTINGS = "Run Settings"
READ = "Read Intake Sheet"
TOKEN = "Get Shopify Access Token"
CHECK = "Check Intake Rows"
TO_LOOK = "Rows To Look Up"
LIST_LOOKUPS = "List Lookups"
LOOKUP = "Look Up Shopify"
DECIDE = "Decide Code Writes"
TO_WRITE = "Codes To Write"
LIST_WRITES = "List Code Writes"
WRITE = "Write Codes"
CHECK_WRITES = "Check Code Writes"
TO_UPDATE = "Rows To Update"
REREAD = "Re-read Row Keys"
BUILD_SHEET = "Build Sheet Update"
SHEET_NEEDED = "Sheet Write Needed"
WRITE_SHEET = "Write Back To Sheet"
CHECK_SHEET = "Check Sheet Write"
TO_EMAIL = "Error To Email"
BUILD_ALERT = "Build Error Alert"
UNEXPECTED = "Build Unexpected Alert"
SEND = "Send XT Alert"
STOP = "End Run As Failed"
SYSTEM = "XT-CODES"
OTHER_SYSTEMS = ("AC2-STOCK", "AC2-ARAP", "ENERGYGRID", "MEMBER-SYNC", "HEALTH", "KIT-RETAG", "MEMBER-POS")

# 24 Nov 2026 10:00 SGT, a fixed clock for every Code run.
NOW_MS = int(datetime(2026, 11, 24, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
FAKE_TOKEN = "fake-access-token-0000000000"
HEADERS = ["Ready", "Invoice no.", "Traveller no.", "Traveller name", "Email", "Trip", "Trip code", "Deposit date",
           "Departure date", "Paid by", "Notes", "Code", "Code ends (SGT)", "Status", "Message", "Shopify customer ID",
           "Last checked (SGT)", "Code made (SGT)"]
STATUSES = ["CHECK_ROW", "NEEDS_EMAIL", "NEEDS_SHOPIFY_ACCOUNT", "CHECK_EMAIL", "CONFLICT", "HELD", "ERROR", "DONE",
            "ADOPTED", "EXPIRED_NO_ACCOUNT"]
NODE_TYPES = {
    "Daily 10:00 SGT": "n8n-nodes-base.scheduleTrigger", "Manual Run": "n8n-nodes-base.manualTrigger",
    SETTINGS: "n8n-nodes-base.set", READ: "n8n-nodes-base.httpRequest", TOKEN: "n8n-nodes-base.httpRequest",
    CHECK: "n8n-nodes-base.code", TO_LOOK: "n8n-nodes-base.if", LIST_LOOKUPS: "n8n-nodes-base.splitOut",
    LOOKUP: "n8n-nodes-base.httpRequest", DECIDE: "n8n-nodes-base.code", TO_WRITE: "n8n-nodes-base.if",
    LIST_WRITES: "n8n-nodes-base.splitOut", WRITE: "n8n-nodes-base.httpRequest", CHECK_WRITES: "n8n-nodes-base.code",
    TO_UPDATE: "n8n-nodes-base.if", REREAD: "n8n-nodes-base.httpRequest", BUILD_SHEET: "n8n-nodes-base.code",
    SHEET_NEEDED: "n8n-nodes-base.if", WRITE_SHEET: "n8n-nodes-base.httpRequest", CHECK_SHEET: "n8n-nodes-base.code",
    TO_EMAIL: "n8n-nodes-base.if", BUILD_ALERT: "n8n-nodes-base.code", UNEXPECTED: "n8n-nodes-base.set",
    SEND: "n8n-nodes-base.executeWorkflow", STOP: "n8n-nodes-base.stopAndError",
}
STICKIES = {"Start Here", "Setup", "Read Stage", "Look Up Stage", "Write Stage"}
EXPECTED_EDGES = {
    ("Daily 10:00 SGT", 0, SETTINGS), ("Manual Run", 0, SETTINGS), (SETTINGS, 0, READ), (READ, 0, TOKEN),
    (TOKEN, 0, CHECK), (CHECK, 0, TO_LOOK), (CHECK, 1, UNEXPECTED), (TO_LOOK, 0, LIST_LOOKUPS), (TO_LOOK, 1, TO_UPDATE),
    (LIST_LOOKUPS, 0, LOOKUP), (LOOKUP, 0, DECIDE), (DECIDE, 0, TO_WRITE), (DECIDE, 1, UNEXPECTED),
    (TO_WRITE, 0, LIST_WRITES), (TO_WRITE, 1, TO_UPDATE), (LIST_WRITES, 0, WRITE), (WRITE, 0, CHECK_WRITES),
    (CHECK_WRITES, 0, TO_UPDATE), (CHECK_WRITES, 1, UNEXPECTED), (TO_UPDATE, 0, REREAD), (TO_UPDATE, 1, TO_EMAIL),
    (REREAD, 0, BUILD_SHEET), (BUILD_SHEET, 0, SHEET_NEEDED), (BUILD_SHEET, 1, UNEXPECTED),
    (SHEET_NEEDED, 0, WRITE_SHEET), (SHEET_NEEDED, 1, TO_EMAIL), (WRITE_SHEET, 0, CHECK_SHEET),
    (CHECK_SHEET, 0, TO_EMAIL), (CHECK_SHEET, 1, UNEXPECTED), (TO_EMAIL, 0, BUILD_ALERT),
    (BUILD_ALERT, 0, SEND), (BUILD_ALERT, 1, UNEXPECTED), (UNEXPECTED, 0, SEND), (SEND, 0, STOP),
}
CODE_NODES = (CHECK, DECIDE, CHECK_WRITES, BUILD_SHEET, CHECK_SHEET, BUILD_ALERT)

HARNESS = r"""
const fs = require('fs');
const [workflowPath, senderPath, casesPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const sender = JSON.parse(fs.readFileSync(senderPath, 'utf8'));
const nodeOf = (w, name) => w.nodes.find((n) => n.name === name);
const inner = (expr) => {
  if (!expr.startsWith('={{') || !expr.endsWith('}}')) throw new Error('not an expression: ' + expr);
  return expr.slice(3, -2);
};
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const realNow = Date.now;
const results = cases.map((c) => {
  Date.now = () => (c.now_ms === undefined ? realNow() : c.now_ms);
  try {
    if (c.kind === 'expr') {
      const ran = (name) => ({ isExecuted: (c.executed || []).includes(name) });
      return { ok: true, out: new Function('$json', '$', 'return (' + inner(c.expr) + ');')(c.json, ran) };
    }
    const w = c.workflow === 'sender' ? sender : wf;
    const code = nodeOf(w, c.node).parameters.jsCode;
    const $input = { all: () => c.items.map((json) => ({ json })), first: () => ({ json: c.items[0] }) };
    const $ = (name) => {
      if (!(name in (c.nodes || {}))) throw new Error('unexpected node reference: ' + name);
      const list = Array.isArray(c.nodes[name]) ? c.nodes[name] : [c.nodes[name]];
      return { first: () => ({ json: list[0] }), all: () => list.map((json) => ({ json })) };
    };
    const out = new Function('$input', '$', code)($input, $);
    return { ok: true, out };
  } catch (error) {
    return { ok: false, error: String(error.message) };
  } finally {
    Date.now = realNow;
  }
});
process.stdout.write(JSON.stringify(results));
"""


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def serial(y, m, d):
    return (date(y, m, d) - date(1899, 12, 30)).days


LAYOUT = None  # the export's own SHEET_LAYOUT, evaluated by Node in XtBase.setUpClass


def settings(**overrides):
    value = {"DRY_RUN": False, "VALIDITY_RULE": "later", "API_VERSION": "2026-10",
             "SHOP_DOMAIN": "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN", "SHEET_ID": "REPLACE_WITH_XT_INTAKE_SHEET_ID",
             "SHEET_LAYOUT": copy.deepcopy(LAYOUT)}
    value.update(overrides)
    return value


def layout(**columns):
    """The export layout with some roles renamed ('' for an optional role that is absent)."""
    value = copy.deepcopy(LAYOUT)
    value["columns"].update(columns)
    return value


def row(ready=True, invoice="INV-000123", traveller=1, email="traveller1@example.com", trip_code="FT-01",
        deposit=None, departure=None, status="", name="Fake Traveller"):
    cells = [ready, invoice, traveller, name, email, "Fake Trip", trip_code,
             serial(2026, 11, 3) if deposit is None else deposit,
             serial(2027, 5, 1) if departure is None else departure, "Fake Payer", "", "", "", status, "", "", "", ""]
    while cells and cells[-1] == "":
        cells.pop()  # the Sheets API drops trailing empty cells
    return cells


def sheet(*rows, header=None):
    return {"range": "Travellers!A1:R1000", "majorDimension": "ROWS", "values": [header or HEADERS] + list(rows)}


def customer(num, email):
    return {"id": "gid://shopify/Customer/%d" % num, "defaultEmailAddress": {"emailAddress": email}}


def code_node(num=1, owners=(7001,), uses=0, limit=None, ends="2027-05-01T14:30:00Z", pct=0.15,
              combines=False, typename="DiscountCodeBasic", context="DiscountCustomers"):
    return {"id": "gid://shopify/DiscountCodeNode/%d" % (5000 + num), "codeDiscount": {
        "__typename": typename, "status": "ACTIVE", "usageLimit": limit, "asyncUsageCount": uses, "endsAt": ends,
        "combinesWith": {"orderDiscounts": combines, "productDiscounts": False, "shippingDiscounts": False},
        "customerGets": {"value": {"__typename": "DiscountPercentage", "percentage": pct}},
        "context": {"__typename": context, "customers": [{"id": "gid://shopify/Customer/%d" % o} for o in owners]}}}


def answer(cust=None, code=None, legacy=None, with_legacy=False):
    data = {"customer": cust, "code": code}
    if with_legacy:
        data["legacy"] = legacy
    return {"data": data, "extensions": {"cost": {"requestedQueryCost": 4}}}


def iso(text):
    return text.replace(".000Z", "Z")


def create_ok(write, node_num=9001):
    return {"data": {"discountCodeBasicCreate": {"codeDiscountNode": {
        "id": "gid://shopify/DiscountCodeNode/%d" % node_num, "codeDiscount": {
            "__typename": "DiscountCodeBasic", "usageLimit": 3, "endsAt": iso(write["end_iso"]),
            "codes": {"nodes": [{"code": write["code"]}]}}}, "userErrors": []}}}


def adopt_ok(write):
    return {"data": {"discountCodeBasicUpdate": {"codeDiscountNode": {"id": write["node_id"], "codeDiscount": {
        "__typename": "DiscountCodeBasic", "usageLimit": 3, "endsAt": iso(write["end_iso"])}}, "userErrors": []}}}


def reread_of(sheet_value):
    """What Re-read Row Keys returns: the same range as Read Intake Sheet, read again."""
    return copy.deepcopy(sheet_value)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class XtBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global LAYOUT
        cls.workflow = load(WORKFLOW)
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}
        value = next(a["value"] for a in cls.nodes[SETTINGS]["parameters"]["assignments"]["assignments"]
                     if a["name"] == "SHEET_LAYOUT")
        LAYOUT = cls.expr_raw(value, {})

    @staticmethod
    def expr_raw(expr, json_value):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps([{"kind": "expr", "expr": expr, "json": json_value}]), encoding="utf-8")
            done = subprocess.run([shutil.which("node"), str(harness), str(WORKFLOW), str(SENDER), str(cases_path)],
                                  capture_output=True, text=True, timeout=300, check=True)
        (result,) = json.loads(done.stdout)
        assert result["ok"], result
        return result["out"]

    def run_raw(self, cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            done = subprocess.run([shutil.which("node"), str(harness), str(WORKFLOW), str(SENDER), str(cases_path)],
                                  capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def run_node(self, node, items, nodes, workflow=None):
        case = {"node": node, "items": items, "nodes": nodes, "now_ms": NOW_MS}
        if workflow:
            case["workflow"] = workflow
        (result,) = self.run_raw([case])
        self.assertTrue(result["ok"], result)
        return result["out"]

    def expr(self, expr, json_value, executed=()):
        (result,) = self.run_raw([{"kind": "expr", "expr": expr, "json": json_value, "executed": list(executed)}])
        self.assertTrue(result["ok"], result)
        return result["out"]

    # ---- one node at a time ---- #
    def check(self, sheet_value, run=None, token=None):
        (out,) = self.run_node(CHECK, [{}], {SETTINGS: run or settings(), READ: sheet_value,
                                             TOKEN: token or {"access_token": FAKE_TOKEN, "scope": "read_customers"}})
        return out["json"]

    def decide(self, base, answers):
        (out,) = self.run_node(DECIDE, answers, {CHECK: base, LIST_LOOKUPS: base["lookups"]})
        return out["json"]

    def check_writes(self, decided, results):
        (out,) = self.run_node(CHECK_WRITES, results, {LIST_WRITES: decided["writes"], DECIDE: decided})
        return out["json"]

    def build_sheet(self, upstream, reread, run=None):
        (out,) = self.run_node(BUILD_SHEET, [reread], {TO_UPDATE: upstream, SETTINGS: run or settings()})
        return out["json"]

    def check_sheet(self, built, response):
        (out,) = self.run_node(CHECK_SHEET, [response], {BUILD_SHEET: built})
        return out["json"]

    def alert(self, problems):
        (out,) = self.run_node(BUILD_ALERT, [{"problems": problems}], {})
        return out["json"]["alert"]

    def through_sender(self, alert):
        request = {"system": SYSTEM, "severity": alert["severity"], "key": alert["key"], "short": alert["short"],
                   "lines": alert["lines"]}
        (out,) = self.run_node("Validate Alert Request", [request], {}, workflow="sender")
        return out["json"]

    def status_of(self, rows, number):
        return next(r for r in rows if r["row"] == number)

    def full_run(self, sheet_value, shopify, run=None, write_results=None, reread=None, sheet_response=None):
        """Walks the real path: check -> lookups -> decide -> writes -> sheet. `shopify(lookup)` answers a lookup."""
        trail = {}
        base = trail["check"] = self.check(sheet_value, run)
        upstream = base
        if base["lookups"]:
            decided = trail["decide"] = self.decide(base, [shopify(l) for l in base["lookups"]])
            upstream = decided
            if decided["writes"]:
                results = [write_results(w) if write_results else (create_ok(w) if w["kind"] == "create" else adopt_ok(w))
                           for w in decided["writes"]]
                upstream = trail["written"] = self.check_writes(decided, results)
        trail["to_update"] = self.expr(self.nodes[TO_UPDATE]["parameters"]["conditions"]["conditions"][0]["leftValue"],
                                       upstream)
        if trail["to_update"]:
            built = trail["built"] = self.build_sheet(upstream, reread or reread_of(sheet_value), run)
            upstream = built
            if built["expected"]:
                response = sheet_response or {"spreadsheetId": "fake", "responses": [{} for _ in built["body"]["data"]]}
                upstream = trail["sheet"] = self.check_sheet(built, response)
        trail["final"] = upstream
        return trail


class XtShapeTests(XtBase):
    def test_inactive_no_saved_executions_and_free_of_live_identity(self):
        wf = self.workflow
        self.assertEqual(wf["name"], "XT Trip Discount Codes")
        self.assertIs(wf["active"], False)
        self.assertIsNone(wf["staticData"])
        self.assertEqual(wf["settings"], {"executionOrder": "v1", "callerPolicy": "workflowsFromSameOwner",
                                          "timezone": "Asia/Singapore", "saveDataErrorExecution": "none",
                                          "saveDataSuccessExecution": "none", "saveManualExecutions": False,
                                          "saveExecutionProgress": False})
        text = WORKFLOW.read_text(encoding="utf-8")
        for forbidden in ('"credentials"', "webhookId", "instanceId", "activeVersionId", "@x-boundaries", "myshopify.com",
                          "docs.google.com", "$env", "$vars", "process.", "require("):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertRegex(text, r"^[\x00-\x7F]*$", "ASCII only")

    def test_node_set_and_exact_graph(self):
        nodes = {n["name"]: n["type"] for n in self.workflow["nodes"]}
        self.assertEqual({k for k, v in nodes.items() if v == "n8n-nodes-base.stickyNote"}, STICKIES)
        self.assertEqual({k: v for k, v in nodes.items() if k not in STICKIES}, NODE_TYPES)
        edges = {(src, i, c["node"]) for src, out in self.workflow["connections"].items()
                 for i, targets in enumerate(out["main"]) for c in targets}
        self.assertEqual(edges, EXPECTED_EDGES)

    def test_settings_dry_run_rule_and_placeholders(self):
        values = {a["name"]: a["value"] for a in self.nodes[SETTINGS]["parameters"]["assignments"]["assignments"]}
        layout_expr = values.pop("SHEET_LAYOUT")
        self.assertEqual(values, {"DRY_RUN": True, "VALIDITY_RULE": "later", "API_VERSION": "2026-10",
                                  "SHOP_DOMAIN": "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN",
                                  "SHEET_ID": "REPLACE_WITH_XT_INTAKE_SHEET_ID"})
        self.assertTrue(layout_expr.startswith("={{ ({") and layout_expr.endswith("}) }}"))
        self.assertEqual(sorted(LAYOUT), ["columns", "invoice_pattern", "last_row", "tab"])
        self.assertEqual(sorted(LAYOUT["columns"]), sorted(["ready", "invoice", "traveller", "email", "trip", "deposit",
                                                            "departure", "code", "ends", "status", "message",
                                                            "customer", "checked", "made"]))
        rule = self.nodes["Daily 10:00 SGT"]["parameters"]["rule"]["interval"]
        self.assertEqual(rule, [{"field": "days", "daysInterval": 1, "triggerAtHour": 10, "triggerAtMinute": 0}])

    def test_credentials_are_by_type_only_and_on_the_right_nodes(self):
        auth = {n["name"]: (n["parameters"].get("authentication"), n["parameters"].get("genericAuthType")
                            or n["parameters"].get("nodeCredentialType"))
                for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.httpRequest"}
        self.assertEqual(auth, {
            READ: ("predefinedCredentialType", "googleSheetsOAuth2Api"),
            TOKEN: ("genericCredentialType", "httpCustomAuth"),
            LOOKUP: ("none", None), WRITE: ("none", None),
            REREAD: ("predefinedCredentialType", "googleSheetsOAuth2Api"),
            WRITE_SHEET: ("predefinedCredentialType", "googleSheetsOAuth2Api")})
        for name in (LOOKUP, WRITE):
            headers = self.nodes[name]["parameters"]["headerParameters"]["parameters"]
            self.assertEqual(headers, [{"name": "X-Shopify-Access-Token",
                                        "value": "={{ $('Get Shopify Access Token').first().json.access_token }}"}])
        token = self.nodes[TOKEN]["parameters"]
        self.assertEqual(token["bodyParameters"]["parameters"], [{"name": "grant_type", "value": "client_credentials"}])
        for n in self.workflow["nodes"]:
            if n["type"] == "n8n-nodes-base.httpRequest":
                self.assertEqual(n["parameters"]["options"]["redirect"], {"redirect": {"followRedirects": False}}, n["name"])

    def test_sheet_ranges_are_exact(self):
        base = "=https://sheets.googleapis.com/v4/spreadsheets/{{ $('Run Settings').first().json.SHEET_ID }}"
        self.assertEqual(self.nodes[READ]["parameters"]["url"], self.nodes[REREAD]["parameters"]["url"])
        self.assertTrue(self.nodes[READ]["parameters"]["url"].startswith(base + "/values/{{ encodeURIComponent("))
        self.assertEqual(self.nodes[WRITE_SHEET]["parameters"]["url"], base + "/values:batchUpdate")
        rng = self.nodes[READ]["parameters"]["url"][len(base + "/values/{{ "):-len(" }}")]
        (out,) = self.run_raw([{"kind": "expr", "expr": "={{ " + rng.replace("$('Run Settings').first().json", "$json")
                                + " }}", "json": settings(SHEET_LAYOUT=layout() | {"tab": "XT intake"})}])
        self.assertEqual(out["out"], "'XT%20intake'!A1%3AZZ1000")  # ' and ! are valid in a URL path
        for name in (READ, REREAD):
            query = {p["name"]: p["value"] for p in self.nodes[name]["parameters"]["queryParameters"]["parameters"]}
            self.assertEqual(query, {"majorDimension": "ROWS", "valueRenderOption": "UNFORMATTED_VALUE",
                                     "dateTimeRenderOption": "SERIAL_NUMBER"})
            self.assertEqual(self.nodes[name]["parameters"]["method"], "GET")

    def test_reads_retry_and_code_writes_never_retry(self):
        for name in (READ, TOKEN, LOOKUP, REREAD, WRITE_SHEET):
            self.assertIs(self.nodes[name].get("retryOnFail"), True, name)
            self.assertEqual(self.nodes[name]["onError"], "continueRegularOutput", name)
            self.assertIs(self.nodes[name]["alwaysOutputData"], True, name)
        self.assertIs(self.nodes[WRITE]["retryOnFail"], False, "a repeated create would fail as taken")
        for name in (LOOKUP, WRITE):
            self.assertEqual(self.nodes[name]["parameters"]["options"]["batching"],
                             {"batch": {"batchSize": 1, "batchInterval": 500}}, name)
            self.assertEqual(self.nodes[name]["parameters"]["jsonBody"],
                             "={{ JSON.stringify({ query: $json.query, variables: $json.variables }) }}")
        for name in (READ, TOKEN, REREAD, WRITE_SHEET):
            self.assertIs(self.nodes[name].get("executeOnce"), True, name)

    def test_only_discount_code_mutations_and_no_customer_writes(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(sorted(set(re.findall(r"\b(discount\w+(?:Create|Update|Delete|Activate|Deactivate))\b", text))),
                         ["discountCodeBasicCreate", "discountCodeBasicUpdate"])
        for forbidden in ("customerCreate", "customerUpdate", "customerDelete", "tagsAdd", "tagsRemove", "customerSet",
                          "customerMerge", "discountCodeDelete", "discountCodeBulk", "write_customers"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertEqual(len(re.findall(r"\bmutation\b", text)), 2)

    def test_alert_call_pins_the_system_and_every_email_ends_the_run_as_failed(self):
        params = self.nodes[SEND]["parameters"]
        self.assertEqual(params["workflowId"], {"__rl": True, "value": "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID",
                                                "mode": "id"})
        self.assertEqual(params["workflowInputs"]["value"], {
            "system": SYSTEM, "severity": "={{ $json.alert.severity }}", "key": "={{ $json.alert.key }}",
            "short": "={{ $json.alert.short }}", "lines": "={{ $json.alert.lines }}"})
        self.assertEqual(self.workflow["connections"][SEND]["main"], [[{"node": STOP, "type": "main", "index": 0}]])
        self.assertEqual(self.nodes[STOP]["parameters"]["errorType"], "errorMessage")

    def test_code_nodes_use_their_error_output(self):
        for name in CODE_NODES:
            node = self.nodes[name]
            self.assertEqual(node["onError"], "continueErrorOutput", name)
            self.assertEqual(node["parameters"]["mode"], "runOnceForAllItems", name)
            self.assertNotIn("$getWorkflowStaticData", node["parameters"]["jsCode"], name)
            self.assertNotIn("$json", node["parameters"]["jsCode"], name + ": $json is not set in run-once-for-all mode")

    def test_notes_readme_runbook_and_ci(self):
        stickies = "\n".join(self.nodes[n]["parameters"]["content"] for n in STICKIES)
        for text in ("Never creates, edits or tags a Shopify customer", "DRY_RUN", "XT-CODES", "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN",
                     "REPLACE_WITH_XT_INTAKE_SHEET_ID", "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "300", "30",
                     "docs/xt_trip_discount_codes.md"):
            self.assertIn(text, stickies)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("### xt_trip_discount_codes.workflow.json", readme)
        self.assertIn("Built, off", readme)
        self.assertIn("`XT-CODES`", readme)
        self.assertIn("XT trip discount codes", ROOT_README.read_text(encoding="utf-8"))
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for text in ("**Status:** built, off.", "read_customers", "read_discounts", "write_discounts", "httpCustomAuth",
                     "googleSheetsOAuth2Api", "| O2 |", "| O4 |", "| X6 |", "| X7 |", "OK XT CODES ACTIVATE"):
            self.assertIn(text, runbook)
        self.assertNotIn("write_customers` only", runbook)
        for status in STATUSES:
            self.assertIn("`%s`" % status, runbook)
        ci = CI.read_text(encoding="utf-8")
        for path in ("n8n-workflows/xt_trip_discount_codes.workflow.json", "tests/test_xt_trip_discount_codes_workflow.py",
                     "docs/xt_trip_discount_codes.md", "n8n-workflows/ops_alert_email.workflow.json",
                     "n8n-workflows/scripts/validate-n8n-workflows.cjs", "n8n-workflow-policy.json",
                     ".github/workflows/xt-trip-discount-codes-tests.yml"):
            self.assertIn('- "%s"' % path, ci)
        self.assertIn("python -m unittest tests.test_xt_trip_discount_codes_workflow -v", ci)

    def test_health_wiring_watches_and_counts_this_workflow(self):
        health = load(HEALTH)
        judge = next(n for n in health["nodes"] if n["name"] == "Judge And Build Email")["parameters"]["jsCode"]
        self.assertIn("{ id: 'REPLACE_WITH_XT_CODES_WORKFLOW_ID', label: 'XT trip codes', errors: false, bind_later: true }",
                      judge)
        read = next(n for n in health["nodes"] if n["name"] == "Read XT Code Errors")["parameters"]
        self.assertEqual(read["filters"]["conditions"][0], {"keyName": "system", "condition": "eq", "keyValue": SYSTEM})


class XtCheckRowsTests(XtBase):
    def test_run_level_refusals_write_nothing(self):
        cases = {
            "settings_invalid": dict(run=settings(VALIDITY_RULE="Later")),
            "sheet_read_failed": dict(sheet_value={"error": "403 forbidden"}),
            "sheet_layout_changed": dict(sheet_value=sheet(row(), header=HEADERS[:12] + ["Code ends"] + HEADERS[13:])),
            "token_failed": dict(token={"error": "401"}),
        }
        for code, kw in cases.items():
            out = self.check(kw.get("sheet_value", sheet(row())), kw.get("run"), kw.get("token"))
            self.assertEqual((out["mode"], out["updates"], out["lookups"], out["problems"]),
                             ("failed", [], [], [{"code": code, "count": 1}]), code)
        out = self.check({"values": [HEADERS] + [row(traveller=1 + i % 99, invoice="INV-%06d" % i) for i in range(1000)]})
        self.assertEqual(out["problems"], [{"code": "sheet_layout_changed", "count": 1}])
        doubled = HEADERS + ["Email"]
        self.assertEqual(self.check(sheet(row(), header=doubled))["problems"][0]["code"], "sheet_layout_changed")
        bad_layouts = [None, "x", layout(email=""), layout(email="Code"), layout(extra="X"), layout() | {"tab": "a'b"},
                       layout() | {"last_row": 1}, layout() | {"invoice_pattern": "^INV-[0-9]+$"},
                       layout() | {"invoice_pattern": "(["}, layout(trip=5)]
        for bad in bad_layouts:
            out = self.check(sheet(row()), settings(SHEET_LAYOUT=bad))
            self.assertEqual(out["problems"], [{"code": "settings_invalid", "count": 1}], bad)
        self.assertEqual(self.check(sheet(row()), token={"access_token": ""})["problems"][0]["code"], "token_failed")

    def test_skips_rows_not_ready_and_final_rows(self):
        out = self.check(sheet(row(ready=False), row(status="DONE", traveller=2), row(status="ADOPTED", traveller=3),
                               row(status="EXPIRED_NO_ACCOUNT", traveller=4), row(ready="TRUE", traveller=5)))
        self.assertEqual((out["mode"], out["lookups"], out["updates"], out["problems"]), ("no_change", [], [], []))

    def test_retried_statuses_are_checked_again(self):
        rows = [row(traveller=i + 1, email="t%d@example.com" % (i + 1), status=s)
                for i, s in enumerate(["NEEDS_SHOPIFY_ACCOUNT", "ERROR", "HELD", "CONFLICT", "CHECK_ROW", "needs_email"])]
        out = self.check(sheet(*rows))
        self.assertEqual([l["row"] for l in out["lookups"]], [2, 3, 4, 5, 6, 7])

    def test_check_row_names_every_bad_field(self):
        out = self.check(sheet(
            row(invoice="INV123", traveller=0, deposit="3 Nov 2026", departure=serial(2026, 10, 1), email="not-an-email"),
            row(traveller=2, deposit=serial(2026, 11, 3), departure=serial(2026, 11, 2), email="t2@example.com"),
            row(traveller=3, email="t3@example.com"), row(traveller=3, email="t3b@example.com", ready=False)))
        rows = out["updates"]
        self.assertEqual(self.status_of(rows, 2)["message"],
                         "Check: Invoice no., Traveller no., Deposit date, Email.")
        self.assertEqual(self.status_of(rows, 3)["message"], "Check: Departure date (before Deposit date).")
        self.assertEqual(self.status_of(rows, 4)["message"],
                         "Check: Invoice no. and Traveller no. (same as row 5).")
        self.assertEqual({r["status"] for r in rows}, {"CHECK_ROW"})
        self.assertEqual(out["lookups"], [])
        for bad in ("abc", 100, 1.5, -1, ""):
            (u,) = self.check(sheet(row(traveller=bad)))["updates"]
            self.assertEqual(u["message"], "Check: Traveller no..", bad)
        self.assertEqual(self.check(sheet(row(traveller="7")))["lookups"][0]["code"], "XT-INV000123-07")

    def test_needs_email_and_same_email_on_the_same_trip(self):
        out = self.check(sheet(row(email=""), row(traveller=2, email=" Lead@Example.com "),
                               row(traveller=3, email="lead@example.com"),
                               row(traveller=4, email="lead@example.com", trip_code="OTHER-TRIP"),
                               row(invoice="INV-000999", traveller=1, email="done@example.com", status="DONE"),
                               row(traveller=5, email="DONE@example.com")))
        self.assertEqual(out["updates"], [], "rows wait for the lookups, then Decide Code Writes writes them all")
        rows = {u["row"]: u for u in out["decided"]}
        self.assertEqual(rows[2]["status"], "NEEDS_EMAIL")
        self.assertEqual(rows[4]["status"], "CHECK_EMAIL")
        self.assertEqual(rows[4]["message"], "Same email as row 3 on the same trip. Each traveller needs their own email.")
        self.assertEqual(rows[7]["message"], "Same email as row 6 on the same trip. Each traveller needs their own email.")
        self.assertEqual([l["row"] for l in out["lookups"]], [3, 5])
        self.assertEqual(out["lookups"][0]["email"], "lead@example.com")

    def test_code_names_and_the_old_name_for_traveller_one(self):
        out = self.check(sheet(row(), row(traveller=12, email="t12@example.com")))
        first, other = out["lookups"]
        self.assertEqual((first["code"], first["legacy"]), ("XT-INV000123-01", "XT-INV000123"))
        self.assertEqual(first["variables"], {"email": "traveller1@example.com", "code": "XT-INV000123-01",
                                              "legacy": "XT-INV000123", "withLegacy": True, "withCustomer": True})
        self.assertEqual((other["code"], other["legacy"]), ("XT-INV000123-12", ""))
        self.assertEqual(other["variables"]["withLegacy"], False)
        self.assertIn("customerByIdentifier(identifier: {emailAddress: $email})", first["query"])
        self.assertNotIn("customers(", first["query"])

    def test_end_date_rule(self):
        def end(deposit, departure, rule="later"):
            (l,) = self.check(sheet(row(deposit=deposit, departure=departure)), settings(VALIDITY_RULE=rule))["lookups"]
            return l["end_iso"], l["end_label"]

        self.assertEqual(end(serial(2026, 11, 3), serial(2027, 5, 1)), ("2027-11-03T15:59:59.000Z", "3 Nov 2027 23:59"))
        self.assertEqual(end(serial(2026, 11, 3), serial(2028, 1, 9)), ("2028-01-09T15:59:59.000Z", "9 Jan 2028 23:59"))
        self.assertEqual(end(serial(2026, 11, 3), serial(2027, 5, 1), "earlier"), ("2027-05-01T15:59:59.000Z", "1 May 2027 23:59"))
        self.assertEqual(end(serial(2028, 2, 29), serial(2028, 3, 1)), ("2029-02-28T15:59:59.000Z", "28 Feb 2029 23:59"))
        self.assertEqual(end(serial(2026, 11, 3) + 0.75, serial(2027, 5, 1)), ("2027-11-03T15:59:59.000Z", "3 Nov 2027 23:59"))

    def test_expired_rows_look_up_codes_only(self):
        (l,) = self.check(sheet(row(deposit=serial(2025, 1, 2), departure=serial(2025, 3, 1))))["lookups"]
        self.assertIs(l["expired"], True)
        self.assertIs(l["variables"]["withCustomer"], False)
        (l,) = self.check(sheet(row(deposit=serial(2025, 11, 24), departure=serial(2026, 1, 1))))["lookups"]
        self.assertIs(l["expired"], False, "ends 24 Nov 2026 23:59 SGT, the run is at 10:00")

    def test_lookup_brake(self):
        rows = [row(invoice="INV-%06d" % (1 + i // 99), traveller=1 + i % 99, email="t%d@example.com" % i) for i in range(301)]
        out = self.check(sheet(*rows))
        self.assertEqual((out["lookups"], out["problems"]), ([], [{"code": "too_many_rows", "count": 301}]))
        self.assertEqual({u["status"] for u in out["updates"]}, {"HELD"})
        self.assertEqual(len(out["updates"]), 301)
        out = self.check(sheet(*rows[:300]))
        self.assertEqual((len(out["lookups"]), out["problems"]), (300, []))

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        out = self.check(sheet(row(email=""), row(traveller=2, email="t2@example.com")), settings(DRY_RUN=True))
        self.assertEqual((out["dry_run"], out["updates"], out["problems"]), (True, [], []))
        self.assertEqual(out["plan"], [{"row": 2, "status": "NEEDS_EMAIL", "message": "Add this traveller's own email."}])
        rows = [row(invoice="INV-%06d" % (1 + i // 99), traveller=1 + i % 99, email="t%d@example.com" % i) for i in range(301)]
        out = self.check(sheet(*rows), settings(DRY_RUN=True))
        self.assertEqual((out["mode"], out["updates"], out["problems"]), ("dry_run", [], []))
        self.assertEqual(out["summary"]["held"], 301)
        for value in ("yes", None, "false", 0):
            self.assertIs(self.check(sheet(row(email="")), settings(DRY_RUN=value))["dry_run"], True, value)


class XtDecideTests(XtBase):
    def base(self, *rows, **run):
        return self.check(sheet(*(rows or (row(),))), settings(**run))

    def test_no_account_yet(self):
        out = self.decide(self.base(), [answer(with_legacy=True)])
        (u,) = out["updates"]
        self.assertEqual((u["status"], out["writes"], out["problems"]), ("NEEDS_SHOPIFY_ACCOUNT", [], []))
        self.assertIn("sign up online with this email", u["message"])

    def test_create_uses_exactly_the_owner_defaults(self):
        out = self.decide(self.base(), [answer(customer(7001, "Traveller1@Example.com"), with_legacy=True)])
        (w,) = out["writes"]
        self.assertEqual((w["kind"], w["code"], w["customer"], w["row"]), ("create", "XT-INV000123-01", "7001", 2))
        self.assertEqual(w["variables"], {"basicCodeDiscount": {
            "title": "XT-INV000123-01", "code": "XT-INV000123-01", "startsAt": "2026-11-24T02:00:00.000Z",
            "endsAt": "2027-11-03T15:59:59.000Z", "customerGets": {"value": {"percentage": 0.15}, "items": {"all": True}},
            "context": {"customers": {"add": ["gid://shopify/Customer/7001"]}}, "usageLimit": 3,
            "appliesOncePerCustomer": False,
            "combinesWith": {"orderDiscounts": False, "productDiscounts": False, "shippingDiscounts": False},
            "tags": ["X-Trekkers"]}})
        self.assertTrue(w["query"].startswith("mutation XtCodeCreate($basicCodeDiscount: DiscountCodeBasicInput!)"))
        self.assertEqual(out["mode"], "write")

    def test_email_must_match_exactly(self):
        out = self.decide(self.base(), [answer(customer(7001, "traveller1.other@example.com"), with_legacy=True)])
        self.assertEqual((out["updates"][0]["status"], out["writes"]), ("CHECK_EMAIL", []))
        out = self.decide(self.base(), [answer({"id": "gid://shopify/Customer/7001", "defaultEmailAddress": None},
                                               with_legacy=True)])
        self.assertEqual(out["updates"][0]["status"], "CHECK_EMAIL")

    def test_adopt_the_old_name_with_one_update(self):
        old = code_node(owners=(7001,), uses=0, limit=None, ends="2027-05-01T14:30:00Z")
        out = self.decide(self.base(), [answer(customer(7001, "traveller1@example.com"), None, old, with_legacy=True)])
        (w,) = out["writes"]
        self.assertEqual((w["kind"], w["code"], w["node_id"]), ("adopt", "XT-INV000123", old["id"]))
        self.assertEqual(w["variables"], {"id": old["id"], "basicCodeDiscount": {
            "usageLimit": 3, "endsAt": "2027-11-03T15:59:59.000Z",
            "combinesWith": {"orderDiscounts": False, "productDiscounts": False, "shippingDiscounts": False}}})
        self.assertTrue(w["query"].startswith("mutation XtCodeAdopt($id: ID!, $basicCodeDiscount: DiscountCodeBasicInput!)"))
        fresh = code_node(owners=(7001,), uses=3, limit=5, ends="2027-11-03T15:59:59Z", combines=True)
        (w,) = self.decide(self.base(), [answer(customer(7001, "traveller1@example.com"), fresh, None, with_legacy=True)])["writes"]
        self.assertEqual((w["kind"], w["code"]), ("adopt", "XT-INV000123-01"))

    def test_a_code_already_at_the_defaults_needs_no_write(self):
        done = code_node(owners=(7001,), uses=1, limit=3, ends="2027-11-03T15:59:59Z")
        out = self.decide(self.base(), [answer(customer(7001, "traveller1@example.com"), done, with_legacy=True)])
        (u,) = out["updates"]
        self.assertEqual((u["status"], u["code"], u["ends"], u["customer"], out["writes"]),
                         ("ADOPTED", "XT-INV000123-01", "3 Nov 2027 23:59", "7001", []))

    def test_conflicts_change_nothing(self):
        me = customer(7001, "traveller1@example.com")
        cases = {
            "belongs to a different customer": [code_node(owners=(7002,)), code_node(owners=(7001, 7002)), code_node(owners=())],
            "has been used more than 3 times": [code_node(uses=4), code_node(uses=None)],
            "is not 15% off": [code_node(pct=0.1), code_node(pct=0.2)],
            "is not a simple code": [code_node(typename="DiscountCodeBxgy"), {"id": "x", "codeDiscount": None}],
        }
        for why, nodes in cases.items():
            for node in nodes:
                out = self.decide(self.base(), [answer(me, node, with_legacy=True)])
                (u,) = out["updates"]
                self.assertEqual((u["status"], out["writes"]), ("CONFLICT", []), (why, node))
                self.assertIn(why, u["message"])
        out = self.decide(self.base(), [answer(me, code_node(context="DiscountCustomerSegments"), with_legacy=True)])
        self.assertEqual(out["updates"][0]["status"], "CONFLICT")
        out = self.decide(self.base(), [answer(me, code_node(), code_node(2), with_legacy=True)])
        self.assertEqual(out["updates"][0]["message"], "Both XT-INV000123 and XT-INV000123-01 exist. Tell XB.")
        ok = self.decide(self.base(), [answer(me, code_node(uses=3), with_legacy=True)])
        self.assertEqual(ok["writes"][0]["kind"], "adopt", "3 uses so far still passes the guard")

    def test_the_old_name_counts_only_for_traveller_one(self):
        base = self.base(row(traveller=2, email="t2@example.com"))
        out = self.decide(base, [answer(customer(7002, "t2@example.com"), None, code_node(owners=(7002,)))])
        self.assertEqual(out["writes"][0]["kind"], "create")

    def test_expired_rows(self):
        base = self.base(row(deposit=serial(2025, 1, 2), departure=serial(2025, 3, 1)))
        out = self.decide(base, [answer(None, None, None, with_legacy=True)])
        self.assertEqual(out["updates"][0]["status"], "EXPIRED_NO_ACCOUNT")
        out = self.decide(base, [answer(None, None, code_node(), with_legacy=True)])
        self.assertEqual(out["updates"][0]["message"], "End date has passed but code XT-INV000123 exists. Tell XB.")
        self.assertEqual(out["writes"], [])

    def test_lookup_failures_are_errors_for_the_health_check(self):
        failures = [{"error": "500 server error"}, {"errors": [{"message": "x", "extensions": {"code": "THROTTLED"}}]},
                    {"errors": [{"message": "Field 'x' doesn't exist"}]}, {"data": None}, None, "text"]
        rows = [row(traveller=i + 1, email="t%d@example.com" % (i + 1)) for i in range(len(failures) + 1)]
        base = self.base(*rows)
        out = self.decide(base, failures + [answer(None, with_legacy=False)])
        self.assertEqual([u["status"] for u in sorted(out["updates"], key=lambda u: u["row"])],
                         ["ERROR"] * len(failures) + ["NEEDS_SHOPIFY_ACCOUNT"])
        self.assertEqual(out["problems"], [{"code": "lookup_failed", "count": len(failures)}])
        out = self.decide(self.base(row(), row(traveller=2, email="t2@example.com")), [answer(with_legacy=True)])
        self.assertEqual(({u["status"] for u in out["updates"]}, out["problems"]),
                         ({"ERROR"}, [{"code": "lookup_failed", "count": 2}]))

    def test_write_brake(self):
        def run(n):
            rows = [row(traveller=i + 1, email="t%d@example.com" % i) for i in range(n)]
            base = self.base(*rows)
            return self.decide(base, [answer(customer(8000 + i, "t%d@example.com" % i), with_legacy=(i == 0))
                                      for i in range(n)])
        out = run(30)
        self.assertEqual((len(out["writes"]), out["problems"]), (30, []))
        out = run(31)
        self.assertEqual((out["mode"], out["writes"]), ("braked", []))
        self.assertEqual(out["problems"], [{"code": "too_many_codes", "count": 31}])
        self.assertEqual({u["status"] for u in out["updates"]}, {"HELD"})

    def test_dry_run_plan(self):
        base = self.base(row(), row(traveller=2, email=""), DRY_RUN=True)
        self.assertEqual(self.expr(self.nodes[TO_LOOK]["parameters"]["conditions"]["conditions"][0]["leftValue"], base), True)
        out = self.decide(base, [answer(customer(7001, "traveller1@example.com"), with_legacy=True)])
        self.assertEqual((out["mode"], out["writes"], out["updates"], out["problems"]), ("dry_run", [], [], []))
        self.assertEqual(out["plan"], [
            {"row": 2, "status": "would CREATE", "code": "XT-INV000123-01", "ends": "3 Nov 2027 23:59"},
            {"row": 3, "status": "NEEDS_EMAIL", "message": "Add this traveller's own email.", "code": ""}])
        self.assertNotIn("example.com", json.dumps(out["plan"]))


class XtWriteTests(XtBase):
    def decided(self):
        rows = [row(), row(traveller=2, email="t2@example.com"), row(traveller=3, email="t3@example.com")]
        base = self.check(sheet(*rows))
        old = code_node(owners=(7002,))
        return self.decide(base, [answer(customer(7001, "traveller1@example.com"), with_legacy=True),
                                  answer(customer(7002, "t2@example.com"), old), answer(None)])

    def test_confirmed_writes_become_done_and_adopted(self):
        decided = self.decided()
        create, adopt = decided["writes"]
        out = self.check_writes(decided, [create_ok(create), adopt_ok(adopt)])
        rows = {u["row"]: u for u in out["updates"]}
        self.assertEqual({k: rows[2][k] for k in ("status", "code", "ends", "customer", "made")},
                         {"status": "DONE", "code": "XT-INV000123-01", "ends": "3 Nov 2027 23:59", "customer": "7001",
                          "made": "24 Nov 2026 10:00"})
        self.assertEqual((rows[3]["status"], rows[3]["code"]), ("ADOPTED", "XT-INV000123-02"))
        self.assertEqual(rows[4]["status"], "NEEDS_SHOPIFY_ACCOUNT")
        self.assertEqual(out["problems"], [])

    def test_every_unconfirmed_write_is_an_error(self):
        decided = self.decided()
        create, adopt = decided["writes"]
        bad_creates = [
            {"error": "422"}, {"errors": [{"message": "x"}]}, {"data": None},
            {"data": {"discountCodeBasicCreate": {"codeDiscountNode": None,
                                                  "userErrors": [{"field": ["code"], "code": "TAKEN", "message": "x"}]}}},
        ]
        for mutate in (lambda a: a["codeDiscount"].update(usageLimit=None), lambda a: a["codeDiscount"].update(endsAt="2027-05-01T15:59:59Z"),
                       lambda a: a["codeDiscount"]["codes"]["nodes"][0].update(code="XT-INV000123-09"),
                       lambda a: a["codeDiscount"].update(__typename="DiscountCodeApp")):
            good = create_ok(create)
            mutate(good["data"]["discountCodeBasicCreate"]["codeDiscountNode"])
            bad_creates.append(good)
        for bad in bad_creates:
            out = self.check_writes(decided, [bad, adopt_ok(adopt)])
            rows = {u["row"]: u for u in out["updates"]}
            self.assertEqual((rows[2]["status"], rows[2]["code"], rows[3]["status"]), ("ERROR", "", "ADOPTED"), bad)
            self.assertEqual(out["problems"], [{"code": "write_failed", "count": 1}])
        wrong = adopt_ok(adopt)
        wrong["data"]["discountCodeBasicUpdate"]["codeDiscountNode"]["id"] = "gid://shopify/DiscountCodeNode/1"
        out = self.check_writes(decided, [create_ok(create), wrong])
        self.assertEqual(out["problems"], [{"code": "write_failed", "count": 1}])
        out = self.check_writes(decided, [create_ok(create)])
        self.assertEqual(out["problems"], [{"code": "write_failed", "count": 2}], "a short answer fails every write")


class XtSheetTests(XtBase):
    def test_write_back_is_columns_l_to_r_as_plain_text(self):
        value = sheet(row(), row(traveller=2, email=""))
        trail = self.full_run(value, lambda l: answer(customer(7001, "traveller1@example.com"), with_legacy=True))
        body = trail["built"]["body"]
        self.assertEqual(body["valueInputOption"], "RAW")
        cells = [(d["range"], d["values"]) for d in body["data"]]
        self.assertEqual(cells, [
            ("'Travellers'!L3", [[""]]), ("'Travellers'!M3", [[""]]), ("'Travellers'!N3", [["NEEDS_EMAIL"]]),
            ("'Travellers'!O3", [["Add this traveller's own email."]]), ("'Travellers'!P3", [[""]]),
            ("'Travellers'!Q3", [["24 Nov 2026 10:00"]]), ("'Travellers'!R3", [[""]]),
            ("'Travellers'!L2", [["XT-INV000123-01"]]), ("'Travellers'!M2", [["3 Nov 2027 23:59"]]),
            ("'Travellers'!N2", [["DONE"]]), ("'Travellers'!O2", [["Code made. Send it to the traveller."]]),
            ("'Travellers'!P2", [["7001"]]), ("'Travellers'!Q2", [["24 Nov 2026 10:00"]]),
            ("'Travellers'!R2", [["24 Nov 2026 10:00"]])])
        self.assertEqual(trail["final"]["problems"], [])
        self.assertIs(self.expr(self.nodes[TO_EMAIL]["parameters"]["conditions"]["conditions"][0]["leftValue"],
                                trail["final"]), False)

    def test_a_row_that_moved_is_not_written(self):
        value = sheet(row(), row(traveller=2, email="t2@example.com"))
        moved = reread_of(value)
        moved["values"][2][4] = "someone.else@example.com"
        trail = self.full_run(value, lambda l: answer(None, with_legacy=l["variables"]["withLegacy"]), reread=moved)
        self.assertEqual({d["range"][-1] for d in trail["built"]["body"]["data"]}, {"2"})
        self.assertEqual(trail["final"]["problems"], [{"code": "rows_moved", "count": 1}])
        inserted = reread_of(value)
        inserted["values"].insert(1, row(invoice="INV-000555", email="new@example.com"))
        trail = self.full_run(value, lambda l: answer(None, with_legacy=l["variables"]["withLegacy"]), reread=inserted)
        self.assertEqual((trail["built"]["expected"], trail["final"]["problems"]), (0, [{"code": "rows_moved", "count": 2}]))
        self.assertIs(self.expr(self.nodes[SHEET_NEEDED]["parameters"]["conditions"]["conditions"][0]["leftValue"],
                                trail["built"]), False)

    def test_check_rows_keys_survive_the_reread(self):
        value = sheet(row(traveller="abc"), row(traveller=2, email=""))
        trail = self.full_run(value, lambda l: answer(None))
        self.assertEqual((trail["built"]["summary"]["writing"], trail["final"]["problems"]), (2, []))

    def test_reread_and_write_failures(self):
        value = sheet(row(email=""))
        trail = self.full_run(value, lambda l: None, reread={"error": "503"})
        self.assertEqual(trail["final"]["problems"], [{"code": "sheet_reread_failed", "count": 1}])
        trail = self.full_run(value, lambda l: None, sheet_response={"error": "403"})
        self.assertEqual(trail["final"]["problems"], [{"code": "sheet_write_failed", "count": 1}])
        trail = self.full_run(value, lambda l: None, sheet_response={"responses": []})
        self.assertEqual(trail["final"]["problems"], [{"code": "sheet_write_failed", "count": 1}])

    def test_dry_run_never_writes_the_sheet(self):
        value = sheet(row(email=""), row(traveller=2, email="t2@example.com"))
        trail = self.full_run(value, lambda l: answer(None), run=settings(DRY_RUN=True))
        self.assertIs(trail["to_update"], False)
        self.assertNotIn("built", trail)

    def test_brakes_still_write_held_and_alert(self):
        rows = [row(traveller=i + 1, email="t%d@example.com" % i) for i in range(31)]
        trail = self.full_run(sheet(*rows), lambda l: answer(customer(8000 + l["row"], l["email"]),
                                                             with_legacy=l["variables"]["withLegacy"]))
        self.assertEqual({d["values"][0][0] for d in trail["built"]["body"]["data"] if d["range"].startswith("'Travellers'!N")},
                         {"HELD"})
        self.assertEqual(trail["final"]["problems"], [{"code": "too_many_codes", "count": 31}])


class XtLayoutTests(XtBase):
    """Controller, 24 Nov 2026: the sheet follows the consultant's own layout; columns live only in SHEET_LAYOUT."""

    def own_layout(self):
        # A fake consultant-style sheet: other names, another order, extra columns, no checked/customer columns.
        header = ["No.", "Trip", "Lead booker", "Participant", "Invoice", "Pax no.", "Email address", "Deposit paid",
                  "Departure", "Discount code", "Code status", "Code expiry", "Code created", "Remarks", "Ready",
                  "Message", "Shopify customer ID"]
        lay = copy.deepcopy(LAYOUT)
        lay.update(tab="XT intake", invoice_pattern="^INV-?([0-9]{1,12})$")
        lay["columns"] = {"ready": "Ready", "invoice": "Invoice", "traveller": "Pax no.", "email": "Email address",
                          "trip": "Trip", "deposit": "Deposit paid", "departure": "Departure", "code": "Discount code",
                          "ends": "Code expiry", "status": "Code status", "message": "Message",
                          "customer": "Shopify customer ID", "checked": "", "made": "Code created"}

        def own_row(invoice="INV000123", pax=1, email="traveller1@example.com", ready=True, status=""):
            return [7, "Fake Trip", "Fake Lead", "Fake Traveller", invoice, pax, email, serial(2026, 11, 3),
                    serial(2027, 5, 1), "", status, "", "", "", ready]
        return header, lay, own_row

    def test_the_consultants_own_layout_reads_and_writes_by_header(self):
        header, lay, own_row = self.own_layout()
        value = sheet(own_row(), own_row(pax=2, email=""), own_row(invoice="INV-000124", email="b@example.com",
                                                                 status="DONE"), header=header)
        run = settings(SHEET_LAYOUT=lay)
        trail = self.full_run(value, lambda l: answer(customer(7001, l["email"]), with_legacy=l["variables"]["withLegacy"]),
                              run=run)
        self.assertEqual([l["code"] for l in trail["check"]["lookups"]], ["XT-INV000123-01"])
        cells = {d["range"]: d["values"][0][0] for d in trail["built"]["body"]["data"]}
        self.assertEqual(cells, {
            "'XT intake'!J3": "", "'XT intake'!L3": "", "'XT intake'!K3": "NEEDS_EMAIL",
            "'XT intake'!P3": "Add this traveller's own email.", "'XT intake'!Q3": "", "'XT intake'!M3": "",
            "'XT intake'!J2": "XT-INV000123-01", "'XT intake'!L2": "3 Nov 2027 23:59", "'XT intake'!K2": "DONE",
            "'XT intake'!P2": "Code made. Send it to the traveller.", "'XT intake'!Q2": "7001",
            "'XT intake'!M2": "24 Nov 2026 10:00"})
        self.assertEqual(trail["final"]["problems"], [])
        (u,) = [u for u in self.check(sheet(own_row(pax="x"), header=header), run)["updates"]]
        self.assertEqual(u["message"], "Check: Pax no..")

    def test_a_header_change_during_the_run_writes_nothing(self):
        header, lay, own_row = self.own_layout()
        value = sheet(own_row(email=""), header=header)
        renamed = copy.deepcopy(value)
        renamed["values"][0][10] = "Status"
        trail = self.full_run(value, lambda l: None, run=settings(SHEET_LAYOUT=lay), reread=renamed)
        self.assertEqual((trail["built"]["expected"], trail["final"]["problems"]),
                         (0, [{"code": "sheet_reread_failed", "count": 1}]))

    def test_columns_beyond_z_get_two_letters(self):
        header = HEADERS + ["Extra %d" % i for i in range(10)] + ["Message 2"]
        value = sheet(row(email=""), header=header)
        trail = self.full_run(value, lambda l: None, run=settings(SHEET_LAYOUT=layout(message="Message 2")))
        self.assertIn("'Travellers'!AC2", [d["range"] for d in trail["built"]["body"]["data"]])

    def test_no_code_node_names_a_column(self):
        names = {n for n in LAYOUT["columns"].values() if n} | {LAYOUT["tab"]}
        for node in CODE_NODES:
            code = self.nodes[node]["parameters"]["jsCode"]
            for name in names:
                self.assertNotIn("'%s'" % name, code, (node, name))
            self.assertNotRegex(code, r"!\$?[A-Z]{1,2}\$?[0-9]+|[A-Z]{1,2}[0-9]*:[A-Z]{1,2}[0-9]*'", node)
        both = [self.nodes[n]["parameters"]["jsCode"] for n in (CHECK, BUILD_SHEET)]
        block = lambda code: code[code.index("// ---- the sheet layout"):code.index("const locate")]
        self.assertEqual(block(both[0]), block(both[1]), "the layout check is the same in both nodes")


class XtAlertTests(XtBase):
    CODES = ("settings_invalid", "sheet_read_failed", "sheet_layout_changed", "token_failed", "too_many_rows",
             "too_many_codes", "lookup_failed", "write_failed", "rows_moved", "sheet_reread_failed", "sheet_write_failed")

    def test_every_problem_code_passes_the_sender(self):
        for code in self.CODES:
            alert = self.alert([{"code": code, "count": 3}])
            self.assertEqual((alert["severity"], alert["key"], alert["short"]), ("ERROR", code, code))
            self.assertEqual(alert["lines"][0], "XT trip discount codes: today's run had a problem. Ask Claude to check.")
            checked = self.through_sender(alert)
            self.assertIs(checked["valid"], True, (code, checked))
        alert = self.alert([{"code": c, "count": 99999} for c in self.CODES])
        self.assertEqual(alert["key"], "several_problems")
        self.assertIs(self.through_sender(alert)["valid"], True)
        for line in alert["lines"]:
            self.assertLessEqual(len(line), 300)
            for system in OTHER_SYSTEMS:
                self.assertNotIn(system, line)

    def test_no_sheet_text_reaches_the_email_and_unknown_codes_are_dropped(self):
        alert = self.alert([{"code": "lookup_failed", "count": "traveller1@example.com"}, {"code": "<script>", "count": 1},
                            {"code": "constructor", "count": 1}])
        self.assertEqual(alert["key"], "lookup_failed")
        self.assertNotIn("example.com", json.dumps(alert))
        (result,) = self.run_raw([{"node": BUILD_ALERT, "items": [{"problems": []}], "nodes": {}, "now_ms": NOW_MS}])
        self.assertFalse(result["ok"], "an empty problem list is a bug, not a quiet run")

    def test_unexpected_alert_expression_passes(self):
        expr = self.nodes[UNEXPECTED]["parameters"]["assignments"]["assignments"][0]["value"]
        for executed, word in (((), "before it made or changed any code"), ((WRITE,), "may already have been made")):
            alert = self.expr(expr, {}, executed)
            self.assertIn(word, alert["lines"][0])
            self.assertIs(self.through_sender(alert)["valid"], True)

    def test_every_route_with_a_problem_reaches_the_alert(self):
        to_email = self.nodes[TO_EMAIL]["parameters"]["conditions"]["conditions"][0]["leftValue"]
        failed = self.check(sheet(row()), token={"error": "401"})
        self.assertIs(self.expr(self.nodes[TO_LOOK]["parameters"]["conditions"]["conditions"][0]["leftValue"], failed), False)
        self.assertIs(self.expr(self.nodes[TO_UPDATE]["parameters"]["conditions"]["conditions"][0]["leftValue"], failed), False)
        self.assertIs(self.expr(to_email, failed), True)
        quiet = self.check(sheet(row(ready=False)))
        self.assertIs(self.expr(to_email, quiet), False)


class XtEndToEndTests(XtBase):
    def test_a_mixed_morning(self):
        rows = [
            row(),                                                          # 2: create
            row(traveller=2, email="t2@example.com"),                       # 3: no account yet
            row(traveller=3, email=""),                                     # 4: needs email
            row(invoice="INV-000200", traveller=1, email="old@example.com"),  # 5: adopt the hand-made old name
            row(invoice="INV-000300", traveller=1, email="x@example.com"),  # 6: code belongs to someone else
            row(invoice="INV-000400", traveller=1, email="d@example.com", status="DONE"),  # 7: skipped
            row(invoice="INV-000500", traveller=1, email="n@example.com", ready=False),    # 8: skipped
        ]
        accounts = {"traveller1@example.com": 7001, "old@example.com": 7005, "x@example.com": 7006}
        codes = {"XT-INV000200": code_node(5, owners=(7005,)), "XT-INV000300-01": code_node(6, owners=(7999,))}

        def shopify(l):
            num = accounts.get(l["email"])
            return answer(customer(num, l["email"]) if num else None, codes.get(l["code"]), codes.get(l["legacy"]),
                          with_legacy=l["variables"]["withLegacy"])

        trail = self.full_run(sheet(*rows), shopify)
        written = {}
        for d in trail["built"]["body"]["data"]:
            col, num = re.match(r"'Travellers'!([A-Z]+)([0-9]+)$", d["range"]).groups()
            written.setdefault(int(num), {})[col] = d["values"][0][0]
        written = {r: [v[c] for c in "LMNOPQR"] for r, v in written.items()}
        self.assertEqual({r: v[2] for r, v in written.items()},
                         {2: "DONE", 3: "NEEDS_SHOPIFY_ACCOUNT", 4: "NEEDS_EMAIL", 5: "ADOPTED", 6: "CONFLICT"})
        self.assertEqual(written[5][0], "XT-INV000200")
        self.assertEqual(trail["final"]["problems"], [])
        self.assertEqual([w["kind"] for w in trail["decide"]["writes"]], ["create", "adopt"])
        # The next morning: the made and adopted rows are final, nothing is written to Shopify again.
        after = copy.deepcopy(sheet(*rows))
        for r, v in written.items():
            after["values"][r - 1] = (after["values"][r - 1] + [""] * 18)[:11] + v
        trail = self.full_run(after, shopify)
        self.assertEqual(trail["decide"]["writes"], [])
        self.assertEqual(sorted(l["row"] for l in trail["check"]["lookups"]), [3, 6])


if __name__ == "__main__":
    unittest.main()
