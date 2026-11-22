"""Offline contract tests for import-n8n-workflows-live.ps1 (Toolkit helper set 35defa821c2b).

Rewritten for #316 (PR #315) after the helper refresh. The owner accepted Toolkit's design on
17 Nov 2026: a plain import run imports changed and new repository workflows as unpublished with
no confirmation prompt. These tests pin the protections that remain:

(a) -DryRun performs no `n8n import:workflow` (it still stages the credential-metadata helper in
    the container's /tmp and clears the configured PreparedDir, which is the new contract);
(b) a published (active) live target blocks the whole run before any import;
(c) an import run calls `n8n import:workflow` only with `--activeState=false`, never restarts,
    publishes or executes, and its unpublished postcondition holds;
(d) every run refuses before any Docker call when a stamped helper file has been edited;
(e) under Windows PowerShell 5.1, exactly one early blocker (a recorded local identity that no
    longer exists in live n8n) stops the whole run at the early gate, with no same-name fallback
    (Toolkit 3.146.1 fix; set a831f7fd41c3 skipped that gate for a single blocker);
(f) the import script refuses -RequireConfirmation and -SkipCredentialBindingRefresh (Toolkit
    3.146.1), and -AllowMissingCredentialBindings and -CredentialExportDir (Toolkit 3.146.3), before
    any Docker call; earlier sets accepted and ignored them.

Refreshed for the Toolkit 3.146.1 helper set (#323): (b) now also pins that one published target
is caught at the early gate, before the credential-metadata step. Refreshed again for Toolkit
3.146.4 (set 35defa821c2b): (f) also covers the two switches refused since 3.146.3.

How it runs: the real PowerShell helper runs end to end inside a throwaway fixture repository
holding copies of the committed workflow exports, the helper folder and the repo-root
`n8n-workflow-policy.json`. The `docker` CLI is replaced by a stub: a copy of the signed node.exe
named docker.exe (Windows application control blocks freshly compiled unsigned binaries) plus a
NODE_OPTIONS --require preload that answers each Docker call the helper makes from a small state
folder and logs every call. The preload acts only when the executable is docker.exe, so the
helper's own node calls are unaffected. The test also removes every other folder holding a
docker.exe from PATH and points DOCKER_HOST at a pipe that does not exist, so no real Docker
daemon, n8n instance or credential is reachable. The helper starts `docker` through .NET
Process.Start, so Windows CreateProcess looks in the working folder and the system folders before
PATH: the stub is also copied into the fixture repository root (the helper's working folder), or a
runner image with docker.exe in System32 would run the real CLI (seen on GitHub's windows-latest).

CI (#316 G2 L7): the full suites (tests/_run_ci_full_suite.py) still leave this module out. It runs
in its own windows-latest job, live-import-dry-run in .github/workflows/n8n-export-guards.yml, on any
change under n8n-workflows/, to n8n-workflow-policy.json or to this file; that job fails if any test
here is skipped. To run it locally on the Windows server PC:

    python -m unittest tests.test_import_n8n_workflows_live_dry_run -v
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_WORKFLOW_DIR = REPO_ROOT / "n8n-workflows"
REAL_SCRIPTS_DIR = REAL_WORKFLOW_DIR / "scripts"
REAL_POLICY_FILE = REPO_ROOT / "n8n-workflow-policy.json"

STUB_CONTAINER_ID = "a" * 64
DEAD_DOCKER_HOST = "npipe:////./pipe/x_boundaries_no_such_docker_engine"

# Answers the Docker calls of import-n8n-workflows-live.ps1 and resolve-n8n-docker-target.cjs.
# State (DOCKER_STUB_STATE): live.json = the live workflow list; cp-map.json = container path ->
# local source of each `docker cp` into the container; log.jsonl = one JSON array per call.
DOCKER_STUB_PRELOAD = r"""
'use strict';
const path = require('path');
const fs = require('fs');

