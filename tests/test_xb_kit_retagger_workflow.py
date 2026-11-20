"""Offline contract of the inactive "XB Kit Re-tagger" n8n workflow (#269 Part A).

No n8n instance, Shopify store or email account is contacted. The committed Code nodes, the
pagination expressions and the shared sender's own `Validate Alert Request` node are executed with
the local Node runtime against synthetic Shopify pages. Every product, handle and id here is fake.
The rules fixture `tests/fixtures/xb_kit_rules_v1.json` is the logic subset (slots[*].any,
profiles[*].slots, exclude.title_words, exclude.types) of the XT partner pack `XB_kit_rules.json`
v1 (22 Oct 2026, sha256 55bec65ee29fe1bdc4425920d85358cfa321f2c050a01f04d2c121bde4b6e1f5).
"""

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
RULES_FIXTURE = ROOT / "tests" / "fixtures" / "xb_kit_rules_v1.json"
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
UNEXPECTED = "Build Unexpected Alert"
SYSTEM = "KIT-RETAG"
OTHER_SYSTEMS = ("AC2-STOCK", "AC2-ARAP", "ENERGYGRID", "MEMBER-SYNC", "HEALTH")
KITS = ("tropical", "hut", "himalaya", "expedition", "winter", "travel", "island", "ride")
# 21 Nov 2026 03:00 SGT, a fixed clock for every Code run.
NOW_MS = int(datetime(2026, 11, 20, 19, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
TODAY = "2026-11-21"
FAKE_TOKEN = "fake-access-token-0000000000"

NODE_TYPES = {
    "Daily 03:00 SGT": "n8n-nodes-base.scheduleTrigger",
    "Manual Run": "n8n-nodes-base.manualTrigger",
    SETTINGS: "n8n-nodes-base.set",
    TOKEN: "n8n-nodes-base.httpRequest",
    READ: "n8n-nodes-base.httpRequest",
    PLAN: "n8n-nodes-base.code",
    "Changes To Write": "n8n-nodes-base.if",
    "Email Is Due": "n8n-nodes-base.if",
    "List Writes": "n8n-nodes-base.splitOut",
    WRITE: "n8n-nodes-base.httpRequest",
    CHECK: "n8n-nodes-base.code",
    UNEXPECTED: "n8n-nodes-base.set",
    SEND: "n8n-nodes-base.executeWorkflow",
}
STICKIES = {"Start Here", "Setup", "Read Stage", "Decide Stage", "Write Stage"}
EXPECTED_EDGES = {
    ("Daily 03:00 SGT", 0, SETTINGS),
    ("Manual Run", 0, SETTINGS),
    (SETTINGS, 0, TOKEN),
    (TOKEN, 0, READ),
    (READ, 0, PLAN),
    (PLAN, 0, "Changes To Write"),
    (PLAN, 1, UNEXPECTED),
    ("Changes To Write", 0, "List Writes"),
    ("Changes To Write", 1, "Email Is Due"),
    ("Email Is Due", 0, SEND),
    ("List Writes", 0, WRITE),
    (WRITE, 0, CHECK),
    (CHECK, 0, SEND),
    (CHECK, 1, UNEXPECTED),
    (UNEXPECTED, 0, SEND),
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
      return { ok: true, out: new Function('$response', 'return (' + inner(c.expr) + ');')(c.response) };
    }
    const w = c.workflow === 'sender' ? sender : wf;
    const code = nodeOf(w, c.node).parameters.jsCode;
    const $input = { all: () => c.raw || c.items.map((json) => ({ json })) };
    const $ = (name) => {
      if (!(name in (c.nodes || {}))) throw new Error('unexpected node reference: ' + name);
      const list = Array.isArray(c.nodes[name]) ? c.nodes[name] : [c.nodes[name]];
      return { first: () => ({ json: list[0] }), all: () => list.map((json) => ({ json })) };
    };
    return { ok: true, out: new Function('$input', '$', code)($input, $) };
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
    value = {"DRY_RUN": True, "CHANGE_CAP": 25, "REMOVE_CAP": 10, "CAP_OVERRIDE": False, "API_VERSION": "2026-10",
             "SHOP_DOMAIN": "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN"}
    value.update(overrides)
    return value


def product(i, title="Fake Product", vendor="Fake Vendor", ptype="Fake Type", tags=(), handle=None, status="ACTIVE"):
    return {"id": "gid://shopify/Product/%d" % (9000000 + i), "handle": handle or "fake-product-%03d" % i,
            "title": title, "vendor": vendor, "productType": ptype, "status": status, "tags": list(tags)}


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


SOCK_TAGS = sorted(["kit-%s" % k for k in ("tropical", "hut", "himalaya", "expedition", "winter", "travel")] + ["ks-socks"])


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class KitRetaggerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load(WORKFLOW)
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}
        cls.rules = load(RULES_FIXTURE)

    def run_cases(self, cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            done = subprocess.run([shutil.which("node"), str(harness), str(WORKFLOW), str(SENDER), str(cases_path)],
                                  capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        results = json.loads(done.stdout)
        for result in results:
            self.assertTrue(result["ok"], result)
        return [result["out"] for result in results]

    def plan(self, pages, token=None, **overrides):
        case = {"node": PLAN, "items": pages, "now_ms": NOW_MS,
                "nodes": {SETTINGS: settings(**overrides),
                          TOKEN: token if token is not None else {"access_token": FAKE_TOKEN, "scope": "fake"}}}
        (out,) = self.run_cases([case])
        (item,) = out
        return item["json"]

    def plan_products(self, products, **overrides):
        return self.plan(pages_of(products), **overrides)

    def check(self, writes, results, summary=None):
        case = {"node": CHECK, "items": results, "now_ms": NOW_MS,
                "nodes": {"List Writes": writes, PLAN: {"summary": summary or {"scanned": 1200}}}}
        (out,) = self.run_cases([case])
        (item,) = out
        return item["json"]

    def validate_alerts(self, alerts):
        bodies = [{"system": SYSTEM, **alert} for alert in alerts]
        outs = self.run_cases([{"workflow": "sender", "node": "Validate Alert Request", "items": [body]}
                               for body in bodies])
        return [out[0]["json"] for out in outs]

    def assert_sender_accepts(self, alert):
        (out,) = self.validate_alerts([alert])
        self.assertIs(out.get("valid"), True, (out, alert))


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

    def test_schedule_and_safe_default_settings(self):
        self.assertEqual(self.nodes["Daily 03:00 SGT"]["parameters"]["rule"]["interval"],
                         [{"field": "days", "daysInterval": 1, "triggerAtHour": 3, "triggerAtMinute": 0}])
        values = {a["name"]: (a["type"], a["value"])
                  for a in self.nodes[SETTINGS]["parameters"]["assignments"]["assignments"]}
        self.assertEqual(values, {
            "DRY_RUN": ("boolean", True), "CHANGE_CAP": ("number", 25), "REMOVE_CAP": ("number", 10),
            "CAP_OVERRIDE": ("boolean", False), "API_VERSION": ("string", "2026-10"),
            "SHOP_DOMAIN": ("string", "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN")})

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

    def test_read_is_paged_capped_and_slow(self):
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

    def test_alert_call_pins_the_system(self):
        node = self.nodes[SEND]
        p = node["parameters"]
        self.assertEqual(p["workflowId"]["value"], "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID")
        self.assertEqual(p["workflowInputs"]["value"], {
            "system": SYSTEM, "severity": "={{ $json.alert.severity }}", "key": "={{ $json.alert.key }}",
            "short": "={{ $json.alert.short }}", "lines": "={{ $json.alert.lines }}"})
        self.assertEqual([s["id"] for s in p["workflowInputs"]["schema"]], ["system", "severity", "key", "short", "lines"])
        self.assertEqual((p["mode"], node["onError"]), ("each", "continueRegularOutput"))
        self.assertNotIn("retryOnFail", node)

    def test_code_nodes_use_their_error_output(self):
        for name in (PLAN, CHECK):
            node = self.nodes[name]
            self.assertEqual((node["parameters"]["mode"], node["onError"]), ("runOnceForAllItems", "continueErrorOutput"))
            self.assertNotIn("alwaysOutputData", node)

    def test_notes_readme_runbook_and_ci(self):
        notes = "\n".join(n["parameters"]["content"] for n in self.workflow["nodes"]
                          if n["type"] == "n8n-nodes-base.stickyNote")
        for word in ("Required manual configuration", "REPLACE_WITH_SHOPIFY_SHOP_DOMAIN",
                     "REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID", "DRY_RUN", "CHANGE_CAP", "REMOVE_CAP", "CAP_OVERRIDE",
                     "KIT-RETAG", "kit-no", "kit-pin-<kit>", "03:00 SGT", "docs/xb_kit_retagger.md"):
            self.assertIn(word, notes)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("### xb_kit_retagger.workflow.json", readme)
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for word in ("Custom Auth", "httpCustomAuth", "client_id", "client_secret", "L0", "L4", "L6", "L8",
                     "OK kit re-tagger dry run", "another tagger"):
            self.assertIn(word, runbook)
        ci = CI.read_text(encoding="utf-8")
        for path in ("n8n-workflows/xb_kit_retagger.workflow.json", "tests/test_xb_kit_retagger_workflow.py",
                     "tests/fixtures/xb_kit_rules_v1.json", "docs/xb_kit_retagger.md",
                     "n8n-workflows/ops_alert_email.workflow.json", ".github/workflows/xb-kit-retagger-tests.yml"):
            self.assertIn('- "%s"' % path, ci)
        self.assertNotIn("**", ci)
        self.assertIn("python -m unittest tests.test_xb_kit_retagger_workflow -v", ci)


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
        plan = self.plan_products(products, DRY_RUN=False, CHANGE_CAP=1000, REMOVE_CAP=1000)
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
        # The dry run names the unknown kit-/ks- tags it leaves alone.
        dry = self.plan_products(products)
        self.assertIn("Unknown kit-/ks- tags left alone: kit-bundle ks-legacy", dry["alert"]["lines"])

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


class KitRetaggerModeTests(KitRetaggerBase):
    def test_dry_run_is_the_default_and_writes_nothing(self):
        products = [socks(i) for i in range(3)]
        plan = self.plan_products(products)
        self.assertEqual((plan["mode"], plan["writes"]), ("dry_run", []))
        alert = plan["alert"]
        self.assertEqual((alert["severity"], alert["key"], alert["short"]), ("INFO", "daily_summary", "DRYRUN " + TODAY))
        self.assertIn("Kit re-tagger DRY RUN %s: nothing was written." % TODAY, alert["lines"])
        self.assertIn("- fake-product-000 " + " ".join("+" + t for t in SOCK_TAGS), alert["lines"])
        self.assertIn("Within the cap: a real run would write these.", alert["lines"])
        # A dry run with nothing to change still reports, so the first manual run always shows a result.
        quiet = self.plan_products([product(1, title="Fake Mug", ptype="Mug")])
        self.assertEqual(quiet["mode"], "dry_run")
        self.assertIn("Active products scanned: 1. Products to change: 0 (cap 25). Products losing a kit tag: 0 (cap 10).",
                      quiet["alert"]["lines"])
        self.assert_sender_accepts(alert)

    def test_quiet_day_sends_nothing(self):
        plan = self.plan_products([product(1, title="Fake Mug", ptype="Mug")], DRY_RUN=False)
        self.assertEqual((plan["mode"], plan["writes"], plan["alert"]), ("no_change", [], None))

    def test_change_cap(self):
        at_cap = self.plan_products([socks(i) for i in range(25)], DRY_RUN=False)
        self.assertEqual((at_cap["mode"], len(at_cap["writes"]), at_cap["alert"]), ("write", 25, None))
        over = self.plan_products([socks(i) for i in range(26)], DRY_RUN=False)
        self.assertEqual((over["mode"], over["writes"]), ("over_cap", []))
        self.assertEqual((over["alert"]["severity"], over["alert"]["key"], over["alert"]["short"]),
                         ("WARN", "change_cap", "change_cap"))
        self.assertIn("... and 6 more", over["alert"]["lines"])
        self.assert_sender_accepts(over["alert"])
        override = self.plan_products([socks(i) for i in range(26)], DRY_RUN=False, CAP_OVERRIDE=True)
        self.assertEqual((override["mode"], len(override["writes"])), ("write", 26))
        # The dry run names the cap result without writing.
        dry = self.plan_products([socks(i) for i in range(26)])
        self.assertIn("Over the cap: a real run would write nothing and send a change_cap warning.", dry["alert"]["lines"])

    def test_remove_cap(self):
        losing = lambda n: [socks(i, tags=["kit-ride"] + SOCK_TAGS) for i in range(n)]
        ten = self.plan_products(losing(10), DRY_RUN=False)
        self.assertEqual((ten["mode"], len(ten["writes"])), ("write", 10))
        eleven = self.plan_products(losing(11), DRY_RUN=False)
        self.assertEqual((eleven["mode"], eleven["writes"], eleven["alert"]["key"]), ("over_cap", [], "change_cap"))
        self.assertIn("Active products scanned: 11. Products to change: 11 (cap 25). Products losing a kit tag: 11 (cap 10).",
                      eleven["alert"]["lines"])
        self.assertIn("Per kit: kit-ride +0 -11", eleven["alert"]["lines"])
        # Losing only a ks- tag is not a kit removal.
        ks_only = self.plan_products([socks(i, tags=SOCK_TAGS + ["ks-bike"]) for i in range(11)], DRY_RUN=False)
        self.assertEqual(ks_only["mode"], "write")
        self.assertEqual(self.plan_products(losing(11), DRY_RUN=False, CAP_OVERRIDE=True)["mode"], "write")

    def test_bad_cap_settings_refuse(self):
        plan = self.plan_products([socks(1)], DRY_RUN=False, CHANGE_CAP="many")
        self.assertEqual((plan["mode"], plan["writes"], plan["alert"]["key"]), ("failed", [], "unexpected"))


class KitRetaggerReadFailureTests(KitRetaggerBase):
    def assert_failed(self, plan, key, line):
        self.assertEqual((plan["mode"], plan["writes"]), ("failed", []))
        alert = plan["alert"]
        self.assertEqual((alert["severity"], alert["key"], alert["short"]), ("ERROR", key, key))
        self.assertIn(line, alert["lines"])
        self.assert_sender_accepts(alert)
        self.assertNotIn(FAKE_TOKEN, json.dumps(plan))

    def test_token_failures(self):
        pages = pages_of([socks(1)])
        self.assert_failed(self.plan(pages, token={"error": "401 - {\"error\":\"invalid_client\"}"}),
                           "token_failed", "Token request: http_401")
        self.assert_failed(self.plan(pages, token={"error": {"message": "connect ETIMEDOUT"}}),
                           "token_failed", "Token request: request_error")
        self.assert_failed(self.plan(pages, token={"scope": "read_products"}), "token_failed", "Token request: no_token")

    def test_page_failures(self):
        good = pages_of([socks(i) for i in range(150)])
        throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}
        cases = {
            "Product read: http_500 on page 1 of 1": [{"error": "500 - Internal Server Error"}],
            "Product read: throttled on page 2 of 2": [good[0], throttled],
            "Product read: graphql_errors on page 1 of 1": [{"errors": [{"message": "Field doesn't exist"}]}],
            "Product read: no_data on page 2 of 2": [good[0], {"extensions": {}}],
            "Product read: no_page": [],
        }
        for line, pages in cases.items():
            with self.subTest(line=line):
                self.assert_failed(self.plan(pages, DRY_RUN=False), "read_failed", line)

    def test_incomplete_reads(self):
        products = [socks(i) for i in range(150)]
        good = pages_of(products)
        self.assert_failed(self.plan(good[:1], DRY_RUN=False), "read_incomplete",
                           "Product read: read stopped at the page limit (1 pages)")
        self.assert_failed(self.plan(pages_of(products, count=151), DRY_RUN=False), "read_incomplete",
                           "Product read: scanned 150 of 151 active products")
        self.assert_failed(self.plan(pages_of(products, precision="AT_LEAST"), DRY_RUN=False), "read_incomplete",
                           "Product read: no exact active product count")
        dup = pages_of(products[:100] + products[:50], count=150)
        self.assert_failed(self.plan(dup, DRY_RUN=False), "read_incomplete",
                           "Product read: duplicate or missing product ids")
        bad = pages_of([socks(1), product(2, status="DRAFT"), {**socks(3), "id": "gid://shopify/Order/1"}])
        self.assert_failed(self.plan(bad, DRY_RUN=False), "read_incomplete",
                           "Product read: 2 product rows with a bad id, tag list or status")


class KitRetaggerWriteTests(KitRetaggerBase):
    def writes(self, n=3):
        products = [socks(i, tags=["kit-ride"]) for i in range(n)]
        return self.plan_products(products, DRY_RUN=False)["writes"], products

    @staticmethod
    def ok(write):
        data = {}
        for part in ("add", "remove"):
            if part in write["variables"]:
                data[part] = {"node": {"id": write["id"]}, "userErrors": []}
        return {"data": data, "extensions": {}}

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
        remove_only = self.plan_products([product(1, title="Fake Mug", ptype="Mug", tags=["kit-hut"])],
                                         DRY_RUN=False)["writes"][0]
        self.assertEqual(set(remove_only["variables"]), {"id", "remove"})
        self.assertNotIn("tagsAdd", remove_only["query"])

    def test_all_writes_ok_send_the_changed_report(self):
        writes, _ = self.writes()
        out = self.check(writes, [self.ok(w) for w in writes])
        alert = out["alert"]
        self.assertEqual((alert["severity"], alert["key"], alert["short"]), ("INFO", "daily_summary", "CHANGED " + TODAY))
        self.assertIn("Products changed: 3 of 3 (active products scanned: 1200).", alert["lines"])
        self.assertIn("- fake-product-000 " + " ".join("+" + t for t in SOCK_TAGS) + " -kit-ride", alert["lines"])
        self.assert_sender_accepts(alert)

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
                self.assertEqual((alert["severity"], alert["key"], alert["short"]), ("ERROR", "write_failed", "write_failed"))
                self.assertIn("Products changed: 2 of 3 (active products scanned: 1200).", alert["lines"])
                self.assertIn("- fake-product-000 " + code, alert["lines"])
                self.assertNotIn("fake problem", json.dumps(alert))
                self.assert_sender_accepts(alert)
        out = self.check(writes, [wrong_node] + [self.ok(x) for x in writes[1:]])
        self.assertIn("- fake-product-000 no_data", out["alert"]["lines"])
        out = self.check(writes, [self.ok(x) for x in writes[1:]])
        self.assertIn("- all result_count_2_of_3", out["alert"]["lines"])
        self.assertIn("Products changed: 0 of 3 (active products scanned: 1200).", out["alert"]["lines"])


class KitRetaggerAlertShapeTests(KitRetaggerBase):
    """Design test 9: every request the workflow can build passes the sender's own validator."""

    def test_large_runs_fit_the_sender_limits(self):
        long_handle = lambda i: ("fake-very-long-product-handle-for-a-limit-test-%03d-" % i) + "x" * 60
        products = [product(i, title="Fake Shell Jacket %d" % i, ptype="Jacket",
                            tags=["GORE-TEX", "Travel kit", "Kit-Bundle", "Keep Cool"], handle=long_handle(i))
                    for i in range(120)]
        alerts = []
        dry = self.plan_products(products)
        alerts.append(dry["alert"])
        over = self.plan_products(products, DRY_RUN=False)
        alerts.append(over["alert"])
        forced = self.plan_products(products, DRY_RUN=False, CAP_OVERRIDE=True)
        self.assertEqual(len(forced["writes"]), 120)
        alerts.append(self.check(forced["writes"], [KitRetaggerWriteTests.ok(w) for w in forced["writes"]])["alert"])
        alerts.append(self.check(forced["writes"], [{"error": "500"} for _ in forced["writes"]])["alert"])
        for alert in alerts:
            self.assertLessEqual(len(alert["lines"]), 30)
            self.assertLessEqual(len("\n".join(alert["lines"])), 4000)
            self.assertTrue(any(line.startswith("... and ") for line in alert["lines"]), alert["lines"][:3])
        for out in self.validate_alerts(alerts):
            self.assertIs(out.get("valid"), True, out)

    def test_unexpected_alert_expression_passes(self):
        value = self.nodes[UNEXPECTED]["parameters"]["assignments"]["assignments"][0]
        self.assertEqual((value["name"], value["type"]), ("alert", "object"))
        (alert,) = self.run_cases([{"kind": "expr", "expr": value["value"], "response": {}}])
        self.assertEqual((alert["severity"], alert["key"], alert["short"]), ("ERROR", "unexpected", "unexpected"))
        self.assert_sender_accepts(alert)

    def test_no_alert_names_another_system(self):
        # Tags and handles are lower-cased, so an odd tag can never put another system's name in a line.
        products = [product(1, title="Fake Mug", ptype="Mug", tags=["KIT-HEALTH", "KS-ENERGYGRID", "kit-hut"],
                            handle="Fake-AC2-STOCK-Handle")]
        plan = self.plan_products(products)
        text = "\n".join(plan["alert"]["lines"])
        for other in OTHER_SYSTEMS:
            self.assertNotIn(other, text)
        self.assert_sender_accepts(plan["alert"])


if __name__ == "__main__":
    unittest.main()
