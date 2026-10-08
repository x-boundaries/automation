"""Offline contracts for the inactive EnergyGrid Drive upload workflow export (#226 G3).

Node executes the committed Code-node JavaScript with mocked Google responses.
Nothing imports, activates or executes an n8n workflow, and nothing contacts
Google. The Python core's own parser and verifier are applied to the workflow's
answers, so the two classifiers cannot silently drift apart.
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
WORKFLOW = REPO_ROOT / "n8n-workflows" / "energygrid_drive_upload.workflow.json"
EMAIL_WORKFLOW = REPO_ROOT / "n8n-workflows" / "energygrid_invoice_delivery.workflow.json"
sys.path.insert(0, str(REPO_ROOT / "energygrid-bill-downloader"))

from energygrid_bill_downloader.drive import parse_result, verify_result  # noqa: E402
from energygrid_bill_downloader.state import canonical_json, drive_app_properties, drive_binding_id  # noqa: E402

ALLOWED_TYPES = {
    "n8n-nodes-base.webhook", "n8n-nodes-base.code", "n8n-nodes-base.if", "n8n-nodes-base.crypto",
    "n8n-nodes-base.httpRequest", "n8n-nodes-base.respondToWebhook", "n8n-nodes-base.stickyNote",
}
CREATE_NODES = {"Start Resumable Upload", "Upload PDF Bytes"}
PDF = b"%PDF-1.4\n% synthetic drive workflow payload\n%%EOF\n"
SHA = hashlib.sha256(PDF).hexdigest()
MD5 = hashlib.md5(PDF).hexdigest()
OPERATION_ID = "11111111-2222-4333-8444-555555555555"
INVOICE_ID = "66666666-7777-5888-9999-000000000000"
ROOT = "synthRootFolder000001"
FOLDER = "synthEbBillFolder00001"
ACCOUNT = "01234567890123456789"
BINDING = drive_binding_id("EB_BILL", ACCOUNT, ROOT, FOLDER)
RESERVED = "synthReservedFile00001"
PROPS = drive_app_properties(binding_id=BINDING, invoice_id=INVOICE_ID, operation_id=OPERATION_ID, local_sha256=SHA, stream="EB_BILL")


def request(mode: str, **overrides) -> dict:
    document = {
        "schema": "energygrid.drive_request.v3", "mode": mode, "operation_id": OPERATION_ID, "invoice_id": INVOICE_ID,
        "stream": "EB_BILL", "binding_id": BINDING, "folder_id": FOLDER, "root_folder_id": ROOT,
        "reserved_file_id": None if mode == "RESERVE_ID" else RESERVED, "remote_name": "2026-10-01.pdf",
        "local_byte_size": len(PDF), "local_sha256": SHA, "local_md5": MD5, "app_properties": dict(PROPS),
    }
    if mode == "RESOLVE_DESTINATION":
        for key in ("invoice_id", "reserved_file_id", "remote_name", "local_byte_size", "local_sha256", "local_md5", "app_properties"):
            document[key] = None
    document.update(overrides)
    return document


def operation_row() -> dict:
    return {
        "operation_id": OPERATION_ID, "invoice_id": INVOICE_ID, "binding_id": BINDING, "stream": "EB_BILL",
        "folder_id": FOLDER, "remote_name": "2026-10-01.pdf", "local_byte_size": len(PDF), "local_sha256": SHA,
        "local_md5": MD5, "app_properties_json": canonical_json(PROPS), "reserved_remote_file_id": RESERVED,
    }


def drive_file(**overrides) -> dict:
    file = {"id": RESERVED, "name": "2026-10-01.pdf", "mimeType": "application/pdf", "parents": [FOLDER],
            "size": str(len(PDF)), "md5Checksum": MD5, "sha256Checksum": SHA, "appProperties": dict(PROPS), "trashed": False}
    file.update(overrides)
    return file


def found(file=None):
    return {"statusCode": 200, "body": file or drive_file(), "headers": {}}


NOT_FOUND = {"statusCode": 404, "body": {"error": {"code": 404}}, "headers": {}}


def listing(files=(), *, incomplete=False, token=None):
    body = {"incompleteSearch": incomplete, "files": list(files)}
    if token:
        body["nextPageToken"] = token
    return {"statusCode": 200, "body": body, "headers": {}}


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
    const pdf = testCase.pdf_b64 === undefined ? null : Buffer.from(testCase.pdf_b64, 'base64');
    const context = { helpers: { getBinaryDataBuffer: async () => { if (!pdf) throw new Error('no binary'); return pdf; } } };
    const fn = new (Object.getPrototypeOf(async function () {}).constructor)('$input', '$', node.parameters.jsCode);
    try {
      const output = await fn.call(context, { all: () => testCase.input, first: () => testCase.input[0] }, lookup);
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
        completed = subprocess.run([node, str(script), str(WORKFLOW), str(cases_path)], capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr[-2000:])
    return json.loads(completed.stdout)


def validated(mode="UPLOAD_IF_ABSENT", **overrides):
    return {"json": {"valid": True, "request": request(mode, **overrides)}}


def downstream(workflow: dict, start: str, output: int | None = None) -> set[str]:
    connections = workflow["connections"]
    frontier = []
    branches = connections.get(start, {}).get("main", [])
    for index, links in enumerate(branches):
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


class StaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = json.loads(cls.raw)
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_inactive_not_mcp_exposed_and_saves_no_execution_data(self) -> None:
        workflow = self.workflow
        self.assertIs(False, workflow["active"])
        self.assertIsNone(workflow["staticData"])
        self.assertEqual({}, workflow.get("pinData", {}))
        self.assertEqual({
            "executionOrder": "v1", "binaryMode": "separate", "availableInMCP": False,
            "saveDataErrorExecution": "none", "saveDataSuccessExecution": "none", "saveManualExecutions": False,
        }, workflow["settings"])

    def test_closed_node_surface_without_smtp_credentials_or_webhook_ids(self) -> None:
        for node in self.workflow["nodes"]:
            with self.subTest(node=node["name"]):
                self.assertIn(node["type"], ALLOWED_TYPES)
                self.assertNotIn("credentials", node)
                self.assertNotIn("webhookId", node)
                self.assertIs(False, node.get("retryOnFail", False))
        types = " ".join(node["type"] for node in self.workflow["nodes"]).lower()
        for forbidden in ("emailsend", "smtp", "gmail", "datatable", "executecommand", "readwritefile", "ssh"):
            self.assertNotIn(forbidden, types)

    def test_every_google_call_uses_the_custom_scope_drive_credential_type_only(self) -> None:
        http_nodes = [node for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"]
        self.assertGreaterEqual(len(http_nodes), 12)
        for node in http_nodes:
            with self.subTest(node=node["name"]):
                parameters = node["parameters"]
                self.assertEqual("predefinedCredentialType", parameters["authentication"])
                self.assertEqual("googleDriveOAuth2Api", parameters["nodeCredentialType"])
                self.assertIs(False, node["retryOnFail"])
                self.assertEqual("continueErrorOutput", node["onError"])
                self.assertEqual({"fullResponse": True, "neverError": True},
                                 {key: parameters["options"]["response"]["response"][key] for key in ("fullResponse", "neverError")})
                self.assertIs(False, parameters["options"]["redirect"]["redirect"]["followRedirects"])
                url = parameters["url"]
                self.assertTrue(url.startswith(("https://www.googleapis.com/", "={{ 'https://www.googleapis.com/", "={{ $json.upload_url }}")), url)
        self.assertNotIn("googleOAuth2Api", self.raw.replace("googleDriveOAuth2Api", ""))
        for secret in ("access_token", "refresh_token", "client_secret", "Bearer ", "Authorization"):
            self.assertNotIn(secret, self.raw)

    def test_webhook_is_header_authenticated_with_placeholder_path(self) -> None:
        hook = self.nodes["EnergyGrid Drive Webhook"]
        self.assertEqual({"httpMethod": "POST", "path": "REPLACE_WITH_DRIVE_WEBHOOK_PATH", "authentication": "headerAuth",
                          "responseMode": "responseNode", "options": {}}, hook["parameters"])
        email = json.loads(EMAIL_WORKFLOW.read_text(encoding="utf-8"))
        email_paths = {node["parameters"].get("path") for node in email["nodes"] if node["type"] == "n8n-nodes-base.webhook"}
        self.assertNotIn(hook["parameters"]["path"], email_paths)

    def test_generate_ids_reservation_is_the_exact_bounded_call(self) -> None:
        node = self.nodes["Generate Drive File ID"]["parameters"]
        self.assertEqual(("GET", "https://www.googleapis.com/drive/v3/files/generateIds"), (node["method"], node["url"]))
        self.assertEqual([("count", "1"), ("space", "drive"), ("type", "files")],
                         [(item["name"], item["value"]) for item in node["queryParameters"]["parameters"]])

    def test_create_carries_the_reserved_id_and_is_resumable_without_retry(self) -> None:
        start = self.nodes["Start Resumable Upload"]["parameters"]
        self.assertEqual("POST", start["method"])
        self.assertEqual("https://www.googleapis.com/upload/drive/v3/files", start["url"])
        self.assertIn(("uploadType", "resumable"), [(item["name"], item["value"]) for item in start["queryParameters"]["parameters"]])
        self.assertIn("id: $('Validate Drive Request').first().json.request.reserved_file_id", start["jsonBody"])
        self.assertIn("appProperties: $('Validate Drive Request').first().json.request.app_properties", start["jsonBody"])
        put = self.nodes["Upload PDF Bytes"]["parameters"]
        self.assertEqual(("PUT", "binaryData", "pdf"), (put["method"], put["contentType"], put["inputDataFieldName"]))

    def test_searches_bind_identity_and_canonical_name_with_complete_paging_fields(self) -> None:
        for prefix in ("", "Upload ", "Readback "):
            identity = {item["name"]: item["value"] for item in self.nodes[f"{prefix}Search Invoice Identity"]["parameters"]["queryParameters"]["parameters"]}
            name = {item["name"]: item["value"] for item in self.nodes[f"{prefix}Search Canonical Name"]["parameters"]["queryParameters"]["parameters"]}
            for query in (identity, name):
                self.assertEqual("user", query["corpora"])
                self.assertEqual("drive", query["spaces"])
                self.assertIn("nextPageToken,incompleteSearch,files(", query["fields"])
                self.assertIn("sha256Checksum", query["fields"])
                self.assertIn("appProperties", query["fields"])
            self.assertIn("appProperties has { key='egInv' and value='", identity["q"])
            self.assertNotIn("trashed", identity["q"], "trashed or moved copies must still be found")
            self.assertIn("in parents and name = '", name["q"])
            self.assertIn("trashed = false", name["q"])


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))

    def test_reconcile_reserve_and_resolve_branches_cannot_reach_any_create(self) -> None:
        for gate in ("Is Reconcile Request", "Is Reserve Request", "Is Resolve Request"):
            with self.subTest(gate=gate):
                reachable = downstream(self.workflow, gate, 0)
                self.assertFalse(CREATE_NODES & reachable, reachable & CREATE_NODES)

    def test_creates_are_reachable_only_through_the_upload_decision(self) -> None:
        for node in CREATE_NODES:
            parents = [source for source, value in self.workflow["connections"].items()
                       for links in value["main"] for link in links if link["node"] == node]
            self.assertEqual({"Upload Is Required"} if node == "Start Resumable Upload" else {"Attach PDF For Upload"}, set(parents))
        self.assertEqual("Start Resumable Upload", self.workflow["connections"]["Upload Is Required"]["main"][0][0]["node"])

    def test_every_fallible_node_routes_its_error_output_to_a_bounded_response(self) -> None:
        responders = {"Respond With Drive Unavailable", "Respond With Bounded Failure", "Respond With Request Rejected"}
        for node in self.workflow["nodes"]:
            if node.get("onError") != "continueErrorOutput":
                continue
            branches = self.workflow["connections"].get(node["name"], {}).get("main", [])
            error_index = 2 if node["type"] == "n8n-nodes-base.if" else 1
            with self.subTest(node=node["name"]):
                self.assertGreater(len(branches), error_index)
                self.assertTrue(branches[error_index])
                self.assertTrue(all(link["node"] in responders for link in branches[error_index]))

    def test_email_workflow_has_no_drive_upload_path(self) -> None:
        email = json.loads(EMAIL_WORKFLOW.read_text(encoding="utf-8"))
        raw = json.dumps(email)
        for forbidden in ("googleapis.com", "googleDriveOAuth2Api", "generateIds", "uploadType"):
            self.assertNotIn(forbidden, raw)


class CodeNodeTests(unittest.TestCase):
    def validate(self, metadata: dict | str, *, binary: dict | None = None, pdf: bytes | None = None, body_extra=None) -> dict:
        text = metadata if isinstance(metadata, str) else json.dumps(metadata)
        body = {"metadata": text}
        if body_extra:
            body.update(body_extra)
        item = {"json": {"body": body}}
        if binary is not None:
            item["binary"] = binary
        case = {"node": "Validate Drive Request", "input": [item]}
        if pdf is not None:
            import base64

            case["pdf_b64"] = base64.b64encode(pdf).decode("ascii")
        result = run_nodes([case])[0]
        self.assertTrue(result["ok"], result)
        return result["output"][0]["json"]

    def test_validator_accepts_each_exact_mode(self) -> None:
        pdf_binary = {"pdf": {"fileName": "2026-10-01.pdf", "mimeType": "application/pdf"}}
        self.assertTrue(self.validate(request("UPLOAD_IF_ABSENT"), binary=pdf_binary, pdf=PDF)["valid"])
        self.assertTrue(self.validate(request("RECONCILE"))["valid"])
        self.assertTrue(self.validate(request("RESERVE_ID"))["valid"])
        self.assertTrue(self.validate(request("RESOLVE_DESTINATION"))["valid"])

    def test_validator_rejects_contract_violations(self) -> None:
        pdf_binary = {"pdf": {"fileName": "2026-10-01.pdf", "mimeType": "application/pdf"}}
        reordered = dict(reversed(list(request("RECONCILE").items())))
        duplicate = json.dumps(request("RECONCILE"))[:-1] + ',"mode":"UPLOAD_IF_ABSENT"}'
        cases = {
            "key order": (reordered, None, None, None),
            "duplicate key": (duplicate, None, None, None),
            "reconcile carries pdf": (request("RECONCILE"), pdf_binary, PDF, None),
            "reserve carries reserved id": (request("RESERVE_ID", reserved_file_id=RESERVED), None, None, None),
            "foreign op in appProperties": (request("RECONCILE", app_properties={**PROPS, "egOp": "00000000-0000-4000-8000-000000000000"}), None, None, None),
            "eighth appProperty": (request("RECONCILE", app_properties={**PROPS, "egExtra": "x"}), None, None, None),
            "quote in name": (request("RECONCILE", remote_name="x' or name contains '.pdf"), None, None, None),
            "short pdf": (request("UPLOAD_IF_ABSENT"), pdf_binary, PDF[:-1], None),
            "extra text field": (request("RECONCILE"), None, None, {"other": "x"}),
        }
        for name, (metadata, binary, pdf, extra) in cases.items():
            with self.subTest(name=name):
                result = self.validate(metadata, binary=binary, pdf=pdf, body_extra=extra)
                self.assertIs(False, result["valid"])
                self.assertEqual(422, result["response_code"])
                self.assertEqual("REQUEST_REJECTED", result["response"]["outcome"])

    def classify(self, node: str, mode: str, refs: dict, last: dict) -> tuple[int, dict]:
        refs = {name: {"json": value} for name, value in refs.items()}
        refs["Validate Drive Request"] = validated(mode)
        result = run_nodes([{"node": node, "input": [{"json": last}], "refs": refs}])[0]
        self.assertTrue(result["ok"], result)
        document = result["output"][0]["json"]
        return document["response_code"], document["response"]

    def core_verdict(self, mode: str, status: int, document: dict) -> str:
        metadata = request(mode)
        parsed = parse_result(status, json.dumps(document).encode("utf-8"), metadata)
        self.assertIsNotNone(parsed, document)
        return verify_result(operation_row(), parsed).kind

    def reconcile(self, lookup, by_identity, by_name):
        return self.classify("Classify Reconcile Result", "RECONCILE",
                             {"Lookup Reserved File": lookup, "Search Invoice Identity": by_identity}, by_name)

    def test_reconcile_matrix_agrees_with_the_python_core(self) -> None:
        cases = {
            "exact sha": ((found(), listing([drive_file()]), listing([drive_file()])), 200, "FOUND_EXACT", "FOUND_EXACT"),
            "md5 only": ((found(drive_file(sha256Checksum=None)), listing(), listing()), 200, "FOUND_EXACT", "FOUND_EXACT"),
            "no checksum": ((found(drive_file(sha256Checksum=None, md5Checksum=None)), listing(), listing()), 200, "CHECKSUM_UNAVAILABLE", "CHECKSUM_UNAVAILABLE"),
            "absent": ((NOT_FOUND, listing(), listing()), 200, "NOT_FOUND", "NOT_FOUND"),
            "wrong bytes": ((found(drive_file(size=str(len(PDF) + 1))), listing(), listing()), 200, "CONFLICT", "CONFLICT"),
            "moved": ((found(drive_file(parents=["synthOtherFolder00001"])), listing(), listing()), 200, "CONFLICT", "CONFLICT"),
            "other id same identity": ((NOT_FOUND, listing([drive_file(id="synthOtherFile0000001")]), listing()), 200, "CONFLICT", "CONFLICT"),
            "name occupied": ((NOT_FOUND, listing(), listing([drive_file(id="synthOtherFile0000001", appProperties=None)])), 200, "CONFLICT", "CONFLICT"),
            "incomplete": ((NOT_FOUND, listing(incomplete=True), listing()), 503, "DRIVE_UNAVAILABLE", "UNAVAILABLE"),
            "next page": ((NOT_FOUND, listing(), listing(token="more")), 503, "DRIVE_UNAVAILABLE", "UNAVAILABLE"),
            "lookup error": (({"statusCode": 500, "body": {}, "headers": {}}, listing(), listing()), 503, "DRIVE_UNAVAILABLE", "UNAVAILABLE"),
        }
        for name, ((lookup, by_identity, by_name), status, outcome, core) in cases.items():
            with self.subTest(name=name):
                code, document = self.reconcile(lookup, by_identity, by_name)
                self.assertEqual((status, outcome), (code, document["outcome"]))
                self.assertEqual("NOT_ATTEMPTED", document["upload_result"])
                self.assertEqual(core, self.core_verdict("RECONCILE", code, document))

    def test_upload_happens_only_when_reserved_id_absent_and_searches_empty(self) -> None:
        def before(lookup, by_identity, by_name):
            refs = {"Upload Lookup Reserved File": {"json": lookup}, "Upload Search Invoice Identity": {"json": by_identity},
                    "Validate Drive Request": validated()}
            result = run_nodes([{"node": "Classify Before Upload", "input": [{"json": by_name}], "refs": refs}])[0]
            self.assertTrue(result["ok"], result)
            return result["output"][0]["json"]

        self.assertIs(True, before(NOT_FOUND, listing(), listing())["upload_required"])
        for name, args in {
            "reserved exists": (found(), listing([drive_file()]), listing([drive_file()])),
            "identity elsewhere": (NOT_FOUND, listing([drive_file(id="synthOtherFile0000001")]), listing()),
            "name occupied": (NOT_FOUND, listing(), listing([drive_file(id="synthOtherFile0000001", appProperties=None)])),
            "incomplete": (NOT_FOUND, listing(incomplete=True), listing()),
        }.items():
            with self.subTest(name=name):
                self.assertIs(False, before(*args)["upload_required"])

    def test_resumable_session_handles_open_409_and_failure(self) -> None:
        def session(start):
            refs = {"Validate Drive Request": validated()}
            result = run_nodes([{"node": "Check Upload Session", "input": [{"json": start}], "refs": refs}])[0]
            self.assertTrue(result["ok"], result)
            return result["output"][0]["json"]

        location = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=synthetic"
        self.assertEqual("OPEN", session({"statusCode": 200, "headers": {"location": location}, "body": ""})["session"])
        self.assertEqual("EXISTS", session({"statusCode": 409, "headers": {}, "body": ""})["session"])
        failed = session({"statusCode": 200, "headers": {"location": "https://evil.example/upload"}, "body": ""})
        self.assertEqual(("FAILED", 503, "DRIVE_UNAVAILABLE"), (failed["session"], failed["response_code"], failed["response"]["outcome"]))

    def readback(self, lookup, by_identity, by_name, *, created=True, exists=False):
        refs = {"Readback Reserved File": lookup, "Readback Search Invoice Identity": by_identity}
        if created:
            refs["Record Upload Result"] = {"upload_result": "CREATED"}
        if exists:
            refs["Check Upload Session"] = {"session": "EXISTS", "upload_result": "ALREADY_EXISTS"}
        return self.classify("Classify Upload Result", "UPLOAD_IF_ABSENT", refs, by_name)

    def test_upload_readback_after_create_and_after_409(self) -> None:
        code, document = self.readback(found(), listing([drive_file()]), listing([drive_file()]))
        self.assertEqual((200, "UPLOADED", "CREATED"), (code, document["outcome"], document["upload_result"]))
        self.assertEqual("FOUND_EXACT", self.core_verdict("UPLOAD_IF_ABSENT", code, document))
        code, document = self.readback(found(), listing([drive_file()]), listing([drive_file()]), created=False, exists=True)
        self.assertEqual((200, "UPLOADED", "ALREADY_EXISTS"), (code, document["outcome"], document["upload_result"]))
        self.assertEqual("FOUND_EXACT", self.core_verdict("UPLOAD_IF_ABSENT", code, document))
        code, document = self.readback(found(drive_file(sha256Checksum="0" * 64)), listing(), listing(), created=False, exists=True)
        self.assertEqual("CONFLICT", document["outcome"])
        self.assertEqual("CONFLICT", self.core_verdict("UPLOAD_IF_ABSENT", code, document))

    def test_upload_not_observable_after_create_is_never_success(self) -> None:
        code, document = self.readback(NOT_FOUND, listing(), listing())
        self.assertEqual((503, "DRIVE_UNAVAILABLE", "EG_DRIVE_UPLOAD_NOT_OBSERVED"), (code, document["outcome"], document["support_ref"]))
        self.assertEqual("UNAVAILABLE", self.core_verdict("UPLOAD_IF_ABSENT", code, document))

    def test_reservation_result_requires_exactly_one_valid_id(self) -> None:
        def reserve(response):
            refs = {"Validate Drive Request": validated("RESERVE_ID")}
            result = run_nodes([{"node": "Build Reserve Result", "input": [{"json": response}], "refs": refs}])[0]
            self.assertTrue(result["ok"], result)
            output = result["output"][0]["json"]
            return output["response_code"], output["response"]

        code, document = reserve({"statusCode": 200, "body": {"ids": ["synthGeneratedId00001"]}})
        self.assertEqual((200, "ID_RESERVED", "synthGeneratedId00001"), (code, document["outcome"], document["generated_id"]))
        self.assertIsNotNone(parse_result(code, json.dumps(document).encode(), request("RESERVE_ID")))
        for body in ({"ids": []}, {"ids": ["a", "b"]}, {"ids": ["bad id"]}, {}):
            with self.subTest(body=body):
                code, document = reserve({"statusCode": 200, "body": body})
                self.assertEqual((503, "DRIVE_UNAVAILABLE"), (code, document["outcome"]))

    def test_destination_folder_check_and_resolution(self) -> None:
        def folder(response):
            refs = {"Validate Drive Request": validated("RECONCILE")}
            result = run_nodes([{"node": "Check Destination Folder", "input": [{"json": response}], "refs": refs}])[0]
            self.assertTrue(result["ok"], result)
            return result["output"][0]["json"]

        good = {"id": FOLDER, "name": "EB Bill", "mimeType": "application/vnd.google-apps.folder", "trashed": False, "parents": [ROOT]}
        self.assertEqual({"route": "RECONCILE"}, folder({"statusCode": 200, "body": good}))
        for name, body in {"renamed": {**good, "name": "EB Bills"}, "trashed": {**good, "trashed": True},
                           "moved": {**good, "parents": ["synthOtherRoot0000001"]}}.items():
            with self.subTest(name=name):
                self.assertEqual("DESTINATION_CHANGED", folder({"statusCode": 200, "body": body})["response"]["outcome"])
        self.assertEqual(503, folder({"statusCode": 500, "body": {}})["response_code"])


if __name__ == "__main__":
    unittest.main()
