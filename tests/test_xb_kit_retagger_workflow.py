"""Offline contract of the inactive "XB Kit Re-tagger" n8n workflow (#269 Part A, hands-off fix 1).

No n8n instance, Shopify store or email account is contacted. The committed Code nodes, the
pagination and IF expressions and the shared sender's own `Validate Alert Request` node are executed
with the local Node runtime against synthetic Shopify pages and a synthetic workflow static data
record. Every product, handle and id here is fake.
The rules fixture `tests/fixtures/xb_kit_rules_v1.fixture` is the logic subset (slots[*].any,
profiles[*].slots, exclude.title_words, exclude.types) of the XT partner pack `XB_kit_rules.json`
v1 (22 Oct 2026, sha256 55bec65ee29fe1bdc4425920d85358cfa321f2c050a01f04d2c121bde4b6e1f5).
"""

import copy
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows" / "xb_kit_retagger.workflow.json"
SENDER = ROOT / "n8n-workflows" / "ops_alert_email.workflow.json"
RULES_FIXTURE = ROOT / "tests" / "fixtures" / "xb_kit_rules_v1.fixture"
README = ROOT / "n8n-workflows" / "README.md"
RUNBOOK = ROOT / "docs" / "xb_kit_retagger.md"
CI = ROOT / ".github" / "workflows" / "xb-kit-retagger-tests.yml"

