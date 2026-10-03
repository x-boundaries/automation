"""Offline contracts for the inactive EnergyGrid invoice-delivery workflow export."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / "n8n-workflows" / "energygrid_invoice_delivery.workflow.json"
ALLOWED_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.code",
    "n8n-nodes-base.if",
    "n8n-nodes-base.crypto",
    "n8n-nodes-base.merge",
    "n8n-nodes-base.dataTable",
    "n8n-nodes-base.emailSend",
    "n8n-nodes-base.respondToWebhook",
    "n8n-nodes-base.stickyNote",
}
RESULT_KEYS = {"schema", "delivery_id", "outcome", "duplicate", "support_ref"}
RUN_ID = "00000000-0000-4000-8000-000000000001"
DELIVERY_ID = "egmail-v1-00000000000000000000000000000001"
PDF_BYTES = b"%PDF-1.4\nsynthetic invoice bytes\n%%EOF\n"
PDF_SHA256 = hashlib.sha256(PDF_BYTES).hexdigest()
BASE_METADATA = {
    "schema": "energygrid.invoice_delivery.v1",
    "delivery_id": DELIVERY_ID,
    "run_id": RUN_ID,
    "stream": "EB_BILL",
    "bill_date": "2026-10-02",
    "attachment_name": "2026-10-02.pdf",
    "pdf_byte_size": len(PDF_BYTES),
    "pdf_sha256": PDF_SHA256,
}

HARNESS = r"""
const fs = require('fs');
const crypto = require('crypto');
const [workflowPath, casesPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const byName = Object.fromEntries(wf.nodes.map((node) => [node.name, node]));
function lookup(outputs) {
  return (name) => ({ first: () => ({ json: outputs[name] && outputs[name][0] && outputs[name][0].json }) });
}
function execute(code, items, helpers, outputs) {
  const wrapped = new Function('$input', '$', 'return (async function(){\n' + code + '\n}).call(this)');
  return wrapped.call({ helpers: helpers || {} }, { all: () => items }, lookup(outputs || {}));
}
function expression(value, item, outputs) {
  if (typeof value !== 'string' || !value.startsWith('={{') || !value.endsWith('}}')) return value;
  return new Function('$json', '$', 'return (' + value.slice(3, -2) + ')')(item && item.json || {}, lookup(outputs || {}));
}
function mappedValues(values, item, outputs) {
  return Object.fromEntries(Object.entries(values).map(([key, value]) => [key, expression(value, item, outputs)]));
}
function renderTemplate(value, item, outputs) {
  if (typeof value !== 'string') return value;
  return value.replace(/\{\{\s*([\s\S]*?)\s*\}\}/g, (_match, source) =>
    String(new Function('$json', '$', 'return (' + source + ')')(item.json, lookup(outputs))));
}
function respond(name, item, outputs) {
  const parameters = byName[name].parameters;
  return {
    status: expression(parameters.options.responseCode, item, outputs),
    body: expression(parameters.responseBody, item, outputs),
  };
}
function updateRows(rows, filter, values) {
  const output = [];
  for (const row of rows) {
    if (row[filter.keyName] === filter.keyValue) {
      Object.assign(row, values);
      output.push({ json: { ...row } });
    }
  }
  return output;
}
async function runFlow(sample, options) {
  options = options || {};
  const rows = (options.rows || []).map((row) => ({ ...row }));
  const outputs = {};
  const messages = [];
  const validation = await execute(
    byName['Validate Invoice Request'].parameters.jsCode,
    sample.items,
    { getBinaryDataBuffer: async (_index, property) => {
      if (property !== 'pdf') throw new Error('unexpected binary property');
      return Buffer.from(sample.pdfBase64, 'base64');
    } },
    outputs,
  );
  outputs['Validate Invoice Request'] = validation;
  const requestItem = validation[0];
  if (!requestItem || !requestItem.json || requestItem.json.valid !== true) {
    return { response: respond('Respond With Request Rejected', requestItem, outputs), rows, messages };
  }

  const cryptoParameters = byName['Hash PDF SHA256'].parameters;
  const bytes = Buffer.from(sample.pdfBase64, 'base64');
  const hash = crypto.createHash(cryptoParameters.type.toLowerCase()).update(bytes).digest(cryptoParameters.encoding);
  const hashed = {
    json: { ...requestItem.json, [cryptoParameters.dataPropertyName]: hash },
    binary: requestItem.binary,
  };
  outputs['Hash PDF SHA256'] = [hashed];
  const base = { json: { ...requestItem.json, ...hashed.json }, binary: requestItem.binary };
  outputs['Merge Request And PDF Hash'] = [base];

  const getNode = byName['Get Existing Delivery'];
  const getFilter = getNode.parameters.filters.conditions[0];
  const deliveryId = expression(getFilter.keyValue, base, outputs);
  const matches = rows.filter((row) => row[getFilter.keyName] === deliveryId).slice(0, getNode.parameters.limit);
  outputs['Get Existing Delivery'] = matches.map((row) => ({ json: { ...row } }));
  const mergedStored = matches.length
    ? matches.map((row) => ({ json: { ...base.json, ...row }, binary: base.binary }))
    : [{ json: { ...base.json }, binary: base.binary }];
  outputs['Merge Request And Stored State'] = mergedStored;

  const decisions = await execute(byName['Decide Delivery'].parameters.jsCode, mergedStored, {}, outputs);
  outputs['Decide Delivery'] = decisions;
  const decision = decisions[0];
  const shouldSend = expression(
    byName['New Delivery Is Required'].parameters.conditions.conditions[0].leftValue,
    decision,
    outputs,
  );
  if (!shouldSend) {
    return { response: respond('Respond With Delivery Result', decision, outputs), rows, messages };
  }

  const insertNode = byName['Insert Pending Delivery'];
  const inserted = mappedValues(insertNode.parameters.columns.value, decision, outputs);
  inserted.id = rows.length + 1;
  rows.push(inserted);
  outputs['Insert Pending Delivery'] = [{ json: { ...inserted } }];
  const pending = { json: { ...decision.json, ...inserted }, binary: decision.binary };
  outputs['Merge Pending Intent And Invoice'] = [pending];

  const emailNode = byName['Send Invoice Email'];
  const attachmentName = emailNode.parameters.options.fileAttachments;
  if (!pending.binary || Object.keys(pending.binary).length !== 1 || !pending.binary[attachmentName]) {
    throw new Error('email did not receive the validated PDF binary');
  }
  messages.push({
    from: emailNode.parameters.fromEmail,
    to: emailNode.parameters.toEmail,
    subject: renderTemplate(emailNode.parameters.subject, pending, outputs),
    text: renderTemplate(emailNode.parameters.text, pending, outputs),
    attachmentName: pending.binary[attachmentName].fileName,
    attachment: bytes.toString('base64'),
  });

  if (options.sendFails) {
    const uncertainNode = byName['Mark Delivery Outcome Uncertain'];
    const filter = uncertainNode.parameters.filters.conditions[0];
    const keyValue = expression(filter.keyValue, pending, outputs);
    updateRows(rows, { keyName: filter.keyName, keyValue }, mappedValues(uncertainNode.parameters.columns.value, pending, outputs));
    return { response: respond('Respond With Email Uncertain', pending, outputs), rows, messages };
  }

  const acceptedNode = byName['Mark Delivery Accepted'];
  const acceptedFilter = acceptedNode.parameters.filters.conditions[0];
  const acceptedId = expression(acceptedFilter.keyValue, pending, outputs);
  outputs['Mark Delivery Accepted'] = options.dropAcceptedUpdate
    ? [{}]
    : updateRows(
      rows,
      { keyName: acceptedFilter.keyName, keyValue: acceptedId },
      mappedValues(acceptedNode.parameters.columns.value, pending, outputs),
    );

  const readbackNode = byName['Read Back Delivery Outcome'];
  const readbackFilter = readbackNode.parameters.filters.conditions[0];
  const readbackId = expression(readbackFilter.keyValue, pending, outputs);
  const readback = rows.filter((row) => row[readbackFilter.keyName] === readbackId)
    .slice(0, readbackNode.parameters.limit);
  outputs['Read Back Delivery Outcome'] = readback.length
    ? readback.map((row) => ({ json: { ...row } }))
    : [{}];
  const verified = await execute(
    byName['Verify Delivery Outcome'].parameters.jsCode,
    outputs['Read Back Delivery Outcome'],
    {},
    outputs,
  );
  return { response: respond('Respond With Delivery Result', verified[0], outputs), rows, messages };
}
(async () => {
  const validated = [];
  for (const sample of cases.validation) {
    try {
      const flow = await runFlow(sample);
      const output = await execute(
        byName['Validate Invoice Request'].parameters.jsCode,
        sample.items,
        { getBinaryDataBuffer: async (_index, property) => {
          if (property !== 'pdf') throw new Error('unexpected binary property');
          return Buffer.from(sample.pdfBase64, 'base64');
        } },
        {},
      );
      validated.push({ ok: true, output, response: flow.response });
    } catch (error) {
      validated.push({ ok: false, error: String(error && error.message || error) });
    }
  }
  const decisions = [];
  for (const items of cases.decisions) {
    try {
      decisions.push({ ok: true, output: await execute(byName['Decide Delivery'].parameters.jsCode, items, {}, {}) });
    } catch (error) {
      decisions.push({ ok: false, error: String(error && error.message || error) });
    }
  }
  const flows = [];
  for (const flowCase of cases.flows || []) {
    try {
      flows.push({ ok: true, ...await runFlow(flowCase.sample, flowCase.options) });
    } catch (error) {
      flows.push({ ok: false, error: String(error && error.message || error) });
    }
  }
  process.stdout.write(JSON.stringify({ validated, decisions, flows }));
})().catch((error) => {
  process.stderr.write(String(error && error.message || error));
  process.exitCode = 1;
});
"""


def multipart_item(metadata: object, *, binary: dict | None = None, extra_form_field: bool = False) -> dict:
    body = {"metadata": metadata}
    if extra_form_field:
        body["unexpected"] = "value"
    return {
        "json": {"body": body},
        "binary": binary if binary is not None else {
            "pdf": {"fileName": "2026-10-02.pdf", "mimeType": "application/pdf", "data": "stub"},
        },
    }


def validation_sample(
    metadata: object,
    *,
    binary: dict | None = None,
    payload: bytes = PDF_BYTES,
    extra_form_field: bool = False,
    item_count: int = 1,
) -> dict:
    item = multipart_item(metadata, binary=binary, extra_form_field=extra_form_field)
    return {
        "items": [item for _ in range(item_count)],
        "pdfBase64": base64.b64encode(payload).decode("ascii"),
    }


def run_harness(cases: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("a Node runtime is required to execute the committed Code nodes")
    with tempfile.TemporaryDirectory() as temporary:
        cases_path = Path(temporary) / "cases.json"
        cases_path.write_text(json.dumps(cases), encoding="utf-8")
        harness_path = Path(temporary) / "harness.js"
        harness_path.write_text(HARNESS, encoding="utf-8")
        completed = subprocess.run(
            [node, str(harness_path), str(WORKFLOW), str(cases_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


class InvoiceDeliveryExportShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        cls.nodes = cls.workflow["nodes"]
        cls.by_name = {node["name"]: node for node in cls.nodes}

    def edges(self) -> set[tuple[str, int, str, int]]:
        return {
            (source, output, link["node"], link["index"])
            for source, connection in self.workflow["connections"].items()
            for output, branch in enumerate(connection["main"])
            for link in branch
        }

    def test_inactive_and_credential_free_with_closed_node_surface(self) -> None:
        self.assertIs(self.workflow["active"], False)
        self.assertIs(self.workflow["settings"]["availableInMCP"], False)
        self.assertEqual("none", self.workflow["settings"]["saveDataSuccessExecution"])
        self.assertEqual("none", self.workflow["settings"]["saveDataErrorExecution"])
        self.assertIs(self.workflow["settings"]["saveManualExecutions"], False)
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        for node in self.nodes:
            self.assertIn(node["type"], ALLOWED_TYPES)
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])

    def test_webhook_and_result_contract(self) -> None:
        webhook = self.by_name["EnergyGrid Invoice Webhook"]["parameters"]
        self.assertEqual("POST", webhook["httpMethod"])
        self.assertEqual("headerAuth", webhook["authentication"])
        self.assertEqual("responseNode", webhook["responseMode"])
        self.assertEqual("REPLACE_WITH_INVOICE_DELIVERY_WEBHOOK_PATH", webhook["path"])
        for name in (
            "Respond With Delivery Result",
            "Respond With Request Rejected",
            "Respond With Email Uncertain",
            "Respond With Bounded Failure",
        ):
            responder = self.by_name[name]
            self.assertEqual("json", responder["parameters"]["respondWith"])
            self.assertEqual("n8n-nodes-base.respondToWebhook", responder["type"])
        self.assertIn("response_code", self.by_name["Decide Delivery"]["parameters"]["jsCode"])

    def test_validation_hash_and_binary_preservation_chain(self) -> None:
        validate = self.by_name["Validate Invoice Request"]["parameters"]["jsCode"]
        for token in (
            "EG_MAIL_METADATA_DUPLICATE_KEY",
            "EG_MAIL_MULTIPART_FIELDS_INVALID",
            "EG_MAIL_BINARY_FIELDS_INVALID",
            "EG_MAIL_PDF_METADATA_INVALID",
            "getBinaryDataBuffer",
            "application/pdf",
            "15000000",
            "%%EOF",
        ):
            self.assertIn(token, validate)
        crypto = self.by_name["Hash PDF SHA256"]["parameters"]
        self.assertEqual("SHA256", crypto["type"])
        self.assertTrue(crypto["binaryData"])
        self.assertEqual("pdf", crypto["binaryPropertyName"])
        self.assertEqual("hex", crypto["encoding"])
        merge = self.by_name["Merge Request And PDF Hash"]["parameters"]
        self.assertEqual(("combine", "combineByPosition"), (merge["mode"], merge["combineBy"]))
        self.assertIn(
            ("Validate Invoice Request", 0, "Request Is Valid", 0),
            self.edges(),
        )
        self.assertIn(
            ("Request Is Valid", 0, "Merge Request And PDF Hash", 0),
            self.edges(),
        )
        self.assertIn(
            ("Hash PDF SHA256", 0, "Merge Request And PDF Hash", 1),
            self.edges(),
        )

    def test_secondary_ledger_dedup_and_one_nonretrying_email_node(self) -> None:
        email_nodes = [node for node in self.nodes if node["type"] == "n8n-nodes-base.emailSend"]
        self.assertEqual(1, len(email_nodes))
        email = email_nodes[0]
        self.assertIs(email["retryOnFail"], False)
        self.assertEqual("pdf", email["parameters"]["options"]["fileAttachments"])
        self.assertFalse(email["parameters"]["options"]["appendAttribution"])
        self.assertNotIn("attachments", email["parameters"]["options"])
        get = self.by_name["Get Existing Delivery"]
        self.assertEqual("get", get["parameters"]["operation"])
        self.assertIs(get["alwaysOutputData"], True)
        self.assertEqual(2, get["parameters"]["limit"])
        insert = self.by_name["Insert Pending Delivery"]
        self.assertEqual("insert", insert["parameters"]["operation"])
        self.assertEqual("update", self.by_name["Mark Delivery Accepted"]["parameters"]["operation"])
        uncertain = self.by_name["Mark Delivery Outcome Uncertain"]
        self.assertEqual("update", uncertain["parameters"]["operation"])
        self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", uncertain["parameters"]["columns"]["value"]["state"])
        self.assertIs(uncertain["alwaysOutputData"], True)
        self.assertIn(("Mark Delivery Outcome Uncertain", 0, "Respond With Email Uncertain", 0), self.edges())
        self.assertIn(("Mark Delivery Outcome Uncertain", 1, "Respond With Bounded Failure", 0), self.edges())
        readback = self.by_name["Read Back Delivery Outcome"]
        self.assertEqual(("get", 2), (readback["parameters"]["operation"], readback["parameters"]["limit"]))
        self.assertIs(readback["alwaysOutputData"], True)
        self.assertIn(("Mark Delivery Accepted", 0, "Read Back Delivery Outcome", 0), self.edges())
        self.assertIn(("Read Back Delivery Outcome", 0, "Verify Delivery Outcome", 0), self.edges())
        verify = self.by_name["Verify Delivery Outcome"]["parameters"]["jsCode"]
        for token in ("DELIVERED", "DELIVERY_OUTCOME_UNCERTAIN", "EG_MAIL_OUTCOME_NOT_DURABLE"):
            self.assertIn(token, verify)
        decision = self.by_name["Decide Delivery"]["parameters"]["jsCode"]
        for token in ("ALREADY_DELIVERED", "DELIVERY_OUTCOME_UNCERTAIN", "EG_MAIL_DELIVERY_ID_CONFLICT"):
            self.assertIn(token, decision)
        boundary = self.by_name["Boundary"]["parameters"]["content"].lower()
        self.assertIn("not atomic", boundary)
        self.assertIn("local sqlite", boundary)
        self.assertIn("never resent", boundary)

    def test_every_fallible_node_routes_its_error_output_to_bounded_response(self) -> None:
        edges = self.edges()
        names = (
            "Validate Invoice Request", "Request Is Valid", "Hash PDF SHA256",
            "Merge Request And PDF Hash", "Get Existing Delivery",
            "Merge Request And Stored State", "Decide Delivery", "New Delivery Is Required",
            "Insert Pending Delivery", "Merge Pending Intent And Invoice", "Send Invoice Email",
            "Merge Successful Email And Intent", "Mark Delivery Accepted", "Read Back Delivery Outcome",
            "Verify Delivery Outcome", "Mark Delivery Outcome Uncertain",
        )
        for name in names:
            node = self.by_name[name]
            self.assertEqual("continueErrorOutput", node.get("onError"), name)
            error_output = 2 if node["type"] == "n8n-nodes-base.if" else 1
            destination = "Mark Delivery Outcome Uncertain" if name == "Send Invoice Email" else "Respond With Bounded Failure"
            self.assertIn((name, error_output, destination, 0), edges, name)
        for node in self.nodes:
            if node["type"] in {"n8n-nodes-base.code", "n8n-nodes-base.crypto", "n8n-nodes-base.merge", "n8n-nodes-base.dataTable", "n8n-nodes-base.if", "n8n-nodes-base.emailSend"}:
                self.assertEqual("continueErrorOutput", node.get("onError"), node["name"])

    def test_successful_email_is_answered_only_after_ledger_readback(self) -> None:
        accepted_edge = ("Verify Delivery Outcome", 0, "Respond With Delivery Result", 0)
        self.assertIn(accepted_edge, self.edges())
        self.assertNotIn("Respond With Delivery Accepted", self.by_name)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code nodes")
class InvoiceDeliveryCodeExecution(unittest.TestCase):
    def test_multipart_validator_accepts_one_matching_pdf_and_rejects_boundary_changes(self) -> None:
        extra = {**BASE_METADATA, "unexpected": "value"}
        wrong_size = {**BASE_METADATA, "pdf_byte_size": 15_000_001}
        duplicate_keys = (
            '{"schema":"energygrid.invoice_delivery.v1","delivery_id":"' + DELIVERY_ID +
            '","delivery_id":"' + DELIVERY_ID + '","run_id":"' + RUN_ID +
            '","stream":"EB_BILL","bill_date":"2026-10-02","attachment_name":"2026-10-02.pdf",' +
            '"pdf_byte_size":' + str(len(PDF_BYTES)) + ',"pdf_sha256":"' + PDF_SHA256 + '"}'
        )
        extra_binary = {
            "pdf": {"fileName": "2026-10-02.pdf", "mimeType": "application/pdf", "data": "stub"},
            "other": {"fileName": "extra.pdf", "mimeType": "application/pdf", "data": "stub"},
        }
        wrong_name = {"pdf": {"fileName": "other.pdf", "mimeType": "application/pdf", "data": "stub"}}
        bad_tail = b"%PDF-1.4\nsynthetic invoice bytes\n"
        samples = [
            validation_sample(json.dumps(BASE_METADATA)),
            validation_sample(json.dumps(BASE_METADATA), item_count=0),
            validation_sample(json.dumps(BASE_METADATA), item_count=2),
            validation_sample(json.dumps(BASE_METADATA), extra_form_field=True),
            validation_sample(duplicate_keys),
            validation_sample(json.dumps(extra)),
            validation_sample(json.dumps(wrong_size)),
            validation_sample(json.dumps(BASE_METADATA), binary=extra_binary),
            validation_sample(json.dumps(BASE_METADATA), binary=wrong_name),
            validation_sample(json.dumps(BASE_METADATA), payload=bad_tail),
            validation_sample("[]"),
        ]
        result = run_harness({"validation": samples, "decisions": []})["validated"]
        accepted = result[0]["output"][0]
        self.assertTrue(accepted["json"]["valid"])
        self.assertEqual(BASE_METADATA, accepted["json"]["request"])
        self.assertEqual({"pdf"}, set(accepted["binary"]))
        for sample, answer in zip(samples[1:], result[1:]):
            with self.subTest(metadata=str(sample["items"])[:50]):
                self.assertTrue(answer["ok"], answer)
                rejected = answer["output"][0]["json"]
                self.assertFalse(rejected["valid"])
                self.assertEqual(422, rejected["response_code"])
                self.assertEqual(RESULT_KEYS, set(rejected["response"]))
        for answer in result[1:3]:
            self.assertEqual("EG_MAIL_REQUEST_SHAPE", answer["output"][0]["json"]["response"]["support_ref"])

    def test_delivery_decision_never_resends_existing_or_uncertain_rows(self) -> None:
        base = {
            "request": BASE_METADATA,
            "data": PDF_SHA256,
            "pdf_sha256": PDF_SHA256,
            "should_send": True,
        }
        delivered = {
            **base, "id": 7, "delivery_id": DELIVERY_ID, "run_id": RUN_ID,
            "stream": "EB_BILL", "bill_date": "2026-10-02",
            "attachment_name": "2026-10-02.pdf", "pdf_sha256": PDF_SHA256,
            "pdf_byte_size": len(PDF_BYTES), "state": "DELIVERED", "support_ref": "EG_MAIL_ACCEPTED",
        }
        pending = {**delivered, "state": "PENDING_SEND", "support_ref": "EG_MAIL_PENDING"}
        conflict = {**delivered, "pdf_sha256": "0" * 64}
        result = run_harness({
            "validation": [],
            "decisions": [
                [{"json": base, "binary": {"pdf": {"fileName": "2026-10-02.pdf"}}}],
                [{"json": delivered}],
                [{"json": pending}],
                [{"json": conflict}],
                [{"json": {**base, "data": "0" * 64}}],
                [{"json": base}, {"json": base}],
            ],
        })["decisions"]
        self.assertTrue(result[0]["output"][0]["json"]["should_send"])
        self.assertEqual(("ALREADY_DELIVERED", True, 200), (
            result[1]["output"][0]["json"]["response"]["outcome"],
            result[1]["output"][0]["json"]["response"]["duplicate"],
            result[1]["output"][0]["json"]["response_code"],
        ))
        self.assertEqual("DELIVERY_OUTCOME_UNCERTAIN", result[2]["output"][0]["json"]["response"]["outcome"])
        self.assertFalse(result[2]["output"][0]["json"]["should_send"])
        self.assertEqual(409, result[3]["output"][0]["json"]["response_code"])
        self.assertEqual(422, result[4]["output"][0]["json"]["response_code"])
        self.assertEqual(503, result[5]["output"][0]["json"]["response_code"])

    def test_mocked_ledger_and_smtp_flow_persists_readback_and_never_resends(self) -> None:
        sample = validation_sample(json.dumps(BASE_METADATA))

        def ledger_row(state: str) -> dict:
            return {
                "id": 41,
                "delivery_id": DELIVERY_ID,
                "run_id": RUN_ID,
                "stream": BASE_METADATA["stream"],
                "bill_date": BASE_METADATA["bill_date"],
                "attachment_name": BASE_METADATA["attachment_name"],
                "pdf_sha256": PDF_SHA256,
                "pdf_byte_size": len(PDF_BYTES),
                "state": state,
                "support_ref": "EG_MAIL_SYNTHETIC",
            }

        flows = run_harness({
            "validation": [],
            "decisions": [],
            "flows": [
                {"sample": sample},
                {"sample": sample, "options": {"rows": [ledger_row("DELIVERED")]}},
                {"sample": sample, "options": {"rows": [ledger_row("DELIVERY_OUTCOME_UNCERTAIN")]}},
                {"sample": sample, "options": {"sendFails": True}},
                {"sample": sample, "options": {"dropAcceptedUpdate": True}},
            ],
        })["flows"]
        self.assertTrue(all(flow["ok"] for flow in flows), flows)

        sent = flows[0]
        self.assertEqual((200, "DELIVERED"), (sent["response"]["status"], sent["response"]["body"]["outcome"]))
        self.assertEqual(1, len(sent["messages"]))
        self.assertEqual("REPLACE_WITH_APPROVED_SENDER", sent["messages"][0]["from"])
        self.assertEqual("REPLACE_WITH_APPROVED_RECIPIENT", sent["messages"][0]["to"])
        self.assertEqual("2026-10-02.pdf", sent["messages"][0]["attachmentName"])
        self.assertEqual(base64.b64encode(PDF_BYTES).decode("ascii"), sent["messages"][0]["attachment"])
        self.assertEqual(("DELIVERED", "EG_MAIL_ACCEPTED"), (sent["rows"][0]["state"], sent["rows"][0]["support_ref"]))

        duplicate = flows[1]
        self.assertEqual((200, "ALREADY_DELIVERED", True), (
            duplicate["response"]["status"],
            duplicate["response"]["body"]["outcome"],
            duplicate["response"]["body"]["duplicate"],
        ))
        self.assertEqual([], duplicate["messages"])
        uncertain = flows[2]
        self.assertEqual((503, "DELIVERY_OUTCOME_UNCERTAIN"), (
            uncertain["response"]["status"],
            uncertain["response"]["body"]["outcome"],
        ))
        self.assertEqual([], uncertain["messages"])

        send_error = flows[3]
        self.assertEqual((503, "DELIVERY_OUTCOME_UNCERTAIN"), (
            send_error["response"]["status"],
            send_error["response"]["body"]["outcome"],
        ))
        self.assertEqual(("DELIVERY_OUTCOME_UNCERTAIN", "EG_MAIL_SEND_UNCERTAIN"), (
            send_error["rows"][0]["state"],
            send_error["rows"][0]["support_ref"],
        ))
        self.assertEqual(1, len(send_error["messages"]))

        lost_update = flows[4]
        self.assertEqual((503, "DELIVERY_OUTCOME_UNCERTAIN", "EG_MAIL_OUTCOME_NOT_DURABLE"), (
            lost_update["response"]["status"],
            lost_update["response"]["body"]["outcome"],
            lost_update["response"]["body"]["support_ref"],
        ))


if __name__ == "__main__":
    unittest.main()
