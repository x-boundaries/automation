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
# Sheet layout v2.1 (mailbox/355/20261124-1430), columns A-T.
HEADERS = ["Ready", "Row no.", "Guest name", "Email", "Trip", "Trip code", "Invoice no. (deposit)", "Deposit date",
           "Deposit paid by", "Departure date", "Code valid until", "Discount code", "Code status",
           "Code created in Shopify (SGT)", "Shopify end date (SGT)", "Shopify end date check", "Email sent",
           "Data check / notes", "Message", "Shopify customer ID"]
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


def row(ready=True, invoice="INV-000123", email="traveller1@example.com", trip="Fake Trip", deposit=None,
        departure=None, status="", code="", created="", ends="", pax=None, name="Fake Traveller"):
    """One fake row in layout v2.1 order (A-T); `pax` adds a 21st cell for layouts with a traveller column."""
    deposit = serial(2026, 11, 3) if deposit is None else deposit
    departure = serial(2027, 5, 1) if departure is None else departure
    cells = [ready, 7, name, email, trip, "FT-01", invoice, deposit, "Fake Payer", departure,
             departure, code, status, created, ends, "", "No", "", "", ""]
    if pax is not None:
        cells.append(pax)
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
                                                            "departure", "code", "ends", "valid_until", "status",
                                                            "message", "customer", "checked", "made"]))
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
        # ' and ! are valid in a URL path; G2 L6: one row past last_row is read, so a longer sheet is seen.
        self.assertEqual(out["out"], "'XT%20intake'!A1%3AZZ1001")
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
        # G2 M1: the runbook names the written columns of the agreed layout, and the X6 rollback never touches the
        # consultant's own columns; G2 L3: X6 stops when any other row is ticked Ready.
        self.assertNotRegex(runbook, r"L:R|L<row>|R<row>")
        x6 = next(line for line in runbook.splitlines() if line.startswith("| X6 |"))
        for text in ("K, L, M, N, O, S, T", "restore K, L, N and O", "clear M, S and T",
                     "Never touch the consultant's other columns", "if any other row is ticked Ready",
                     "for row `<n>` only"):
            self.assertIn(text, x6)
        self.assertIn("| `traveller` (`''` today: the number comes from row order within the invoice, ruling F1) | read | no |",
                      runbook)
        self.assertIn("`valid_until` (ruling F2: the same end date)", runbook)
        self.assertIn("Google Sheets API", runbook)
        self.assertIn("7 days", runbook)
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
        renamed = [h if h != "Discount code" else "Code" for h in HEADERS]
        cases = {
            "settings_invalid": dict(run=settings(VALIDITY_RULE="Later")),
            "sheet_read_failed": dict(sheet_value={"error": "403 forbidden"}),
            "sheet_layout_changed": dict(sheet_value=sheet(row(), header=renamed)),
            "token_failed": dict(token={"error": "401"}),
        }
        for code, kw in cases.items():
            out = self.check(kw.get("sheet_value", sheet(row())), kw.get("run"), kw.get("token"))
            self.assertEqual((out["mode"], out["updates"], out["lookups"], out["problems"]),
                             ("failed", [], [], [{"code": code, "count": 1}]), code)
        # G2 L6: last_row 1000 = 999 data rows; the 1000th data row (read because the range runs one row past
        # last_row) stops the run, never skipped silently.
        out = self.check({"values": [HEADERS] + [row(invoice="INV-%06d" % i, ready=False) for i in range(1000)]})
        self.assertEqual(out["problems"], [{"code": "sheet_too_long", "count": 1}])
        out = self.check({"values": [HEADERS] + [row(invoice="INV-%06d" % i, ready=False) for i in range(999)]})
        self.assertEqual(out["problems"], [])
        self.assertEqual(self.check(sheet(row(), header=HEADERS + ["Email"]))["problems"][0]["code"],
                         "sheet_layout_changed")
        self.assertEqual(self.check(sheet(row()), token={"access_token": ""})["problems"][0]["code"], "token_failed")
        bad_layouts = [None, "x", layout(email=""), layout(email="Discount code"), layout(extra="X"),
                       layout() | {"tab": "a'b"}, layout() | {"last_row": 1}, layout() | {"invoice_pattern": "^INV-[0-9]+$"},
                       layout() | {"invoice_pattern": "(["}, layout(trip=5)]
        for bad in bad_layouts:
            out = self.check(sheet(row()), settings(SHEET_LAYOUT=bad))
            self.assertEqual(out["problems"], [{"code": "settings_invalid", "count": 1}], bad)
        out = self.check(sheet(row()), settings(SHEET_LAYOUT=layout(traveller="Pax no.")))
        self.assertEqual(out["problems"], [{"code": "sheet_layout_changed", "count": 1}], "a named header must exist")

    def test_skips_rows_not_ready_and_final_rows(self):
        out = self.check(sheet(row(ready=False), row(invoice="INV-000002", status="DONE"),
                               row(invoice="INV-000003", status="ADOPTED"),
                               row(invoice="INV-000004", status="EXPIRED_NO_ACCOUNT"),
                               row(invoice="INV-000005", ready="TRUE")))
        self.assertEqual((out["mode"], out["lookups"], out["updates"], out["problems"]), ("no_change", [], [], []))

    def test_old_and_retried_statuses_are_checked_again(self):
        # Layout v2 rule 1: the consultant's own Active / Expired (or anything not on the list) = not processed yet.
        values = ["Active", "Expired", "", "NEEDS_SHOPIFY_ACCOUNT", "ERROR", "HELD", "CONFLICT", "CHECK_ROW", "needs_email"]
        rows = [row(invoice="INV-%06d" % (i + 1), email="t%d@example.com" % i, status=v) for i, v in enumerate(values)]
        out = self.check(sheet(*rows))
        self.assertEqual([l["row"] for l in out["lookups"]], list(range(2, 2 + len(values))))

    def test_a_filled_code_column_is_only_a_hint(self):
        # Layout v2 rule 2: the pre-filled code column does not mean the code exists; Shopify is still asked.
        (l,) = self.check(sheet(row(code="XT-INV000123", status="Active")))["lookups"]
        self.assertEqual(l["code"], "XT-INV000123")
        out = self.decide(self.check(sheet(row(code="XT-INV000123", status="Active"))),
                          [answer(customer(7001, "traveller1@example.com"))])
        self.assertEqual(out["writes"][0]["kind"], "create")

    def test_check_row_names_every_bad_field(self):
        out = self.check(sheet(
            row(invoice="INV123", deposit="3 Nov 2026", departure=serial(2026, 10, 1), email="not-an-email"),
            row(invoice="INV-000002", deposit=serial(2026, 11, 3), departure=serial(2026, 11, 2), email="t2@example.com"),
            row(invoice="INV-000003", email=""))) # a row without problems in the fields CHECK_ROW checks
        rows = out["updates"]
        self.assertEqual(self.status_of(rows, 2)["message"], "Check: Invoice no. (deposit), Deposit date, Email.")
        self.assertEqual(self.status_of(rows, 3)["message"], "Check: Departure date (before Deposit date).")
        self.assertEqual([r["status"] for r in rows], ["CHECK_ROW", "CHECK_ROW", "NEEDS_EMAIL"])
        self.assertEqual(out["lookups"], [])

    def test_needs_email_and_same_email_on_the_same_trip(self):
        out = self.check(sheet(row(email=""), row(invoice="INV-000002", email=" Lead@Example.com "),
                               row(invoice="INV-000003", email="lead@example.com"),
                               row(invoice="INV-000004", email="lead@example.com", trip="Other Trip"),
                               row(invoice="INV-000999", email="done@example.com", status="DONE"),
                               row(invoice="INV-000005", email="DONE@example.com")))
        self.assertEqual(out["updates"], [], "rows wait for the lookups, then Decide Code Writes writes them all")
        rows = {u["row"]: u for u in out["decided"]}
        self.assertEqual(rows[2]["status"], "NEEDS_EMAIL")
        self.assertEqual(rows[4]["message"], "Same email as row 3 on the same trip. Each traveller needs their own email.")
        self.assertEqual(rows[7]["message"], "Same email as row 6 on the same trip. Each traveller needs their own email.")
        self.assertEqual({rows[4]["status"], rows[7]["status"]}, {"CHECK_EMAIL"})
        self.assertEqual([l["row"] for l in out["lookups"]], [3, 5])
        self.assertEqual(out["lookups"][0]["email"], "lead@example.com")

    def test_a_second_row_of_the_same_invoice_gets_02_and_so_on(self):
        # Controller ruling F1 (24 Nov 2026): the first row of an invoice is XT-INV<digits>; the 2nd, 3rd ... rows
        # with the same invoice, in row order and whatever their state, get -02, -03 ...
        out = self.check(sheet(row(email="a@example.com"), row(invoice="INV-000999", email="z@example.com"),
                               row(email="b@example.com", ready=False), row(email="c@example.com"),
                               row(email="d@example.com", status="DONE"), row(email="e@example.com")))
        self.assertEqual([(l["row"], l["code"], l["legacy"], l["variables"]["withLegacy"]) for l in out["lookups"]],
                         [(2, "XT-INV000123", "", False), (3, "XT-INV000999", "", False),
                          (5, "XT-INV000123-03", "", False), (7, "XT-INV000123-05", "", False)])
        self.assertEqual(out["decided"], [], "a repeated invoice is not a row problem")
        out = self.decide(self.check(sheet(row(), row(email="t2@example.com"))),
                          [answer(customer(7001, "traveller1@example.com")), answer(customer(7002, "t2@example.com"))])
        self.assertEqual([(w["code"], w["customer"]) for w in out["writes"]],
                         [("XT-INV000123", "7001"), ("XT-INV000123-02", "7002")])

    def test_one_code_per_invoice_row(self):
        # No traveller column, so the code is XT-INV<digits> and there is no old name.
        (first,) = self.check(sheet(row()))["lookups"]
        self.assertEqual((first["code"], first["legacy"]), ("XT-INV000123", ""))
        self.assertEqual(first["variables"], {"email": "traveller1@example.com", "code": "XT-INV000123",
                                              "legacy": "XT-INV000123", "withLegacy": False, "withCustomer": True})
        self.assertIn("customerByIdentifier(identifier: {emailAddress: $email})", first["query"])
        self.assertNotIn("customers(", first["query"])
        self.assertEqual(first["siblings"], [])
        self.assertNotIn("$s1", first["query"])

    def test_a_traveller_column_gives_numbered_codes(self):
        run = settings(SHEET_LAYOUT=layout(traveller="Pax no."))
        out = self.check(sheet(row(pax=1), row(pax=12, email="t12@example.com"), row(pax="x", email="tx@example.com"),
                               header=HEADERS + ["Pax no."]), run)
        first, other = out["lookups"]
        self.assertEqual((first["code"], first["legacy"], first["variables"]["withLegacy"]),
                         ("XT-INV000123-01", "XT-INV000123", True))
        self.assertEqual((other["code"], other["legacy"], other["variables"]["withLegacy"]), ("XT-INV000123-12", "", False))
        (bad,) = out["decided"]
        self.assertEqual((bad["status"], bad["message"]), ("CHECK_ROW", "Check: Pax no.."))
        self.assertEqual(self.check(sheet(row(pax="7"), header=HEADERS + ["Pax no."]), run)["lookups"][0]["code"],
                         "XT-INV000123-07")
        twins = self.check(sheet(row(pax=2), row(pax=2, email="b@example.com"), header=HEADERS + ["Pax no."]), run)
        self.assertEqual(twins["updates"][0]["message"], "Check: Invoice no. (deposit) and Pax no. (same as row 3).")

    def test_end_date_rule(self):
        def end(deposit, departure, rule="later"):
            (l,) = self.check(sheet(row(deposit=deposit, departure=departure)), settings(VALIDITY_RULE=rule))["lookups"]
            return l["end_iso"], l["end_label"], l["end_serial"]

        self.assertEqual(end(serial(2026, 11, 3), serial(2027, 5, 1)),
                         ("2027-11-03T15:59:59.000Z", "3 Nov 2027 23:59", serial(2027, 11, 3)))
        self.assertEqual(end(serial(2026, 11, 3), serial(2028, 1, 9))[:2], ("2028-01-09T15:59:59.000Z", "9 Jan 2028 23:59"))
        self.assertEqual(end(serial(2026, 11, 3), serial(2027, 5, 1), "earlier")[:2],
                         ("2027-05-01T15:59:59.000Z", "1 May 2027 23:59"))
        self.assertEqual(end(serial(2028, 2, 29), serial(2028, 3, 1))[:2], ("2029-02-28T15:59:59.000Z", "28 Feb 2029 23:59"))
        self.assertEqual(end(serial(2026, 11, 3) + 0.75, serial(2027, 5, 1))[:2], ("2027-11-03T15:59:59.000Z", "3 Nov 2027 23:59"))

    def test_expired_rows_look_up_codes_only(self):
        (l,) = self.check(sheet(row(deposit=serial(2025, 1, 2), departure=serial(2025, 3, 1))))["lookups"]
        self.assertIs(l["expired"], True)
        self.assertIs(l["variables"]["withCustomer"], False)
        (l,) = self.check(sheet(row(deposit=serial(2025, 11, 24), departure=serial(2026, 1, 1))))["lookups"]
        self.assertIs(l["expired"], False, "ends 24 Nov 2026 23:59 SGT, the run is at 10:00")

    def test_lookup_brake(self):
        rows = [row(invoice="INV-%06d" % (i + 1), email="t%d@example.com" % i) for i in range(301)]
        out = self.check(sheet(*rows))
        self.assertEqual((out["lookups"], out["problems"]), ([], [{"code": "too_many_rows", "count": 301}]))
        self.assertEqual({u["status"] for u in out["updates"]}, {"HELD"})
        self.assertEqual(len(out["updates"]), 301)
        out = self.check(sheet(*rows[:300]))
        self.assertEqual((len(out["lookups"]), out["problems"]), (300, []))

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        out = self.check(sheet(row(email=""), row(invoice="INV-000002", email="t2@example.com")), settings(DRY_RUN=True))
        self.assertEqual((out["dry_run"], out["updates"], out["problems"]), (True, [], []))
        self.assertEqual(out["plan"], [{"row": 2, "status": "NEEDS_EMAIL", "message": "Add this traveller's own email."}])
        rows = [row(invoice="INV-%06d" % (i + 1), email="t%d@example.com" % i) for i in range(301)]
        out = self.check(sheet(*rows), settings(DRY_RUN=True))
        self.assertEqual((out["mode"], out["updates"], out["problems"]), ("dry_run", [], []))
        self.assertEqual(out["summary"]["held"], 301)
        for value in ("yes", None, "false", 0):
            self.assertIs(self.check(sheet(row(email="")), settings(DRY_RUN=value))["dry_run"], True, value)