PLAN = "Work Out Kit Tag Changes"
CHECK = "Check Write Results"
TOKEN = "Get Shopify Access Token"
READ = "Read Active Products"
WRITE = "Write Tag Changes"
SETTINGS = "Run Settings"
SEND = "Send Kit Alert"
STOP = "End Run As Failed"
UNEXPECTED = "Build Unexpected Alert"
TO_WRITE = "Changes To Write"
TO_EMAIL = "Error To Email"
SYSTEM = "KIT-RETAG"
OTHER_SYSTEMS = ("AC2-STOCK", "AC2-ARAP", "ENERGYGRID", "MEMBER-SYNC", "HEALTH")
KITS = ("tropical", "hut", "himalaya", "expedition", "winter", "travel", "island", "ride")
RECORD = "kitTagRecord"
# 21 Nov 2026 03:00 SGT, a fixed clock for every Code run.
NOW_MS = int(datetime(2026, 11, 20, 19, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
TODAY = "2026-11-21"
FAKE_TOKEN = "fake-access-token-0000000000"
OUTSIDE_LINE = ("Kit tags on the website: many kit tags were changed outside the re-tagger since last night, so "
                "nothing was changed tonight. Ask Claude to check.")
BRAKE_LINE = ("Kit tags on the website: tonight's run stopped itself because it planned to remove more tags than "
              "normal. Nothing was changed. Ask Claude to check.")
# Words the owner never sees in an email (owner, 21 Nov 2026).
JARGON = re.compile(r"\b(pin|pins|pinned|cap|caps|override)\b|ks-|kit-pin|CHANGE_CAP|REMOVE_CAP|CAP_OVERRIDE", re.I)

NODE_TYPES = {
    "Daily 03:00 SGT": "n8n-nodes-base.scheduleTrigger",
    "Manual Run": "n8n-nodes-base.manualTrigger",
    SETTINGS: "n8n-nodes-base.set",
    TOKEN: "n8n-nodes-base.httpRequest",
    READ: "n8n-nodes-base.httpRequest",
    PLAN: "n8n-nodes-base.code",
    TO_WRITE: "n8n-nodes-base.if",
    TO_EMAIL: "n8n-nodes-base.if",
    "List Writes": "n8n-nodes-base.splitOut",
    WRITE: "n8n-nodes-base.httpRequest",
    CHECK: "n8n-nodes-base.code",
    UNEXPECTED: "n8n-nodes-base.set",
    SEND: "n8n-nodes-base.executeWorkflow",
    STOP: "n8n-nodes-base.stopAndError",
}
STICKIES = {"Start Here", "Setup", "Read Stage", "Decide Stage", "Write Stage"}
EXPECTED_EDGES = {
    ("Daily 03:00 SGT", 0, SETTINGS),
    ("Manual Run", 0, SETTINGS),
    (SETTINGS, 0, TOKEN),
    (TOKEN, 0, READ),
    (READ, 0, PLAN),
    (PLAN, 0, TO_WRITE),
    (PLAN, 1, UNEXPECTED),
    (TO_WRITE, 0, "List Writes"),
    (TO_WRITE, 1, TO_EMAIL),
    (TO_EMAIL, 0, SEND),
    ("List Writes", 0, WRITE),
    (WRITE, 0, CHECK),
    (CHECK, 0, TO_EMAIL),
    (CHECK, 1, UNEXPECTED),
    (UNEXPECTED, 0, SEND),
    (SEND, 0, STOP),
}

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
      return { ok: true, out: new Function('$response', '$json', '$', 'return (' + inner(c.expr) + ');')(c.response, c.json, ran) };
    }
    const w = c.workflow === 'sender' ? sender : wf;
    const code = nodeOf(w, c.node).parameters.jsCode;
    const $input = { all: () => c.raw || c.items.map((json) => ({ json })) };
    const $ = (name) => {
      if (!(name in (c.nodes || {}))) throw new Error('unexpected node reference: ' + name);
      const list = Array.isArray(c.nodes[name]) ? c.nodes[name] : [c.nodes[name]];
      return { first: () => ({ json: list[0] }), all: () => list.map((json) => ({ json })) };
    };
    const store = c.static === undefined ? {} : c.static;
    const $getWorkflowStaticData = (type) => {
      if (type !== 'global') throw new Error('unexpected static data type: ' + type);
      return store;
    };
    const out = new Function('$input', '$', '$getWorkflowStaticData', code)($input, $, $getWorkflowStaticData);
    return { ok: true, out, static: store };
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


def settings(**overrides):
    value = {"DRY_RUN": True, "API_VERSION": "2026-10", "SHOP_DOMAIN": "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN"}
    value.update(overrides)
    return value


def product(i, title="Fake Product", vendor="Fake Vendor", ptype="Fake Type", tags=(), handle=None, status="ACTIVE"):
    return {"id": "gid://shopify/Product/%d" % (9000000 + i), "handle": handle or "fake-product-%03d" % i,
            "title": title, "vendor": vendor, "productType": ptype, "status": status, "tags": list(tags)}


def num(i):
    return str(9000000 + i)


def pages_of(products, per_page=100, count=None, precision="EXACT"):
    chunks = [products[i:i + per_page] for i in range(0, len(products), per_page)] or [[]]
    out = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        out.append({"data": {
            "productsCount": {"count": len(products) if count is None else count, "precision": precision},
            "products": {"pageInfo": {"hasNextPage": not last, "endCursor": None if last else "fake-cursor-%d" % index},
                         "nodes": chunk}},
            "extensions": {"cost": {"requestedQueryCost": 102}}})
    return out


def socks(i, title=None, **extra):
    return product(i, title=title or "Fake Trail Socks %d" % i, ptype="Socks", **extra)


def mug(i, tags=("kit-hut",)):
    """A product no kit wants: every known tag on it is a planned removal."""
    return product(i, title="Fake Mug %d" % i, ptype="Mug", tags=tags)


def record(entries):
    return {RECORD: {"v": 1, "products": copy.deepcopy(entries)}}


SOCK_TAGS = sorted(["kit-%s" % k for k in ("tropical", "hut", "himalaya", "expedition", "winter", "travel")] + ["ks-socks"])


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class KitRetaggerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load(WORKFLOW)
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}
        cls.rules = load(RULES_FIXTURE)

    def run_raw(self, cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            done = subprocess.run([shutil.which("node"), str(harness), str(WORKFLOW), str(SENDER), str(cases_path)],
                                  capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        results = json.loads(done.stdout)
        for result in results:
            self.assertTrue(result["ok"], result)
        return results

    def run_cases(self, cases):
        return [result["out"] for result in self.run_raw(cases)]

    def plan_full(self, pages, token=None, static=None, **overrides):
        case = {"node": PLAN, "items": pages, "now_ms": NOW_MS, "static": {} if static is None else static,
                "nodes": {SETTINGS: settings(**overrides),
                          TOKEN: token if token is not None else {"access_token": FAKE_TOKEN, "scope": "fake"}}}
        (result,) = self.run_raw([case])
        (item,) = result["out"]
        return item["json"], result["static"]

    def plan(self, pages, token=None, **overrides):
        return self.plan_full(pages, token=token, **overrides)[0]

    def plan_products(self, products, **overrides):
        return self.plan(pages_of(products), **overrides)

    def check_full(self, writes, results, static=None):
        case = {"node": CHECK, "items": results, "now_ms": NOW_MS, "static": {} if static is None else static,
                "nodes": {"List Writes": writes}}
        (result,) = self.run_raw([case])
        (item,) = result["out"]
        return item["json"], result["static"]

    def check(self, writes, results, static=None):
        return self.check_full(writes, results, static)[0]

    def validate_alerts(self, alerts):
        bodies = [{"system": SYSTEM, **alert} for alert in alerts]
        outs = self.run_cases([{"workflow": "sender", "node": "Validate Alert Request", "items": [body]}
                               for body in bodies])
        return [out[0]["json"] for out in outs]

    def assert_owner_alert(self, alert):
        """Every email is one plain-English ERROR the shared sender accepts."""
        self.assertEqual(alert["severity"], "ERROR")
        self.assertEqual(alert["key"], alert["short"])
        self.assertTrue(alert["lines"][0].startswith("Kit tags on the website: "), alert["lines"][0])
        self.assertTrue(alert["lines"][0].endswith("Ask Claude to check."), alert["lines"][0])
        self.assertIsNone(JARGON.search("\n".join([alert["short"]] + alert["lines"])), alert)
        (out,) = self.validate_alerts([alert])
        self.assertIs(out.get("valid"), True, (out, alert))

    def route(self, item):
        """The two IF nodes on a plan or check item: (goes to the writes, goes to the error email)."""
        cond = lambda name: self.nodes[name]["parameters"]["conditions"]["conditions"][0]["leftValue"]
        return tuple(self.run_cases([{"kind": "expr", "expr": cond(TO_WRITE), "json": item},
                                     {"kind": "expr", "expr": cond(TO_EMAIL), "json": item}]))

    @staticmethod
    def ok(write):
        data = {}
        for part in ("add", "remove"):
            if part in write["variables"]:
                data[part] = {"node": {"id": write["id"]}, "userErrors": []}
        return {"data": data, "extensions": {}}


class KitRetaggerShapeTests(KitRetaggerBase):
    def test_inactive_no_saved_executions_and_free_of_live_identity(self):
        wf = self.workflow
        self.assertEqual(wf["name"], "XB Kit Re-tagger")
        self.assertIs(wf["active"], False)
        self.assertIsNone(wf["staticData"])
        self.assertNotIn("pinData", wf)
        self.assertEqual(wf["settings"], {
            "executionOrder": "v1", "callerPolicy": "workflowsFromSameOwner", "timezone": "Asia/Singapore",
            "saveDataErrorExecution": "none", "saveDataSuccessExecution": "none", "saveManualExecutions": False,
            "saveExecutionProgress": False})
        for node in wf["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        for forbidden in ("myshopify.com", "x-trekkers", "$env", "process.env", "this.helpers", "require(", "fetch(",
                          "@", "shpat_", "shpss_", "client_secret", "client_id"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_node_set_and_exact_graph(self):
        working = {n["name"]: n["type"] for n in self.workflow["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"}
        self.assertEqual(working, NODE_TYPES)
        notes = {n["name"] for n in self.workflow["nodes"] if n["type"] == "n8n-nodes-base.stickyNote"}
        self.assertEqual(notes, STICKIES)
        edges = {(source, index, link["node"])
                 for source, outputs in self.workflow["connections"].items()
                 for index, branch in enumerate(outputs["main"]) for link in branch}
        self.assertEqual(edges, EXPECTED_EDGES)

    def test_schedule_and_settings_have_no_limits(self):
        self.assertEqual(self.nodes["Daily 03:00 SGT"]["parameters"]["rule"]["interval"],
                         [{"field": "days", "daysInterval": 1, "triggerAtHour": 3, "triggerAtMinute": 0}])
        values = {a["name"]: (a["type"], a["value"])
                  for a in self.nodes[SETTINGS]["parameters"]["assignments"]["assignments"]}
        self.assertEqual(values, {"DRY_RUN": ("boolean", True), "API_VERSION": ("string", "2026-10"),
                                  "SHOP_DOMAIN": ("string", "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN")})
        text = WORKFLOW.read_text(encoding="utf-8")
        for gone in ("CHANGE_CAP", "REMOVE_CAP", "CAP_OVERRIDE", "daily_summary", "DRYRUN", "change_cap", "'WARN'",
                     "'INFO'"):
            self.assertNotIn(gone, text)

    def test_only_the_token_request_uses_a_credential(self):
        token = self.nodes[TOKEN]
        p = token["parameters"]
        self.assertEqual((p["method"], p["authentication"], p["genericAuthType"]),
                         ("POST", "genericCredentialType", "httpCustomAuth"))
        self.assertEqual(p["url"], "=https://{{ $json.SHOP_DOMAIN }}/admin/oauth/access_token")
        # The explicit form content type makes n8n merge the credential's `body` values into the form body.
        self.assertEqual(p["headerParameters"]["parameters"],
                         [{"name": "Content-Type", "value": "application/x-www-form-urlencoded"}])
        self.assertEqual((p["contentType"], p["bodyParameters"]["parameters"]),
                         ("form-urlencoded", [{"name": "grant_type", "value": "client_credentials"}]))
        self.assertEqual((token["retryOnFail"], token["maxTries"], token["onError"], token["alwaysOutputData"],
                          token["executeOnce"]), (True, 3, "continueRegularOutput", True, True))
        for name in (READ, WRITE):
            p = self.nodes[name]["parameters"]
            self.assertEqual(p["authentication"], "none", name)
            self.assertEqual(p["headerParameters"]["parameters"],
                             [{"name": "X-Shopify-Access-Token",
                               "value": "={{ $('Get Shopify Access Token').first().json.access_token }}"}], name)
            self.assertEqual(p["url"], "=https://{{ $('Run Settings').first().json.SHOP_DOMAIN }}/admin/api/"
                                       "{{ $('Run Settings').first().json.API_VERSION }}/graphql.json", name)
        for name in (TOKEN, READ, WRITE):
            self.assertEqual(self.nodes[name]["parameters"]["options"]["redirect"],
                             {"redirect": {"followRedirects": False}}, name)
            self.assertEqual(self.nodes[name]["typeVersion"], 4.2, name)

    def test_read_is_paged_limited_and_slow(self):
        node = self.nodes[READ]
        body = json.loads(node["parameters"]["jsonBody"])
        self.assertEqual(body["variables"], {"after": None})
        self.assertIn('productsCount(query: "status:active") { count precision }', body["query"])
        self.assertIn('products(first: 100, after: $after, query: "status:active", sortKey: ID)', body["query"])
        self.assertIn("nodes { id handle title vendor productType status tags }", body["query"])
        self.assertNotIn("mutation", body["query"])
        pag = node["parameters"]["options"]["pagination"]["pagination"]
        self.assertEqual((pag["paginationMode"], pag["paginationCompleteWhen"], pag["limitPagesFetched"],
                          pag["maxRequests"], pag["requestInterval"]),
                         ("updateAParameterInEachRequest", "other", True, 40, 1000))
        (param,) = pag["parameters"]["parameters"]
        self.assertEqual((param["type"], param["name"]), ("body", "variables"))
        self.assertEqual((node["onError"], node["alwaysOutputData"], node["executeOnce"]),
                         ("continueRegularOutput", True, True))

    def test_pagination_expressions(self):
        pag = self.nodes[READ]["parameters"]["options"]["pagination"]["pagination"]
        cursor = pag["parameters"]["parameters"][0]["value"]
        done = pag["completeExpression"]
        page = lambda more, cursor_value: {"body": {"data": {"products": {"pageInfo": {
            "hasNextPage": more, "endCursor": cursor_value}}}}}
        cases = [
            {"kind": "expr", "expr": cursor, "response": {}},  # first request: $response is still empty
            {"kind": "expr", "expr": cursor, "response": page(True, "fake-cursor-7")},
            {"kind": "expr", "expr": done, "response": page(True, "fake-cursor-7")},
            {"kind": "expr", "expr": done, "response": page(False, None)},
            {"kind": "expr", "expr": done, "response": {"body": {"errors": [{"extensions": {"code": "THROTTLED"}}]}}},
        ]
        self.assertEqual(self.run_cases(cases), [{"after": None}, {"after": "fake-cursor-7"}, False, True, True])

    def test_writes_are_batched_retried_and_built_from_the_plan_only(self):
        node = self.nodes[WRITE]
        p = node["parameters"]
        self.assertEqual(p["jsonBody"], "={{ JSON.stringify({ query: $json.query, variables: $json.variables }) }}")
        self.assertEqual(p["options"]["batching"], {"batch": {"batchSize": 1, "batchInterval": 500}})
        self.assertEqual((node["retryOnFail"], node["maxTries"], node["onError"], node["alwaysOutputData"]),
                         (True, 3, "continueRegularOutput", True))
        self.assertNotIn("executeOnce", node)
        self.assertEqual(self.nodes["List Writes"]["parameters"],
                         {"fieldToSplitOut": "writes", "include": "noOtherFields", "options": {}})

    def test_only_tag_mutations_exist_in_the_export(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"\bmutation\b", text)), 3)
        fields = set(re.findall(r"(?:add|remove): (\w+)\(id: \$id", text))
        self.assertEqual(fields, {"tagsAdd", "tagsRemove"})
        for word in ("productUpdate", "productDelete", "productSet", "productCreate", "metafieldsSet", "collection"):
            self.assertNotIn(word, text)

    def test_alert_call_pins_the_system_and_every_email_ends_the_run_as_failed(self):
        node = self.nodes[SEND]
        p = node["parameters"]
        self.assertEqual(p["workflowId"]["value"], "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID")
        self.assertEqual(p["workflowInputs"]["value"], {
            "system": SYSTEM, "severity": "={{ $json.alert.severity }}", "key": "={{ $json.alert.key }}",
            "short": "={{ $json.alert.short }}", "lines": "={{ $json.alert.lines }}"})
        self.assertEqual([s["id"] for s in p["workflowInputs"]["schema"]], ["system", "severity", "key", "short", "lines"])
        self.assertEqual((p["mode"], node["onError"]), ("each", "continueRegularOutput"))
        self.assertNotIn("retryOnFail", node)
        stop = self.nodes[STOP]
        self.assertEqual((stop["typeVersion"], stop["parameters"]["errorType"]), (1, "errorMessage"))
        self.assertIn("KIT-RETAG", stop["parameters"]["errorMessage"])
        self.assertNotIn("onError", stop)
        # The sender is the only way to the stop node, and every email goes through it.
        into_stop = [s for s, outs in self.workflow["connections"].items() for branch in outs["main"]
                     for link in branch if link["node"] == STOP]
        self.assertEqual(into_stop, [SEND])

    def test_code_nodes_use_their_error_output_and_static_data_only(self):
        for name in (PLAN, CHECK):
            node = self.nodes[name]
            self.assertEqual((node["parameters"]["mode"], node["onError"]), ("runOnceForAllItems", "continueErrorOutput"))
            self.assertNotIn("alwaysOutputData", node)
            code = node["parameters"]["jsCode"]
            self.assertIn("$getWorkflowStaticData('global')", code)
            self.assertIn("const RECORD_KEY = 'kitTagRecord';", code)

    def test_notes_readme_runbook_and_ci(self):
        notes = "\n".join(n["parameters"]["content"] for n in self.workflow["nodes"]
                          if n["type"] == "n8n-nodes-base.stickyNote")
        for word in ("Required manual configuration", "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN",
                     "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "DRY_RUN", "KIT-RETAG", "kit-no", "kit-pin-<kit>",
                     "03:00 SGT", "docs/xb_kit_retagger.md", "kitTagRecord", "Hands-off", "End Run As Failed"):
            self.assertIn(word, notes)
        for gone in ("CHANGE_CAP", "REMOVE_CAP", "CAP_OVERRIDE", "DRYRUN", "CHANGED", "Retried up to 3 times"):
            self.assertNotIn(gone, notes)
        # G2 L-6 and L-7: what n8n really retries, and what the stops are for.
        for word in ("first write fails", "re-runs every write", "put right the next night", "store-wide anomalies",
                     "one vendor or type rename usually stays under them", "Outside changes"):
            self.assertIn(word, notes)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("### xb_kit_retagger.workflow.json", readme)
        # G2 T-2: the daily health line describes what #340 shipped.
        self.assertIn("- Daily health (#340, on main): `XB Daily Health` counts the `KIT-RETAG` `ERROR` rows of "
                      "`xb_alert_email_log` (`kit_retag_errors` WARN) and has a `bind_later` `WATCH` entry", readme)
        self.assertNotIn("Not yet watched by `XB Daily Health`", readme)
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for word in ("Custom Auth", "httpCustomAuth", "client_id", "client_secret", "L0", "L4", "L6", "L8",
                     "OK kit re-tagger dry run", "Another tagger", "open n8n editor", "kitTagRecord", "#340", "bind_later",
                     "OK bind kit re-tagger in daily health",
                     "stopped_outside_changes", "store-wide anomalies", "one vendor or type rename usually stays under"):
            self.assertIn(word, runbook)
        for gone in ("CHANGE_CAP", "REMOVE_CAP", "CAP_OVERRIDE", "DRYRUN", "includeData", "/api/v1/executions"):
            self.assertNotIn(gone, runbook)
        # Controller ruling, 21 Nov 2026: a dry run is read in the editor, never from a saved execution.
        self.assertIn('"Save manual executions" is never switched on', runbook)
        # The owner's part of the runbook is plain English.
        owner = runbook.split("## For the owner", 1)[1].split("\n## ", 1)[0]
        self.assertIsNone(JARGON.search(owner), owner)
        ci = CI.read_text(encoding="utf-8")
        for path in ("n8n-workflows/xb_kit_retagger.workflow.json", "tests/test_xb_kit_retagger_workflow.py",
                     "tests/fixtures/xb_kit_rules_v1.fixture", "docs/xb_kit_retagger.md",
                     "n8n-workflows/ops_alert_email.workflow.json", ".github/workflows/xb-kit-retagger-tests.yml"):
            self.assertIn('- "%s"' % path, ci)
        self.assertNotIn("**", ci)
        self.assertIn("python -m unittest tests.test_xb_kit_retagger_workflow -v", ci)


class KitRetaggerRunbookTests(KitRetaggerBase):
    """G2 L-1, L-2, L-4, L-5 and G0 Q5: the live steps, re-imports and rules changes, pinned in the runbook."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")

    def section(self, title):
        return self.runbook.split("## " + title, 1)[1].split("\n## ", 1)[0]

    def rows(self):
        rows = {}
        for line in self.section("Live path").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if re.fullmatch(r"L[0-9]", cells[0]):
                rows[cells[0]] = cells
        return rows

    def test_every_live_step_has_a_rollback(self):
        self.assertIn("| Step | Who | What | Owner OK words | Rollback |", self.section("Live path"))
        rows = self.rows()
        self.assertEqual(set(rows), {"L%d" % i for i in range(10)})
        for step, cells in rows.items():
            self.assertEqual(len(cells), 5, step)
            self.assertTrue(all(cells), step)
        self.assertIn("Uninstall the app", rows["L0"][4])
        self.assertIn("Delete the credential", rows["L1"][4])
        self.assertIn("Archive the imported workflow", rows["L3"][4])
        self.assertIn("corrected run or by hand", rows["L6"][4])
        self.assertIn("Deactivate", rows["L8"][4])

    def test_the_sender_update_runs_after_the_merge_by_the_house_procedure(self):
        order = list(self.rows())
        self.assertLess(order.index("L7"), order.index("L2"))
        self.assertLess(order.index("L2"), order.index("L8"))
        l2 = self.rows()["L2"]
        for word in ("After L7, before L8", "**merged** export", "docs/daily_health_check.md", "node by node",
                     "stop on any drift", "recipient, table ID, Gmail credential binding", "read back"):
            self.assertIn(word, l2[2])
        self.assertIn("Re-apply the backup", l2[4])

    def test_every_re_import_keeps_the_record(self):
        steps = self.section("Re-importing this workflow (keeps the record)")
        for word in ("including its `staticData`", "Re-applies the backed-up `staticData`", "kitTagRecord",
                     "same product count", "Any difference stops the step", "127.0.0.1:5678/api/v1"):
            self.assertIn(word, steps)
        self.assertIn("back up `staticData` first", self.rows()["L8"][2])
        self.assertIn("re-apply the backup (nodes, settings, `staticData`) and read back", self.rows()["L8"][4])

    def test_a_403_on_re_import_is_rolled_back_without_publishing(self):
        # G2 N-1 (checked at n8n@2.48.5): on a 403 the draft, settings and staticData are already saved.
        steps = self.section("Re-importing this workflow (keeps the record)")
        for word in ("`workflow:activate` **and** the caller's `workflow:publish` permission",
                     "**already saved** the new nodes as an unpublished draft and written the sent settings and "
                     "`staticData`",
                     "`PUT /workflows/{id}?publishIfActive=false` with the backup's nodes, connections, settings and "
                     "`staticData`",
                     "It needs only `workflow:update` and publishes nothing",
                     "Read back that `activeVersionId` is unchanged",
                     "not from `activeVersion` (settings and `staticData` are not versioned)",
                     "re-publishing is the Controller's call", "`n8n@2.48.5`"):
            self.assertIn(word, steps)
        self.assertNotIn("Rollback: re-apply the whole backup", steps)
        self.assertNotIn("stop and report it.", steps)
        rules = self.section("Rules")
        self.assertIn("re-import procedure above (record kept)", rules)

    def test_re_import_wording_is_pinned(self):
        # G2 K-0 (#344 LATER): the N-1 sentences the first round left unpinned, plus the optional ones.
        steps = self.section("Re-importing this workflow (keeps the record)")
        for word in (
            "(`publishIfActive` defaults to `true`)",
            "on an active workflow `activeVersionId` changed and `activeVersion` carries the new nodes",
            "that the settings and the `kitTagRecord` product count equal the backup's",
            "**After a successful re-import that must be undone:**",
            "the same PUT without `publishIfActive=false`",
            "so the backup's nodes are published again",
            "Not near the 03:00 SGT run",
            "On an inactive workflow (L8 before activation) nothing is published",
            "`UpdateWorkflowQueryDto` (`publishIfActive` default `true`)",
        ):
            self.assertIn(word, steps)

    def test_re_import_later_wording(self):
        # G2 L-A..L-D (#344 LATER), checked at n8n@2.48.5.
        steps = self.section("Re-importing this workflow (keeps the record)")
        for word in (
            # L-A: workflow:publish is checked on the workflow (a share counts), not only on the project.
            "the caller's `workflow:publish` permission on this workflow",
            "a project role or a share of the workflow both count",
            # L-B: after the 403 rollback versionId differs from activeVersionId, as expected.
            "Afterwards `versionId` differs from `activeVersionId`",
            "That is expected, not an unknown draft",
            # L-C: default publication path in step 4, and case 3's example is the old path.
            "`activeVersionId` changes as soon as the publication is queued and the triggers are registered a moment "
            "later",
            "a failed registration does not unpublish the workflow",
            "with `N8N_USE_WORKFLOW_PUBLICATION_SERVICE=false`, a failed re-publication can leave the workflow "
            "unpublished",
            # L-D: the undo PUT must not roll back a record saved by a run since the re-import.
            "first `GET` the workflow and note the current `kitTagRecord` product count",
            "**without `staticData`** (a field left out is not written, so the current record is kept)",
            "Send the backup's `staticData` only when the record itself must be undone",
            "the record count equals the one noted just before the PUT",
            "workflow-entity-mapper.ts",
            "publication/trigger-diff.ts",
        ):
            self.assertIn(word, steps)
        self.assertNotIn("on the project.", steps)
        self.assertNotIn("(for example a failed re-publication left the workflow unpublished)", steps)
        self.assertNotIn("the settings and the record count equal the backup's", steps)

    def test_runbook_status_and_bound_health_entry(self):
        # Part A is live (L0-L9 done, 21 Nov 2026); the bound WATCH entry keeps bind_later, which is then inert.
        self.assertIn("**Status:** live since 21 Nov 2026. Steps L0-L9 are done", self.runbook)
        health = self.section("Daily health (added by PR #340, now on main)")
        self.assertIn("the `bind_later` flag can stay: it skips the entry only while the id still starts with "
                      "`REPLACE_WITH_`", health)

    def test_a_rules_change_is_dry_run_first_and_never_brakes_on_the_owner(self):
        rules = self.section("Rules")
        for word in ("never gets an anomaly email for a planned change", "One editor dry run with the merged rules",
                     "reviewed one-run PR"):
            self.assertIn(word, rules)

    def test_another_tagger_is_a_note_not_a_gate(self):
        l5 = self.rows()["L5"]
        self.assertIn("Note (not a gate)", l5[2])
        self.assertIn("never routed to the owner", l5[2])
        self.assertNotIn("gated on L5", self.runbook)
        self.assertNotIn("Before the first real run, confirm with staff", self.runbook)


class KitRetaggerRulesTests(KitRetaggerBase):
    def pasted_rules(self):
        code = self.nodes[PLAN]["parameters"]["jsCode"]
        match = re.search(r"^const RULES = (\{.*\});$", code, re.M)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_pasted_rules_equal_the_fixture(self):
        self.assertEqual(self.pasted_rules(), self.rules)

    def test_fixture_is_the_logic_subset_of_rules_v1(self):
        rules = self.rules
        self.assertEqual(set(rules), {"slots", "profiles", "exclude"})
        self.assertEqual(len(rules["slots"]), 22)
        self.assertEqual(tuple(rules["profiles"]), KITS)
        self.assertEqual(set(rules["exclude"]), {"title_words", "types"})
        self.assertEqual(rules["exclude"]["types"], ["Underwear", "Bra"])
        self.assertEqual(len(rules["exclude"]["title_words"]), 19)
        for name, slot in rules["slots"].items():
            self.assertEqual(set(slot), {"any"}, name)
            for clause in slot["any"]:
                self.assertTrue(set(clause) <= {"vendor", "type", "not_type", "tag", "not_tag", "title"}, name)
        for name, profile in rules["profiles"].items():
            self.assertEqual(set(profile), {"slots"}, name)
            self.assertTrue(set(profile["slots"]) <= set(rules["slots"]), name)
        used = {s for p in rules["profiles"].values() for s in p["slots"]}
        self.assertEqual(used, set(rules["slots"]))
        self.assertEqual([len(p["slots"]) for p in rules["profiles"].values()], [9, 11, 13, 11, 9, 8, 5, 6])
        managed = {"kit-" + k for k in rules["profiles"]} | {"ks-" + s for s in rules["slots"]}
        self.assertEqual(len(managed), 30)
        text = RULES_FIXTURE.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        for display_field in ("_about", "label", "xt_url", "pitch", "http"):
            self.assertNotIn(display_field, text)


class KitRetaggerDiffTests(KitRetaggerBase):
    def changes(self, products):
        plan = self.plan_products(products, DRY_RUN=False)
        self.assertIn(plan["mode"], ("write", "no_change"), plan)
        return {w["handle"]: (sorted(w["variables"].get("add", [])), sorted(w["variables"].get("remove", [])))
                for w in plan["writes"]}

    def test_each_clause_type(self):
        products = [
            product(1, title="Fake Solar Panel", vendor="BioLite", ptype="Power Bank"),            # vendor
            socks(2, vendor="Smartwool"),                                                           # type; not_type blocks merino
            product(3, title="Fake Bottle", ptype="Bottle", tags=["Hydration"]),                   # type or tag
            product(4, title="Fake Water Shoe", ptype="Shoes", tags=["Sandals"]),                  # not_tag blocks footwear
            product(5, title="Fake Superior Down Vest", ptype="Vest", tags=["Down"]),              # tag + type, tag + title
            product(6, title="Fake Rope", ptype="Rope", tags=["Climbing"]),                        # tag only
            product(7, title="Fake Multi-tool", vendor="Leatherman", ptype="Tools", tags=["lm-multi-tool"]),
            product(8, title="Fake Knife", vendor="Leatherman", ptype="Tools"),                    # vendor without the tag
            product(9, title="Fake Shell Jacket", vendor="Fake Vendor", ptype="Jacket", tags=["GORE-TEX"]),
        ]
        got = self.changes(products)
        k = lambda *kits: ["kit-" + x for x in kits]
        self.assertEqual(got["fake-product-001"], (sorted(k("himalaya", "expedition", "winter") + ["ks-power"]), []))
        self.assertEqual(got["fake-product-002"], (SOCK_TAGS, []))
        self.assertEqual(got["fake-product-003"],
                         (sorted(k("tropical", "hut", "himalaya", "island", "ride") + ["ks-hydration"]), []))
        self.assertEqual(got["fake-product-004"], (sorted(k("island") + ["ks-water"]), []))
        self.assertEqual(got["fake-product-005"],
                         (sorted(k("hut", "himalaya", "expedition", "winter") + ["ks-insulation", "ks-light_insul"]), []))
        self.assertEqual(got["fake-product-006"], (sorted(k("expedition") + ["ks-alpine"]), []))
        self.assertEqual(got["fake-product-007"],
                         (sorted(k("tropical", "hut", "himalaya", "expedition", "ride") + ["ks-tool_trail"]), []))
        self.assertNotIn("fake-product-008", got)
        self.assertEqual(got["fake-product-009"], (sorted(k(*KITS[:6]) + k("ride") + ["ks-shell"]), []))

    def test_exclusions_kit_no_and_kit_pin(self):
        products = [
            socks(1, title="Fake Kid's Trail Socks", tags=["kit-hut", "ks-socks"]),          # kid's: removed
            socks(2, title="Fake Kids' Socks"),
            socks(3, title="Fake Kidston Trail Socks"),                                      # not a whole word
            product(4, title="Fake Merino Brief", vendor="Icebreaker", ptype="Briefs"),      # brief
            product(5, title="Fake Merino Top", vendor="Icebreaker", ptype="Underwear"),     # excluded type
            product(6, title="Fake Solar Panel", vendor="BioLite", ptype="Power Bank",
                    tags=["kit-no", "kit-winter", "ks-power", "kit-pin-ride"]),              # kit-no wins
            product(7, title="Fake Gift Card", ptype="Gift Card", tags=["kit-pin-island", "Kit-Pin-Ride"]),
        ]
        got = self.changes(products)
        self.assertEqual(got["fake-product-001"], ([], ["kit-hut", "ks-socks"]))
        self.assertNotIn("fake-product-002", got)
        self.assertEqual(got["fake-product-003"], (SOCK_TAGS, []))
        self.assertNotIn("fake-product-004", got)
        self.assertNotIn("fake-product-005", got)
        self.assertEqual(got["fake-product-006"], ([], ["kit-winter", "ks-power"]))
        self.assertEqual(got["fake-product-007"], (["kit-island", "kit-ride"], []))
        for add, remove in got.values():
            for tag in add + remove:
                self.assertNotIn(tag.lower(), ("kit-no",))
                self.assertFalse(tag.lower().startswith("kit-pin-"), tag)

    def test_unmanaged_unknown_and_mixed_case_tags(self):
        products = [
            product(1, title="Fake Solar Panel", vendor="BioLite", ptype="Power Bank",
                    tags=["Sale", "KIT-Expedition", "Ks-Power", "kit-himalaya", "kit-winter", "kit-bundle", "ks-legacy",
                          "Kit-Ride"]),
            product(2, title="Fake Mug", ptype="Mug", tags=["kit-bundle", "ks-legacy", "Gift"]),
        ]
        plan = self.plan_products(products, DRY_RUN=False)
        (write,) = plan["writes"]
        self.assertEqual(write["handle"], "fake-product-001")
        # Mixed-case kit tags already present are not re-added; only the wrong kit is removed, in its own case.
        self.assertEqual(write["variables"], {"id": products[0]["id"], "remove": ["Kit-Ride"]})
        # The dry run output names the unknown kit-/ks- tags it leaves alone.
        dry = self.plan_products(products)
        self.assertEqual(dry["summary"]["unknown_tags_left_alone"], ["kit-bundle", "ks-legacy"])

    def test_a_second_pass_on_the_result_changes_nothing(self):
        products = [socks(1, tags=["Sale", "kit-ride"]), product(2, title="Fake Bottle", ptype="Bottle"),
                    product(3, title="Fake Gift Card", ptype="Gift Card", tags=["kit-pin-hut"]),
                    product(4, title="Fake Kid's Socks", ptype="Socks", tags=["ks-socks", "kit-hut"])]
        plan = self.plan_products(products, DRY_RUN=False)
        self.assertEqual(plan["mode"], "write")
        by_id = {p["id"]: p for p in products}
        for write in plan["writes"]:
            p = by_id[write["id"]]
            removed = set(write["variables"].get("remove", []))
            p["tags"] = [t for t in p["tags"] if t not in removed] + write["variables"].get("add", [])
        again = self.plan_products(products, DRY_RUN=False)
        self.assertEqual((again["mode"], again["writes"], again["alert"]), ("no_change", [], None))


class KitRetaggerQuietTests(KitRetaggerBase):
    """Hands-off (owner, 21 Nov 2026): no email on a dry run, a quiet day or a good run; no add limit."""

    def test_dry_run_is_the_default_writes_nothing_and_sends_nothing(self):
        products = [socks(i) for i in range(3)] + [mug(9)]
        before = record({num(500): {"d": "2026-11-01", "t": {"kit-hut": "a"}}})  # out of scope, kept as it is
        plan, after = self.plan_full(pages_of(products), static=copy.deepcopy(before))
        self.assertEqual((plan["mode"], plan["writes"], plan["alert"]), ("dry_run", [], None))
        self.assertEqual(after, before)
        self.assertEqual(plan["plan"][0], {"handle": "fake-product-000", "add": SOCK_TAGS, "remove": []})
        self.assertEqual(plan["plan"][-1], {"handle": "fake-product-009", "add": [], "remove": ["kit-hut"]})
        summary = plan["summary"]
        self.assertEqual((summary["scanned"], summary["changed"], summary["losing"], summary["braked"]), (4, 4, 1, False))
        self.assertEqual(self.route(plan), (False, False))
        # Anything but an explicit false is a dry run.
        for value in (True, "false", 0, None):
            self.assertEqual(self.plan_products(products, DRY_RUN=value)["mode"], "dry_run", value)
        quiet = self.plan_products([product(1, title="Fake Mug", ptype="Mug")])
        self.assertEqual((quiet["mode"], quiet["alert"], quiet["plan"]), ("dry_run", None, []))

    def test_quiet_day_sends_nothing(self):
        plan = self.plan_products([product(1, title="Fake Mug", ptype="Mug")], DRY_RUN=False)
        self.assertEqual((plan["mode"], plan["writes"], plan["alert"]), ("no_change", [], None))
        self.assertEqual(self.route(plan), (False, False))

    def test_adds_have_no_limit(self):
        plan = self.plan_products([socks(i) for i in range(300)], DRY_RUN=False)
        self.assertEqual((plan["mode"], len(plan["writes"]), plan["alert"]), ("write", 300, None))
        self.assertEqual(self.route(plan), (True, False))

    def test_a_good_run_sends_nothing(self):
        writes = self.plan_products([socks(i, tags=["kit-ride"]) for i in range(3)], DRY_RUN=False)["writes"]
        out = self.check(writes, [self.ok(w) for w in writes])
        self.assertEqual((out["mode"], out["changed"], out["alert"]), ("done", 3, None))
        self.assertEqual(self.route(out), (False, False))


class KitRetaggerBrakeTests(KitRetaggerBase):
    def losing(self, steady, losing, untagged=0, **plan_args):
        """`steady` tagged products that keep their tags, `losing` products that would each lose two known tags,
        and `untagged` products with no known tag (they never count towards the brake)."""
        products = ([socks(i, tags=SOCK_TAGS) for i in range(steady)]
                    + [mug(1000 + i, tags=("kit-hut", "ks-socks")) for i in range(losing)]
                    + [product(3000 + i, title="Fake Plain Mug %d" % i, ptype="Mug") for i in range(untagged)])
        plan_args.setdefault("DRY_RUN", False)
        return self.plan_full(pages_of(products), **plan_args)

    def test_just_under_at_and_over_the_brake(self):
        # 200 steady + k losing (+ 300 untagged): the brake is 10% of the products carrying a known tag,
        # rounded up, and counts products, not tags (each loser loses two tags).
        under, _ = self.losing(200, 22, untagged=300)   # 22 of 222 tagged, brake at 23
        at, _ = self.losing(200, 23, untagged=300)      # 23 of 223, brake at 23: still allowed
        over, _ = self.losing(200, 24, untagged=300)    # 24 of 224, brake at 23 (not 53 of all 524)
        self.assertEqual((under["mode"], len(under["writes"])), ("write", 22))
        self.assertEqual((at["mode"], len(at["writes"])), ("write", 23))
        self.assertEqual(at["summary"]["brake_at"], 23)
        self.assertEqual((over["mode"], over["writes"]), ("braked", []))
        self.assertEqual((over["summary"]["tagged"], over["summary"]["losing"], over["summary"]["tags_to_remove"]),
                         (224, 24, 48))

    def test_never_below_ten_products(self):
        ten, _ = self.losing(0, 10)
        eleven, _ = self.losing(0, 11)
        self.assertEqual((ten["mode"], len(ten["writes"]), ten["summary"]["brake_at"]), ("write", 10, 10))
        self.assertEqual(eleven["mode"], "braked")

    def test_a_brake_changes_nothing_sends_one_error_and_fails_the_run(self):
        before = record({num(1000): {"d": "2026-11-20", "t": {"kit-ride": "a"}},
                         num(999999): {"d": "2026-11-01", "t": {"kit-hut": "r"}}})
        plan, after = self.losing(0, 30, static=copy.deepcopy(before))
        self.assertEqual((plan["mode"], plan["writes"]), ("braked", []))
        self.assertEqual(after, before)  # the record is not pruned or touched either
        alert = plan["alert"]
        self.assertEqual((alert["key"], alert["lines"][0]), ("stopped_unusual_removals", BRAKE_LINE))
        self.assertEqual(alert["lines"][1], "Detail for Claude: 30 of 30 tagged products would lose kit tags; run it by "
                                            "hand with DRY_RUN on and read the plan.")
        self.assertNotIn("10", " ".join(alert["lines"]))  # the internal limit is never shown
        self.assert_owner_alert(alert)
        # No writes, to the error email, which leads only to End Run As Failed.
        self.assertEqual(self.route(plan), (False, True))
        self.assertEqual(self.workflow["connections"][SEND]["main"], [[{"node": STOP, "type": "main", "index": 0}]])

    def test_adds_never_brake_and_a_dry_run_shows_the_brake_without_email(self):
        many_adds = self.plan_products([socks(i) for i in range(500)], DRY_RUN=False)
        self.assertEqual(many_adds["mode"], "write")
        dry, _ = self.losing(0, 30, DRY_RUN=True)
        self.assertEqual((dry["mode"], dry["alert"], dry["summary"]["braked"]), ("dry_run", None, True))


class KitRetaggerOutsideChangesTests(KitRetaggerBase):
    """L-3: many tags this workflow set, undone outside it since the last run, stop the night (one ERROR)."""

    def undone(self, steady, k, kind="removed", extra=(), untagged=0, static=None, **plan_args):
        """`steady` tagged products, `k` products where someone undid this workflow's kit-hut change, plus
        `extra` products and `untagged` products with no known tag."""
        if kind == "removed":   # it added kit-hut, someone took it off
            changed = [socks(500 + i, tags=[t for t in SOCK_TAGS if t != "kit-hut"]) for i in range(k)]
            did = "a"
        else:                   # it removed kit-hut, someone put it back
            changed = [mug(500 + i, tags=("kit-hut",)) for i in range(k)]
            did = "r"
        products = ([socks(i, tags=SOCK_TAGS) for i in range(steady)] + changed + list(extra)
                    + [product(3000 + i, title="Fake Plain Mug %d" % i, ptype="Mug") for i in range(untagged)])
        if static is None:
            static = record({num(500 + i): {"d": "2026-11-20", "t": {"kit-hut": did}} for i in range(k)})
        plan_args.setdefault("DRY_RUN", False)
        before = copy.deepcopy(static)
        plan, after = self.plan_full(pages_of(products), static=static, **plan_args)
        return plan, after, before

    def test_just_under_at_and_over(self):
        for kind in ("removed", "added"):
            with self.subTest(kind=kind):
                # 200 steady + k undone (+ 300 untagged): the stop is 10% of the products carrying a known tag.
                under, _, _ = self.undone(200, 22, kind, untagged=300)
                at, _, _ = self.undone(200, 23, kind, untagged=300)
                over, after, before = self.undone(200, 24, kind, untagged=300, extra=[mug(900)])
                self.assertEqual((under["mode"], under["summary"]["held"]), ("no_change", False))
                self.assertEqual((at["mode"], at["summary"]["held"], at["summary"]["brake_at"]),
                                 ("no_change", False, 23))
                self.assertEqual((over["mode"], over["writes"], over["summary"]["held"]), ("held", [], True))
                self.assertEqual(over["summary"]["new_person_" + kind], 24)
                self.assertEqual(after, before)  # record untouched
                alert = over["alert"]
                self.assertEqual((alert["key"], alert["lines"][0]), ("stopped_outside_changes", OUTSIDE_LINE))
                self.assertNotIn("10", " ".join(alert["lines"]))
                self.assert_owner_alert(alert)
                self.assertEqual(self.route(over), (False, True))

    def test_never_below_ten_products_and_counts_products(self):
        ten, _, _ = self.undone(0, 10, "added")
        eleven, _, _ = self.undone(0, 11, "added")
        self.assertEqual((ten["mode"], ten["summary"]["brake_at"]), ("no_change", 10))
        self.assertEqual(eleven["mode"], "held")
        # Two undone tags on one product count once.
        start = record({num(500 + i): {"d": "2026-11-20", "t": {"kit-hut": "r", "ks-socks": "r"}} for i in range(11)})
        products = [mug(500 + i, tags=("kit-hut", "ks-socks")) for i in range(11)]
        plan, _ = self.plan_full(pages_of(products), static=start, DRY_RUN=False)
        self.assertEqual((plan["mode"], plan["summary"]["new_person_added"], plan["summary"]["brake_at"]),
                         ("held", 11, 10))
        start = record({num(500 + i): {"d": "2026-11-20", "t": {"kit-hut": "r", "ks-socks": "r"}} for i in range(10)})
        plan, _ = self.plan_full(pages_of(products[:10]), static=start, DRY_RUN=False)
        self.assertEqual((plan["mode"], plan["summary"]["new_person_added"]), ("no_change", 10))

    def test_taken_off_tags_count_products_not_tags(self):
        # Two taken-off tags on one product count once (G2 T-1, L3-07).
        def night(k):
            start = record({num(500 + i): {"d": "2026-11-20", "t": {"kit-hut": "a", "kit-tropical": "a"}}
                            for i in range(k)})
            products = [socks(500 + i, tags=[t for t in SOCK_TAGS if t not in ("kit-hut", "kit-tropical")])
                        for i in range(k)]
            plan, _ = self.plan_full(pages_of(products), static=start, DRY_RUN=False)
            return plan
        ten, eleven = night(10), night(11)
        self.assertEqual((ten["mode"], ten["summary"]["new_person_removed"], ten["summary"]["brake_at"]),
                         ("no_change", 10, 10))
        self.assertEqual((eleven["mode"], eleven["summary"]["new_person_removed"]), ("held", 11))

    def test_its_own_changes_never_count(self):
        # The night after a big write (G2 T-1, L3-16): 50 products keep the tags it added ('a', present) and
        # 50 stay without the tags it removed ('r', absent). Nothing is an outside change.
        start = record({**{num(i): {"d": "2026-11-20", "t": {t: "a" for t in SOCK_TAGS}} for i in range(50)},
                        **{num(100 + i): {"d": "2026-11-20", "t": {"kit-hut": "r", "ks-socks": "r"}}
                           for i in range(50)}})
        products = ([socks(i, tags=SOCK_TAGS) for i in range(50)]
                    + [product(100 + i, title="Fake Plain Mug %d" % i, ptype="Mug") for i in range(50)])
        before = copy.deepcopy(start)
        plan, after = self.plan_full(pages_of(products), static=start, DRY_RUN=False)
        summary = plan["summary"]
        self.assertEqual((plan["mode"], plan["alert"], summary["held"], summary["braked"]),
                         ("no_change", None, False, False))
        self.assertEqual((summary["new_person_removed"], summary["new_person_added"],
                          summary["left_alone_person_removed"], summary["left_alone_person_added"]), (0, 0, 0, 0))
        self.assertEqual(summary["brake_at"], 10)
        self.assertEqual(after, before)  # nothing marked as seen

    def test_a_tag_no_longer_wanted_and_taken_off_is_not_an_outside_change(self):
        # G2 K-0 (T1-c): it added kit-hut to these 11 products; after a rules change they no longer want it, and a
        # person took it off. Nothing is left to do, so nothing counts as an outside change (11 would hold the run).
        start = record({num(600 + i): {"d": "2026-11-20", "t": {"kit-hut": "a"}} for i in range(11)})
        products = [product(600 + i, title="Fake Plain Mug %d" % i, ptype="Mug") for i in range(11)]
        before = copy.deepcopy(start)
        plan, after = self.plan_full(pages_of(products), static=start, DRY_RUN=False)
        summary = plan["summary"]
        self.assertEqual((plan["mode"], plan["alert"], summary["held"], summary["braked"]),
                         ("no_change", None, False, False))
        self.assertEqual((summary["new_person_removed"], summary["left_alone_person_removed"]), (0, 0))
        self.assertEqual(after, before)  # nothing marked as seen

    def test_a_dry_run_shows_it_without_email(self):
        dry, after, before = self.undone(0, 30, DRY_RUN=True)
        self.assertEqual((dry["mode"], dry["alert"], dry["summary"]["held"]), ("dry_run", None, True))
        self.assertEqual(after, before)
        self.assertEqual(self.route(dry), (False, False))

    def test_each_outside_change_counts_once(self):
        # Night 1: 23 undone, at the limit: allowed, and marked as seen.
        first, after, _ = self.undone(200, 23)
        self.assertEqual(first["mode"], "no_change")
        self.assertEqual({e["t"]["kit-hut"] for e in after[RECORD]["products"].values()}, {"na"})
        # Night 2: 23 more undone; only the new ones count, so it still runs; nothing is re-added.
        after[RECORD]["products"].update({num(700 + i): {"d": "2026-11-21", "t": {"kit-hut": "a"}} for i in range(23)})
        extra = [socks(700 + i, tags=[t for t in SOCK_TAGS if t != "kit-hut"]) for i in range(23)]
        second, _, _ = self.undone(200, 23, extra=extra, static=after)
        self.assertEqual((second["mode"], second["summary"]["new_person_removed"],
                          second["summary"]["left_alone_person_removed"]), ("no_change", 23, 46))

    def test_the_brake_wins_when_both_fire(self):
        plan, _, _ = self.undone(0, 30, extra=[mug(900 + i, tags=("kit-hut",)) for i in range(30)])
        self.assertEqual((plan["mode"], plan["summary"]["braked"], plan["summary"]["held"]), ("braked", True, True))
        self.assertEqual(plan["alert"]["key"], "stopped_unusual_removals")


class KitRetaggerPeopleTests(KitRetaggerBase):
    """Never fight a person: tags a person put back or took off are left alone, silently."""

    def test_a_tag_a_person_put_back_is_never_removed_again(self):
        mine = record({num(1): {"d": "2026-11-20", "t": {"kit-hut": "r", "ks-socks": "r"}}})
        products = [mug(1, tags=["Kit-Hut", "ks-socks", "kit-ride"])]
        plan, after = self.plan_full(pages_of(products), static=mine, DRY_RUN=False)
        (write,) = plan["writes"]
        self.assertEqual(write["variables"], {"id": products[0]["id"], "remove": ["kit-ride"]})
        # Counted in products, not tags; the run marks it as seen ('nr'), so it is not new again tomorrow.
        summary = plan["summary"]
        self.assertEqual((summary["left_alone_person_added"], summary["new_person_added"], plan["alert"]), (1, 1, None))
        self.assertEqual(after[RECORD]["products"][num(1)], {"d": TODAY, "t": {"kit-hut": "nr", "ks-socks": "nr"}})
        again, after2 = self.plan_full(pages_of(products), static=after, DRY_RUN=False)
        self.assertEqual((again["summary"]["left_alone_person_added"], again["summary"]["new_person_added"]), (1, 0))
        self.assertEqual(again["writes"], plan["writes"])
        self.assertEqual(after2, after)

    def test_a_mixed_case_tag_it_removed_and_a_person_put_back_stays(self):
        products = [mug(1, tags=["Kit-Hut", "Sale"])]
        plan, store = self.plan_full(pages_of(products), DRY_RUN=False)
        (write,) = plan["writes"]
        self.assertEqual(write["variables"], {"id": products[0]["id"], "remove": ["Kit-Hut"]})
        out, store = self.check_full(plan["writes"], [self.ok(write)], static=store)
        self.assertIsNone(out["alert"])
        self.assertEqual(store[RECORD]["products"], {num(1): {"d": TODAY, "t": {"kit-hut": "r"}}})
        products[0]["tags"] = ["Sale", "KIT-HUT"]
        again, _ = self.plan_full(pages_of(products), static=store, DRY_RUN=False)
        self.assertEqual((again["mode"], again["writes"], again["summary"]["left_alone_person_added"]),
                         ("no_change", [], 1))

    def test_a_tag_a_person_took_off_is_never_added_again(self):
        mine = record({num(1): {"d": "2026-11-20", "t": {"kit-hut": "a"}}})
        products = [socks(1, tags=[t for t in SOCK_TAGS if t != "kit-hut"])]
        plan, _ = self.plan_full(pages_of(products), static=mine, DRY_RUN=False)
        self.assertEqual((plan["mode"], plan["writes"], plan["alert"]), ("no_change", [], None))
        self.assertEqual((plan["summary"]["left_alone_person_removed"], plan["summary"]["new_person_removed"]), (1, 1))

    def test_its_own_earlier_changes_do_not_block_later_rule_changes(self):
        # It removed kit-hut earlier and the rules want it now: add it. It added kit-ride and the rules no
        # longer want it, still there: remove it.
        mine = record({num(1): {"d": "2026-11-20", "t": {"kit-hut": "r", "kit-ride": "a"}}})
        products = [socks(1, tags=[t for t in SOCK_TAGS if t != "kit-hut"] + ["kit-ride"])]
        plan, _ = self.plan_full(pages_of(products), static=mine, DRY_RUN=False)
        (write,) = plan["writes"]
        self.assertEqual(write["variables"], {"id": products[0]["id"], "add": ["kit-hut"], "remove": ["kit-ride"]})

    def test_full_cycle_write_record_then_a_person_undoes_it(self):
        products = [socks(1, tags=["kit-ride"])]
        plan, store = self.plan_full(pages_of(products), DRY_RUN=False)
        (write,) = plan["writes"]
        out, store = self.check_full(plan["writes"], [self.ok(write)], static=store)
        self.assertIsNone(out["alert"])
        expected = {t: "a" for t in SOCK_TAGS}
        expected["kit-ride"] = "r"
        self.assertEqual(store[RECORD], {"v": 1, "products": {num(1): {"d": TODAY, "t": expected}}})
        # A person takes kit-hut off and puts kit-ride back: the next run leaves both alone.
        products[0]["tags"] = [t for t in SOCK_TAGS if t != "kit-hut"] + ["kit-ride"]
        again, _ = self.plan_full(pages_of(products), static=store, DRY_RUN=False)
        self.assertEqual((again["mode"], again["writes"], again["alert"]), ("no_change", [], None))

    def test_only_confirmed_parts_are_recorded(self):
        products = [socks(1, tags=["kit-ride"]), socks(2, tags=["kit-ride"])]
        plan, store = self.plan_full(pages_of(products), DRY_RUN=False)
        w1, _ = plan["writes"]
        half = self.ok(w1)
        half["data"]["remove"]["userErrors"] = [{"field": ["tags"], "message": "fake problem"}]
        out, store = self.check_full(plan["writes"], [half, {"error": "502 - Bad Gateway"}], static=store)
        self.assertEqual(out["alert"]["key"], "some_updates_failed")
        self.assertEqual(store[RECORD]["products"], {num(1): {"d": TODAY, "t": {t: "a" for t in SOCK_TAGS}}})


class KitRetaggerRecordTests(KitRetaggerBase):
    def test_empty_or_malformed_record_is_safe(self):
        products = [socks(1, tags=["kit-ride"])]
        for start in ({}, {RECORD: None}, {RECORD: "x"}, {RECORD: {"v": 2, "products": {}}}, {RECORD: {"v": 1}},
                      {RECORD: {"v": 1, "products": []}}):
            with self.subTest(start=start):
                plan, after = self.plan_full(pages_of(products), static=copy.deepcopy(start), DRY_RUN=False)
                self.assertEqual(len(plan["writes"]), 1)
                self.assertEqual(after[RECORD], {"v": 1, "products": {}})
        # Bad entries are dropped one by one; good ones stay and still count.
        mixed = record({num(1): {"d": "2026-11-20", "t": {"kit-ride": "r", "kit-bundle": "r", "kit-hut": "x"}},
                        "not-a-number": {"d": "2026-11-20", "t": {"kit-hut": "a"}},
                        num(2): {"d": "yesterday", "t": {"kit-hut": "a"}},
                        num(3): {"d": "2026-11-20", "t": {}}})
        plan, after = self.plan_full(pages_of(products + [socks(2), socks(3)]), static=mixed, DRY_RUN=False)
        # kit-ride: a person put it back; this run saw it, so it is now 'nr' with today's date.
        self.assertEqual(after[RECORD]["products"], {num(1): {"d": TODAY, "t": {"kit-ride": "nr"}}})
        self.assertEqual([w["handle"] for w in plan["writes"]], ["fake-product-001", "fake-product-002", "fake-product-003"])
        self.assertNotIn("remove", plan["writes"][0]["variables"])  # kit-ride: a person put it back

    def test_record_is_pruned_to_products_in_scope(self):
        mine = record({num(1): {"d": "2026-11-20", "t": {"kit-hut": "a"}},
                       num(77): {"d": "2026-11-20", "t": {"kit-hut": "a"}}})  # archived or deleted since
        _, after = self.plan_full(pages_of([product(1, title="Fake Mug", ptype="Mug")]), static=mine, DRY_RUN=False)
        self.assertEqual(list(after[RECORD]["products"]), [num(1)])

    def test_oversized_record_keeps_the_newest_5000_products(self):
        products = [product(i, title="Fake Mug %d" % i, ptype="Mug") for i in range(5003)]
        entries = {num(i): {"d": "2026-11-20", "t": {"kit-hut": "r"}} for i in range(3, 5003)}
        entries.update({num(i): {"d": "2026-10-0%d" % (i + 1), "t": {"kit-hut": "r"}} for i in range(3)})
        plan, after = self.plan_full(pages_of(products), static=record(entries), DRY_RUN=False)
        self.assertEqual(plan["mode"], "no_change")
        kept = after[RECORD]["products"]
        self.assertEqual(len(kept), 5000)
        self.assertNotIn(num(0), kept)
        self.assertNotIn(num(1), kept)
        self.assertNotIn(num(2), kept)


class KitRetaggerReadFailureTests(KitRetaggerBase):
    def assert_failed(self, pages, key, detail, token=None):
        before = record({num(1): {"d": "2026-11-20", "t": {"kit-hut": "a"}}})
        plan, after = self.plan_full(pages, token=token, static=copy.deepcopy(before), DRY_RUN=False)
        self.assertEqual((plan["mode"], plan["writes"]), ("failed", []))
        self.assertEqual(after, before)
        alert = plan["alert"]
        self.assertEqual(alert["key"], key)
        self.assertIn("Nothing was changed.", alert["lines"][0])
        self.assertEqual(alert["lines"][1], "Detail for Claude: " + detail)
        self.assert_owner_alert(alert)
        self.assertNotIn(FAKE_TOKEN, json.dumps(plan))
        self.assertEqual(self.route(plan), (False, True))

    def test_token_failures(self):
        pages = pages_of([socks(1)])
        self.assert_failed(pages, "could_not_sign_in", "access token request http_401.",
                           token={"error": "401 - {\"error\":\"invalid_client\"}"})
        self.assert_failed(pages, "could_not_sign_in", "access token request request_error.",
                           token={"error": {"message": "connect ETIMEDOUT"}})
        self.assert_failed(pages, "could_not_sign_in", "access token request no_token.", token={"scope": "read_products"})
        self.assert_failed(pages, "could_not_sign_in", "access token request no_token.", token={"access_token": "fake-shrt"})

    def test_page_failures(self):
        good = pages_of([socks(i) for i in range(150)])
        throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}
        cases = {
            "product read http_500 on page 1 of 1.": [{"error": "500 - Internal Server Error"}],
            "product read throttled on page 2 of 2.": [good[0], throttled],
            "product read graphql_errors on page 1 of 1.": [{"errors": [{"message": "Field doesn't exist"}]}],
            "product read no_data on page 2 of 2.": [good[0], {"extensions": {}}],
            "product read no_page.": [],
        }
        for detail, pages in cases.items():
            with self.subTest(detail=detail):
                self.assert_failed(pages, "could_not_read_products", detail)

    def test_incomplete_reads(self):
        products = [socks(i) for i in range(150)]
        good = pages_of(products)
        self.assert_failed(good[:1], "product_list_incomplete",
                           "read stopped at the page limit (1 pages); scanned 100 of 150 active products.")
        self.assert_failed(pages_of(products, count=151), "product_list_incomplete", "scanned 150 of 151 active products.")
        self.assert_failed(pages_of(products, precision="AT_LEAST"), "product_list_incomplete",
                           "no exact active product count.")
        self.assert_failed(pages_of(products[:100] + products[:50], count=150), "product_list_incomplete",
                           "duplicate or missing product ids.")
        bad = pages_of([socks(1), product(2, status="DRAFT"), {**socks(3), "id": "gid://shopify/Order/1"}])
        self.assert_failed(bad, "product_list_incomplete", "2 product rows with a bad id, tag list or status.")


class KitRetaggerWriteTests(KitRetaggerBase):
    def writes(self, n=3):
        products = [socks(i, tags=["kit-ride"]) for i in range(n)]
        return self.plan_products(products, DRY_RUN=False)["writes"], products

    def test_write_targets_are_read_products_and_known_tags_only(self):
        writes, products = self.writes()
        code = self.nodes[PLAN]["parameters"]["jsCode"]
        mutations = re.findall(r"^const (ADD_ONLY|REMOVE_ONLY|ADD_AND_REMOVE) = '([^']*)';$", code, re.M)
        self.assertEqual(len(mutations), 3)
        allowed = {text for _, text in mutations}
        known = {"kit-" + k for k in KITS} | {"ks-" + s for s in self.rules["slots"]}
        ids = {p["id"] for p in products}
        for write in writes:
            self.assertIn(write["query"], allowed)
            self.assertEqual(write["variables"]["id"], write["id"])
            self.assertIn(write["id"], ids)
            self.assertTrue(set(write["variables"]) <= {"id", "add", "remove"})
            for tag in write["variables"].get("add", []) + write["variables"].get("remove", []):
                self.assertIn(tag.lower(), known)
            if "add" in write["variables"] and "remove" in write["variables"]:
                self.assertIn("tagsAdd", write["query"])
                self.assertIn("tagsRemove", write["query"])
        add_only = self.plan_products([socks(1)], DRY_RUN=False)["writes"][0]
        self.assertEqual(set(add_only["variables"]), {"id", "add"})
        self.assertNotIn("tagsRemove", add_only["query"])
        remove_only = self.plan_products([mug(1)], DRY_RUN=False)["writes"][0]
        self.assertEqual(set(remove_only["variables"]), {"id", "remove"})
        self.assertNotIn("tagsAdd", remove_only["query"])

    def test_each_failure_kind_is_a_failed_write(self):
        writes, _ = self.writes()
        w = writes[0]
        user_error = self.ok(w)
        user_error["data"]["add"]["userErrors"] = [{"field": ["tags"], "message": "fake problem"}]
        missing_part = self.ok(w)
        del missing_part["data"]["remove"]
        wrong_node = self.ok(w)
        wrong_node["data"]["add"]["node"] = {"id": "gid://shopify/Product/1"}
        cases = {
            "user_errors": user_error,
            "http_502": {"error": "502 - Bad Gateway"},
            "throttled": {"errors": [{"extensions": {"code": "THROTTLED"}}]},
            "graphql_errors": {"errors": [{"message": "x"}]},
            "no_data": missing_part,
        }
        for code, result in cases.items():
            with self.subTest(code=code):
                out = self.check(writes, [result] + [self.ok(x) for x in writes[1:]])
                alert = out["alert"]
                self.assertEqual((out["mode"], alert["key"]), ("write_failed", "some_updates_failed"))
                self.assertEqual(alert["lines"][0], "Kit tags on the website: tonight's run could not update some "
                                                    "products. The other products were updated. Ask Claude to check.")
                self.assertIn("Detail for Claude: updated 2 of 3 products; failed:", alert["lines"])
                self.assertIn("- fake-product-000 " + code, alert["lines"])
                self.assertNotIn("fake problem", json.dumps(alert))
                self.assert_owner_alert(alert)
                self.assertEqual(self.route(out), (False, True))
        out = self.check(writes, [wrong_node] + [self.ok(x) for x in writes[1:]])
        self.assertIn("- fake-product-000 no_data", out["alert"]["lines"])
        out = self.check(writes, [self.ok(x) for x in writes[1:]])
        self.assertIn("- all result_count_2_of_3", out["alert"]["lines"])
        self.assertEqual(out["alert"]["lines"][0], "Kit tags on the website: tonight's run could not update the "
                                                   "products on its list. Ask Claude to check.")
        self.assertIn("Detail for Claude: updated 0 of 3 products; failed:", out["alert"]["lines"])


class KitRetaggerAlertShapeTests(KitRetaggerBase):
    """Every request the workflow can build passes the sender's own validator and stays plain."""

    def test_large_failures_fit_the_sender_limits(self):
        long_handle = lambda i: ("fake-very-long-product-handle-for-a-limit-test-%03d-" % i) + "x" * 60
        products = [product(i, title="Fake Shell Jacket %d" % i, ptype="Jacket",
                            tags=["GORE-TEX", "Travel kit", "Kit-Bundle", "Keep Cool"], handle=long_handle(i))
                    for i in range(120)]
        plan = self.plan_products(products, DRY_RUN=False)
        self.assertEqual(len(plan["writes"]), 120)
        alert = self.check(plan["writes"], [{"error": "500"} for _ in plan["writes"]])["alert"]
        self.assertLessEqual(len(alert["lines"]), 30)
        self.assertLessEqual(len("\n".join(alert["lines"])), 4000)
        self.assertTrue(any(line.startswith("... and ") for line in alert["lines"]), alert["lines"][:3])
        self.assert_owner_alert(alert)

    def test_unexpected_alert_expression_passes(self):
        value = self.nodes[UNEXPECTED]["parameters"]["assignments"]["assignments"][0]
        self.assertEqual((value["name"], value["type"]), ("alert", "object"))
        before, after = self.run_cases([{"kind": "expr", "expr": value["value"], "executed": [PLAN]},
                                        {"kind": "expr", "expr": value["value"], "executed": [PLAN, WRITE, CHECK]}])
        for alert in (before, after):
            self.assertEqual(alert["key"], "unexpected_error")
            self.assert_owner_alert(alert)
        # Before any write it says so; once Write Tag Changes has run it keeps the safe wording.
        self.assertIn("before it changed anything. Nothing was changed.", before["lines"][0])
        self.assertIn("Some products may already have been updated.", after["lines"][0])
        self.assertNotIn("Nothing was changed", after["lines"][0])

    def test_no_alert_names_another_system(self):
        # Handles are lower-cased, so an odd handle can never put another system's name in a line.
        products = [socks(1, tags=["kit-ride"], handle="Fake-AC2-STOCK-Handle")]
        writes = self.plan_products(products, DRY_RUN=False)["writes"]
        alert = self.check(writes, [{"error": "500"}])["alert"]
        text = "\n".join(alert["lines"])
        for other in OTHER_SYSTEMS:
            self.assertNotIn(other, text)
        self.assert_owner_alert(alert)


if __name__ == "__main__":
    unittest.main()