if (path.basename(process.execPath).toLowerCase() === 'docker.exe') {
  const args = process.argv.slice(1);
  // node treats the first argument as a script name and resolves it against the working
  // directory ("version" becomes "<cwd>\version"), so take the bare Docker verb back.
  if (args.length > 0 && path.isAbsolute(args[0]) && path.dirname(args[0]) === process.cwd()) {
    args[0] = path.basename(args[0]);
  }
  const state = process.env.DOCKER_STUB_STATE;
  fs.appendFileSync(path.join(state, 'log.jsonl'), JSON.stringify(args) + '\n');
  const containerId = process.env.DOCKER_STUB_CONTAINER_ID;
  const liveFile = path.join(state, 'live.json');
  const cpMapFile = path.join(state, 'cp-map.json');
  const readJson = (file, fallback) => (fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : fallback);
  const done = (text, code = 0) => {
    if (text) process.stdout.write(text + '\n');
    process.exit(code);
  };
  // A container reference is "<name or id>:/path"; a Windows drive path has one letter before ':'.
  const containerRef = (value) => /^[^\\/:]{2,}:\//.test(value);

  if (args[0] === 'version') done('99.9.9');
  if (args[0] === 'ps') done(containerId);
  if (args[0] === 'inspect') {
    done(JSON.stringify([{
      Id: containerId,
      Name: '/n8n-stub',
      Config: { Image: 'docker.n8n.io/n8nio/n8n', Labels: {} },
      State: { Running: true },
      NetworkSettings: { Ports: {} },
    }]));
  }
  if (args[0] === 'cp') {
    const [source, target] = args.slice(1);
    if (containerRef(target)) {
      const map = readJson(cpMapFile, {});
      map[target.slice(target.indexOf(':') + 1)] = source;
      fs.writeFileSync(cpMapFile, JSON.stringify(map));
      done('');
    }
    if (containerRef(source) && source.endsWith('credential-metadata.json')) {
      fs.writeFileSync(target, '[]\n');
      done('');
    }
    done('', 1);
  }
  if (args[0] === 'exec') {
    if (args.includes('export:workflow')) done(JSON.stringify(readJson(liveFile, []), null, 2));
    if (args.includes('export:credentials')) {
      process.stderr.write('No credentials found with specified filters\n');
      process.exit(1);
    }
    if (args.includes('import:workflow')) {
      const input = args.find((arg) => arg.startsWith('--input=')).slice('--input='.length);
      const source = readJson(cpMapFile, {})[input];
      if (!source) done('stub: import input was never copied into the container', 1);
      const imported = JSON.parse(fs.readFileSync(source, 'utf8').replace(/^﻿/, ''));
      const live = readJson(liveFile, []);
      for (const workflow of Array.isArray(imported) ? imported : [imported]) {
        const index = live.findIndex((entry) => entry.id === workflow.id);
        // n8n stores the state the flag asks for; only --activeState=false leaves it unpublished.
        const entry = { ...workflow, active: !args.includes('--activeState=false'), activeVersionId: null, isArchived: false };
        if (index >= 0) live[index] = entry; else live.push(entry);
      }
      fs.writeFileSync(liveFile, JSON.stringify(live));
      done('Successfully imported 1 workflow.');
    }
    // Staging checks and the credential-metadata helper (init, empty, cleanup, remove): succeed.
    done('');
  }
  done('stub: unexpected docker call', 1);
}
"""


def live_workflow(workflow_id, name, active):
    return {
        "id": workflow_id,
        "name": name,
        "active": active,
        "activeVersionId": "StubVersion00001" if active else None,
        "isArchived": False,
        "nodes": [],
        "connections": {},
        "settings": {},
    }


UNRELATED_LIVE = live_workflow("StubUnrelated001", "Stub Unrelated Workflow", False)


def live_copies_of(workflow_files, published_index=None):
    """Unpublished live copies of the repo exports, under the exports' own ids (the realistic
    overwrite case: a new workflow, or one whose live id differs from the export's id, is refused
    at preparation by the helper's selector check). One entry can be published instead."""
    copies = []
    for index, workflow_file in enumerate(workflow_files):
        workflow = json.loads(workflow_file.read_text(encoding="utf-8"))
        workflow_id = workflow.get("id") or f"StubLiveCopy{index:04d}"
        copies.append(live_workflow(workflow_id, workflow["name"], index == published_index))
    return copies