class XtDecideTests(XtBase):
    def base(self, *rows, **run):
        return self.check(sheet(*(rows or (row(),))), settings(**run))

    def numbered(self, *rows):
        return self.check(sheet(*rows, header=HEADERS + ["Pax no."]), settings(SHEET_LAYOUT=layout(traveller="Pax no.")))

    def test_no_account_yet(self):
        out = self.decide(self.base(), [answer()])
        (u,) = out["updates"]
        self.assertEqual((u["status"], out["writes"], out["problems"]), ("NEEDS_SHOPIFY_ACCOUNT", [], []))
        self.assertIn("sign up online with this email", u["message"])

    def test_create_uses_exactly_the_owner_defaults(self):
        out = self.decide(self.base(), [answer(customer(7001, "Traveller1@Example.com"))])
        (w,) = out["writes"]
        self.assertEqual((w["kind"], w["code"], w["customer"], w["row"], w["end_serial"]),
                         ("create", "XT-INV000123", "7001", 2, serial(2027, 11, 3)))
        self.assertEqual(w["variables"], {"basicCodeDiscount": {
            "title": "XT-INV000123", "code": "XT-INV000123", "startsAt": "2026-11-24T02:00:00.000Z",
            "endsAt": "2027-11-03T15:59:59.000Z", "customerGets": {"value": {"percentage": 0.15}, "items": {"all": True}},
            "context": {"customers": {"add": ["gid://shopify/Customer/7001"]}}, "usageLimit": 3,
            "appliesOncePerCustomer": False,
            "combinesWith": {"orderDiscounts": False, "productDiscounts": False, "shippingDiscounts": False},
            "tags": ["X-Trekkers"]}})
        self.assertTrue(w["query"].startswith("mutation XtCodeCreate($basicCodeDiscount: DiscountCodeBasicInput!)"))
        self.assertEqual(out["mode"], "write")

    def test_email_must_match_exactly(self):
        out = self.decide(self.base(), [answer(customer(7001, "traveller1.other@example.com"))])
        self.assertEqual((out["updates"][0]["status"], out["writes"]), ("CHECK_EMAIL", []))
        out = self.decide(self.base(), [answer({"id": "gid://shopify/Customer/7001", "defaultEmailAddress": None})])
        self.assertEqual(out["updates"][0]["status"], "CHECK_EMAIL")

    def test_adopt_a_hand_made_code_with_one_update(self):
        old = code_node(owners=(7001,), uses=0, limit=None, ends="2027-07-03T15:59:00Z")
        out = self.decide(self.base(row(status="Active", code="XT-INV000123")),
                          [answer(customer(7001, "traveller1@example.com"), old)])
        (w,) = out["writes"]
        self.assertEqual((w["kind"], w["code"], w["node_id"]), ("adopt", "XT-INV000123", old["id"]))
        self.assertEqual(w["variables"], {"id": old["id"], "basicCodeDiscount": {
            "usageLimit": 3, "endsAt": "2027-11-03T15:59:59.000Z",
            "combinesWith": {"orderDiscounts": False, "productDiscounts": False, "shippingDiscounts": False}}})
        self.assertTrue(w["query"].startswith("mutation XtCodeAdopt($id: ID!, $basicCodeDiscount: DiscountCodeBasicInput!)"))

    def test_with_a_traveller_column_the_old_name_is_adopted_for_traveller_one_only(self):
        old = code_node(owners=(7001,))
        out = self.decide(self.numbered(row(pax=1)), [answer(customer(7001, "traveller1@example.com"), None, old,
                                                             with_legacy=True)])
        self.assertEqual((out["writes"][0]["kind"], out["writes"][0]["code"]), ("adopt", "XT-INV000123"))
        out = self.decide(self.numbered(row(pax=2, email="t2@example.com")),
                          [answer(customer(7002, "t2@example.com"), None, code_node(owners=(7002,)))])
        self.assertEqual(out["writes"][0]["kind"], "create")
        out = self.decide(self.numbered(row(pax=1)), [answer(customer(7001, "traveller1@example.com"), code_node(),
                                                             code_node(2), with_legacy=True)])
        self.assertEqual(out["updates"][0]["message"], "Both XT-INV000123 and XT-INV000123-01 exist. Tell XB.")

    def test_a_code_already_at_the_defaults_needs_no_write(self):
        done = code_node(owners=(7001,), uses=1, limit=3, ends="2027-11-03T15:59:59Z")
        out = self.decide(self.base(), [answer(customer(7001, "traveller1@example.com"), done)])
        (u,) = out["updates"]
        self.assertEqual((u["status"], u["code"], u["ends"], u["ends_serial"], u["customer"], out["writes"]),
                         ("ADOPTED", "XT-INV000123", "3 Nov 2027 23:59", serial(2027, 11, 3), "7001", []))

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
                out = self.decide(self.base(), [answer(me, node)])
                (u,) = out["updates"]
                self.assertEqual((u["status"], out["writes"], u["customer"]), ("CONFLICT", [], "7001"), (why, node))
                self.assertIn(why, u["message"])
        out = self.decide(self.base(), [answer(me, code_node(context="DiscountCustomerSegments"))])
        self.assertEqual(out["updates"][0]["status"], "CONFLICT")
        ok = self.decide(self.base(), [answer(me, code_node(uses=3))])
        self.assertEqual(ok["writes"][0]["kind"], "adopt", "3 uses so far still passes the guard")

    def test_expired_rows(self):
        base = self.base(row(deposit=serial(2025, 1, 2), departure=serial(2025, 3, 1)))
        out = self.decide(base, [answer(None, None)])
        self.assertEqual(out["updates"][0]["status"], "EXPIRED_NO_ACCOUNT")
        out = self.decide(base, [answer(None, code_node())])
        self.assertEqual(out["updates"][0]["message"], "End date has passed but code XT-INV000123 exists. Tell XB.")
        self.assertEqual(out["writes"], [])

    def test_lookup_failures_are_errors_for_the_health_check(self):
        failures = [{"error": "500 server error"}, {"errors": [{"message": "x", "extensions": {"code": "THROTTLED"}}]},
                    {"errors": [{"message": "Field 'x' doesn't exist"}]}, {"data": None}, None, "text"]
        rows = [row(invoice="INV-%06d" % (i + 1), email="t%d@example.com" % (i + 1)) for i in range(len(failures) + 1)]
        out = self.decide(self.base(*rows), failures + [answer(None)])
        self.assertEqual([u["status"] for u in sorted(out["updates"], key=lambda u: u["row"])],
                         ["ERROR"] * len(failures) + ["NEEDS_SHOPIFY_ACCOUNT"])
        self.assertEqual(out["problems"], [{"code": "lookup_failed", "count": len(failures)}])
        out = self.decide(self.base(row(), row(invoice="INV-000002", email="t2@example.com")), [answer()])
        self.assertEqual(({u["status"] for u in out["updates"]}, out["problems"]),
                         ({"ERROR"}, [{"code": "lookup_failed", "count": 2}]))

    def test_write_brake(self):
        def run(n):
            rows = [row(invoice="INV-%06d" % (i + 1), email="t%d@example.com" % i) for i in range(n)]
            return self.decide(self.base(*rows), [answer(customer(8000 + i, "t%d@example.com" % i)) for i in range(n)])
        out = run(30)
        self.assertEqual((len(out["writes"]), out["problems"]), (30, []))
        out = run(31)
        self.assertEqual((out["mode"], out["writes"]), ("braked", []))
        self.assertEqual(out["problems"], [{"code": "too_many_codes", "count": 31}])
        self.assertEqual({u["status"] for u in out["updates"]}, {"HELD"})

    def test_dry_run_plan(self):
        base = self.base(row(), row(invoice="INV-000002", email=""), DRY_RUN=True)
        self.assertEqual(self.expr(self.nodes[TO_LOOK]["parameters"]["conditions"]["conditions"][0]["leftValue"], base), True)
        out = self.decide(base, [answer(customer(7001, "traveller1@example.com"))])
        self.assertEqual((out["mode"], out["writes"], out["updates"], out["problems"]), ("dry_run", [], [], []))
        self.assertEqual(out["plan"], [
            {"row": 2, "status": "would CREATE", "code": "XT-INV000123", "ends": "3 Nov 2027 23:59"},
            {"row": 3, "status": "NEEDS_EMAIL", "message": "Add this traveller's own email.", "code": ""}])
        self.assertNotIn("example.com", json.dumps(out["plan"]))


