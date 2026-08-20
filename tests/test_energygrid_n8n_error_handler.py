"""Focused offline coverage for the source-controlled EnergyGrid n8n error handler.

The committed export (`n8n-workflows/energygrid_download_error_handler.workflow.json`)
is design evidence, not proof of deployment: no case here contacts n8n, Telegram, the
Energy@Grid portal, or any network, and no credential is required.

Two layers of proof:

* Structural cases assert the committed JSON directly - the sanitisation contract (no
  credential binding, no live destination, no webhook or instance identity), the
  four-node design, and the absence of any raw whole-payload capture.
* Behavioural cases execute the committed Set/Code/Telegram expressions verbatim in
  Node against synthetic Error Trigger payloads, so the rendered alert is verified
  rather than pattern-matched. They skip only when no Node runtime is present; every
  mandatory contract above is proven by the structural layer regardless.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows" / "energygrid_download_error_handler.workflow.json"
README = ROOT / "n8n-workflows" / "README.md"

WORKFLOW_NAME = "Error Handler - EnergyGrid Download"

# The operator replaces this in their separately authorised local import copy. The real
# destination is never committed, so the test asserts the placeholder, not a value.
CHAT_ID_PLACEHOLDER = "REPLACE_WITH_TELEGRAM_CHAT_ID"

NODE_CHAIN = [
    "Error Trigger",
    "Build Error Row",
    "Build Safe Error Alert Context",
    "Send EnergyGrid Failure Alert",
]

ALLOWED_NODE_TYPES = {
    "n8n-nodes-base.errorTrigger",
    "n8n-nodes-base.set",
    "n8n-nodes-base.code",
    "n8n-nodes-base.telegram",
}

# The accepted four-node design needs none of these. Any of them appearing would mean a
# live-action surface was added to a handler that must only normalise and notify.
FORBIDDEN_NODE_TYPE_TOKENS = [
    "webhook",
    "scheduleTrigger",
    "cron",
    "interval",
    "executeCommand",
    "httpRequest",
    "ssh",
    "ftp",
    "postgres",
    "mySql",
    "microsoftSql",
    "snowflake",
    "executeWorkflow",
    "readWriteFile",
    "spreadsheetFile",
]

BOUNDED_ROW_FIELDS = [
    "logged_date",
    "logged_time",
    "contact_id",
    "error_type",
    "error_message",
    "error_workflow_name",
    "last_node_executed",
    "execution_id",
    "execution_url",
    "source",
    "event_type",
    "failure_stage",
    "support_ref",
    "run_id",
    "exit_code",
    "failure_feedback_enabled",
]

ENERGYGRID_FIELDS = ["source", "event_type", "failure_stage", "support_ref", "run_id", "exit_code"]

SAFE_FIELDS = [
    "safe_contact_id",
    "safe_source",
    "safe_event_type",
    "safe_failure_stage",
    "safe_error_type",
    "safe_error_message",
    "safe_error_workflow_name",
    "safe_last_node_executed",
    "safe_execution_id",
    "safe_execution_url",
    "safe_support_ref",
    "safe_run_id",
    "safe_exit_code",
]

# Unrestricted raw-payload capture was deliberately removed and must never return, in any
# spelling, including a renamed replacement that stringifies the whole Error Trigger input.
FORBIDDEN_PAYLOAD_FIELDS = ["payload_json", "safe_payload_json", "sheet_payload_json"]

# Node-level settings that would report a failed Telegram delivery as a success.
MASKING_KEYS = ["onError", "continueOnFail", "alwaysOutputData", "retryOnFail"]

HEARTBEAT_EVENT = "scheduler_heartbeat_missing"
HEARTBEAT_LABEL = "Scheduled job did not run / heartbeat missing"
FAILURE_LABEL = "Download job failed"

# Executes the committed expressions verbatim: the workflow file is the only source of
# logic here, so the cases cannot drift away from what is actually committed.
HARNESS_JS = r"""
'use strict';
const fs = require('fs');
const wf = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const byName = (name) => wf.nodes.find((node) => node.name === name);

// $now is the only n8n global the Set node uses; a fixed stub keeps output deterministic.
const nowStub = {
  setZone: () => ({ toFormat: (fmt) => (fmt === 'yyyy-MM-dd' ? '2026-08-20' : '09:15:42') }),
};

function evalExpression(expr, json) {
  const body = expr.slice(3, -2);
  return new Function('$json', '$now', 'return (' + body + ');')(json, nowStub);
}

