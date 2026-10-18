"""Offline contracts for the inactive EnergyGrid Drive Verify export (#226 V3 G1, Claude-free daily run).

Node executes the committed Code-node JavaScript with mocked webhook and Google responses. The Python
Core's own listing checks (native_bridge.record_remote_listing) are applied to the workflow's answers, so
the workflow and the Core cannot silently drift apart. Nothing imports, activates or executes an n8n
workflow, and nothing contacts Google.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / "n8n-workflows" / "energygrid_drive_verify.workflow.json"
sys.path.insert(0, str(REPO_ROOT / "energygrid-bill-downloader"))

from energygrid_bill_downloader import native_bridge as bridge  # noqa: E402

ALLOWED_TYPES = {
    "n8n-nodes-base.webhook", "n8n-nodes-base.code", "n8n-nodes-base.if", "n8n-nodes-base.httpRequest",
    "n8n-nodes-base.respondToWebhook", "n8n-nodes-base.stickyNote",
}
FOLDER = "REPLACE_WITH_DRIVE_FOLDER_ID"
NONCE = "0123456789abcdef0123456789abcdef"
READER = "reader@example.com"
PROXY_HEADERS = ("cf-ray", "CF-Connecting-IP", "cf-ipcountry", "X-Forwarded-For", "x-forwarded-host", "x-real-ip")

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
    const refs = testCase.refs || {};
    const lookup = (name) => {
      if (!(name in refs)) throw new Error('node not executed: ' + name);
      return { first: () => refs[name] };
    };
    const source = testCase.expr
      ? 'return (' + node.parameters.responseBody.replace(/^=\{\{/, '').replace(/\}\}$/, '') + ');'
      : node.parameters.jsCode;
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
    return json.loads(completed.stdout)


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


def webhook_item(body, headers=None) -> dict:
    return {"json": {"headers": {"content-type": "application/json", **(headers or {})}, "body": body, "query": {},
                     "params": {}}}


def drive_file(name: str, data: bytes, **extra) -> dict:
    item = {"id": "CLOUDFILE" + hashlib.md5(name.encode()).hexdigest()[:10], "name": name,
            "mimeType": "application/pdf", "parents": [FOLDER], "size": str(len(data)),
            "md5Checksum": hashlib.md5(data).hexdigest(), "sha256Checksum": hashlib.sha256(data).hexdigest(),
            "trashed": False, "appProperties": {"x": "dropped"}}
    item.update(extra)
    return item


def page(files: list[dict], token: str | None = None, incomplete: bool = False, status: int = 200) -> dict:
    body = {"files": files, "incompleteSearch": incomplete}
    if token:
        body["nextPageToken"] = token
    return {"json": {"statusCode": status, "body": body, "headers": {}}}


ABOUT = {"statusCode": 200, "body": {"user": {"emailAddress": READER}}, "headers": {}}
VALIDATED = {"route": "OK", "nonce": NONCE, "folder_id": FOLDER}


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
        self.assertEqual(("none", "none", False), (workflow["settings"]["saveDataSuccessExecution"],
                                                  workflow["settings"]["saveDataErrorExecution"],
                                                  workflow["settings"]["saveManualExecutions"]))
        self.assertIsNone(workflow["staticData"])
        self.assertEqual({}, workflow["pinData"])
        for node in workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES, node["name"])
        self.assertNotRegex(self.raw, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        self.assertTrue(self.raw.isascii())

    def test_authenticated_fixed_path_webhook_with_response_node(self) -> None:
        hooks = [node for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.webhook"]
        self.assertEqual(1, len(hooks))
        self.assertEqual({"httpMethod": "POST", "path": "xb-energygrid-drive-verify-v1", "authentication": "headerAuth",
                          "responseMode": "responseNode", "options": {}}, hooks[0]["parameters"])

    def test_only_read_only_google_gets_with_no_retry_and_no_redirect(self) -> None:
        http = [node for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"]
        self.assertEqual({"Get Drive Account", "List Fixed Folder"}, {node["name"] for node in http})
        urls = set()
        for node in http:
            params = node["parameters"]
            urls.add(params["url"])
            self.assertEqual("GET", params["method"])
            self.assertEqual(("predefinedCredentialType", "googleDriveOAuth2Api"),
                             (params["authentication"], params["nodeCredentialType"]))
            # Read-only GETs: bounded retry is safe (official n8n HTTP node guidance); failures still answer.
            self.assertEqual((True, 3, 5000), (node["retryOnFail"], node["maxTries"], node["waitBetweenTries"]))
            self.assertEqual("continueErrorOutput", node["onError"])
            self.assertIs(False, params["options"]["redirect"]["redirect"]["followRedirects"])
            self.assertTrue(params["options"]["response"]["response"]["neverError"])
            self.assertGreater(params["options"]["timeout"], 0)
        self.assertEqual({"https://www.googleapis.com/drive/v3/about", "https://www.googleapis.com/drive/v3/files"}, urls)
        raw = json.dumps([node for node in self.workflow["nodes"] if node["type"] != "n8n-nodes-base.stickyNote"]).lower()
        for marker in ("upload", "permissions", "/copy", "generateids", "/trash"):
            self.assertFalse(marker in raw, marker)

    def test_listing_query_is_the_fixed_folder_never_request_input(self) -> None:
        params = {p["name"]: p["value"] for p in self.nodes["List Fixed Folder"]["parameters"]["queryParameters"]["parameters"]}
        self.assertEqual("={{ \"'\" + $('Validate Verify Request').first().json.folder_id + \"' in parents and trashed = false\" }}",
                         params["q"])
        self.assertEqual(("true", "true", "1000"), (params["supportsAllDrives"], params["includeItemsFromAllDrives"],
                                                    params["pageSize"]))
        self.assertIn("sha256Checksum", params["fields"])
        self.assertIn("incompleteSearch", params["fields"])
        self.assertIn("const FOLDER_ID = 'REPLACE_WITH_DRIVE_FOLDER_ID';", self.nodes["Validate Verify Request"]["parameters"]["jsCode"])
        pagination = self.nodes["List Fixed Folder"]["parameters"]["options"]["pagination"]["pagination"]
        self.assertEqual({"paginationMode": "updateAParameterInEachRequest",
                          "parameters": {"parameters": [{"type": "qs", "name": "pageToken",
                                                         "value": "={{ $response.body.nextPageToken }}"}]},
                          "paginationCompleteWhen": "other", "completeExpression": "={{ !$response.body.nextPageToken }}",
                          "limitPagesFetched": True, "maxRequests": 20, "requestInterval": 200}, pagination)

    def test_every_path_ends_in_a_response(self) -> None:
        responders = {name for name, node in self.nodes.items() if node["type"] == "n8n-nodes-base.respondToWebhook"}
        for name in self.nodes:
            if name in responders or self.nodes[name]["type"] == "n8n-nodes-base.stickyNote":
                continue
            with self.subTest(node=name):
                self.assertTrue(downstream(self.workflow, name) & responders)
        self.assertEqual({"respondWith": "noData", "options": {"responseCode": 404}},
                         self.nodes["Respond Not Found"]["parameters"])
        self.assertEqual({"Respond Not Found"}, downstream(self.workflow, "Request Is Proxied", 0))
        for name in ("Get Drive Account", "List Fixed Folder", "Assemble Listing"):
            self.assertEqual({"Respond Google Unavailable"}, downstream(self.workflow, name, 1), name)

    def test_fixed_respond_nodes_return_structured_5xx(self) -> None:
        nonce = {"Validate Verify Request": {"json": VALIDATED}}
        cases = [{"node": "Respond Google Unavailable", "expr": True, "refs": nonce}, {"node": "Respond Bounded Failure", "expr": True}]
        unavailable, failed = (result["output"] for result in run_nodes(cases))
        self.assertEqual({"schema": "energygrid.drive_verify.response.v1", "nonce": NONCE, "outcome": "GOOGLE_UNAVAILABLE"},
                         unavailable)
        self.assertEqual(502, self.nodes["Respond Google Unavailable"]["parameters"]["options"]["responseCode"])
        self.assertEqual({"schema": "energygrid.drive_verify.response.v1", "outcome": "FAILED"}, failed)
        self.assertEqual(500, self.nodes["Respond Bounded Failure"]["parameters"]["options"]["responseCode"])


class CodeNodeTests(unittest.TestCase):
    def validate(self, body, headers=None) -> dict:
        result = run_nodes([{"node": "Validate Verify Request", "input": [webhook_item(body, headers)]}])[0]
        self.assertTrue(result["ok"], result)
        return result["output"][0]["json"]

    def test_valid_request_routes_with_the_fixed_folder(self) -> None:
        out = self.validate({"schema": "energygrid.drive_verify.request.v1", "nonce": NONCE})
        self.assertEqual({"route": "OK", "nonce": NONCE, "folder_id": FOLDER}, out)

    def test_proxy_marked_requests_are_not_found(self) -> None:
        for header in PROXY_HEADERS:
            with self.subTest(header=header):
                out = self.validate({"schema": "energygrid.drive_verify.request.v1", "nonce": NONCE}, {header: "1"})
                self.assertEqual({"route": "NOT_FOUND"}, out)

    def test_malformed_requests_are_refused(self) -> None:
        good = {"schema": "energygrid.drive_verify.request.v1", "nonce": NONCE}
        for body in (None, [], {}, {**good, "q": "name contains 'x'"}, {**good, "nonce": "XYZ"},
                     {**good, "nonce": NONCE.upper()}, {**good, "schema": "other"}, {"nonce": NONCE}):
            with self.subTest(body=body):
                out = self.validate(body)
                self.assertEqual(("REFUSED", 422, "EG_VERIFY_REQUEST_INVALID"),
                                 (out["route"], out["response_code"], out["response"]["reason"]))

    def assemble(self, pages: list[dict], about: dict = ABOUT) -> dict:
        result = run_nodes([{"node": "Assemble Listing", "input": pages,
                             "refs": {"Validate Verify Request": {"json": VALIDATED},
                                      "Get Drive Account": {"json": about}}}])[0]
        self.assertTrue(result["ok"], result)
        return result["output"][0]["json"]

    def core_accepts(self, response: dict) -> dict:
        """The Python Core's own listing checks, on a disposable private config."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = bridge.NativeConfig(
                mode=bridge.MODE_UAT_SYNTHETIC, state_path=root / "s.sqlite", archive_root=root / "a",
                named_archive_root=root / "n", temp_root=root / "t", outbox_root=root / "o",
                drive=bridge.NativeDriveSettings(account="uat@example.com", folder_ids={"EB_BILL": FOLDER, "TENANT_BILL": FOLDER},
                                                 mode=bridge.DRIVE_LOCAL_SYNC,
                                                 sync_folders={"EB_BILL": root / "y", "TENANT_BILL": root / "y"}),
                gmail=bridge.NativeGmailSettings(account="s@example.com", to=("r@example.com",), subject_prefix="EG"),
                max_drive_uploads=1, max_gmail_sends=1, synthetic_root=root / "p",
                daily=bridge.NativeDailySettings(runs_root=root / "r", drive_reader_account=READER, n8n_port=5678,
                                                 token_file=root / "k", header_name="X-EnergyGrid-Monitor-Key",
                                                 verify_window_minutes=20, verify_poll_seconds=60,
                                                 run_deadline_minutes=75))
            return bridge.record_remote_listing(cfg, NONCE, response, "2026-10-20T03:30:00Z")

    def test_single_page_is_complete_and_accepted_by_the_core(self) -> None:
        out = self.assemble([page([drive_file("A.pdf", b"%PDF a"), drive_file("B.pdf", b"%PDF b")])])
        self.assertEqual(200, out["response_code"])
        response = out["response"]
        self.assertEqual((True, 1, False, False, READER), (response["complete"], response["pages"],
                                                           response["incomplete_search"],
                                                           response["next_page_token_present"],
                                                           response["reader_account"]))
        self.assertNotIn("appProperties", response["files"][0], "only the listed fields pass through")
        listing = self.core_accepts(response)
        self.assertEqual(2, len(listing["files"]))
        self.assertEqual(hashlib.sha256(b"%PDF a").hexdigest(), listing["files"][0]["sha256Checksum"])

    def test_multi_page_listing_is_concatenated(self) -> None:
        out = self.assemble([page([drive_file("A.pdf", b"a")], token="T1"), page([drive_file("B.pdf", b"b")])])
        self.assertEqual((True, 2, 2), (out["response"]["complete"], out["response"]["pages"], len(out["response"]["files"])))
        self.core_accepts(out["response"])

    def test_page_cap_or_incomplete_search_is_never_complete_and_the_core_refuses(self) -> None:
        capped = [page([drive_file(f"F{i}.pdf", b"x")], token=f"T{i}") for i in range(20)]
        for name, pages in (("page cap reached", capped),
                            ("incomplete search", [page([drive_file("A.pdf", b"a")], incomplete=True)])):
            with self.subTest(case=name):
                response = self.assemble(pages)["response"]
                self.assertIs(False, response["complete"])
                with self.assertRaises(bridge.NativeRefusal) as caught:
                    self.core_accepts(response)
                self.assertEqual("EG_DRIVE_LISTING_INCOMPLETE", caught.exception.support_ref)

    def test_google_errors_are_unavailable_never_a_listing(self) -> None:
        for name, pages, about in (
                ("page error", [page([], status=403)], ABOUT),
                ("about error", [page([])], {"statusCode": 401, "body": {}, "headers": {}}),
                ("no files array", [{"json": {"statusCode": 200, "body": {}, "headers": {}}}], ABOUT)):
            with self.subTest(case=name):
                out = self.assemble(pages, about)
                self.assertEqual((502, "GOOGLE_UNAVAILABLE"), (out["response_code"], out["response"]["outcome"]))
                self.assertNotIn("files", out["response"])


if __name__ == "__main__":
    unittest.main()