class XtSecondCodeTests(XtBase):
    """G2 L1: never a second code for the same traveller when rows of one invoice are renumbered."""

    def test_the_lookup_asks_for_the_other_code_names_of_the_invoice(self):
        out = self.check(sheet(row(email="a@example.com"), row(email="b@example.com", status="DONE"),
                               row(invoice="INV-000999", email="z@example.com"), row(email="c@example.com")))
        first, other, third = out["lookups"]
        self.assertEqual(first["siblings"], ["XT-INV000123-02", "XT-INV000123-03"])
        self.assertEqual(third["siblings"], ["XT-INV000123", "XT-INV000123-02"])
        self.assertEqual(other["siblings"], [])
        self.assertEqual((third["variables"]["s1"], third["variables"]["s2"]), ("XT-INV000123", "XT-INV000123-02"))
        self.assertIn(", $s1: String!, $s2: String!) {", third["query"])
        self.assertIn("s2: codeDiscountNodeByCode(code: $s2) { ...XtCode }", third["query"])
        expired = self.check(sheet(row(deposit=serial(2025, 1, 2), departure=serial(2025, 3, 1)),
                                   row(email="b@example.com", deposit=serial(2025, 1, 2), departure=serial(2025, 3, 1))))
        self.assertEqual([l["siblings"] for l in expired["lookups"]], [[], []])
        many = self.check(sheet(*[row(email="t%d@example.com" % i) for i in range(25)]))
        self.assertEqual(len(many["lookups"][0]["siblings"]), 20)

    def test_g2_probe_p5_rows_inserted_above_after_a_failed_write_back(self):
        # Run 1 made XT-INV000123 for a@ and XT-INV000123-02 for b@, but the sheet write failed. Then a row for c@
        # was inserted above, so a@ now derives -02 and b@ derives -03. Run 2 must make no code at all.
        shopify_codes = {"XT-INV000123": code_node(1, owners=(7001,), limit=3, ends="2027-11-03T15:59:59Z"),
                         "XT-INV000123-02": code_node(2, owners=(7002,), limit=3, ends="2027-11-03T15:59:59Z")}
        accounts = {"c@example.com": 7003, "a@example.com": 7001, "b@example.com": 7002}

        def shopify(l):
            data = answer(customer(accounts[l["email"]], l["email"]), shopify_codes.get(l["code"]))
            for k, name in enumerate(l.get("siblings", [])):
                data["data"]["s%d" % (k + 1)] = shopify_codes.get(name)
            return data

        rows = [row(email="c@example.com"), row(email="a@example.com"), row(email="b@example.com")]
        trail = self.full_run(sheet(*rows), shopify)
        self.assertEqual(trail["decide"]["writes"], [], "no second code for a@ or b@, and none over c@'s head")
        got = {u["row"]: (u["status"], u["code"]) for u in trail["decide"]["updates"]}
        self.assertEqual(got, {2: ("CONFLICT", ""), 3: ("ADOPTED", "XT-INV000123"), 4: ("ADOPTED", "XT-INV000123-02")})
        self.assertIn("XT-INV000123 already exists and belongs to a different customer",
                      self.status_of(trail["decide"]["updates"], 2)["message"])

    def test_a_sibling_needing_the_defaults_is_adopted_with_one_update_and_two_are_a_conflict(self):
        me = customer(7002, "b@example.com")
        base = self.check(sheet(row(email="a@example.com"), row(email="b@example.com")))
        lookup = base["lookups"][1]
        self.assertEqual((lookup["code"], lookup["siblings"]), ("XT-INV000123-02", ["XT-INV000123"]))
        mine = code_node(1, owners=(7002,))
        out = self.decide(base, [answer(customer(7001, "a@example.com")),
                                 answer(me, None) | {"data": {"customer": me, "code": None, "s1": mine}}])
        adopt = [w for w in out["writes"] if w["row"] == 3]
        self.assertEqual([(w["kind"], w["code"], w["node_id"]) for w in adopt], [("adopt", "XT-INV000123", mine["id"])])
        three = self.check(sheet(row(email="a@example.com"), row(email="b@example.com"), row(email="c@example.com")))
        data = {"customer": customer(7003, "c@example.com"), "code": None,
                "s1": code_node(1, owners=(7003,)), "s2": code_node(2, owners=(7003,))}
        out = self.decide(three, [answer(), answer(), {"data": data}])
        self.assertEqual(self.status_of(out["updates"], 4)["message"],
                         "Codes XT-INV000123 and XT-INV000123-02 already belong to this customer. Nothing was changed. Tell XB.")
        theirs = {"customer": customer(7003, "c@example.com"), "code": None, "s1": code_node(1, owners=(7001,)),
                  "s2": code_node(2, owners=(7001, 7003))}
        out = self.decide(three, [answer(), answer(), {"data": theirs}])
        self.assertEqual([w["kind"] for w in out["writes"]], ["create"], "other customers' codes are not adopted")


