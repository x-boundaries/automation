"""Offline contracts for the inactive SIMBA invoice intake export (#244).

Node executes the committed Code-node JavaScript against synthetic Gmail API messages and mocked Google
Drive answers. Every address, invoice number, link and amount below is invented. Nothing imports,
activates or executes an n8n workflow, and nothing contacts Gmail, Google Drive, SIMBA or the PDF host.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / "n8n-workflows" / "simba_invoice_intake.workflow.json"

ALLOWED_TYPES = {
    "n8n-nodes-base.gmailTrigger", "n8n-nodes-base.set", "n8n-nodes-base.splitInBatches", "n8n-nodes-base.noOp",
    "n8n-nodes-base.httpRequest", "n8n-nodes-base.code", "n8n-nodes-base.if", "n8n-nodes-base.crypto",
    "n8n-nodes-base.stickyNote",
}
LOOP = "One Email At A Time"

BROADBAND_ADDR = "personal-billing@example.com"
MOBILE_ADDR = "company-billing@example.com"
SETTINGS = {
    "broadband_folder_id": "FOLDER_BROADBAND",
    "mobile_folder_id": "FOLDER_MOBILE",
    "review_folder_id": "FOLDER_REVIEW",
    "billing_addresses": f"{BROADBAND_ADDR} = BROADBAND;\n{MOBILE_ADDR}=mobile",
}
INVOICE = "S0000012345"
PDF_URL = "https://billing-invoice-pdf.amdocs-dbs.com/v1/invoices/SYNTHETICTOKEN123/pdf"
TRACKER = ("https://abcd1234.r.ap-southeast-1.awstrack.me/L0/" + quote(PDF_URL, safe="")
           + "/1/0100018f-synthetic-0000/AbCdEfSynthetic=123")
GOOD_AUTH = ("mx.google.com;\r\n       dkim=pass header.i=@my.simba.sg header.s=s1 header.b=AbC1;\r\n"
             "       dkim=pass header.i=@amazonses.com header.s=6gbrjp header.b=XyZ2;\r\n"
             "       spf=pass (google.com: domain of 0108018f@ap-southeast-1.amazonses.com designates 192.0.2.1"
             " as permitted sender) smtp.mailfrom=0108018f@ap-southeast-1.amazonses.com;\r\n"
             "       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=my.simba.sg")
PDF_BYTES = b"%PDF-1.7\n% synthetic invoice\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"

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
    const input = testCase.input || [];
    const context = { helpers: { getBinaryDataBuffer: async (index, key) => Buffer.from(input[index].binary[key].data, 'base64') } };
    const fn = new (Object.getPrototypeOf(async function () {}).constructor)('$input', '$', node.parameters.jsCode);
    try {
      const output = await fn.call(context, { all: () => input, first: () => input[0] }, lookup);
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


def run_one(node: str, input_items: list[dict], refs: dict | None = None) -> dict:
    return run_nodes([{"node": node, "input": input_items, "refs": refs or {}}])[0]


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def body_text(invoice: str = INVOICE, issued: str = "15-Oct-2026", amount: str = "12.34", link: str = TRACKER) -> str:
    return ("Dear CUSTOMER NAME\n15-Oct-2026\n"
            f"Your SIMBA invoice {invoice} <{link}> for the current period is ready.\n"
            f"Issued On:\u00a0{issued}\nAmount Due: $ {amount}\nRegards, SIMBA Telecom\n")


def body_html(invoice: str = INVOICE, issued: str = "15-Oct-2026", link: str = TRACKER) -> str:
    return (f"<p>Your SIMBA invoice <a href=\"{link.replace('&', '&amp;')}\">{invoice}</a> is ready.</p>"
            f"<p>Issued On: {issued}</p><img src=\"https://simba.example.com/logo.png\">")


def message(headers: list[tuple[str, str]], plain: str | None = None, html: str | None = None,
            attachments: tuple[str, ...] = (), message_id: str = "18f0a1b2c3d4e5f6") -> dict:
    parts = []
    if plain is not None:
        parts.append({"partId": "0", "mimeType": "text/plain", "filename": "", "headers": [],
                      "body": {"size": len(plain), "data": b64url(plain)}})
    if html is not None:
        parts.append({"partId": "1", "mimeType": "text/html", "filename": "", "headers": [],
                      "body": {"size": len(html), "data": b64url(html)}})
    for index, name in enumerate(attachments):
        parts.append({"partId": str(2 + index), "mimeType": "application/pdf", "filename": name, "headers": [],
                      "body": {"size": 100, "attachmentId": "ATT" + str(index)}})
    return {"id": message_id, "threadId": message_id, "internalDate": "1792130892000", "payload": {
        "mimeType": "multipart/alternative", "filename": "", "body": {"size": 0}, "parts": parts,
        "headers": [{"name": name, "value": value} for name, value in headers]}}


def direct(to: str = BROADBAND_ADDR, auth: str | list[str] = GOOD_AUTH,
           sender: str = "Simba Mobile <bills@my.simba.sg>", extra: list[tuple[str, str]] | None = None, **body) -> dict:
    auths = auth if isinstance(auth, list) else [auth]
    headers = [("Delivered-To", "intake@example.com")]
    headers += [("Authentication-Results", value) for value in auths]
    headers += [("From", sender), ("To", to), ("Subject", "Your SIMBA Invoice"),
                ("Message-ID", "<0100018f-synthetic@ap-southeast-1.amazonses.com>")]
    headers += extra or []
    html_body = {key: value for key, value in body.items() if key != "amount"}
    return message(headers, plain=body_text(**body), html=body_html(**html_body))


def forwarded(quoted_to: str, prefix: str = "Fwd:") -> dict:
    plain = ("This is another invoice from SIMBA.\n\nBegin forwarded message:\n\n"
             "> From: Simba Mobile <bills@my.simba.sg>\n> Subject: Your SIMBA Invoice\n"
             f"> To: CUSTOMER NAME <{quoted_to}>\n>\n> " + body_text().replace("\n", "\n> "))
    auth = "mx.google.com;\r\n dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=forwarder@example.com; dmarc=pass header.from=example.com"
    return message([("Authentication-Results", auth), ("From", "Forwarder <forwarder@example.com>"),
                    ("To", "intake@example.com"), ("Subject", prefix + " Your SIMBA Invoice")], plain=plain)


def classify(msg: dict, settings: dict | None = None) -> dict:
    return run_one("Read And Classify Invoice Email", [{"json": msg}], {"Intake Settings": {"json": settings or SETTINGS}})


def decision_for(msg: dict) -> dict:
    result = classify(msg)
    assert result["ok"], result
    return result["output"][0]["json"]


def drive_search(files: list[dict], status: int = 200, incomplete: bool = False) -> dict:
    return {"json": {"statusCode": status, "body": {"files": files, "incompleteSearch": incomplete}, "headers": {}}}


def existing(status: str, key: str = INVOICE) -> dict:
    return {"id": "FILE1", "name": "x", "trashed": False, "properties": {"simbaIntakeKey": key, "simbaStatus": status}}


def downstream(workflow: dict, start: str, output: int | None = None) -> set[str]:
    connections = workflow["connections"]
    frontier = []
    for index, links in enumerate(connections.get(start, {}).get("main", [])):
        if output is None or index == output:
            frontier.extend(link["node"] for link in links)
    seen: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in seen or name == LOOP:
            seen.add(name)
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

    def test_inactive_mcp_disabled_and_free_of_bindings(self) -> None:
        workflow = self.workflow
        self.assertIs(False, workflow["active"])
        self.assertIs(False, workflow["settings"]["availableInMCP"])
        self.assertIsNone(workflow["staticData"])
        self.assertEqual({}, workflow["pinData"])
        # Failed runs are kept so a lost email is visible; successful runs keep no email data.
        self.assertEqual(("none", "all", False), (workflow["settings"]["saveDataSuccessExecution"],
                                                 workflow["settings"]["saveDataErrorExecution"],
                                                 workflow["settings"]["saveManualExecutions"]))
        for node in workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
            self.assertIn(node["type"], ALLOWED_TYPES, node["name"])
        self.assertNotIn("errorWorkflow", workflow["settings"])

    def test_export_carries_no_private_values(self) -> None:
        lowered = self.raw.lower()
        for forbidden in ("x-boundaries", "amount due"):
            self.assertNotIn(forbidden, lowered)
        self.assertIsNone(re.search(r"S\d{10}", self.raw))
        settings = self.nodes["Intake Settings"]["parameters"]["assignments"]["assignments"]
        self.assertEqual(set(SETTINGS), {item["name"] for item in settings})
        for item in settings:
            self.assertRegex(item["value"], r"^REPLACE_WITH_[A-Z_]+$")
        emails = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", self.raw))
        self.assertEqual({"bills@my.simba.sg"}, {email for email in emails if not email.endswith("@example.com")})

    def test_trigger_polls_for_direct_mail_and_forwards(self) -> None:
        trigger = self.nodes["Watch Mailbox For SIMBA Emails"]
        self.assertEqual("n8n-nodes-base.gmailTrigger", trigger["type"])
        self.assertEqual([{"mode": "everyMinute"}], trigger["parameters"]["pollTimes"]["item"])
        query = trigger["parameters"]["filters"]["q"]
        self.assertIn("from:bills@my.simba.sg", query)
        self.assertIn('subject:"SIMBA Invoice"', query)

    def test_http_nodes_are_bounded_and_never_call_the_tracker(self) -> None:
        http = {name: node for name, node in self.nodes.items() if node["type"] == "n8n-nodes-base.httpRequest"}
        self.assertEqual({"Fetch Full Email", "Look For Existing Copy In Drive", "Download Invoice PDF",
                          "Start Drive Upload", "Upload File Bytes", "Read Back Stored File"}, set(http))
        for name, node in http.items():
            params = node["parameters"]
            self.assertIs(False, params["options"]["redirect"]["redirect"]["followRedirects"], name)
            self.assertGreater(params["options"]["timeout"], 0, name)
            self.assertNotIn("awstrack", json.dumps(params), name)
            self.assertNotIn("amdocs", json.dumps(params), name)
        self.assertEqual({"Start Drive Upload": "POST", "Upload File Bytes": "PUT"},
                         {name: node["parameters"]["method"] for name, node in http.items()
                          if node["parameters"]["method"] != "GET"})
        download = http["Download Invoice PDF"]["parameters"]
        self.assertEqual("={{ $json.pdf_url }}", download["url"])
        self.assertNotIn("authentication", download)
        self.assertEqual("file", download["options"]["response"]["response"]["responseFormat"])
        self.assertEqual("gmailOAuth2", http["Fetch Full Email"]["parameters"]["nodeCredentialType"])
        for name in ("Look For Existing Copy In Drive", "Start Drive Upload", "Upload File Bytes", "Read Back Stored File"):
            self.assertEqual("googleDriveOAuth2Api", http[name]["parameters"]["nodeCredentialType"], name)
        for name in ("Start Drive Upload", "Upload File Bytes"):
            self.assertIs(False, http[name]["retryOnFail"], name)

    def test_tracker_host_appears_only_as_a_pattern_in_the_classifier(self) -> None:
        holders = [node["name"] for node in self.workflow["nodes"] if "awstrack" in json.dumps(node)]
        self.assertEqual(["Read And Classify Invoice Email"], holders)

    def test_every_path_returns_to_the_loop_and_pdf_failures_reach_review(self) -> None:
        connections = self.workflow["connections"]
        self.assertEqual("All New Emails Handled", connections[LOOP]["main"][0][0]["node"])
        self.assertEqual("Fetch Full Email", connections[LOOP]["main"][1][0]["node"])
        for name in ("Is SIMBA Invoice Email", "Needs Storing"):
            self.assertEqual(LOOP, connections[name]["main"][1][0]["node"], name)
        self.assertEqual(LOOP, connections["Verify Stored File"]["main"][0][0]["node"])
        for name in ("Download Invoice PDF", "Fingerprint PDF"):
            self.assertEqual("continueErrorOutput", self.nodes[name]["onError"], name)
            self.assertEqual("Prepare Drive File", connections[name]["main"][1][0]["node"], name)
        self.assertEqual("Prepare Drive File", connections["Has PDF Link"]["main"][1][0]["node"])
        self.assertNotIn("Start Drive Upload", downstream(self.workflow, "Is SIMBA Invoice Email", 1))
        self.assertNotIn("Start Drive Upload", downstream(self.workflow, "Needs Storing", 1))
        self.assertEqual("Something To Store", connections["Prepare Drive File"]["main"][0][0]["node"])
        self.assertEqual("Start Drive Upload", connections["Something To Store"]["main"][0][0]["node"])
        self.assertEqual(LOOP, connections["Something To Store"]["main"][1][0]["node"])

    def test_canvas_documents_purpose_and_required_settings(self) -> None:
        note = self.nodes["Overview"]["parameters"]["content"]
        self.assertTrue(note.startswith("# SIMBA invoices: email to Google Drive"))
        self.assertIn("## Required manual configuration:", note)
        for key in ("broadband_folder_id", "mobile_folder_id", "review_folder_id", "billing_addresses"):
            self.assertIn("`" + key + "`", note)
        self.assertTrue(all(ord(ch) < 128 for ch in self.raw))


class ClassifyTests(unittest.TestCase):
    def test_genuine_broadband_invoice_is_filed(self) -> None:
        decision = decision_for(direct())
        self.assertEqual(("FILE", "DIRECT_VERIFIED", "BROADBAND", []),
                         (decision["route"], decision["kind"], decision["service"], decision["review_reasons"]))
        self.assertEqual((INVOICE, "2026-10-15", PDF_URL, INVOICE),
                         (decision["invoice_number"], decision["issue_date"], decision["pdf_url"], decision["intake_key"]))
        self.assertNotIn("amount", json.dumps(decision).lower())

    def test_mobile_account_recognised_from_to(self) -> None:
        decision = decision_for(direct(to="Customer <" + MOBILE_ADDR.upper() + ">"))
        self.assertEqual(("FILE", "MOBILE"), (decision["route"], decision["service"]))

    def test_added_recipient_headers_never_choose_the_account(self) -> None:
        # A genuine SIMBA email to someone else, resent to us with an extra unsigned header, must not be filed.
        stranger = "Customer <someone@example.com>"
        probes = {
            "X-Original-To": (direct(to=stranger, extra=[("X-Original-To", BROADBAND_ADDR)]), "ACCOUNT_NOT_RECOGNISED"),
            "Delivered-To": (direct(to=stranger, extra=[("Delivered-To", MOBILE_ADDR)]), "ACCOUNT_NOT_RECOGNISED"),
            "second To": (direct(to=stranger, extra=[("To", BROADBAND_ADDR)]), "RECIPIENT_HEADERS_UNCLEAR"),
            "Cc": (direct(to=stranger, extra=[("Cc", MOBILE_ADDR)]), "ACCOUNT_NOT_RECOGNISED"),
            "two Cc": (direct(extra=[("Cc", "a@example.com"), ("Cc", "b@example.com")]), "RECIPIENT_HEADERS_UNCLEAR"),
        }
        for label, (msg, reason) in probes.items():
            with self.subTest(label):
                decision = decision_for(msg)
                self.assertEqual(("REVIEW", None, [reason]),
                                 (decision["route"], decision["service"], decision["review_reasons"]))

    def test_unknown_or_ambiguous_account_goes_to_review(self) -> None:
        unknown = decision_for(direct(to="other@example.com"))
        self.assertEqual(("REVIEW", None, ["ACCOUNT_NOT_RECOGNISED"]),
                         (unknown["route"], unknown["service"], unknown["review_reasons"]))
        self.assertEqual(PDF_URL, unknown["pdf_url"])
        both = decision_for(direct(to=BROADBAND_ADDR + ", " + MOBILE_ADDR))
        self.assertEqual(["ACCOUNT_AMBIGUOUS"], both["review_reasons"])

    def test_unconfigured_billing_addresses_never_match(self) -> None:
        settings = dict(SETTINGS, billing_addresses="REPLACE_WITH_BILLING_ADDRESSES")
        result = classify(direct(), settings)
        self.assertEqual(["ACCOUNT_NOT_RECOGNISED"], result["output"][0]["json"]["review_reasons"])

    def test_billing_address_change_is_a_settings_edit_only(self) -> None:
        alias = "intake+new-mobile@example.com"
        settings = dict(SETTINGS, billing_addresses=f"{BROADBAND_ADDR} = BROADBAND, {MOBILE_ADDR} = MOBILE, {alias} = MOBILE")
        for to in (MOBILE_ADDR, alias):
            with self.subTest(to=to):
                result = classify(direct(to=to), settings)
                self.assertEqual(("FILE", "MOBILE"), (result["output"][0]["json"]["route"], result["output"][0]["json"]["service"]))
        moved = classify(direct(to=MOBILE_ADDR), dict(SETTINGS, billing_addresses=f"{alias} = MOBILE"))
        self.assertEqual(["ACCOUNT_NOT_RECOGNISED"], moved["output"][0]["json"]["review_reasons"])

    def test_bad_billing_address_list_stops_the_run(self) -> None:
        for value in ("someone@example.com", "someone@example.com = TV", f"{MOBILE_ADDR} = MOBILE; {MOBILE_ADDR} = BROADBAND"):
            with self.subTest(value=value):
                result = classify(direct(), dict(SETTINGS, billing_addresses=value))
                self.assertFalse(result["ok"])
                self.assertIn("billing_addresses", result["error"])

    def test_missing_folder_settings_stop_the_run(self) -> None:
        result = classify(direct(), dict(SETTINGS, review_folder_id="REPLACE_WITH_REVIEW_FOLDER_ID"))
        self.assertFalse(result["ok"])
        self.assertIn("review_folder_id", result["error"])

    def test_review_folder_must_differ_from_invoice_folders(self) -> None:
        shared = dict(SETTINGS, broadband_folder_id="FOLDER_SHARED", mobile_folder_id="FOLDER_SHARED")
        self.assertTrue(classify(direct(), shared)["ok"])
        for key in ("broadband_folder_id", "mobile_folder_id"):
            with self.subTest(key=key):
                result = classify(direct(), dict(SETTINGS, review_folder_id=SETTINGS[key]))
                self.assertFalse(result["ok"])
                self.assertIn("review_folder_id", result["error"])

    def test_sender_must_be_proven_by_gmails_own_top_header(self) -> None:
        failing = "mx.google.com; dkim=none; spf=softfail smtp.mailfrom=bills@my.simba.sg; dmarc=fail header.from=my.simba.sg"
        cases = {
            "dmarc fails": direct(auth=failing),
            "forged pass below Gmail's header": direct(auth=[failing, GOOD_AUTH]),
            "other auth server": direct(auth=GOOD_AUTH.replace("mx.google.com", "mx.example.com", 1)),
            "no SES evidence": direct(auth="mx.google.com; dkim=pass header.i=@my.simba.sg; spf=pass smtp.mailfrom=bills@my.simba.sg; dmarc=pass header.from=my.simba.sg"),
            "SES only, not SIMBA's domain": direct(auth="mx.google.com; dkim=pass header.i=@amazonses.com; spf=pass smtp.mailfrom=x@eu-west-1.amazonses.com; dmarc=fail header.from=my.simba.sg"),
            "look-alike SES domain": direct(auth="mx.google.com; dkim=pass header.i=@my.simba.sg; dkim=pass header.i=@notamazonses.com; dmarc=pass header.from=my.simba.sg"),
            "no auth header": direct(auth=[]),
            "two From headers": direct(extra=[("From", "bills@my.simba.sg")]),
            "dmarc fail outvoted by an extra pass": direct(auth=GOOD_AUTH.replace(
                "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=my.simba.sg",
                "dmarc=fail header.from=my.simba.sg; dmarc=pass header.from=my.simba.sg")),
            "no dmarc result": direct(auth="mx.google.com; dkim=pass header.d=my.simba.sg; dkim=pass header.d=amazonses.com; spf=pass smtp.mailfrom=x@ap-southeast-1.amazonses.com"),
            "dmarc none": direct(auth="mx.google.com; dkim=pass header.d=my.simba.sg; dkim=pass header.d=amazonses.com; spf=pass smtp.mailfrom=x@ap-southeast-1.amazonses.com; dmarc=none header.from=my.simba.sg"),
        }
        for label, msg in cases.items():
            with self.subTest(label):
                decision = decision_for(msg)
                self.assertEqual(("REVIEW", "DIRECT_UNVERIFIED"), (decision["route"], decision["kind"]))
                self.assertIn("SENDER_NOT_VERIFIED", decision["review_reasons"])

    def test_ses_proof_by_spf_alone_is_enough(self) -> None:
        auth = ("mx.google.com; dkim=pass header.d=my.simba.sg; spf=pass smtp.mailfrom=x@ap-southeast-1.amazonses.com;"
                " dmarc=pass header.from=my.simba.sg")
        self.assertEqual("FILE", decision_for(direct(auth=auth))["route"])

    def test_forwards_go_to_review_with_a_hint_only(self) -> None:
        apple = decision_for(forwarded(BROADBAND_ADDR))
        self.assertEqual(("REVIEW", "FORWARDED_COPY", None, "BROADBAND"),
                         (apple["route"], apple["kind"], apple["service"], apple["service_hint"]))
        self.assertEqual(INVOICE, apple["invoice_number"])
        self.assertEqual(PDF_URL, apple["pdf_url"])
        outlook = decision_for(forwarded(MOBILE_ADDR, prefix="FW:"))
        self.assertEqual(("REVIEW", "MOBILE"), (outlook["route"], outlook["service_hint"]))
        unknown = decision_for(forwarded("someone@example.com"))
        self.assertIsNone(unknown["service_hint"])

    def test_unrelated_internal_reply_is_ignored(self) -> None:
        msg = message([("From", "Colleague <colleague@example.com>"), ("Subject", "RE: FW: Your SIMBA Invoice")],
                      plain="Got it, thanks.\n")
        decision = decision_for(msg)
        self.assertEqual(("IGNORE", "NOT_SIMBA", ""), (decision["route"], decision["kind"], decision["pdf_url"]))

    def test_unclear_content_goes_to_review(self) -> None:
        cases = {
            "SEVERAL_INVOICE_NUMBERS": direct(invoice=INVOICE + " and S0000054321"),
            "NO_INVOICE_NUMBER": direct(invoice="S12345"),
            "NO_ISSUE_DATE": direct(issued="soon"),
            "ISSUE_DATE_UNCLEAR": direct(issued="31-Feb-2026"),
        }
        for reason, msg in cases.items():
            with self.subTest(reason):
                decision = decision_for(msg)
                self.assertEqual("REVIEW", decision["route"])
                self.assertIn(reason, decision["review_reasons"])
        no_number = decision_for(direct(invoice="no number"))
        self.assertEqual("gmail-18f0a1b2c3d4e5f6", no_number["intake_key"])

    def test_only_https_pdf_host_links_are_accepted(self) -> None:
        bad_links = {
            "tracker to another host": TRACKER.replace(quote("billing-invoice-pdf.amdocs-dbs.com", safe=""), "evil.example.com"),
            "plain http": PDF_URL.replace("https://", "http://"),
            "not a pdf path": PDF_URL.replace("/pdf", "/view"),
            "look-alike host": PDF_URL.replace("amdocs-dbs.com", "amdocs-dbs.com.example.com"),
        }
        for label, link in bad_links.items():
            with self.subTest(label):
                msg = direct(link=link)
                msg["payload"]["parts"] = msg["payload"]["parts"][:1]
                decision = decision_for(msg)
                self.assertEqual("", decision["pdf_url"])
                self.assertIn("NO_PDF_LINK", decision["review_reasons"])
        direct_link = direct(link=PDF_URL + "?lang=en&amp;x=1")
        direct_link["payload"]["parts"] = direct_link["payload"]["parts"][:1]
        self.assertTrue(decision_for(direct_link)["pdf_url"].startswith(PDF_URL))

    def test_numbers_inside_links_are_not_invoice_numbers(self) -> None:
        link = TRACKER.replace("AbCdEfSynthetic", "S0000099999")
        decision = decision_for(direct(link=link))
        self.assertEqual(INVOICE, decision["invoice_number"])

    def test_amount_never_changes_the_decision(self) -> None:
        first = decision_for(direct(amount="12.34"))
        second = decision_for(direct(amount="999.00"))
        self.assertEqual(first, second)

    def test_attachments_are_unexpected(self) -> None:
        msg = direct()
        msg["payload"]["parts"].append({"mimeType": "application/pdf", "filename": "invoice.pdf",
                                        "body": {"attachmentId": "A1", "size": 10}})
        self.assertIn("UNEXPECTED_ATTACHMENT", decision_for(msg)["review_reasons"])

    def test_unreadable_message_stops_the_run(self) -> None:
        self.assertFalse(classify({"id": "not hex!", "payload": {}})["ok"])


class DecideTests(unittest.TestCase):
    def decide(self, decision: dict, search: dict) -> dict:
        return run_one("Decide Whether To Store", [search], {"Read And Classify Invoice Email": {"json": decision}})

    def test_duplicate_rules(self) -> None:
        file_route = decision_for(direct())
        review_route = decision_for(direct(to="other@example.com"))
        cases = [
            (file_route, [], True),
            (file_route, [existing("REVIEW")], True),
            (file_route, [existing("FILED")], False),
            (file_route, [existing("FILED", key="S0000000001")], True),
            (review_route, [], True),
            (review_route, [existing("REVIEW")], False),
            (review_route, [existing("FILED")], False),
        ]
        for decision, files, store in cases:
            with self.subTest(route=decision["route"], files=files):
                result = self.decide(decision, drive_search(files))
                self.assertTrue(result["ok"], result)
                self.assertIs(store, result["output"][0]["json"]["store"])

    def test_drive_problems_stop_the_run(self) -> None:
        decision = decision_for(direct())
        for search in (drive_search([], status=403), drive_search([], incomplete=True), {"json": {}}):
            self.assertFalse(self.decide(decision, search)["ok"])


class PdfAndUploadTests(unittest.TestCase):
    def check(self, data: bytes | None) -> dict:
        item = {"json": {"pdf_sha256": hashlib.sha256(data or b"").hexdigest()}}
        if data is not None:
            item["binary"] = {"data": {"data": base64.b64encode(data).decode("ascii"), "mimeType": "application/pdf"}}
        return run_one("Check Downloaded PDF", [item])

    def test_pdf_check(self) -> None:
        good = self.check(PDF_BYTES)["output"][0]
        self.assertEqual(("OK", len(PDF_BYTES)), (good["json"]["pdf_check"], good["json"]["pdf_size"]))
        self.assertIn("data", good["binary"])
        self.assertEqual("NOT_A_PDF", self.check(b"<html>Please log in</html>")["output"][0]["json"]["pdf_problem"])
        self.assertEqual("NO_FILE_RETURNED", self.check(None)["output"][0]["json"]["pdf_problem"])

    def prepare(self, decision: dict, input_item: dict, existing_copies: int = 0) -> dict:
        decided = dict(decision, store=True, skip_reason=None, existing_copies=existing_copies)
        result = run_one("Prepare Drive File", [input_item],
                         {"Intake Settings": {"json": SETTINGS}, "Decide Whether To Store": {"json": decided}})
        self.assertTrue(result["ok"], result)
        return result["output"][0]

    def pdf_item(self) -> dict:
        checked = self.check(PDF_BYTES)["output"][0]
        return {"json": checked["json"], "binary": checked["binary"]}

    def test_filed_invoice_name_folder_and_properties(self) -> None:
        out = self.prepare(decision_for(direct()), self.pdf_item())
        upload = out["json"]["upload"]
        self.assertEqual("FILE", out["json"]["route"])
        self.assertEqual("Oct 2026 - SIMBA Broadband Invoice " + INVOICE + ".pdf", upload["name"])
        self.assertEqual(("FOLDER_BROADBAND", "application/pdf", len(PDF_BYTES)),
                         (upload["folder_id"], upload["mime_type"], upload["size"]))
        self.assertEqual({"simbaIntakeKey": INVOICE, "simbaStatus": "FILED", "simbaGmailId": "18f0a1b2c3d4e5f6",
                          "simbaAccount": "BROADBAND", "simbaSha256": hashlib.sha256(PDF_BYTES).hexdigest()},
                         upload["properties"])
        self.assertIn("file", out["binary"])

    def test_review_pdf_keeps_hint_in_name(self) -> None:
        out = self.prepare(decision_for(forwarded(MOBILE_ADDR)), self.pdf_item())
        upload = out["json"]["upload"]
        self.assertEqual("Needs review - Oct 2026 - SIMBA probably Mobile Invoice " + INVOICE + ".pdf", upload["name"])
        self.assertEqual(("FOLDER_REVIEW", "REVIEW", "HINT_MOBILE"),
                         (upload["folder_id"], upload["properties"]["simbaStatus"], upload["properties"]["simbaAccount"]))

    def test_failed_download_turns_a_filing_into_a_review_note(self) -> None:
        decision = decision_for(direct())
        for input_item in ({"json": {"error": "403 Forbidden"}}, {"json": {"pdf_check": "FAILED", "pdf_problem": "NOT_A_PDF"}}):
            with self.subTest(input_item=input_item):
                out = self.prepare(decision, input_item)
                upload = out["json"]["upload"]
                self.assertEqual("REVIEW", out["json"]["route"])
                self.assertEqual("Needs review - Oct 2026 - SIMBA Invoice " + INVOICE + ".txt", upload["name"])
                self.assertEqual(("FOLDER_REVIEW", "text/plain", "REVIEW"),
                                 (upload["folder_id"], upload["mime_type"], upload["properties"]["simbaStatus"]))
                note = base64.b64decode(out["binary"]["file"]["data"]).decode("utf-8")
                self.assertEqual(upload["size"], len(note.encode("utf-8")))
                self.assertIn("could not be downloaded", note)
                self.assertIn("rfc822msgid:0100018f-synthetic@ap-southeast-1.amazonses.com", note)
                self.assertNotIn("SYNTHETICTOKEN", note)
                self.assertNotIn("http", note)
                self.assertNotIn("12.34", note)

    def test_failed_filing_adds_no_second_review_copy(self) -> None:
        out = self.prepare(decision_for(direct()), {"json": {"error": "403 Forbidden"}}, existing_copies=1)
        self.assertEqual({"store": False, "skip_reason": "REVIEW_COPY_EXISTS", "route": "REVIEW"}, out["json"])
        self.assertNotIn("binary", out)
        stored = self.prepare(decision_for(direct()), self.pdf_item(), existing_copies=1)
        self.assertEqual((True, "FILE"), (stored["json"]["store"], stored["json"]["route"]))

    def test_no_link_note_explains_reasons(self) -> None:
        msg = direct(link="https://example.com/nothing")
        msg["payload"]["parts"] = msg["payload"]["parts"][:1]
        decision = decision_for(msg)
        out = self.prepare(decision, {"json": dict(decision, store=True)})
        note = base64.b64decode(out["binary"]["file"]["data"]).decode("utf-8")
        self.assertIn("No link to the invoice PDF was found.", note)
        self.assertIn("Invoice PDF link in the email: no", note)

    def test_attach_and_verify(self) -> None:
        prepared = self.prepare(decision_for(direct()), self.pdf_item())
        location = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=SYNTH"
        attach = run_one("Attach File For Upload", [{"json": {"statusCode": 200, "headers": {"location": location}}}],
                         {"Prepare Drive File": prepared})
        self.assertEqual(location, attach["output"][0]["json"]["upload_url"])
        self.assertIn("file", attach["output"][0]["binary"])
        refused = run_one("Attach File For Upload", [{"json": {"statusCode": 403, "headers": {}}}],
                          {"Prepare Drive File": prepared})
        self.assertFalse(refused["ok"])

        upload = prepared["json"]["upload"]
        put = {"json": {"statusCode": 200, "body": {"id": "DRIVEFILE1"}}}
        stored = {"id": "DRIVEFILE1", "name": upload["name"], "parents": [upload["folder_id"]],
                  "size": str(upload["size"]), "sha256Checksum": upload["sha256"], "trashed": False,
                  "properties": dict(upload["properties"])}
        refs = {"Prepare Drive File": prepared, "Upload File Bytes": put}
        ok = run_one("Verify Stored File", [{"json": {"statusCode": 200, "body": stored}}], refs)
        self.assertEqual({"stored": True, "route": "FILE", "drive_file_id": "DRIVEFILE1", "name": upload["name"]},
                         ok["output"][0]["json"])
        for change in ({"sha256Checksum": "0" * 64}, {"parents": ["OTHER"]}, {"size": "1"},
                       {"properties": {"simbaIntakeKey": INVOICE}}):
            with self.subTest(change=change):
                bad = run_one("Verify Stored File", [{"json": {"statusCode": 200, "body": dict(stored, **change)}}], refs)
                self.assertFalse(bad["ok"])
        failed_put = run_one("Verify Stored File", [{"json": {"statusCode": 404, "body": {}}}],
                             {"Prepare Drive File": prepared, "Upload File Bytes": {"json": {"statusCode": 500, "body": {}}}})
        self.assertFalse(failed_put["ok"])


if __name__ == "__main__":
    unittest.main()
