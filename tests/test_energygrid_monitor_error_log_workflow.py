"""Offline contract of the inactive "EnergyGrid - Monitor Error Log" n8n workflow (#288 option (c)).

The run monitor saves no failed executions (a rejected webhook report would keep the valid Header
Auth token in n8n's execution data). n8n calls this error workflow instead, and it writes one row of
closed codes to a Data Table that XB Daily Health counts. No n8n instance, n8n API or Data Table is
contacted: the committed Code node is executed with the local Node runtime against Error Trigger items
shaped like the ones n8n 2.48.5 builds (packages/cli/src/execution-lifecycle/execute-error-workflow.ts).
Every value here is fake.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows" / "energygrid_monitor_error_log.workflow.json"
MONITOR = ROOT / "n8n-workflows" / "energygrid_local_failure_monitor.workflow.json"
MONITOR_README = ROOT / "n8n-workflows" / "energygrid_local_failure_monitor" / "README.md"
README = ROOT / "n8n-workflows" / "README.md"
CI = ROOT / ".github" / "workflows" / "energygrid-bill-downloader-tests.yml"
REDUCE = "Reduce To Closed Codes"
TABLE = "REPLACE_WITH_MONITOR_ERROR_LOG_TABLE_ID"
LINK = "REPLACE_WITH_ENERGYGRID_MONITOR_ERROR_LOG_WORKFLOW_ID"
COLUMNS = ["workflow_id", "execution_id", "failed_node", "code", "mode", "failed_at"]
NOW_MS = 1795046400000  # 2026-11-19T00:00:00Z
NOW_ISO = "2026-11-19T00:00:00.000Z"
SECRETS = ("fake-monitor-token-0000", "fake-private-path-0000", "FAKE_REPORT_BODY_0000", "fake-stack-0000")

HARNESS = r"""
const fs = require('fs');
const [workflowPath, casesPath] = process.argv.slice(2);
const workflow = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const code = workflow.nodes.find((n) => n.name === 'Reduce To Closed Codes').parameters.jsCode;
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const realNow = Date.now;
const results = cases.map((c) => {
  const $input = { all: () => c.items.map((json) => ({ json })) };
  Date.now = () => c.now_ms;
  try {
    return { ok: true, out: new Function('$input', code)($input) };
  } catch (error) {
    return { ok: false, error: String(error.message) };
  } finally {
    Date.now = realNow;
  }
});
process.stdout.write(JSON.stringify(results));
"""

# How n8n 2.48.5 turns an error thrown in a JavaScript Code node into execution.error.message (B1 of the
# #339 G2). The Code node always runs in the task runner (Code.node.ts, JsTaskRunnerSandbox). In secure
# mode js-task-runner.ts runs the code in a vm context wrapped as
# `module.exports = async function VmCodeWrapper() {<code>\n}()`; an Error made inside the vm is not a host
# Error, so toExecutionErrorIfNeeded builds an ExecutionError, and execution-error.ts sets
# message = "<details> [line N]" from the `evalmachine.<anonymous>:N` stack frame
# ("[line N, for item M]" when an item index is known). This harness runs the monitor's own
# "Validate Both Streams and Classify" code that way and ports populateFromStack, so the fixtures carry the
# exact message n8n would hand to the Error Trigger, not the bare code.
N8N_MESSAGE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [monitorPath, casesPath] = process.argv.slice(2);
const monitor = JSON.parse(fs.readFileSync(monitorPath, 'utf8'));
const code = monitor.nodes.find((n) => n.name === 'Validate Both Streams and Classify').parameters.jsCode;
const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const STACK_HEADER_REGEX = /^(?<errorType>\S.*?): (?<errorDetails>.+)$/;
const STACK_FRAME_REGEX = /^\s+at\s/;
const SOURCE_EXCERPT_CARET_REGEX = /^\s*\^+\s*$/;
function executionErrorMessage(error, itemIndex) {
  const rows = String(error.stack || '').split(/\r?\n/);
  let line;
  const userFn = rows.find((r) => /\(evalmachine\.<anonymous>:\d+:\d+\)/.test(r) && !r.includes('VmCodeWrapper'));
  if (userFn) line = Number(userFn.match(/evalmachine\.<anonymous>:(\d+):/)[1]);
  if (line === undefined) {
    const top = rows.find((r) => r.includes('VmCodeWrapper') && r.includes('evalmachine.<anonymous>'));
    if (top) line = Number(top.match(/evalmachine\.<anonymous>:(\d+):/)[1]);
  }
  const display = line === undefined ? '' : itemIndex === undefined ? `[line ${line}]` : `[line ${line}, for item ${itemIndex}]`;
  const firstFrame = rows.findIndex((r) => STACK_FRAME_REGEX.test(r));
  const header = firstFrame === -1 ? rows : rows.slice(0, firstFrame);
  const excerptEnd = header.reduce((last, r, i) => (SOURCE_EXCERPT_CARET_REGEX.test(r) ? i + 1 : last), 0);
  const groups = (header.slice(excerptEnd).find((r) => STACK_HEADER_REGEX.test(r)) || '').match(STACK_HEADER_REGEX)?.groups;
  const details = groups ? groups.errorDetails.trim() || null : null;
  return `${details ?? (error.message || 'Unknown error')} ${display}`.trim();
}
(async () => {
  const out = [];
  for (const c of cases) {
    const context = vm.createContext({ $input: { first: () => ({ json: { body: c.body } }), all: () => [{ json: { body: c.body } }] } });
    const wrapped = ['globalThis.global = globalThis', 'var module = { exports: {} }',
      `module.exports = async function VmCodeWrapper() {${code}\n}()`].join('; ');
    try {
      await vm.runInContext(wrapped, context);
      out.push({ thrown: false });
    } catch (error) {
      out.push({ thrown: true, hostError: error instanceof Error, message: executionErrorMessage(error, c.item_index ?? undefined) });
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def load(path=WORKFLOW):
    return json.loads(path.read_text(encoding="utf-8"))


def edges_of(workflow):
    return {(source, index, link["node"]) for source, outputs in workflow["connections"].items()
            for index, branch in enumerate(outputs["main"]) for link in branch}


def reduce(*items_list, workflow=WORKFLOW):
    """Run the committed Code node once per input list; return each run's single row."""

    cases = [{"items": items, "now_ms": NOW_MS} for items in items_list]
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps(cases), encoding="utf-8")
        done = subprocess.run([shutil.which("node"), str(harness), str(workflow), str(path)],
                              capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    rows = []
    for result in json.loads(done.stdout):
        assert result["ok"], result
        (item,) = result["out"]
        assert list(item["json"]) == ["row"], item
        rows.append(item["json"]["row"])
    return rows


def n8n_messages(*cases):
    """execution.error.message as n8n 2.48.5 builds it when the monitor's validator rejects each body."""

    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "n8n_message.js"
        harness.write_text(N8N_MESSAGE_HARNESS, encoding="utf-8")
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps([{"body": body, "item_index": index} for body, index in cases]), encoding="utf-8")
        done = subprocess.run([shutil.which("node"), str(harness), str(MONITOR), str(path)],
                              capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    results = json.loads(done.stdout)
    for result in results:
        assert result["thrown"] and not result["hostError"], result  # a vm Error, as in n8n's task runner
    return [result["message"] for result in results]


# The real shape of a monitor rejection message at n8n 2.48.5 (see N8N_MESSAGE_HARNESS): code + " [line N]".
REAL_SCHEMA_MESSAGE = "EG_MONITOR_SCHEMA [line 7]"


def failure(message=REAL_SCHEMA_MESSAGE, node="Validate Both Streams and Classify", mode="webhook",
            execution_id="4711", workflow_id="wfEgMonitor001"):
    """An Error Trigger item as n8n 2.48.5 builds it for a failed run, with fake secrets in every free-text spot."""

    return {
        "execution": {
            "id": execution_id,
            "url": "http://127.0.0.1:5678/workflow/%s/executions/%s" % (workflow_id, execution_id),
            "retryOf": None,
            "error": {
                "message": message,
                "description": "header X-EnergyGrid-Monitor-Key: " + SECRETS[0],
                "context": {"itemIndex": 0, "body": SECRETS[2]},
                "cause": {"name": "Error", "message": SECRETS[2]},
                "stack": "Error: %s\n    at %s" % (message, SECRETS[3]),
                "timestamp": NOW_MS,
                "name": "Error",
                "node": {"name": node, "parameters": {"path": SECRETS[1], "authentication": "headerAuth"}},
            },
            "lastNodeExecuted": node,
            "mode": mode,
            "executionContext": {"note": SECRETS[2]},
        },
        "workflow": {"id": workflow_id, "name": "EnergyGrid - Local Run Monitor"},
    }


def monitor_codes():
    text = "\n".join(n["parameters"].get("jsCode", "") for n in load(MONITOR)["nodes"])
    return sorted(set(re.findall(r"throw new Error\('(EG_MONITOR_[A-Z_]+)'\)", text)))


def reducer_list(name):
    code = next(n for n in load()["nodes"] if n["name"] == REDUCE)["parameters"]["jsCode"]
    start = code.index("const %s = [" % name)
    return re.findall(r"'([^']+)'", code[start:code.index("];", start)])


class ShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load()
        cls.nodes = {node["name"]: node for node in cls.workflow["nodes"]}

    def test_inactive_saves_nothing_and_is_free_of_live_identity(self):
        self.assertEqual(self.workflow["name"], "EnergyGrid - Monitor Error Log")
        self.assertIs(self.workflow["active"], False)
        self.assertIsNone(self.workflow["staticData"])
        self.assertNotIn("pinData", self.workflow)
        settings = self.workflow["settings"]
        # Its input can hold item data, so none of its own runs is saved either.
        self.assertEqual((settings["saveDataSuccessExecution"], settings["saveDataErrorExecution"]), ("none", "none"))
        self.assertIs(settings["saveManualExecutions"], False)
        self.assertIs(settings["saveExecutionProgress"], False)
        self.assertIs(settings["availableInMCP"], False)
        self.assertEqual(settings["timezone"], "Asia/Singapore")
        # n8n runs an error workflow only when its caller policy lets the failed workflow call it.
        self.assertEqual(settings["callerPolicy"], "workflowsFromSameOwner")
        self.assertNotIn("errorWorkflow", settings, "the error workflow has no error workflow of its own")
        for node in self.workflow["nodes"]:
            self.assertNotIn("credentials", node, node["name"])
            self.assertNotIn("webhookId", node, node["name"])
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        self.assertEqual(sorted(set(re.findall(r"REPLACE_WITH_[A-Z_]+", text))), sorted([LINK, TABLE]))

    def test_three_working_nodes_in_one_line(self):
        working = {name: node["type"] for name, node in self.nodes.items() if node["type"] != "n8n-nodes-base.stickyNote"}
        self.assertEqual(working, {"Monitor Run Failed": "n8n-nodes-base.errorTrigger", REDUCE: "n8n-nodes-base.code",
                                   "Record Failure Row": "n8n-nodes-base.dataTable"})
        self.assertEqual(edges_of(self.workflow), {("Monitor Run Failed", 0, REDUCE), (REDUCE, 0, "Record Failure Row")})
        self.assertEqual(self.nodes["Monitor Run Failed"]["parameters"], {})

    def test_the_row_is_the_closed_row_and_nothing_else(self):
        params = self.nodes["Record Failure Row"]["parameters"]
        self.assertEqual((params["resource"], params["operation"]), ("row", "insert"))
        self.assertEqual(params["dataTableId"], {"__rl": True, "value": TABLE, "mode": "id"})
        columns = params["columns"]
        self.assertEqual(columns["mappingMode"], "defineBelow")
        self.assertEqual([c["id"] for c in columns["schema"]], COLUMNS)
        self.assertEqual({c["type"] for c in columns["schema"]}, {"string"})
        self.assertEqual(columns["value"], {c: "={{ $json.row.%s }}" % c for c in COLUMNS})
        # The Code node reads only its input item: no other node, no execution data, no headers.
        code = "\n".join(line for line in self.nodes[REDUCE]["parameters"]["jsCode"].splitlines()
                         if not line.lstrip().startswith("//"))
        for forbidden in ("$(", "$execution", "headers", "description", "context", "stack", "cause", "$env", "url"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_notes_name_every_placeholder(self):
        notes = "\n".join(n["parameters"]["content"] for n in self.workflow["nodes"]
                          if n["type"] == "n8n-nodes-base.stickyNote")
        for phrase in ("Required manual configuration", TABLE, LINK, "energygrid_monitor_error_log", "published",
                       "XB Daily Health", "#288 option (c)"):
            self.assertIn(phrase, notes)
        for column in COLUMNS:
            self.assertIn("`%s`" % column, notes)


class MonitorLinkTests(unittest.TestCase):
    def test_monitor_saves_no_failed_run_and_points_at_this_workflow(self):
        settings = load(MONITOR)["settings"]
        self.assertEqual(settings["saveDataErrorExecution"], "none")
        self.assertEqual(settings["saveDataSuccessExecution"], "none")
        self.assertEqual(settings["errorWorkflow"], LINK)
        sticky = "\n".join(n["parameters"]["content"] for n in load(MONITOR)["nodes"]
                           if n["type"] == "n8n-nodes-base.stickyNote")
        self.assertIn("EnergyGrid - Monitor Error Log", sticky)
        self.assertIn(LINK, sticky)

    def test_closed_lists_follow_the_monitor(self):
        # Drift guard: a new EG_MONITOR_* code or a renamed node in the monitor must reach the reducer.
        self.assertEqual(sorted(reducer_list("CODES")), monitor_codes())
        self.assertEqual(len(monitor_codes()), 11)
        monitor_nodes = sorted(n["name"] for n in load(MONITOR)["nodes"] if n["type"] != "n8n-nodes-base.stickyNote")
        self.assertEqual(sorted(reducer_list("NODES")), monitor_nodes)

    def test_docs_and_ci_carry_the_new_export(self):
        readme = README.read_text(encoding="utf-8")
        self.assertIn("### energygrid_monitor_error_log.workflow.json", readme)
        package = MONITOR_README.read_text(encoding="utf-8")
        for phrase in ("energygrid_monitor_error_log", "errorWorkflow", "Synthetic failure test on a copy",
                       "n8n@2.48.5", "executeErrorWorkflow", "execution-error.ts", "[line N]",
                       # G2 L4-L6: OK words per live step, one project, short M3 window (M4 below).
                       '"OK create EG error log table"', '"OK import EG error log workflow"',
                       '"OK test EG error log once"', '"OK switch EG monitor to error workflow"',
                       "same n8n project", "any other row the copy wrote"):
            self.assertIn(phrase, package)
        ci = CI.read_text(encoding="utf-8")
        for path in ("n8n-workflows/energygrid_monitor_error_log.workflow.json",
                     "tests/test_energygrid_monitor_error_log_workflow.py"):
            self.assertEqual(ci.count(path), 3, path)  # trigger path, owned list, allowed list
        self.assertIn('-p "test_energygrid_monitor_error_log_workflow.py"', ci)

    def test_m3_copy_points_at_live_sender(self):
        # Live M3 (21 Nov) stopped: n8n 2.48.5 refused to publish the test copy because `Send Owner Alert Email`
        # still held the export placeholder (a referenced sub-workflow must exist and be published). Controller
        # ruling option 1: the copy calls the live published sender, as the live monitor does; alerts stay off.
        package = MONITOR_README.read_text(encoding="utf-8")
        start = package.index("| M3 |")
        m3 = package[start:package.index("\n", start)]
        for phrase in (
            "**`Send Owner Alert Email` set to the live published `XB Ops Alert Email` workflow ID, the same as the "
            "live monitor**",
            "not the export placeholder `REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID`",
            "will not publish a workflow whose referenced sub-workflow is missing or not published",
            "with `ENABLE_OWNER_ALERTS` false that node is never reached, so no email",
            "**Each retry makes a new copy**",
            "`_validateSubWorkflowReferences`",
            "workflow-validation.service.ts",
            "Please publish all referenced sub-workflows first",
            # G2 P-4: the copy keeps alerts off and reports to the M2 workflow; old gaps M3-f and M3-j.
            "`ENABLE_OWNER_ALERTS` false, `errorWorkflow` = the M2 workflow",
            "never reuse or re-activate an archived copy",
            "**0** saved executions for the copy",
        ):
            self.assertIn(phrase, m3)

    def test_m3_waits_for_the_async_publication(self):
        # G2 P-1: at n8n 2.48.5 the default publication service only queues the publication, so the activate call
        # answers before the copy's webhook exists; live M3 retry 1 POSTed about 0.1 s later and got HTTP 404,
        # retry 2 waited 5 s and passed. The row records that recipe and its source.
        package = MONITOR_README.read_text(encoding="utf-8")
        start = package.index("| M3 |")
        m3 = package[start:package.index("\n", start)]
        for phrase in (
            "activate it; then, after the wait below, POST `{}` once",
            "**Activation only queues the publication:**",
            "`N8N_USE_WORKFLOW_PUBLICATION_SERVICE=true`",
            "the activate call answers before the copy's webhook is registered",
            "activate, **wait at least 5 s, then POST once**",
            "**only on HTTP 404** resend, up to 5 more times 3 s apart (on any other answer, do not resend)",
            "deactivate **within 5 minutes**",
            "`packages/@n8n/config/src/configs/workflows.config.ts` (`useWorkflowPublicationService` defaults to "
            "`true`)",
            "`_publishViaOutbox`",
            # G2 #346 survivors E20 and E07: why the wait is needed.
            "so a POST right after it can answer HTTP 404",
            "the outbox consumer registers the webhook a moment later",
        ):
            self.assertIn(phrase, m3)
        self.assertNotIn("POST right after activating", package)

    def test_m3_token_is_never_printed(self):
        # G2 #346 survivor E24: the operator reads the webhook token from its file; nobody prints or pastes it.
        package = MONITOR_README.read_text(encoding="utf-8")
        start = package.index("| M3 |")
        m3 = package[start:package.index("\n", start)]
        self.assertIn("POST `{}` once on loopback with the header (the operator reads the token file; nobody prints it)",
                      m3)
        for stale in ("pastes the token", "prints the token", "token in chat"):
            self.assertNotIn(stale, package)

    def test_live_steps_status_is_recorded(self):
        package = MONITOR_README.read_text(encoding="utf-8")
        self.assertIn("**Status:** M1-M5 were completed live on 21 Nov 2026, each with its owner OK.", package)

    def test_m4_wording_matches_n8n_2_48_5(self):
        # G2 recheck L7: at n8n 2.48.5 settings are kept on the workflow row and are not part of a version. M4
        # checks for a draft before the PUT and reads back the row's settings and an unchanged activeVersionId.
        # G2 N-2, as corrected by P-3: with publishIfActive=false a 403 from the scope checks comes before the save
        # (nothing saved), but a publish-policy 403 can come after the save, and any other non-200 may leave the
        # settings on the row and, on the old path, the monitor unpublished (a failed re-activation sets
        # activeVersionId to null). So M4 undoes on every non-200, a 403 included, and stops if activeVersionId is
        # not the backup's; it reads back an unchanged versionId; and it leaves staticData out of the PUT body.
        package = MONITOR_README.read_text(encoding="utf-8")
        start = package.index("| M4 |")
        m4 = package[start:package.index("\n", start)]
        for phrase in (
            '"OK switch EG monitor to error workflow"',
            "**not part of a version**",
            "**Before the PUT**, read back that `versionId` equals `activeVersionId`",
            "if they differ, **stop** and report",
            "`PUT /workflows/{id}?publishIfActive=false`",
            "**exactly as the backup GET returned them**",
            "**Leave `staticData` out of the PUT body**",
            "a field left out is not written, so the dedupe state the 5-minute tick has saved since the backup is kept",
            "needs only `workflow:update`",
            "`workflow:activate`",
            "**On any answer other than 200:**",
            "the monitor may be **unpublished**",
            "n8n sets `activeVersionId` to null",
            # G2 P-4 (M4-i): the in-row Undo is the same PUT with the flag, not a plain PUT.
            "re-apply the backup's settings at once (Undo, the same PUT with `publishIfActive=false`)",
            "then read back `settings` and `activeVersionId`",
            "If `activeVersionId` is not the backup's, **stop and report**",
            "re-publishing needs `workflow:activate` and is the Controller's call",
            "Do not go on to M5",
            # G2 P-2: name the publication path each failure explanation applies to.
            "With n8n 2.48.5's **default publication service** (`N8N_USE_WORKFLOW_PUBLICATION_SERVICE=true`), a "
            "settings-only change re-registers nothing (the running webhook and schedule stay)",
            "a non-200 after the save comes from the activation pre-checks",
            "with `activeVersionId` unchanged",
            "On the **old path** (`N8N_USE_WORKFLOW_PUBLICATION_SERVICE=false`), the re-activation first takes the "
            "running monitor out",
            "On either path, re-apply",
            # G2 P-3: a publish-policy 403 can come after the save; every 403 is handled like any other non-200.
            "the `workflow:update` scope checks run **before the save**, so a 403 from them saved nothing",
            "a **publish-policy 403** (`PolicyViolationError`, only when a publish policy is configured) can come "
            "**after the save**",
            "treat every 403 like any other non-200",
            "If the Undo itself answers non-200, its read-back still decides",
            # G2 P-4 optional (M4-t, M4-v): the backup keeps versionId; step 3 points errorWorkflow at M2.
            "keep the answer, including `settings`, `versionId` and `activeVersionId`",
            "and `errorWorkflow` = the M2 workflow changed",
            "**HTTP 200**",
            "a GET shows `settings.errorWorkflow` = the M2 workflow and `settings.saveDataErrorExecution` = `none`",
            "`activeVersionId` is **unchanged** from the backup",
            "`versionId` is **unchanged** from the backup",
            "`n8n@2.48.5`",
            "workflow-history.ts",
            "assertMayPublishOnSave",
            "workflow-entity-mapper.ts",
            "base-workflow-public.dto.ts",
            "@ApiKeyScope('workflow:update')",
            "workflow-publication-applier.ts",
            "publication/trigger-diff.ts",
            "policy-violation.error.ts",
            "| Re-apply the backup's settings with `publishIfActive=false`, and send `errorWorkflow: \"DEFAULT\"` when "
            "the backup has no `errorWorkflow` (n8n merges sent settings, so a missing key is not removed; `DEFAULT` "
            "clears it); read back `settings`, `versionId` and `activeVersionId`; if `activeVersionId` is not the "
            "backup's, stop and report (Controller's call). |",
            # G2 P-5: n8n merges the sent settings over the stored ones, so the Undo must clear an errorWorkflow the
            # backup did not have by sending the literal DEFAULT (removeDefaultValues deletes it after the merge).
            "re-apply the backup's settings at once (Undo, the same PUT with `publishIfActive=false`), and send "
            "`errorWorkflow: \"DEFAULT\"` when the backup has no `errorWorkflow` (n8n merges sent settings, so a "
            "missing key is not removed; `DEFAULT` clears it), then read back `settings` and `activeVersionId`",
            "`packages/cli/src/workflow-helpers.ts` (`removeDefaultValues`",
            # G2 N-1: the pre-check list is an example list and names dynamic credentials and trigger node IDs.
            "the activation pre-checks (for example webhook conflict, dynamic credentials, trigger node IDs, node "
            "validation, sub-workflow references, the activate hook, a publish policy)",
            # G2 #346 survivor E17: the flag value in the M4 source list.
            "`packages/@n8n/config/src/configs/workflows.config.ts` (`useWorkflowPublicationService`, default `true`)",
        ):
            self.assertIn(phrase, m4)
        # Inexact wording is gone. L7: settings have no published version. N-2: with the flag a 403 saves nothing,
        # undo covers every non-200 (not only a 403), and staticData is left out of the body, not "kept".
        for stale in ("nothing changed live", "the old version stays live", "**published** (active) version",
                      "carries `errorWorkflow`", "keep `staticData`", "n8n saves first and then refuses",
                      "**On a 403:**", "settings are **already on the row**",
                      # P-3: the old claim that every 403 comes before the save.
                      "so on a 403 nothing was saved"):
            self.assertNotIn(stale, package)


@unittest.skipUnless(shutil.which("node"), "a Node runtime is required to execute the committed Code node")
class ReduceTests(unittest.TestCase):
    def assertClosed(self, row):
        self.assertEqual(list(row), COLUMNS)
        blob = json.dumps(row)
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
        self.assertRegex(row["workflow_id"], r"^([A-Za-z0-9]{1,64}|unknown)$")
        self.assertRegex(row["execution_id"], r"^[0-9]{0,20}$")
        self.assertRegex(row["code"], r"^EG_MONITOR_[A-Z_]+$")
        self.assertEqual(row["failed_at"], NOW_ISO)

    def test_every_monitor_code_is_kept_as_its_own_closed_code(self):
        # n8n's real shape: "<code> [line N]" (and "[line N, for item M]"); the bare code is also accepted.
        codes = monitor_codes()
        messages = [("%s [line %d]" % (code, 6 + i), code) for i, code in enumerate(codes)]
        messages += [("%s [line 28, for item 0]" % code, code) for code in codes]
        messages += [(code, code) for code in codes]
        rows = reduce(*[[failure(message=message)] for message, _ in messages])
        for (message, code), row in zip(messages, rows):
            self.assertClosed(row)
            self.assertEqual(row, {"workflow_id": "wfEgMonitor001", "execution_id": "4711",
                                   "failed_node": "Validate Both Streams and Classify", "code": code,
                                   "mode": "webhook", "failed_at": NOW_ISO}, message)

    def test_real_n8n_messages_from_the_monitor_validator_are_classified(self):
        # The monitor's own validator, run the way n8n 2.48.5's task runner runs it, rejects these bodies;
        # the resulting messages carry " [line N]", and the reducer must still record the closed code.
        stamp = "2026-11-19T00:00:00Z"
        run_id = "00000000-0000-4000-8000-000000000000"
        cases = [
            ({}, None, "EG_MONITOR_REPORT_SHAPE"),  # M3's synthetic test body
            ({"schema": "nope", "run_id": run_id, "observed_at_utc": stamp, "streams": {}}, None, "EG_MONITOR_SCHEMA"),
            ({"schema": "xb.energygrid.monitor.v1", "run_id": "x", "observed_at_utc": stamp, "streams": {}}, None,
             "EG_MONITOR_RUN_ID"),
            ({"schema": "xb.energygrid.monitor.v1", "run_id": run_id, "observed_at_utc": "x", "streams": {}}, None,
             "EG_MONITOR_TIMESTAMP"),
            ({"schema": "xb.energygrid.monitor.v1", "run_id": run_id, "observed_at_utc": "2000-01-01T00:00:00Z",
              "streams": {}}, None, "EG_MONITOR_OLD_REPORT"),
        ]
        messages = n8n_messages(*[(body, index) for body, index, _ in cases])
        for message, (_, index, code) in zip(messages, cases):
            suffix = r" \[line \d+\]" if index is None else r" \[line \d+, for item %d\]" % index
            self.assertRegex(message, "^%s%s$" % (code, suffix))  # never the bare code at n8n 2.48.5
        self.assertEqual(messages[0], "EG_MONITOR_REPORT_SHAPE [line 6]")
        self.assertEqual(messages[1], REAL_SCHEMA_MESSAGE)
        rows = reduce(*[[failure(message=message)] for message in messages])
        self.assertEqual([row["code"] for row in rows], [code for _, _, code in cases])
        for row in rows:
            self.assertClosed(row)

    def test_message_text_never_reaches_the_row(self):
        messages = [
            "Unexpected token 'f', \"%s\" is not valid JSON [line 3]" % SECRETS[0],  # a parse error quoting its input
            "EG_MONITOR_SCHEMA " + SECRETS[2],  # a closed code plus data is not a closed code
            "EG_MONITOR_SCHEMA [line 7] " + SECRETS[2],  # nor is a closed code, the suffix and data
            "EG_MONITOR_SCHEMA [line 7]" + SECRETS[2],
            SECRETS[2] + " EG_MONITOR_SCHEMA [line 7]",  # data before the code
            "EG_MONITOR_SCHEMA [line x]",
            "EG_MONITOR_SCHEMA [line 7, for item x]",
            "EG_MONITOR_SCHEMA [line 7] [line 8]",
            "EG_MONITOR_NOPE [line 1]",  # not one of the monitor's codes
            "[line 7]",
            SECRETS[0],
            "eg_monitor_schema [line 7]",
            "",
            "Cannot read properties of undefined (reading 'streams') [line 12]",
        ]
        rows = reduce(*[[failure(message=m)] for m in messages])
        for message, row in zip(messages, rows):
            self.assertClosed(row)
            self.assertEqual(row["code"], "EG_MONITOR_UNCLASSIFIED", message)
        (row,) = reduce([failure(message="  EG_MONITOR_REF [line 28, for item 0]\n")])
        self.assertEqual(row["code"], "EG_MONITOR_REF")

    def test_unknown_node_mode_and_ids_become_fixed_values(self):
        rows = reduce(
            [failure(node="Node " + SECRETS[1])],
            [failure(mode="manual-" + SECRETS[0])],
            [failure(execution_id="abc-" + SECRETS[0])],
            [failure(workflow_id="wf/" + SECRETS[1])],
            [failure(node="")],
        )
        self.assertEqual([r["failed_node"] for r in rows], ["other", "Validate Both Streams and Classify",
                                                            "Validate Both Streams and Classify",
                                                            "Validate Both Streams and Classify", ""])
        self.assertEqual(rows[1]["mode"], "other")
        self.assertEqual(rows[2]["execution_id"], "")
        self.assertEqual(rows[3]["workflow_id"], "unknown")
        for row in rows:
            self.assertClosed(row)
        (numeric,) = reduce([dict(failure(), execution=dict(failure()["execution"], id=4712))])
        self.assertEqual(numeric["execution_id"], "4712")

    def test_the_schedule_path_and_every_monitor_node(self):
        names = reducer_list("NODES")
        rows = reduce(*[[failure(message="boom " + SECRETS[2], node=name, mode="trigger")] for name in names])
        for name, row in zip(names, rows):
            self.assertClosed(row)
            self.assertEqual((row["failed_node"], row["code"], row["mode"]), (name, "EG_MONITOR_UNCLASSIFIED", "trigger"))

    def test_a_failed_trigger_and_an_empty_item(self):
        trigger = {"trigger": {"error": {"message": SECRETS[0], "stack": SECRETS[3]}, "mode": "trigger"},
                   "workflow": {"id": "wfEgMonitor001", "name": "EnergyGrid - Local Run Monitor"}}
        rows = reduce([trigger], [{}], [])
        self.assertEqual(rows[0], {"workflow_id": "wfEgMonitor001", "execution_id": "", "failed_node": "",
                                   "code": "EG_MONITOR_TRIGGER_FAILED", "mode": "trigger", "failed_at": NOW_ISO})
        for row in rows[1:]:
            self.assertEqual((row["workflow_id"], row["code"], row["mode"]), ("unknown", "EG_MONITOR_TRIGGER_FAILED", "other"))
        for row in rows:
            self.assertClosed(row)


if __name__ == "__main__":
    unittest.main()