class XtWriteTests(XtBase):
    def decided(self):
        rows = [row(), row(invoice="INV-000124", email="t2@example.com"), row(invoice="INV-000125", email="t3@example.com")]
        base = self.check(sheet(*rows))
        return self.decide(base, [answer(customer(7001, "traveller1@example.com")),
                                  answer(customer(7002, "t2@example.com"), code_node(owners=(7002,))), answer(None)])

    def test_confirmed_writes_become_done_and_adopted(self):
        decided = self.decided()
        create, adopt = decided["writes"]
        out = self.check_writes(decided, [create_ok(create), adopt_ok(adopt)])
        rows = {u["row"]: u for u in out["updates"]}
        self.assertEqual({k: rows[2][k] for k in ("status", "code", "ends", "ends_serial", "customer", "made")},
                         {"status": "DONE", "code": "XT-INV000123", "ends": "3 Nov 2027 23:59",
                          "ends_serial": serial(2027, 11, 3), "customer": "7001", "made": "24 Nov 2026 10:00"})
        self.assertEqual((rows[3]["status"], rows[3]["code"]), ("ADOPTED", "XT-INV000124"))
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
        for mutate in (lambda a: a["codeDiscount"].update(usageLimit=None),
                       lambda a: a["codeDiscount"].update(endsAt="2027-05-01T15:59:59Z"),
                       lambda a: a["codeDiscount"]["codes"]["nodes"][0].update(code="XT-INV000999"),
                       lambda a: a["codeDiscount"].update(__typename="DiscountCodeApp")):
            good = create_ok(create)
            mutate(good["data"]["discountCodeBasicCreate"]["codeDiscountNode"])
            bad_creates.append(good)
        for bad in bad_creates:
            out = self.check_writes(decided, [bad, adopt_ok(adopt)])
            rows = {u["row"]: u for u in out["updates"]}
            self.assertEqual((rows[2]["status"], rows[2]["code"], rows[2]["customer"], rows[3]["status"]),
                             ("ERROR", "", "7001", "ADOPTED"), bad)
            self.assertEqual(out["problems"], [{"code": "write_failed", "count": 1}])
        wrong = adopt_ok(adopt)
        wrong["data"]["discountCodeBasicUpdate"]["codeDiscountNode"]["id"] = "gid://shopify/DiscountCodeNode/1"
        out = self.check_writes(decided, [create_ok(create), wrong])
        self.assertEqual(out["problems"], [{"code": "write_failed", "count": 1}])
        out = self.check_writes(decided, [create_ok(create)])
        self.assertEqual(out["problems"], [{"code": "write_failed", "count": 2}], "a short answer fails every write")