function buildRow(trigger) {
  const row = {};
  for (const assignment of byName('Build Error Row').parameters.assignments.assignments) {
    row[assignment.name] = evalExpression(assignment.value, trigger);
  }
  return row;
}

function buildSafeContext(row) {
  const code = byName('Build Safe Error Alert Context').parameters.jsCode;
  return new Function('$input', code)({ all: () => [{ json: row }] })[0].json;
}

function renderAlert(json) {
  const expr = byName('Send EnergyGrid Failure Alert').parameters.text;
  return new Function('$json', 'return (' + expr.slice(3, -2) + ');')(json);
}

const scenarios = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = {};
for (const [label, trigger] of Object.entries(scenarios)) {
  const row = buildRow(trigger);
  out[label] = { row: row, message: renderAlert(buildSafeContext(row)) };
}
process.stdout.write(JSON.stringify(out));
"""

SCENARIOS = {
    "execution_failure": {
        "workflow": {"name": "EnergyGrid Download Ingress"},
        "execution": {
            "id": "4821",
            "url": "https://n8n.invalid/execution/4821",
            "lastNodeExecuted": "Report Launcher Failure",
            "error": {
                "name": "downloader_launch_failed",
                "message": "EnergyGrid downloader exited with a non-zero status.",
                "context": {
                    "metadata": {
                        "source": "energygrid",
                        "event_type": "execution_failure",
                        "failure_stage": "downloader_launch",
                        "support_ref": "EG-SYNTHETIC-0001",
                        "run_id": "run-synthetic-0001",
                        "exit_code": 3,
                    }
                },
            },
        },
    },
    "heartbeat_missing": {
        "workflow": {"name": "EnergyGrid Heartbeat Watchdog"},
        "execution": {
            "id": "4822",
            "error": {
                "name": HEARTBEAT_EVENT,
                "message": "No EnergyGrid scheduler heartbeat was received in the expected window.",
                "context": {
                    "metadata": {
                        "source": "energygrid",
                        "event_type": HEARTBEAT_EVENT,
                        "failure_stage": "scheduler_start",
                        "support_ref": "EG-SYNTHETIC-0002",
                        "run_id": "",
                        "exit_code": "",
                    }
                },
            },
        },
    },
    "exit_code_zero": {
        "execution": {
            "error": {
                "name": "downloader_reported_success_with_no_file",
                "message": "Downloader reported success but no statement file was produced.",
                "context": {"metadata": {"failure_stage": "artifact_check", "exit_code": 0}},
            }
        }
    },
    "hostile_text": {
        "execution": {
            "error": {
                "name": "portal_error",
                "message": '<b>fail</b> & "quoted" <script>alert(1)</script>',
            }
        }
    },
    "bare_payload": {"execution": {"error": {}}},
}


def load_workflow() -> dict:
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


def node_named(workflow: dict, name: str) -> dict:
    return next(node for node in workflow["nodes"] if node["name"] == name)


class CommittedExportContractTests(unittest.TestCase):
    """The committed JSON must parse and hold the accepted four-node design."""

    def setUp(self) -> None:
        self.raw = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = json.loads(self.raw)

    def test_export_parses_with_the_locked_name_and_stays_inactive(self) -> None:
        self.assertEqual(self.workflow["name"], WORKFLOW_NAME)
        self.assertIs(self.workflow["active"], False)

    def test_exactly_the_four_designed_nodes_exist(self) -> None:
        self.assertEqual([node["name"] for node in self.workflow["nodes"]], NODE_CHAIN)
        self.assertEqual(
            {node["type"] for node in self.workflow["nodes"]}, ALLOWED_NODE_TYPES
        )

    def test_connections_form_the_intended_linear_chain(self) -> None:
        edges = []
        for source, outputs in self.workflow["connections"].items():
            for output in outputs["main"]:
                for link in output:
                    edges.append((source, link["node"]))
        self.assertEqual(sorted(edges), sorted(zip(NODE_CHAIN, NODE_CHAIN[1:])))
        for source, target in edges:
            self.assertIn(source, NODE_CHAIN)
            self.assertIn(target, NODE_CHAIN)

    def test_no_live_action_node_type_was_added(self) -> None:
        for node in self.workflow["nodes"]:
            for token in FORBIDDEN_NODE_TYPE_TOKENS:
                self.assertNotIn(
                    token.lower(), node["type"].lower(), f"{node['name']} added a live-action node"
                )


class SanitisationTests(unittest.TestCase):
    """No live-instance identity or binding may reach source control."""

    def setUp(self) -> None:
        self.raw = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = json.loads(self.raw)
        self.telegram = node_named(self.workflow, "Send EnergyGrid Failure Alert")

    def test_no_credential_binding_is_committed(self) -> None:
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
        self.assertNotIn('"credentials"', self.raw)
        self.assertIs(self.workflow["meta"]["templateCredsSetupCompleted"], False)

    def test_no_webhook_or_instance_identity_is_committed(self) -> None:
        self.assertNotIn("webhookId", self.raw)
        self.assertNotIn("instanceId", self.raw)
        for node in self.workflow["nodes"]:
            self.assertNotIn("webhookId", node, node["name"])

    def test_telegram_destination_is_the_placeholder_and_never_a_real_value(self) -> None:
        self.assertEqual(self.telegram["parameters"]["chatId"], CHAT_ID_PLACEHOLDER)
        # A committed destination would be a numeric chat id, with or without the n8n
        # expression prefix. Asserting the shape avoids putting a real value in this test.
        self.assertIsNone(
            re.search(r'"chatId":\s*"=?\s*-?\d{4,}', self.raw),
            "a numeric Telegram destination must never be committed",
        )

    def test_no_pinned_or_execution_data_is_committed(self) -> None:
        self.assertIn(self.workflow.get("pinData"), (None, {}))
        self.assertNotIn("resultData", self.raw)
        self.assertNotIn("runData", self.raw)

    def test_repo_local_identity_only(self) -> None:
        # Sibling exports carry repo-local ids; what matters is that neither value is the
        # live export identity, which is what `sourceWorkflowId: null` also records.
        self.assertEqual(self.workflow["id"], "energygridErrHandler01")
        self.assertIsNone(self.workflow["sourceWorkflowId"])


class BoundedMetadataTests(unittest.TestCase):
    """Only bounded operational metadata is retained - never the raw payload."""

    def setUp(self) -> None:
        self.raw = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = json.loads(self.raw)
        self.row = node_named(self.workflow, "Build Error Row")
        self.code = node_named(self.workflow, "Build Safe Error Alert Context")

    def test_bounded_fields_are_present_in_the_designed_order(self) -> None:
        names = [a["name"] for a in self.row["parameters"]["assignments"]["assignments"]]
        self.assertEqual(names, BOUNDED_ROW_FIELDS)
        for field in ENERGYGRID_FIELDS:
            self.assertIn(field, names)

    def test_safe_escaped_variants_exist_for_every_displayed_value(self) -> None:
        js_code = self.code["parameters"]["jsCode"]
        for field in SAFE_FIELDS:
            self.assertIn(field, js_code)

    def test_raw_whole_payload_capture_is_absent(self) -> None:
        for field in FORBIDDEN_PAYLOAD_FIELDS:
            self.assertNotIn(field, self.raw)
        # No renamed replacement either: nothing may stringify the whole trigger input.
        self.assertNotIn("JSON.stringify($json)", self.raw)

    def test_html_escaping_logic_is_preserved(self) -> None:
        js_code = self.code["parameters"]["jsCode"]
        for replacement in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(replacement, js_code)
        self.assertIn("function escapeHtml(value)", js_code)


class TelegramAlertContractTests(unittest.TestCase):
    """The alert must stay an internal operations message that cannot mask its own failure."""

    def setUp(self) -> None:
        self.workflow = load_workflow()
        self.telegram = node_named(self.workflow, "Send EnergyGrid Failure Alert")
        self.text = self.telegram["parameters"]["text"]

    def test_message_options_are_preserved(self) -> None:
        self.assertEqual(
            self.telegram["parameters"]["additionalFields"],
            {"appendAttribution": False, "disable_web_page_preview": True, "parse_mode": "HTML"},
        )

    def test_delivery_failure_is_never_masked_as_success(self) -> None:
        for key in MASKING_KEYS:
            self.assertNotIn(key, self.telegram, f"{key} would hide a failed Telegram delivery")
        self.assertNotIn("continueRegularOutput", json.dumps(self.workflow))

    def test_both_event_branches_are_committed(self) -> None:
        self.assertIn(HEARTBEAT_EVENT, self.text)
        self.assertIn(HEARTBEAT_LABEL, self.text)
        self.assertIn(FAILURE_LABEL, self.text)

    def test_optional_lines_are_conditional_not_blank(self) -> None:
        for guard in ("if (supportRef)", "if (runId)", "if (exitCode)", "if (executionId)"):
            self.assertIn(guard, self.text)

    def test_exit_code_normalisation_survives_a_numeric_zero(self) -> None:
        expression = next(
            a["value"]
            for a in node_named(self.workflow, "Build Error Row")["parameters"]["assignments"][
                "assignments"
            ]
            if a["name"] == "exit_code"
        )
        # `??` rather than `||`, so exit code 0 is kept instead of coalesced away.
        self.assertIn("??", expression)
        self.assertIn("String(raw)", expression)
        self.assertNotIn("|| $json.exit_code", expression)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute n8n expressions")
class RenderedAlertTests(unittest.TestCase):
    """Execute the committed expressions against synthetic payloads - no n8n involved."""

    rendered: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "harness.js"
            scenarios = Path(directory) / "scenarios.json"
            harness.write_text(HARNESS_JS, encoding="utf-8")
            scenarios.write_text(json.dumps(SCENARIOS), encoding="utf-8")
            result = subprocess.run(
                [shutil.which("node"), str(harness), str(WORKFLOW), str(scenarios)],
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode != 0:
            raise AssertionError(f"committed expressions failed to execute: {result.stderr}")
        cls.rendered = json.loads(result.stdout)

    def test_ordinary_execution_failure_renders_the_bounded_operations_alert(self) -> None:
        message = self.rendered["execution_failure"]["message"]
        self.assertIn("<b>", message.splitlines()[0])
        self.assertIn(f"Event: {FAILURE_LABEL}", message)
        self.assertIn("Stage: <code>downloader_launch</code>", message)
        self.assertIn("Reference: <code>EG-SYNTHETIC-0001</code>", message)
        self.assertIn("Run: <code>run-synthetic-0001</code>", message)
        self.assertIn("Exit code: <code>3</code>", message)
        self.assertIn("SGT", message)

    def test_missing_heartbeat_renders_its_own_event_label(self) -> None:
        message = self.rendered["heartbeat_missing"]["message"]
        self.assertIn(f"Event: {HEARTBEAT_LABEL}", message)
        self.assertIn("Stage: <code>scheduler_start</code>", message)
        self.assertNotIn(FAILURE_LABEL, message)

    def test_empty_optional_values_are_omitted_rather_than_shown_blank(self) -> None:
        heartbeat = self.rendered["heartbeat_missing"]["message"]
        self.assertNotIn("Run:", heartbeat)
        self.assertNotIn("Exit code:", heartbeat)
        bare = self.rendered["bare_payload"]["message"]
        self.assertNotIn("Reference:", bare)
        self.assertNotIn("n8n execution:", bare)

    def test_numeric_exit_code_zero_is_displayed(self) -> None:
        rendered = self.rendered["exit_code_zero"]
        self.assertEqual(rendered["row"]["exit_code"], "0")
        self.assertIn("Exit code: <code>0</code>", rendered["message"])

    def test_hostile_text_is_escaped_exactly_once(self) -> None:
        message = self.rendered["hostile_text"]["message"]
        self.assertIn("&lt;script&gt;", message)
        self.assertNotIn("<script>", message)
        self.assertIn("&quot;", message)
        self.assertNotIn("&amp;amp;", message)

    def test_defaults_apply_when_no_structured_metadata_is_supplied(self) -> None:
        row = self.rendered["bare_payload"]["row"]
        self.assertEqual(row["source"], "energygrid")
        self.assertEqual(row["event_type"], "execution_failure")
        self.assertEqual(row["failure_stage"], "unknown")
        self.assertEqual(row["exit_code"], "")

    def test_no_raw_payload_reaches_the_rendered_output(self) -> None:
        dumped = json.dumps(self.rendered)
        for field in FORBIDDEN_PAYLOAD_FIELDS:
            self.assertNotIn(field, dumped)


class DirectoryReadmeTests(unittest.TestCase):
    """The directory README must describe the export and hold the safety line."""

    def setUp(self) -> None:
        self.readme = README.read_text(encoding="utf-8")

    def test_readme_documents_the_new_export(self) -> None:
        self.assertIn("### energygrid_download_error_handler.workflow.json", self.readme)
        self.assertIn(CHAT_ID_PLACEHOLDER, self.readme)
        self.assertIn(HEARTBEAT_EVENT, self.readme)

    def test_readme_does_not_claim_deployment_or_expose_the_destination(self) -> None:
        self.assertIsNone(
            re.search(r"chat id[^\n]*\b\d{4,}", self.readme, re.IGNORECASE),
            "the live Telegram destination must not appear in the README",
        )


if __name__ == "__main__":
    unittest.main()
