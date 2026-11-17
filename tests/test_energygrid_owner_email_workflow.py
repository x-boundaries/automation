"""Offline contracts for the inactive EnergyGrid Owner Email export (#226 V3 G1, Claude-free daily run).

Node executes the committed Code-node JavaScript and the Respond nodes' body expressions. The Python
Core builds each SEND request (native_notify.email_request); the workflow's own render of that request
must hash to the Core's render_sha256 byte for byte, for all three templates. Graph checks prove PROBE,
refusals, render mismatches and duplicate claims can never reach the single Gmail node. Nothing imports,
activates or executes an n8n workflow, and nothing contacts Google or Gmail.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / "n8n-workflows" / "energygrid_owner_email.workflow.json"
sys.path.insert(0, str(REPO_ROOT / "energygrid-bill-downloader"))

from energygrid_bill_downloader import native_notify as notify  # noqa: E402

ALLOWED_TYPES = {
    "n8n-nodes-base.webhook", "n8n-nodes-base.code", "n8n-nodes-base.if", "n8n-nodes-base.crypto",
    "n8n-nodes-base.httpRequest", "n8n-nodes-base.dataTable", "n8n-nodes-base.gmail",
    "n8n-nodes-base.respondToWebhook", "n8n-nodes-base.stickyNote",
}
PREFIX = "EnergyGrid Test"
RECIPIENT = "owner-recipient@example.com"
NID = "egn-" + "1" * 32
DID = "egd-" + "2" * 32
NONCE = "0123456789abcdef0123456789abcdef"
GMAIL = "Send Owner Email"
SCHEMA = "energygrid.owner_email.response.v1"

HARNESS = r"""
const fs = require('fs');
const [workflowPath, casesPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const byName = Object.fromEntries(wf.nodes.map((node) => [node.name, node]));
(async () => {
  const results = [];
  for (const testCase of cases) {
    const node = byName[testCase.node];
    let source = testCase.expr
      ? 'return (' + node.parameters.responseBody.replace(/^=\{\{/, '').replace(/\}\}$/, '') + ');'
      : node.parameters.jsCode;
    for (const [from, to] of Object.entries(testCase.replace || {})) source = source.split(from).join(to);
    const refs = testCase.refs || {};
    const lookup = (name) => {
      if (!(name in refs)) throw new Error('node not executed: ' + name);
      return { first: () => refs[name] };
    };
    const fn = new (Object.getPrototypeOf(async function () {}).constructor)('$input', '$', source);
    try {
      const input = testCase.input || [];
      const output = await fn.call({}, { all: () => input, first: () => input[0] }, lookup);
      results.push({ ok: true, output });
    } catch (error) {
      results.push({ ok: false, error: String(error && error.message || error) });
    }
  }
  process.stdout.write(JSON.stringify(results));
})();
"""


def run_nodes(cases: list[dict]) -> list[dict]:
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("Node is required to execute the committed Code-node JavaScript")
    with tempfile.TemporaryDirectory() as temp:
        script = Path(temp) / "harness.js"
        script.write_text(HARNESS, encoding="utf-8")
        cases_path = Path(temp) / "cases.json"
        cases_path.write_text(json.dumps(cases), encoding="utf-8")
        completed = subprocess.run([node, str(script), str(WORKFLOW), str(cases_path)], capture_output=True, text=True,
                                   timeout=60, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr[-2000:])
    results = json.loads(completed.stdout)
    for result in results:
        if not result["ok"]:
            raise AssertionError(result["error"])
    return [result["output"] for result in results]


def downstream(workflow: dict, start: str, output: int | None = None) -> set[str]:
    connections = workflow["connections"]
    frontier = []
    for index, links in enumerate(connections.get(start, {}).get("main", [])):
        if output is None or index == output:
            frontier.extend(link["node"] for link in links)
    seen: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        for links in connections.get(name, {}).get("main", []):
            frontier.extend(link["node"] for link in links)
    return seen


def parents(workflow: dict, target: str) -> set[tuple[str, int]]:
    found = set()
    for source, outputs in workflow["connections"].items():
        for index, links in enumerate(outputs.get("main", [])):
            if any(link["node"] == target for link in links):
                found.add((source, index))
    return found


CFG = SimpleNamespace(gmail=SimpleNamespace(subject_prefix=PREFIX))


def row(kind: str, **values) -> dict:
    base = {"notification_id": NID, "draft_id": DID, "kind": kind, "stream": "EB_BILL", "status_text": "PROCESSED",
            "support_ref": None, "display_filename": None, "drive_file_id": None}
    base.update(values)
    return base


SUCCESS = row("SUCCESS", display_filename="SEP 2026 - EnergyGrid - EB Bill - 2026-09-26_ACCOUNT.pdf",
              drive_file_id="CLOUDFILE0000000001")
FAILURE = row("FAILURE", stream="TENANT_BILL", status_text="ACQUISITION_FAILED", support_ref="EG_TENANT_LIST_INCOMPLETE")
RELEASED = row("FAILURE", status_text="HOLD_RELEASED_NO_DISPATCH", support_ref="EG_V5_HOLD_RELEASED",
               display_filename="SEP 2026 - EnergyGrid - EB Bill - x's bill.pdf")
VALIDATED_REF = {"Validate Email Request": {"json": {"request": {"notification_id": NID, "dispatch_id": DID}}}}


def webhook_item(body, headers=None) -> dict:
    return {"json": {"headers": {"content-type": "application/json", **(headers or {})}, "body": body, "query": {},
                     "params": {}}}


class StaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = json.loads(cls.raw)
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_inactive_mcp_disabled_and_free_of_bindings(self) -> None:
        workflow = self.workflow
        self.assertIs(False, workflow["active"])
        self.assertIs(False, workflow["settings"]["availableInMCP"])
        self.assertNotIn("errorWorkflow", workflow["settings"], "the Core owns owner visibility")
        self.assertEqual(("none", "none", False), (workflow["settings"]["saveDataSuccessExecution"],
                                                  workflow["settings"]["saveDataErrorExecution"],
                                                  workflow["settings"]["saveManualExecutions"]))
        self.assertIsNone(workflow["staticData"])
        self.assertEqual({}, workflow["pinData"])
        for node in workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES, node["name"])
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", self.raw))
        self.assertTrue(self.raw.isascii())

    def test_authenticated_fixed_path_webhook(self) -> None:
        hooks = [node for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.webhook"]
        self.assertEqual(1, len(hooks))
        self.assertEqual({"httpMethod": "POST", "path": "xb-energygrid-owner-email-v1", "authentication": "headerAuth",
                          "responseMode": "responseNode", "options": {}}, hooks[0]["parameters"])

    def test_exactly_one_gmail_send_with_no_retry_attribution_or_extra_recipients(self) -> None:
        gmail = [node for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.gmail"]
        self.assertEqual([GMAIL], [node["name"] for node in gmail])
        node = gmail[0]
        self.assertIs(False, node["retryOnFail"], "at most one send attempt: never retried")
        self.assertEqual("continueErrorOutput", node["onError"])
        self.assertEqual({"resource": "message", "operation": "send",
                          "sendTo": "={{ $('Validate Email Request').first().json.recipient }}",
                          "subject": "={{ $('Validate Email Request').first().json.subject }}", "emailType": "text",
                          "message": "={{ $('Validate Email Request').first().json.text }}",
                          "options": {"appendAttribution": False}}, node["parameters"])
        self.assertNotIn(GMAIL, downstream(self.workflow, GMAIL), "no loop back to the Gmail node")
        for name, other in self.nodes.items():
            if other["type"] in ("n8n-nodes-base.dataTable", "n8n-nodes-base.crypto"):
                self.assertIsNot(True, other.get("retryOnFail"), name)

    def test_only_a_new_claim_after_a_matching_render_reaches_gmail(self) -> None:
        self.assertEqual({("Insert Claim", 0)}, parents(self.workflow, GMAIL))
        self.assertEqual({("Claim Is New", 0)}, parents(self.workflow, "Insert Claim"))
        self.assertEqual({("Find Existing Claim", 0)}, parents(self.workflow, "Claim Is New"))
        self.assertEqual({("Render Matches", 0)}, parents(self.workflow, "Find Existing Claim"))
        self.assertEqual({("Hash Render", 0)}, parents(self.workflow, "Render Matches"))
        for source, output in (("Is Probe", 0), ("Render Matches", 1), ("Claim Is New", 1), ("Find Existing Claim", 1),
                               ("Insert Claim", 1), ("Request Is Refused", 0), ("Request Is Proxied", 0),
                               ("Validate Email Request", 1), ("Hash Render", 1)):
            with self.subTest(branch=(source, output)):
                self.assertNotIn(GMAIL, downstream(self.workflow, source, output))

    def test_render_check_compares_the_workflow_digest_with_the_core_digest(self) -> None:
        condition = self.nodes["Render Matches"]["parameters"]["conditions"]["conditions"][0]
        self.assertEqual(("={{ $('Hash Render').first().json.render_sha256_actual }}",
                          "={{ $('Validate Email Request').first().json.request.render_sha256 }}",
                          {"type": "string", "operation": "equals"}),
                         (condition["leftValue"], condition["rightValue"], condition["operator"]))
        crypto = self.nodes["Hash Render"]["parameters"]
        self.assertEqual(("hash", "SHA256", "hex", False), (crypto["action"], crypto["type"], crypto["encoding"],
                                                            crypto["binaryData"]))
        self.assertEqual("={{ $('Validate Email Request').first().json.canonical }}", crypto["value"])

    def test_claim_lookup_uses_the_notification_id_column(self) -> None:
        find = self.nodes["Find Existing Claim"]
        self.assertTrue(find["alwaysOutputData"], "an empty lookup must still reach Claim Is New")
        self.assertEqual([{"keyName": "notificationId", "condition": "eq",
                           "keyValue": "={{ $('Validate Email Request').first().json.request.notification_id }}"}],
                         find["parameters"]["filters"]["conditions"])
        insert = self.nodes["Insert Claim"]["parameters"]["columns"]["value"]
        self.assertEqual({"notificationId", "dispatchId", "state"}, set(insert))

    def test_bookkeeping_after_the_send_never_changes_the_answer(self) -> None:
        for name in ("Mark Claim Sent", "Mark Claim Uncertain"):
            self.assertEqual("continueRegularOutput", self.nodes[name]["onError"])
        self.assertEqual({"Build Sent Answer", "Respond With Answer", "Respond Send Uncertain", "Mark Claim Sent"},
                         downstream(self.workflow, GMAIL, 0))
        self.assertEqual({"Mark Claim Uncertain", "Respond Send Uncertain"}, downstream(self.workflow, GMAIL, 1))

    def test_probe_identity_uses_the_sender_credential_type(self) -> None:
        node = self.nodes["Get Sender Identity"]
        params = node["parameters"]
        self.assertEqual(("GET", "https://openidconnect.googleapis.com/v1/userinfo", "gmailOAuth2"),
                         (params["method"], params["url"], params["nodeCredentialType"]))
        self.assertIs(False, params["options"]["redirect"]["redirect"]["followRedirects"])
        self.assertEqual((True, 3, 5000), (node["retryOnFail"], node["maxTries"], node["waitBetweenTries"]))

    def test_every_path_ends_in_a_response_with_an_explicit_code(self) -> None:
        responders = {name for name, node in self.nodes.items() if node["type"] == "n8n-nodes-base.respondToWebhook"}
        for name, node in self.nodes.items():
            if name in responders or node["type"] == "n8n-nodes-base.stickyNote":
                continue
            with self.subTest(node=name):
                self.assertTrue(downstream(self.workflow, name) & responders)
        for name in responders:
            self.assertIn("responseCode", self.nodes[name]["parameters"]["options"], name)


class CodeNodeTests(unittest.TestCase):
    replace = {"REPLACE_WITH_SUBJECT_PREFIX": PREFIX, "REPLACE_WITH_OWNER_RECIPIENT": RECIPIENT}

    @classmethod
    def setUpClass(cls) -> None:
        cls.nodes = {node["name"]: node for node in json.loads(WORKFLOW.read_text(encoding="utf-8"))["nodes"]}

    def validate(self, bodies: list, headers=None) -> list[dict]:
        cases = [{"node": "Validate Email Request", "input": [webhook_item(body, headers)], "replace": self.replace}
                 for body in bodies]
        return [output[0]["json"] for output in run_nodes(cases)]

    def test_the_workflow_renders_exactly_what_the_core_expects_for_every_template(self) -> None:
        sources = (SUCCESS, FAILURE, RELEASED)
        requests = [notify.email_request(CFG, source) for source in sources]
        for source, request, out in zip(sources, requests, self.validate(requests)):
            with self.subTest(status=source["status_text"]):
                self.assertEqual("SEND", out["route"])
                self.assertEqual(request["render_sha256"], hashlib.sha256(out["canonical"].encode("utf-8")).hexdigest())
                subject, body = notify.message_for(CFG, source)
                self.assertEqual((subject, body, RECIPIENT), (out["subject"], out["text"], out["recipient"]))
                self.assertNotIn("\n", out["subject"])

    def test_a_different_subject_prefix_changes_the_digest(self) -> None:
        request = notify.email_request(SimpleNamespace(gmail=SimpleNamespace(subject_prefix="Other")), SUCCESS)
        out = self.validate([request])[0]
        self.assertNotEqual(request["render_sha256"], hashlib.sha256(out["canonical"].encode("utf-8")).hexdigest())

    def test_probe_is_validated_and_carries_no_send_fields(self) -> None:
        out = self.validate([{"schema": notify.EMAIL_REQUEST_SCHEMA, "mode": "PROBE", "nonce": NONCE}])[0]
        self.assertEqual({"route": "PROBE", "nonce": NONCE, "recipient_lower": RECIPIENT}, out)

    def test_proxy_marked_requests_are_not_found(self) -> None:
        request = notify.email_request(CFG, SUCCESS)
        for header in ("cf-ray", "CF-Connecting-IP", "x-forwarded-for", "X-Real-IP", "x-forwarded-host", "cf-ipcountry"):
            with self.subTest(header=header):
                self.assertEqual([{"route": "NOT_FOUND"}], self.validate([request], {header: "1"}))

    def test_requests_outside_the_closed_vocabulary_are_refused(self) -> None:
        good = notify.email_request(CFG, SUCCESS)
        failure = notify.email_request(CFG, FAILURE)
        cases = {
            "extra key (recipient)": {**good, "to": "attacker@example.com"},
            "missing key": {k: v for k, v in good.items() if k != "render_sha256"},
            "bad kind": {**good, "kind": "OTHER"},
            "bad stream": {**good, "stream": "WATER_BILL"},
            "header injection": {**good, "display_filename": "x\r\nBcc: someone.pdf"},
            "not a pdf name": {**good, "display_filename": "x.exe"},
            "bad drive id": {**good, "drive_file_id": "../../x"},
            "success with support ref": {**good, "support_ref": "EG_X_Y"},
            "failure with drive id": {**failure, "drive_file_id": "CLOUDFILE0000000001"},
            "failure with filename": {**failure, "display_filename": "x.pdf"},
            "bad notification id": {**good, "notification_id": "egn-x"},
            "bad dispatch id": {**good, "dispatch_id": "DRAFT123"},
            "bad digest": {**good, "render_sha256": "XYZ"},
            "probe with extra key": {"schema": notify.EMAIL_REQUEST_SCHEMA, "mode": "PROBE", "nonce": NONCE, "x": 1},
            "unknown mode": {**good, "mode": "RESEND"},
            "wrong schema": {**good, "schema": "other"},
            "not an object": [good],
        }
        for (name, _body), out in zip(cases.items(), self.validate(list(cases.values()))):
            with self.subTest(case=name):
                self.assertEqual(("REFUSED", 422, "REFUSED", "EG_EMAIL_REQUEST_INVALID"),
                                 (out["route"], out["response_code"], out["response"]["outcome"], out["response"]["reason"]))

    def test_probe_answer_binds_sender_and_recipient_hash(self) -> None:
        refs = {"Validate Email Request": {"json": {"nonce": NONCE}},
                "Hash Recipient": {"json": {"recipient_sha256": hashlib.sha256(RECIPIENT.encode()).hexdigest()}}}
        inputs = [{"statusCode": 200, "body": {"email": "sender@example.com", "email_verified": True}},
                  {"statusCode": 401, "body": {}}, {"statusCode": 200, "body": {"email": "x@example.com"}}]
        outs = run_nodes([{"node": "Build Probe Answer", "input": [{"json": item}], "refs": refs} for item in inputs])
        good, *bad = (out[0]["json"] for out in outs)
        self.assertEqual((200, True, "sender@example.com", hashlib.sha256(RECIPIENT.encode()).hexdigest()),
                         (good["response_code"], good["response"]["ready"], good["response"]["sender_account"],
                          good["response"]["recipient_sha256"]))
        for out in bad:
            self.assertEqual((502, False), (out["response_code"], out["response"]["ready"]))

    def test_sent_answer_never_claims_sent_without_a_message_id(self) -> None:
        cases = [{"node": "Build Sent Answer", "input": [{"json": {}}],
                  "refs": {**VALIDATED_REF, GMAIL: {"json": gmail}}} for gmail in ({"id": "18c0ffee0000abcd"}, {}, {"id": "a b"})]
        sent, lost, odd = (out[0]["json"] for out in run_nodes(cases))
        self.assertEqual((200, "SENT", "18c0ffee0000abcd", NID, DID),
                         (sent["response_code"], sent["response"]["outcome"], sent["response"]["message_id"],
                          sent["response"]["notification_id"], sent["response"]["dispatch_id"]))
        for out in (lost, odd):
            self.assertEqual((502, "SEND_UNCERTAIN"), (out["response_code"], out["response"]["outcome"]))

    def test_fixed_respond_nodes_echo_ids_with_cause_mapped_codes(self) -> None:
        expected = {"Respond Duplicate": (409, {"outcome": "DUPLICATE"}),
                    "Respond Claim Failed": (422, {"outcome": "REFUSED", "reason": "EG_EMAIL_CLAIM_FAILED"}),
                    "Respond Render Mismatch": (422, {"outcome": "REFUSED", "reason": "EG_EMAIL_RENDER_MISMATCH"}),
                    "Respond Send Uncertain": (502, {"outcome": "SEND_UNCERTAIN"})}
        outs = run_nodes([{"node": name, "expr": True, "refs": VALIDATED_REF} for name in expected])
        for (name, (code, fields)), body in zip(expected.items(), outs):
            with self.subTest(node=name):
                self.assertEqual(code, self.nodes[name]["parameters"]["options"]["responseCode"])
                self.assertEqual({"schema": SCHEMA, "mode": "SEND", "notification_id": NID, "dispatch_id": DID, **fields},
                                 body)
        failed = run_nodes([{"node": "Respond Bounded Failure", "expr": True}])[0]
        self.assertEqual({"schema": SCHEMA, "outcome": "FAILED"}, failed)


if __name__ == "__main__":
    unittest.main()