class XtSheetTests(XtBase):
    def cells(self, trail):
        return [(d["range"], d["values"][0][0]) for d in trail["built"]["body"]["data"]]

    def test_layout_v2_write_rules(self):
        # DONE: code, end date (a Sheets date number), the same date in "Code valid until" (ruling F2), status, empty
        # message, customer ID, created time.
        # Any other status: status and message only (plus the customer ID when one matched).
        value = sheet(row(code="XT-INV000123", status="Active"), row(invoice="INV-000002", email="", code="XT-INV000002"))
        trail = self.full_run(value, lambda l: answer(customer(7001, "traveller1@example.com")))
        self.assertEqual(trail["built"]["body"]["valueInputOption"], "RAW")
        self.assertEqual(self.cells(trail), [
            ("'Travellers'!M3", "NEEDS_EMAIL"), ("'Travellers'!S3", "Add this traveller's own email."),
            ("'Travellers'!L2", "XT-INV000123"), ("'Travellers'!O2", serial(2027, 11, 3)),
            ("'Travellers'!K2", serial(2027, 11, 3)), ("'Travellers'!M2", "DONE"),
            ("'Travellers'!S2", ""), ("'Travellers'!T2", "7001"), ("'Travellers'!N2", "24 Nov 2026 10:00")])
        self.assertEqual(trail["final"]["problems"], [])
        self.assertIs(self.expr(self.nodes[TO_EMAIL]["parameters"]["conditions"]["conditions"][0]["leftValue"],
                                trail["final"]), False)

    def test_the_consultants_own_values_stay_until_a_code_is_made(self):
        value = sheet(row(code="XT-INV000123", status="Active", created="Yes", ends=serial(2027, 7, 3)),
                      row(invoice="INV-000300", email="x@example.com", code="XT-INV000300", status="Active"))
        trail = self.full_run(value, lambda l: answer(customer(7001, l["email"]) if l["row"] == 3 else None,
                                                      code_node(owners=(7999,)) if l["row"] == 3 else None))
        self.assertEqual(self.cells(trail), [
            ("'Travellers'!M2", "NEEDS_SHOPIFY_ACCOUNT"), ("'Travellers'!S2", self.status_of(
                trail["decide"]["updates"], 2)["message"]),
            ("'Travellers'!M3", "CONFLICT"), ("'Travellers'!S3", self.status_of(trail["decide"]["updates"], 3)["message"]),
            ("'Travellers'!T3", "7001")])

    def test_a_row_that_moved_is_not_written(self):
        value = sheet(row(), row(invoice="INV-000002", email="t2@example.com"))
        moved = reread_of(value)
        moved["values"][2][3] = "someone.else@example.com"
        trail = self.full_run(value, lambda l: answer(None), reread=moved)
        self.assertEqual({r[0][-1] for r in self.cells(trail)}, {"2"})
        self.assertEqual(trail["final"]["problems"], [{"code": "rows_moved", "count": 1}])
        inserted = reread_of(value)
        inserted["values"].insert(1, row(invoice="INV-000555", email="new@example.com"))
        trail = self.full_run(value, lambda l: answer(None), reread=inserted)
        self.assertEqual((trail["built"]["expected"], trail["final"]["problems"]), (0, [{"code": "rows_moved", "count": 2}]))
        self.assertIs(self.expr(self.nodes[SHEET_NEEDED]["parameters"]["conditions"]["conditions"][0]["leftValue"],
                                trail["built"]), False)

    def test_check_rows_keys_survive_the_reread(self):
        value = sheet(row(invoice="INV-ABC"), row(invoice="INV-000002", email=""))
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
        value = sheet(row(email=""), row(invoice="INV-000002", email="t2@example.com"))
        trail = self.full_run(value, lambda l: answer(None), run=settings(DRY_RUN=True))
        self.assertIs(trail["to_update"], False)
        self.assertNotIn("built", trail)

    def test_brakes_still_write_held_and_alert(self):
        rows = [row(invoice="INV-%06d" % (i + 1), email="t%d@example.com" % i) for i in range(31)]
        trail = self.full_run(sheet(*rows), lambda l: answer(customer(8000 + l["row"], l["email"])))
        self.assertEqual({v for r, v in self.cells(trail) if r.startswith("'Travellers'!M")}, {"HELD"})
        self.assertEqual(trail["final"]["problems"], [{"code": "too_many_codes", "count": 31}])


