"""Offline contracts for the inactive, manual-only EnergyGrid Drive destination setup export (#226 G3)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / "n8n-workflows" / "energygrid_drive_destination_setup.workflow.json"
UPLOAD_WORKFLOW = REPO_ROOT / "n8n-workflows" / "energygrid_drive_upload.workflow.json"
ALLOWED_TYPES = {
    "n8n-nodes-base.manualTrigger", "n8n-nodes-base.code", "n8n-nodes-base.if",
    "n8n-nodes-base.httpRequest", "n8n-nodes-base.stickyNote",
}

HARNESS = r"""
const fs = require('fs');
const [workflowPath, casesPath] = process.argv.slice(2);
const wf = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const byName = Object.fromEntries(wf.nodes.map((node) => [node.name, node]));
(async () => {
  const results = [];
  for (const testCase of cases) {
    const refs = testCase.refs || {};
    const lookup = (name) => {
      if (!(name in refs)) throw new Error('node not executed: ' + name);
      return { first: () => ({ json: refs[name] }) };
    };
    const fn = new (Object.getPrototypeOf(async function () {}).constructor)('$input', '$', byName[testCase.node].parameters.jsCode);
    try {
      results.push({ ok: true, output: await fn({ first: () => ({ json: testCase.input }), all: () => [{ json: testCase.input }] }, lookup) });
    } catch (error) {
      results.push({ ok: false, error: String(error && error.message || error) });
    }
  }
  process.stdout.write(JSON.stringify(results));
})();
"""


def run(cases):
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


def listing(files, *, incomplete=False):
    return {"statusCode": 200, "body": {"incompleteSearch": incomplete, "files": files}}


FOLDER = "application/vnd.google-apps.folder"


class DestinationSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = json.loads(cls.raw)
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_inactive_manual_only_and_saves_nothing(self) -> None:
        self.assertIs(False, self.workflow["active"])
        self.assertIsNone(self.workflow["staticData"])
        self.assertIs(False, self.workflow["settings"]["availableInMCP"])
        self.assertEqual(("none", "none", False), (self.workflow["settings"]["saveDataErrorExecution"],
                                                  self.workflow["settings"]["saveDataSuccessExecution"],
                                                  self.workflow["settings"]["saveManualExecutions"]))
        triggers = [node for node in self.workflow["nodes"] if "trigger" in node["type"].lower() or node["type"].endswith("webhook")]
        self.assertEqual(["n8n-nodes-base.manualTrigger"], [node["type"] for node in triggers])

    def test_closed_surface_with_drive_credential_type_only(self) -> None:
        for node in self.workflow["nodes"]:
            with self.subTest(node=node["name"]):
                self.assertIn(node["type"], ALLOWED_TYPES)
                self.assertNotIn("credentials", node)
                self.assertNotIn("webhookId", node)
                if node["type"] == "n8n-nodes-base.httpRequest":
                    self.assertEqual(("predefinedCredentialType", "googleDriveOAuth2Api"),
                                     (node["parameters"]["authentication"], node["parameters"]["nodeCredentialType"]))
                    self.assertIs(False, node["retryOnFail"])
        self.assertNotIn("googleOAuth2Api", self.raw.replace("googleDriveOAuth2Api", ""))

    def test_never_deletes_and_creates_only_the_two_child_folders_once(self) -> None:
        methods = [(node["name"], node["parameters"]["method"]) for node in self.workflow["nodes"]
                   if node["type"] == "n8n-nodes-base.httpRequest"]
        posts = sorted(name for name, method in methods if method == "POST")
        self.assertEqual(["Create EB Bill Folder", "Create Tenant Bill Folder"], posts)
        self.assertFalse([name for name, method in methods if method in {"DELETE", "PATCH", "PUT"}])
        for name in posts:
            body = self.nodes[name]["parameters"]["jsonBody"]
            self.assertIn("mimeType: 'application/vnd.google-apps.folder'", body)
            self.assertIn("egKind: 'dest-v3'", body)

    def test_not_called_by_the_upload_workflow(self) -> None:
        upload = UPLOAD_WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("Drive Destination Setup", upload)
        self.assertNotIn("executeWorkflow", upload)

    def test_chain_check_holds_on_missing_ambiguous_or_incomplete(self) -> None:
        prior = {"account_ref": "0123", "parent": "root"}
        good = listing([{"id": "synthAutomationFolder1", "mimeType": FOLDER, "trashed": False}])
        cases = [
            {"node": "Check Automation Folder", "input": good, "refs": {"Record Account": prior}},
            {"node": "Check Automation Folder", "input": listing([]), "refs": {"Record Account": prior}},
            {"node": "Check Automation Folder", "input": listing([{"id": "a" * 12, "mimeType": FOLDER, "trashed": False}] * 2), "refs": {"Record Account": prior}},
            {"node": "Check Automation Folder", "input": listing([{"id": "a" * 12, "mimeType": "application/vnd.google-apps.shortcut", "trashed": False}]), "refs": {"Record Account": prior}},
            {"node": "Check Automation Folder", "input": listing([], incomplete=True), "refs": {"Record Account": prior}},
        ]
        results = run(cases)
        self.assertTrue(results[0]["ok"])
        self.assertEqual("synthAutomationFolder1", results[0]["output"][0]["json"]["automation_id"])
        self.assertEqual(
            ["EG_DRIVE_CHAIN_MISSING", "EG_DRIVE_CHAIN_AMBIGUOUS", "EG_DRIVE_CHAIN_AMBIGUOUS", "EG_DRIVE_SEARCH_INCOMPLETE"],
            [item["error"] for item in results[1:]],
        )

    def test_child_decision_creates_only_when_absent(self) -> None:
        results = run([
            {"node": "Decide EB Bill Folder", "input": listing([])},
            {"node": "Decide EB Bill Folder", "input": listing([{"id": "synthEbBillFolder00001", "mimeType": FOLDER, "trashed": False}])},
            {"node": "Decide EB Bill Folder", "input": listing([{"id": "x" * 12, "mimeType": FOLDER, "trashed": False}] * 2)},
        ])
        self.assertEqual({"create_required": True}, results[0]["output"][0]["json"])
        self.assertEqual({"create_required": False, "folder_id": "synthEbBillFolder00001"}, results[1]["output"][0]["json"])
        self.assertEqual("EG_DRIVE_CHILD_AMBIGUOUS", results[2]["error"])


if __name__ == "__main__":
    unittest.main()