@unittest.skipUnless(os.name == "nt", "import helper is a Windows PowerShell script")
@unittest.skipUnless(shutil.which("powershell"), "powershell is required")
@unittest.skipUnless(shutil.which("node"), "node is required by the helper scripts")
class ImportHelperOfflineContractTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._class_tmp = tempfile.mkdtemp(prefix="import-contract-stub-")
        stub_dir = Path(cls._class_tmp)
        shutil.copy(shutil.which("node"), stub_dir / "docker.exe")
        preload = stub_dir / "docker-stub-preload.cjs"
        preload.write_text(DOCKER_STUB_PRELOAD, encoding="utf-8")
        cls.stub_dir = stub_dir
        cls.preload_path = preload

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._class_tmp, ignore_errors=True)

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="import-contract-repo-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        base = Path(self._tmp)
        self.repo = self._build_fixture_repo(base)
        self.state = base / "docker-state"
        self.state.mkdir()
        self.configured_prepared_dir = self.repo / ".tmp" / "n8n-live-import"

    def _build_fixture_repo(self, base):
        repo = base / "repo"
        (repo / ".git").mkdir(parents=True)
        workflow_dir = repo / "n8n-workflows"
        scripts_dir = workflow_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        self.workflow_files = sorted(REAL_WORKFLOW_DIR.glob("*.workflow.json"))
        self.assertTrue(self.workflow_files, "expected committed workflow JSON in n8n-workflows/")
        for workflow_file in self.workflow_files:
            shutil.copy(workflow_file, workflow_dir / workflow_file.name)
        for helper_file in sorted(REAL_SCRIPTS_DIR.iterdir()):
            if helper_file.is_file():
                shutil.copy(helper_file, scripts_dir / helper_file.name)
        # The validator reads the policy from the directory it runs in (the repository root).
        self.assertTrue(REAL_POLICY_FILE.is_file(), "expected the repo-root n8n-workflow-policy.json")
        shutil.copy(REAL_POLICY_FILE, repo / REAL_POLICY_FILE.name)
        return repo

    def _set_live(self, workflows):
        (self.state / "live.json").write_text(json.dumps(workflows), encoding="utf-8")

    def _offline_path(self, path_value):
        # Drop every PATH folder that holds a real docker.exe; the stub folder goes first.
        kept = [entry for entry in path_value.split(os.pathsep)
                if entry and not (Path(entry) / "docker.exe").is_file()]
        return os.pathsep.join([str(self.stub_dir)] + kept)

    def _run_import(self, extra_args):
        # CreateProcess (the helper's Process.Start) and node's spawn search the working folder before
        # System32 and PATH, so the stub there wins over any system docker.exe. Keep that search on.
        shutil.copy(self.stub_dir / "docker.exe", self.repo / "docker.exe")
        env = {key: value for key, value in os.environ.items()
               if key.upper() != "NODEFAULTCURRENTDIRECTORYINEXEPATH"}
        env["PATH"] = self._offline_path(env.get("PATH", ""))
        env["DOCKER_HOST"] = DEAD_DOCKER_HOST
        env["DOCKER_STUB_STATE"] = str(self.state)
        env["DOCKER_STUB_CONTAINER_ID"] = STUB_CONTAINER_ID
        # NODE_OPTIONS treats backslashes inside quotes as escapes; node accepts forward slashes.
        env["NODE_OPTIONS"] = f'--require "{self.preload_path.as_posix()}"'
        for name in ("N8N_WORKFLOW_HOOK_SCRIPT", "N8N_WORKFLOW_HOOK_AUTOLOAD", "N8N_HELPERS_ALLOW_LOCAL_CHANGES",
                     "N8N_CONTAINER_NAME", "N8N_CONTAINER_ID", "N8N_COMPOSE_PROJECT", "N8N_COMPOSE_SERVICE"):
            env.pop(name, None)
        command = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(self.repo / "n8n-workflows" / "scripts" / "import-n8n-workflows-live.ps1"),
            "-ContainerId", "stubcontainer",
        ] + extra_args
        return subprocess.run(command, cwd=str(self.repo), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, check=False, timeout=900)

    def _docker_calls(self):
        log = self.state / "log.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _imports(self):
        return [call for call in self._docker_calls() if "import:workflow" in call]

    def _copies_into_container(self):
        return [call for call in self._docker_calls() if call[0] == "cp" and call[2].startswith(STUB_CONTAINER_ID + ":")]

    def _assert_never_publishes_executes_or_restarts(self):
        for call in self._docker_calls():
            joined = " ".join(call)
            self.assertNotEqual(call[0], "restart", joined)
            for forbidden in ("--activeState=true", "publish:workflow", "update:workflow", "execute", "activate"):
                self.assertNotIn(forbidden, joined)

    def test_a_dry_run_performs_no_import(self):
        self._set_live([UNRELATED_LIVE] + live_copies_of(self.workflow_files))
        sentinel = self.configured_prepared_dir / "stale.live-import.json"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text('{"sentinel": "prior staged artifact"}\n', encoding="utf-8")

        result = self._run_import(["-DryRun"])

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Live n8n was not changed.", result.stdout)
        self.assertIn("Would import", result.stdout)
        self.assertEqual([], self._imports(), "a dry run must never call n8n import:workflow")
        # Contract since Toolkit a831f7fd41c3: the dry run clears the configured PreparedDir and
        # stages only the credential-metadata helper in the container's /tmp, then cleans up.
        self.assertFalse(sentinel.exists(), "the configured PreparedDir is cleared on every run")
        copies_in = self._copies_into_container()
        self.assertEqual(1, len(copies_in), copies_in)
        self.assertRegex(copies_in[0][2], r":/tmp/ai-agent-toolkit-n8n-metadata-[0-9a-f]{32}\.cjs$")
        self.assertTrue(any("cleanup" in call for call in self._docker_calls()), "credential staging must be cleaned up")
        self._assert_never_publishes_executes_or_restarts()

    def test_b_a_published_live_target_blocks_the_whole_run(self):
        # Every other repo workflow has an unpublished live copy that would otherwise be imported.
        initial = [UNRELATED_LIVE] + live_copies_of(self.workflow_files, published_index=0)
        self._set_live(initial)

        result = self._run_import([])

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("is published (active)", output)
        self.assertIn("Unpublish it in n8n first", output)
        self.assertEqual([], self._imports(), "no workflow may be imported while any target is published")
        # One published target is one early target-resolution blocker. Toolkit 3.146.1 wraps the
        # result in @() (import-n8n-workflows-live.ps1:1833), so Windows PowerShell 5.1 counts it and
        # the early gate stops the run before the credential-metadata step: nothing is copied into
        # the container at all.
        self.assertEqual([], self._copies_into_container())
        live = json.loads((self.state / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(initial, live)
        self._assert_never_publishes_executes_or_restarts()

    def test_c_an_import_run_imports_unpublished_only(self):
        self._set_live([UNRELATED_LIVE] + live_copies_of(self.workflow_files))

        result = self._run_import([])

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("Read-Host", output)
        imports = self._imports()
        self.assertEqual(len(self.workflow_files), len(imports), "every live copy differs from its export, so each is imported")
        for call in imports:
            flags = [arg for arg in call if arg.startswith("--activeState")]
            self.assertEqual(["--activeState=false"], flags, call)
        self.assertIn("Postcondition     : every imported workflow is unpublished", result.stdout)
        live = json.loads((self.state / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(len(self.workflow_files) + 1, len(live))
        self.assertTrue(all(entry["active"] is False for entry in live), "nothing in live n8n is published")
        self._assert_never_publishes_executes_or_restarts()

    def test_d_an_edited_stamped_helper_is_refused_before_any_docker_call(self):
        self._set_live([UNRELATED_LIVE])
        edited = self.repo / "n8n-workflows" / "scripts" / "should-import-n8n-workflow.cjs"
        edited.write_text(edited.read_text(encoding="utf-8") + "\n// local edit\n", encoding="utf-8")

        for args in (["-DryRun"], []):
            with self.subTest(args=args):
                result = self._run_import(args)
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("should-import-n8n-workflow.cjs", output)
                self.assertEqual([], self._docker_calls(), "the stamp check runs before any Docker call")

    def test_e_one_missing_local_identity_blocks_without_same_name_fallback(self):
        # The one export without a template id is the only one a same-name fallback could reach.
        monitor_file = next(f for f in self.workflow_files if f.name == "energygrid_local_failure_monitor.workflow.json")
        monitor = json.loads(monitor_file.read_text(encoding="utf-8"))
        self.assertNotIn("id", monitor, "the monitor export is expected to carry no template id")
        identity_dir = self.repo / ".n8n-local"
        identity_dir.mkdir()
        (identity_dir / "n8n-workflow-identities.json").write_text(json.dumps({
            "schemaVersion": 1,
            "workflows": [{
                "workflowFile": monitor_file.name,
                "workflowName": monitor["name"],
                "targetWorkflowId": "StubGoneTarget01",
            }],
        }), encoding="utf-8")
        # Every export has an unpublished live copy; the monitor's copy sits under a different id,
        # so the recorded identity is gone and only a same-name workflow remains.
        initial = [UNRELATED_LIVE] + live_copies_of(self.workflow_files)
        same_name = [entry for entry in initial if entry["name"] == monitor["name"]]
        self.assertEqual(1, len(same_name))
        same_name[0]["id"] = "StubSameName0001"
        self._set_live(initial)

        result = self._run_import([])

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("dedicated local target identity no longer exists; refusing a same-name fallback", output)
        self.assertEqual([], self._imports(), "one early blocker must stop the whole run")
        self.assertEqual([], self._copies_into_container(), "the early gate stops before any container staging")
        live = json.loads((self.state / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(initial, live)
        self._assert_never_publishes_executes_or_restarts()

    def test_f_unused_import_switches_are_refused_before_any_docker_call(self):
        self._set_live([UNRELATED_LIVE] + live_copies_of(self.workflow_files))

        refused = (
            ["-RequireConfirmation"],
            ["-SkipCredentialBindingRefresh"],
            ["-AllowMissingCredentialBindings"],
            ["-CredentialExportDir", ".tmp/stub-credential-exports"],
        )
        for args in refused:
            switch = args[0]
            with self.subTest(switch=switch):
                result = self._run_import(args)
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn("N8N_POLICY_VALIDATION_FAILED", output)
                self.assertIn(switch, output)
                self.assertEqual([], self._docker_calls(), "the switch is refused before any Docker call")


if __name__ == "__main__":
    unittest.main()