class XtLayoutTests(XtBase):
    """The sheet follows the consultant's own layout; columns live only in Run Settings SHEET_LAYOUT."""

    def other_layout(self):
        # Another fake layout: other names, another order, extra columns, a traveller column, no customer column.
        header = ["No.", "Trip", "Lead booker", "Participant", "Invoice", "Pax no.", "Email address", "Deposit paid",
                  "Departure", "Discount code", "Code status", "Code expiry", "Code created", "Remarks", "Ready",
                  "Message", "Last run"]
        lay = copy.deepcopy(LAYOUT)
        lay.update(tab="XT intake", invoice_pattern="^INV-?([0-9]{1,12})$")
        lay["columns"] = {"ready": "Ready", "invoice": "Invoice", "traveller": "Pax no.", "email": "Email address",
                          "trip": "Trip", "deposit": "Deposit paid", "departure": "Departure", "code": "Discount code",
                          "ends": "Code expiry", "status": "Code status", "message": "Message",
                          "customer": "", "checked": "Last run", "made": "Code created"}

        def other_row(invoice="INV000123", pax=1, email="traveller1@example.com", ready=True, status=""):
            return [7, "Fake Trip", "Fake Lead", "Fake Traveller", invoice, pax, email, serial(2026, 11, 3),
                    serial(2027, 5, 1), "", status, "", "", "", ready]
        return header, lay, other_row

    def test_any_layout_reads_and_writes_by_header(self):
        header, lay, other_row = self.other_layout()
        value = sheet(other_row(), other_row(pax=2, email=""), other_row(invoice="INV-000124", email="b@example.com",
                                                                     status="DONE"), header=header)
        run = settings(SHEET_LAYOUT=lay)
        trail = self.full_run(value, lambda l: answer(customer(7001, l["email"]), with_legacy=l["variables"]["withLegacy"]),
                              run=run)
        self.assertEqual([l["code"] for l in trail["check"]["lookups"]], ["XT-INV000123-01"])
        self.assertEqual([(d["range"], d["values"][0][0]) for d in trail["built"]["body"]["data"]], [
            ("'XT intake'!K3", "NEEDS_EMAIL"), ("'XT intake'!P3", "Add this traveller's own email."),
            ("'XT intake'!Q3", "24 Nov 2026 10:00"),
            ("'XT intake'!J2", "XT-INV000123-01"), ("'XT intake'!L2", serial(2027, 11, 3)), ("'XT intake'!K2", "DONE"),
            ("'XT intake'!P2", ""), ("'XT intake'!Q2", "24 Nov 2026 10:00"), ("'XT intake'!M2", "24 Nov 2026 10:00")])
        self.assertEqual(trail["final"]["problems"], [])
        (u,) = self.check(sheet(other_row(pax="x"), header=header), run)["updates"]
        self.assertEqual(u["message"], "Check: Pax no..")

    def test_a_header_change_during_the_run_writes_nothing(self):
        header, lay, other_row = self.other_layout()
        value = sheet(other_row(email=""), header=header)
        renamed = copy.deepcopy(value)
        renamed["values"][0][10] = "Status"
        trail = self.full_run(value, lambda l: None, run=settings(SHEET_LAYOUT=lay), reread=renamed)
        self.assertEqual((trail["built"]["expected"], trail["final"]["problems"]),
                         (0, [{"code": "sheet_reread_failed", "count": 1}]))

    def test_a_header_doubled_during_the_run_writes_nothing(self):
        # G2 L2: the write-time header check refuses a header that now appears twice.
        value = sheet(row(email=""))
        doubled = copy.deepcopy(value)
        doubled["values"][0] = HEADERS + ["Code status"]
        trail = self.full_run(value, lambda l: None, reread=doubled)
        self.assertEqual((trail["built"]["expected"], trail["final"]["problems"]),
                         (0, [{"code": "sheet_reread_failed", "count": 1}]))

    def test_columns_beyond_z_get_two_letters(self):
        header = HEADERS + ["Extra %d" % i for i in range(10)] + ["Message 2"]
        trail = self.full_run(sheet(row(email=""), header=header), lambda l: None,
                              run=settings(SHEET_LAYOUT=layout(message="Message 2")))
        self.assertIn("'Travellers'!AE2", [d["range"] for d in trail["built"]["body"]["data"]])

    def test_no_code_node_names_a_column(self):
        names = {n for n in LAYOUT["columns"].values() if n} | {LAYOUT["tab"]}
        for node in CODE_NODES:
            code = self.nodes[node]["parameters"]["jsCode"]
            for name in names:
                self.assertNotIn("'%s'" % name, code, (node, name))
            self.assertNotRegex(code, r"!\$?[A-Z]{1,2}\$?[0-9]+|'[A-Z]{1,2}[0-9]*:[A-Z]{1,2}[0-9]*'", node)
        both = [self.nodes[n]["parameters"]["jsCode"] for n in (CHECK, BUILD_SHEET)]
        block = lambda code: code[code.index("// ---- the sheet layout"):code.index("const locate")]
        self.assertEqual(block(both[0]), block(both[1]), "the layout check is the same in both nodes")

    def test_the_export_layout_is_contract_v2_1(self):
        self.assertEqual((LAYOUT["tab"], LAYOUT["invoice_pattern"]), ("Travellers", "^INV-([0-9]{1,12})$"))
        self.assertEqual(LAYOUT["columns"], {
            "ready": "Ready", "invoice": "Invoice no. (deposit)", "traveller": "", "email": "Email", "trip": "Trip",
            "deposit": "Deposit date", "departure": "Departure date", "code": "Discount code", "status": "Code status",
            "made": "Code created in Shopify (SGT)", "ends": "Shopify end date (SGT)", "valid_until": "Code valid until",
            "message": "Message", "customer": "Shopify customer ID", "checked": ""})
        written = {v for k, v in LAYOUT["columns"].items()
                   if k in ("code", "status", "made", "ends", "valid_until", "message", "customer")}
        self.assertEqual(sorted("ABCDEFGHIJKLMNOPQRST"[HEADERS.index(h)] for h in written), list("KLMNOST"))


class XtAlertTests(XtBase):
    CODES = ("settings_invalid", "sheet_read_failed", "sheet_layout_changed", "sheet_too_long", "token_failed", "too_many_rows",
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
            row(code="XT-INV000123", status="Active"),                              # 2: create
            row(invoice="INV-000124", email="t2@example.com"),                      # 3: no account yet
            row(invoice="INV-000125", email=""),                                    # 4: needs email
            row(invoice="INV-000200", email="old@example.com", code="XT-INV000200", status="Active", created="Yes",
                ends=serial(2027, 7, 3)),                                           # 5: adopt the hand-made code
            row(invoice="INV-000300", email="x@example.com"),                       # 6: code belongs to someone else
            row(invoice="INV-000400", email="d@example.com", status="DONE"),        # 7: skipped
            row(invoice="INV-000500", email="n@example.com", ready=False),          # 8: skipped
        ]
        accounts = {"traveller1@example.com": 7001, "old@example.com": 7005, "x@example.com": 7006}
        codes = {"XT-INV000200": code_node(5, owners=(7005,)), "XT-INV000300": code_node(6, owners=(7999,))}

        def shopify(l):
            num = accounts.get(l["email"])
            return answer(customer(num, l["email"]) if num else None, codes.get(l["code"]))

        trail = self.full_run(sheet(*rows), shopify)
        written = {}
        for d in trail["built"]["body"]["data"]:
            col, num = re.match(r"'Travellers'!([A-Z]+)([0-9]+)$", d["range"]).groups()
            written.setdefault(int(num), {})[col] = d["values"][0][0]
        self.assertEqual({r: v["M"] for r, v in written.items()},
                         {2: "DONE", 3: "NEEDS_SHOPIFY_ACCOUNT", 4: "NEEDS_EMAIL", 5: "ADOPTED", 6: "CONFLICT"})
        self.assertEqual((written[5]["L"], written[5]["O"], written[5]["K"], written[5]["T"]),
                         ("XT-INV000200", serial(2027, 11, 3), serial(2027, 11, 3), "7005"))
        self.assertNotIn("K", written[3], "his own valid-until stays until a code is made")
        self.assertEqual(trail["final"]["problems"], [])
        self.assertEqual([w["kind"] for w in trail["decide"]["writes"]], ["create", "adopt"])
        # The next morning: the made and adopted rows are final, nothing is written to Shopify again.
        after = copy.deepcopy(sheet(*rows))
        for r, cols in written.items():
            cells = (after["values"][r - 1] + [""] * 20)[:20]
            for col, v in cols.items():
                cells[ord(col) - 65] = v
            after["values"][r - 1] = cells
        trail = self.full_run(after, shopify)
        self.assertEqual(trail["decide"]["writes"], [])
        self.assertEqual(sorted(l["row"] for l in trail["check"]["lookups"]), [3, 6])


if __name__ == "__main__":
    unittest.main()
