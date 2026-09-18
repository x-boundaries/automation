"""Focused mocked tests for import-n8n-workflows-live.ps1 dry-run isolation.

The import helper's -DryRun previously cleared and regenerated the configured
persistent PreparedDir (late Codex review on PR #107). These tests run the real
PowerShell helper end-to-end inside a throwaway fixture repository, with the
`docker` CLI replaced by a stub, and assert:

- -DryRun preserves an existing configured PreparedDir byte-for-byte;
- -DryRun does not create the configured PreparedDir when it is absent;
- -DryRun leaves no temporary planning directory behind;
- the confirmed-import path still fails closed at the confirmation gate on
  non-interactive input, with import-mode PreparedDir behaviour unchanged;
- no mutating docker command (cp / import:workflow / restart) is ever issued.

The docker stub is a copy of the signed node.exe named docker.exe (Windows
application control blocks freshly compiled unsigned binaries) plus a
NODE_OPTIONS --require preload that answers docker CLI calls with canned
read-only data and logs every invocation. The preload self-activates only when
the executable basename is docker.exe, so the helper's real node invocations
are unaffected. No live n8n, Docker, credential, Sheet, or AutoCount surface
is touched.
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

DOCKER_STUB_PRELOAD = r"""
'use strict';
const path = require('path');
const fs = require('fs');

if (path.basename(process.execPath).toLowerCase() === 'docker.exe') {
  const rawArgs = process.argv.slice(1);
  const args = rawArgs.length && path.isAbsolute(rawArgs[0])
    ? [path.basename(rawArgs[0]), ...rawArgs.slice(1)]
    : rawArgs;
  const log = process.env.DOCKER_STUB_LOG;
  if (log) {
    fs.appendFileSync(log, args.join(' ') + '\n');
  }

  if (args[0] === 'version') {
    process.stdout.write('99.9.9\n');
    process.exit(0);
  }

  if (args[0] === 'inspect') {
    const container = {
      Id: 'a'.repeat(64),
      Name: '/n8n-stub',
      Config: { Image: 'docker.n8n.io/n8nio/n8n', Labels: {} },
      State: { Running: true },
      NetworkSettings: { Ports: {} },
    };
    process.stdout.write(JSON.stringify([container]) + '\n');
    process.exit(0);
  }

  if (args[0] === 'exec' && args.includes('export:workflow')) {
    const workflowsFile = log ? log + '.workflows.json' : null;
    const importedFile = log ? log + '.imported.json' : null;
    if (process.env.DOCKER_STUB_READBACK_FAIL === '1' && importedFile && fs.existsSync(importedFile)) {
      process.stderr.write('simulated bounded readback failure\n');
      process.exit(1);
    }
    if (importedFile && fs.existsSync(importedFile) && process.env.DOCKER_STUB_READBACK_STALE !== '1') {
      process.stdout.write(JSON.stringify([JSON.parse(fs.readFileSync(importedFile, 'utf8'))]) + '\n');
      process.exit(0);
    }
    if (workflowsFile && fs.existsSync(workflowsFile)) {
      let workflows = JSON.parse(fs.readFileSync(workflowsFile, 'utf8'));
      const idArg = args.find(value => value.startsWith('--id='));
      if (idArg) workflows = workflows.filter(item => String(item.id) === idArg.slice(5));
      process.stdout.write(JSON.stringify(workflows) + '\n');
      process.exit(0);
    }
    if (process.env.DOCKER_STUB_WORKFLOWS_FILE) {
      process.stdout.write(fs.readFileSync(process.env.DOCKER_STUB_WORKFLOWS_FILE, 'utf8') + '\n');
      process.exit(0);
    }
    if (process.env.DOCKER_STUB_WORKFLOWS) {
      process.stdout.write(process.env.DOCKER_STUB_WORKFLOWS + '\n');
      process.exit(0);
    }
    process.stderr.write('No workflows found with specified filters\n');
    process.exit(1);
  }

  if (args[0] === 'exec' && args.includes('list:workflow')) {
    const workflowsFile = log ? log + '.workflows.json' : null;
    let workflows = [];
    if (workflowsFile && fs.existsSync(workflowsFile)) workflows = JSON.parse(fs.readFileSync(workflowsFile, 'utf8'));
    if (process.env.DOCKER_STUB_INCLUDE_IMPORTED === '1' && log && fs.existsSync(log + '.imported.json')) {
      const imported = JSON.parse(fs.readFileSync(log + '.imported.json', 'utf8'));
      if (!workflows.some(item => String(item.id) === String(imported.id))) workflows.push(imported);
    }
    for (const workflow of workflows) process.stdout.write(String(workflow.id) + '|' + String(workflow.name) + '\n');
    process.exit(0);
  }

  if (args[0] === 'cp') {
    if (log) fs.copyFileSync(args[1], log + '.imported.json');
    process.exit(0);
  }

  process.exit(0);
}
"""


@unittest.skipUnless(os.name == "nt", "import helper is a Windows PowerShell script")
@unittest.skipUnless(shutil.which("powershell"), "powershell is required")
@unittest.skipUnless(shutil.which("node"), "node is required by the helper scripts")
class ImportDryRunIsolationTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._class_tmp = tempfile.mkdtemp(prefix="import-dryrun-stub-")
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
        self._tmp = tempfile.mkdtemp(prefix="import-dryrun-repo-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.repo = self._build_fixture_repo(Path(self._tmp))
        self.configured_prepared_dir = self.repo / ".tmp" / "n8n-live-import"
        self.docker_log = self.repo / "docker-stub-log.txt"

    def _build_fixture_repo(self, base):
        repo = base / "repo"
        repo.mkdir(parents=True)
        workflow_dir = repo / "n8n-workflows"
        scripts_dir = workflow_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        workflow_files = sorted(REAL_WORKFLOW_DIR.glob("*.json"))
        self.assertTrue(workflow_files, "expected committed workflow JSON in n8n-workflows/")
        for workflow_file in workflow_files:
            shutil.copy(workflow_file, workflow_dir / workflow_file.name)
        for helper_file in sorted(REAL_SCRIPTS_DIR.iterdir()):
            if helper_file.is_file():
                shutil.copy(helper_file, scripts_dir / helper_file.name)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "WJ"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "10020253+weijunswj@users.noreply.github.com"], cwd=repo, check=True)
        (repo / "initial.txt").write_text("parent\n", encoding="utf-8")
        (repo / ".gitignore").write_text(".tmp/\n.n8n-local/\n", encoding="utf-8")
        subprocess.run(["git", "add", "initial.txt", ".gitignore"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "parent"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "n8n-workflows"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "deployment checkout"], cwd=repo, check=True, capture_output=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
        parent = subprocess.check_output(["git", "rev-parse", "HEAD^"], cwd=repo, text=True).strip()
        questions = {"name": "q_name", "phone": "q_phone", "email": "q_email", "birthday_month": "q_birth", "marketing_consent": "q_marketing", "pdpa_acknowledged": "q_pdpa"}
        qhash = __import__("hashlib").sha256(json.dumps(questions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manifest = json.loads((REPO_ROOT / "config/member_forms_gateway_ingest.binding.template.json").read_text(encoding="utf-8"))
        rendered = json.dumps(manifest)
        values = {
            "FORMS_API_URL": "https://forms.googleapis.com/v1/forms/form_ref/responses", "SOURCE_CURSOR_URL": "https://gateway.invalid/v1/source/cursor",
            "GATEWAY_INGEST_URL": "https://gateway.invalid/v1/source-events", "PAGE_CHECKPOINT_URL": "https://gateway.invalid/v1/source/cursor/page",
            "FORM_ID_REFERENCE": "form_ref", "QUESTION_MAP_SHA256": qhash, "QUESTION_NAME_REFERENCE": questions["name"], "QUESTION_PHONE_REFERENCE": questions["phone"],
            "QUESTION_EMAIL_REFERENCE": questions["email"], "QUESTION_BIRTHDAY_MONTH_REFERENCE": questions["birthday_month"], "QUESTION_MARKETING_CONSENT_REFERENCE": questions["marketing_consent"],
            "QUESTION_PDPA_ACKNOWLEDGED_REFERENCE": questions["pdpa_acknowledged"], "CURSOR_BINDING_REFERENCE": "cursor_ref", "WATERMARK_BINDING_REFERENCE": "watermark_ref",
            "GOOGLE_CREDENTIAL_REFERENCE": "google_cred", "GATEWAY_CREDENTIAL_REFERENCE": "gateway_cred", "TARGET_PROJECT_REFERENCE": "project_ref",
            "TARGET_WORKFLOW_REFERENCE": "xbMemberGatewayTemplate02", "TARGET_PREIMAGE_REFERENCE": "preimage_ref", "DEPLOYMENT_COMMIT": commit, "DEPLOYMENT_TREE": tree, "DEPLOYMENT_PARENT": parent,
        }
        for key, value in values.items(): rendered = rendered.replace("{{" + key + "}}", value)
        binding_path = repo / ".tmp" / "test-binding.json"
        binding_path.parent.mkdir(parents=True)
        binding_path.write_text(json.dumps(json.loads(rendered)), encoding="utf-8")
        self.binding_manifest = binding_path
        return repo

    def _bounded_args(self, *extra):
        return ["-WorkflowFile", "n8n-workflows/member_forms_gateway_ingest.workflow.json", "-BindingManifestFile", str(self.binding_manifest), "-ProjectId", "project_ref", *extra]

    def _run_import(self, extra_args, extra_env=None):
        env = os.environ.copy()
        env["PATH"] = str(self.stub_dir) + os.pathsep + env["PATH"]
        env["DOCKER_STUB_LOG"] = str(self.docker_log)
        # NODE_OPTIONS treats backslashes inside quotes as escapes; node accepts
        # forward slashes on Windows.
        env["NODE_OPTIONS"] = f'--require "{self.preload_path.as_posix()}"'
        env.pop("N8N_WORKFLOW_HOOK_SCRIPT", None)
        env.pop("N8N_WORKFLOW_HOOK_AUTOLOAD", None)
        if extra_env:
            env.update(extra_env)
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.repo / "n8n-workflows" / "scripts" / "import-n8n-workflows-live.ps1"),
            "-ContainerId",
            "stubcontainer",
        ] + extra_args
        return subprocess.run(
            command,
            cwd=str(self.repo),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )

    def _snapshot(self, root):
        entries = {}
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            entries[rel] = path.read_bytes() if path.is_file() else "<dir>"
        return entries

    def _docker_log_lines(self):
        if not self.docker_log.is_file():
            return []
        return [
            line
            for line in self.docker_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _assert_no_mutating_docker_calls(self):
        for line in self._docker_log_lines():
            first_word = line.split(" ", 1)[0]
            self.assertNotEqual(first_word, "cp", f"unexpected docker cp: {line}")
            self.assertNotEqual(first_word, "restart", f"unexpected docker restart: {line}")
            self.assertNotIn("import:workflow", line, f"unexpected live import: {line}")

    def _assert_no_leftover_planning_dirs(self):
        tmp_root = self.repo / ".tmp"
        if not tmp_root.is_dir():
            return
        leftovers = [
            entry.name
            for entry in tmp_root.iterdir()
            if entry.name.startswith("n8n-live-import-dryrun-")
        ]
        self.assertEqual(leftovers, [], "temporary dry-run planning directory was not removed")

    def test_dry_run_preserves_existing_prepared_dir_byte_for_byte(self):
        nested = self.configured_prepared_dir / "nested"
        nested.mkdir(parents=True)
        (self.configured_prepared_dir / "stale.live-import.json").write_bytes(
            b'{"sentinel": "prior staged import artifact"}\n'
        )
        (nested / "keep.txt").write_bytes(b"sentinel-bytes-123\n")
        before = self._snapshot(self.configured_prepared_dir)

        result = self._run_import(["-DryRun"])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Live n8n was not changed.", result.stdout)
        self.assertIn("was not read, cleared, created, or modified", result.stdout)
        self.assertEqual(before, self._snapshot(self.configured_prepared_dir))
        self._assert_no_leftover_planning_dirs()
        self._assert_no_mutating_docker_calls()

    def test_dry_run_does_not_create_absent_prepared_dir(self):
        self.assertFalse(self.configured_prepared_dir.exists())

        result = self._run_import(["-DryRun"])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Would import", result.stdout)
        self.assertFalse(
            self.configured_prepared_dir.exists(),
            "-DryRun must not create the configured prepared dir",
        )
        self._assert_no_leftover_planning_dirs()
        self._assert_no_mutating_docker_calls()

    def test_confirmed_import_gate_still_fails_closed_non_interactively(self):
        result = self._run_import([])

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            "Live import requires explicit confirmation",
            result.stdout + result.stderr,
        )
        self.assertIn("Live n8n was not changed", result.stdout + result.stderr)
        prepared_files = (
            sorted(path.name for path in self.configured_prepared_dir.glob("*.live-import.json"))
            if self.configured_prepared_dir.is_dir()
            else []
        )
        self.assertTrue(
            prepared_files,
            "import mode (non-dry-run) should still stage prepared files in the configured dir",
        )
        self._assert_no_mutating_docker_calls()

    def test_single_workflow_mode_plans_exactly_one_canonical_input(self):
        result = self._run_import(self._bounded_args("-DryRun"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Would import      : 1", result.stdout)
        self.assertIn("member_forms_gateway_ingest.workflow.json", result.stdout)
        for other in REAL_WORKFLOW_DIR.glob("*.workflow.json"):
            if other.name != "member_forms_gateway_ingest.workflow.json":
                self.assertNotIn(other.name, result.stdout)
        self._assert_no_mutating_docker_calls()

    def test_single_workflow_mode_rejects_noncanonical_file_and_broad_mode_mix(self):
        wrong = self._run_import([
            "-WorkflowFile", "n8n-workflows/member_create_uat_result_mapping.workflow.json", "-BindingManifestFile", str(self.binding_manifest), "-DryRun"
        ])
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("immediate canonical child", wrong.stdout + wrong.stderr)
        mixed = self._run_import([
            "-WorkflowFile", "n8n-workflows/member_forms_gateway_ingest.workflow.json",
            "-BindingManifestFile", str(self.binding_manifest), "-WorkflowDir", "n8n-workflows", "-DryRun",
        ])
        self.assertEqual(mixed.returncode, 1)
        self.assertIn("mutually exclusive", mixed.stdout + mixed.stderr)

    def test_single_workflow_mode_blocks_active_archived_scheduled_and_ambiguous_targets(self):
        canonical = json.loads((REAL_WORKFLOW_DIR / "member_forms_gateway_ingest.workflow.json").read_text(encoding="utf-8"))
        minimal = {
            "id": canonical["id"],
            "name": canonical["name"],
            "active": False,
            "isArchived": False,
            "nodes": [{"name": "Manual", "type": "n8n-nodes-base.manualTrigger", "parameters": {}}],
            "connections": {},
            "settings": canonical["settings"],
            "staticData": None,
        }
        cases = {}
        active = dict(minimal)
        active["active"] = True
        cases["active"] = [active]
        archived = dict(minimal)
        archived["isArchived"] = True
        cases["archived"] = [archived]
        scheduled = json.loads(json.dumps(minimal))
        scheduled["nodes"].append({"name": "Forbidden schedule", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {}})
        cases["scheduled"] = [scheduled]
        duplicate = json.loads(json.dumps(minimal))
        duplicate["id"] = "anotherCanonicalNameMatch"
        cases["ambiguous"] = [minimal, duplicate]

        for label, workflows in cases.items():
            with self.subTest(label=label):
                live_fixture = Path(str(self.docker_log) + ".workflows.json")
                live_fixture.write_text(json.dumps(workflows), encoding="utf-8")
                result = self._run_import(
                    self._bounded_args("-DryRun"),
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                if label == "ambiguous":
                    self.assertIn("ambiguous", (result.stdout + result.stderr).lower())
                else:
                    self.assertIn("BLOCK", result.stdout + result.stderr)
                self._assert_no_mutating_docker_calls()

    def test_bounded_mode_exports_only_exact_target_and_applies_exact_manifest(self):
        canonical = json.loads((REAL_WORKFLOW_DIR / "member_forms_gateway_ingest.workflow.json").read_text(encoding="utf-8"))
        target = json.loads(json.dumps(canonical))
        target["nodes"] = [{"name": "Manual", "type": "n8n-nodes-base.manualTrigger", "parameters": {}}]
        unrelated = {"id": "unrelated", "name": "Unrelated private workflow", "active": False, "nodes": [], "connections": {}, "settings": {}, "staticData": None}
        live_fixture = Path(str(self.docker_log) + ".workflows.json")
        live_fixture.write_text(json.dumps([target, unrelated]), encoding="utf-8")
        bindings = self.repo / ".n8n-local" / "n8n-credential-bindings.json"
        bindings.parent.mkdir(parents=True)
        bindings.write_bytes(b'{"sentinel":"unchanged"}\n')

        result = self._run_import(self._bounded_args())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Live import requires explicit confirmation", result.stdout + result.stderr)
        prepared = json.loads((self.configured_prepared_dir / "member_forms_gateway_ingest.workflow.live-import.json").read_text(encoding="utf-8"))
        config = next(node for node in prepared["nodes"] if node["name"] == "Repository-safe source configuration")
        values = {item["name"]: item["value"] for item in config["parameters"]["assignments"]["assignments"]}
        self.assertFalse(values["activation_enabled"])
        self.assertEqual(values["gateway_ingest_url"], "https://gateway.invalid/v1/source-events")
        google = next(node for node in prepared["nodes"] if node["name"].startswith("Google Forms single page"))
        self.assertEqual(set(google["credentials"]), {"googleOAuth2Api"})
        self.assertEqual(google["parameters"]["authentication"], "predefinedCredentialType")
        for name in ("Read durable source cursor", "Protected XB Gateway ingest (configured outside repo)", "Commit durable page checkpoint"):
            node = next(item for item in prepared["nodes"] if item["name"] == name)
            self.assertEqual(set(node["credentials"]), {"httpBearerAuth"})
            self.assertEqual(node["parameters"]["genericAuthType"], "httpBearerAuth")
            self.assertNotIn("Authorization", json.dumps(node))
        self.assertNotIn("QUESTION_ID_", next(node for node in prepared["nodes"] if node["name"] == "Canonicalize source page")["parameters"]["jsCode"])
        self.assertEqual(bindings.read_bytes(), b'{"sentinel":"unchanged"}\n')
        calls = self._docker_log_lines()
        self.assertTrue(any("list:workflow" in line for line in calls))
        self.assertTrue(any("--id=xbMemberGatewayTemplate02" in line for line in calls))
        self.assertFalse(any("--all" in line for line in calls))
        self.assertFalse(any("--id=unrelated" in line for line in calls))
        self._assert_no_mutating_docker_calls()

    def test_bounded_metadata_rejects_case_distinct_duplicates_and_name_id_collisions(self):
        cases = {
            "case-distinct": [{"id": "AbC", "name": "one"}, {"id": "abc", "name": "two"}],
            "contradictory-duplicate": [{"id": "xbMemberGatewayTemplate02", "name": "one"}, {"id": "xbMemberGatewayTemplate02", "name": "two"}],
            "name-id-collision": [{"id": "Member Gateway - Google Forms durable source adapter (inactive)", "name": "other"}],
        }
        for label, workflows in cases.items():
            with self.subTest(label=label):
                Path(str(self.docker_log) + ".workflows.json").write_text(json.dumps(workflows), encoding="utf-8")
                result = self._run_import(self._bounded_args("-DryRun"))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("ambiguous", (result.stdout + result.stderr).lower() if label == "name-id-collision" else (result.stdout + result.stderr).lower().replace("duplicate", "ambiguous"))
                self._assert_no_mutating_docker_calls()

    def test_bounded_manifest_and_workflow_drift_fail_before_import(self):
        original = json.loads(self.binding_manifest.read_text(encoding="utf-8"))
        cases = []
        wrong_type = json.loads(json.dumps(original)); wrong_type["credentials"]["gateway_source_bearer"]["type"] = "httpBasicAuth"; cases.append(("credential", wrong_type))
        extra = json.loads(json.dumps(original)); extra["extra"] = True; cases.append(("shape", extra))
        active = json.loads(json.dumps(original)); active["posture"]["active"] = True; cases.append(("posture", active))
        stale = json.loads(json.dumps(original)); stale["deployment_admission"]["commit"] = "0" * 40; cases.append(("admission", stale))
        missing_cursor = json.loads(json.dumps(original)); missing_cursor["source"]["cursor_binding_reference"] = ""; cases.append(("missing-cursor-binding", missing_cursor))
        missing_watermark = json.loads(json.dumps(original)); missing_watermark["source"]["watermark_binding_reference"] = ""; cases.append(("missing-watermark-binding", missing_watermark))
        duplicate_state_binding = json.loads(json.dumps(original)); duplicate_state_binding["source"]["watermark_binding_reference"] = duplicate_state_binding["source"]["cursor_binding_reference"]; cases.append(("duplicate-state-binding", duplicate_state_binding))
        wrong_project = json.loads(json.dumps(original)); wrong_project["target"]["project_reference"] = "other_project"; cases.append(("wrong-project", wrong_project))
        wrong_workflow = json.loads(json.dumps(original)); wrong_workflow["target"]["workflow_reference"] = "other_workflow"; cases.append(("wrong-workflow", wrong_workflow))
        missing_preimage = json.loads(json.dumps(original)); missing_preimage["target"]["preimage_reference"] = ""; cases.append(("missing-preimage-binding", missing_preimage))
        for label, manifest in cases:
            with self.subTest(label=label):
                self.binding_manifest.write_text(json.dumps(manifest), encoding="utf-8")
                result = self._run_import(self._bounded_args("-DryRun"))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self._assert_no_mutating_docker_calls()
        self.binding_manifest.write_text(json.dumps(original), encoding="utf-8")
        for label, extra_args in (
            ("project-conflict", ("-ProjectId", "other_project")),
            ("user-context", ("-UserId", "user_ref")),
            ("missing-project-context", ("-ProjectId", "")),
        ):
            with self.subTest(label=label):
                result = self._run_import(self._bounded_args("-DryRun", *extra_args))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self._assert_no_mutating_docker_calls()

    def test_bounded_mode_rejects_generic_hooks_before_any_hook_or_mutation(self):
        explicit_sentinel = self.repo / "explicit-hook-sentinel.txt"
        explicit_hook = self.repo / "explicit-hook.cjs"
        explicit_hook.write_text(
            f"require('fs').writeFileSync({json.dumps(explicit_sentinel.as_posix())}, 'executed');\n",
            encoding="utf-8",
        )
        explicit = self._run_import(
            self._bounded_args("-DryRun"),
            {"N8N_WORKFLOW_HOOK_SCRIPT": str(explicit_hook)},
        )
        self.assertEqual(explicit.returncode, 1, explicit.stdout + explicit.stderr)
        self.assertIn("forbids", (explicit.stdout + explicit.stderr).lower())
        self.assertFalse(explicit_sentinel.exists())
        self._assert_no_mutating_docker_calls()

        autoload_sentinel = self.repo / "autoload-hook-sentinel.txt"
        autoload_dir = self.repo / "scripts"
        autoload_dir.mkdir(parents=True, exist_ok=True)
        autoload_hook = autoload_dir / "n8n-workflow-hooks.cjs"
        autoload_hook.write_text(
            f"require('fs').writeFileSync({json.dumps(autoload_sentinel.as_posix())}, 'executed');\n",
            encoding="utf-8",
        )
        autoload = self._run_import(
            self._bounded_args("-DryRun"),
            {"N8N_WORKFLOW_HOOK_AUTOLOAD": "1"},
        )
        self.assertEqual(autoload.returncode, 1, autoload.stdout + autoload.stderr)
        self.assertIn("forbids", (autoload.stdout + autoload.stderr).lower())
        self.assertFalse(autoload_sentinel.exists())
        self._assert_no_mutating_docker_calls()

    def test_failed_readback_retry_preserves_original_durable_preimage(self):
        canonical = json.loads((REAL_WORKFLOW_DIR / "member_forms_gateway_ingest.workflow.json").read_text(encoding="utf-8"))
        preimage = json.loads(json.dumps(canonical))
        preimage["nodes"] = [{"name": "Manual", "type": "n8n-nodes-base.manualTrigger", "parameters": {}}]
        Path(str(self.docker_log) + ".workflows.json").write_text(json.dumps([preimage]), encoding="utf-8")
        first = self._run_import(self._bounded_args("-ConfirmLiveImport"), {"DOCKER_STUB_READBACK_STALE": "1"})
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        receipts = list((self.repo / ".n8n-local/member-gateway-recovery").rglob("recovery-receipt.json"))
        self.assertEqual(len(receipts), 1)
        receipt_before = receipts[0].read_bytes()
        preimage_before = (receipts[0].parent / "preimage.workflow.json").read_bytes()
        second = self._run_import(self._bounded_args("-ConfirmLiveImport"), {"DOCKER_STUB_READBACK_STALE": "1"})
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        self.assertEqual(receipts[0].read_bytes(), receipt_before)
        self.assertEqual((receipts[0].parent / "preimage.workflow.json").read_bytes(), preimage_before)

    def test_recovery_reuse_rejects_deleted_modified_and_substituted_evidence(self):
        cases = ("deleted-preimage", "modified-preimage", "corrupt-receipt", "wrong-schema", "wrong-hash", "wrong-state")
        for label in cases:
            with self.subTest(label=label):
                shutil.rmtree(self.repo / ".n8n-local" / "member-gateway-recovery", ignore_errors=True)
                shutil.rmtree(self.configured_prepared_dir, ignore_errors=True)
                for path in (
                    self.docker_log,
                    Path(str(self.docker_log) + ".workflows.json"),
                    Path(str(self.docker_log) + ".imported.json"),
                ):
                    path.unlink(missing_ok=True)
                canonical = json.loads((REAL_WORKFLOW_DIR / "member_forms_gateway_ingest.workflow.json").read_text(encoding="utf-8"))
                preimage = json.loads(json.dumps(canonical))
                preimage["nodes"] = [{"name": "Manual", "type": "n8n-nodes-base.manualTrigger", "parameters": {}}]
                Path(str(self.docker_log) + ".workflows.json").write_text(json.dumps([preimage]), encoding="utf-8")
                first = self._run_import(self._bounded_args("-ConfirmLiveImport"), {"DOCKER_STUB_READBACK_STALE": "1"})
                self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
                receipts = list((self.repo / ".n8n-local/member-gateway-recovery").rglob("recovery-receipt.json"))
                self.assertEqual(len(receipts), 1)
                receipt_path = receipts[0]
                preimage_path = receipt_path.parent / "preimage.workflow.json"
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if label == "deleted-preimage":
                    preimage_path.unlink()
                elif label == "modified-preimage":
                    preimage_path.write_bytes(b'{"substituted":true}\n')
                elif label == "corrupt-receipt":
                    receipt_path.write_text("{", encoding="utf-8")
                elif label == "wrong-schema":
                    receipt["schema_version"] = "wrong.schema"
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                elif label == "wrong-hash":
                    receipt["prepared_sha256"] = "0" * 64
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                else:
                    receipt["preimage_state"] = "absent"
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                mutating_before = [
                    line for line in self._docker_log_lines()
                    if line.startswith("cp ") or "import:workflow" in line or line.startswith("restart ")
                ]
                second = self._run_import(self._bounded_args("-ConfirmLiveImport"), {"DOCKER_STUB_READBACK_STALE": "1"})
                self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
                mutating_after = [
                    line for line in self._docker_log_lines()
                    if line.startswith("cp ") or "import:workflow" in line or line.startswith("restart ")
                ]
                self.assertEqual(mutating_after, mutating_before)

    def test_partial_create_retry_repairs_receipt_without_repeating_mutation(self):
        Path(str(self.docker_log) + ".workflows.json").write_text("[]", encoding="utf-8")
        imported = Path(str(self.docker_log) + ".imported.json")
        imported.unlink(missing_ok=True)
        first = self._run_import(
            self._bounded_args("-ConfirmLiveImport"),
            {"DOCKER_STUB_READBACK_FAIL": "1"},
        )
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        calls_before = self._docker_log_lines()
        second = self._run_import(
            self._bounded_args("-ConfirmLiveImport"),
            {"DOCKER_STUB_INCLUDE_IMPORTED": "1"},
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        calls_after = self._docker_log_lines()
        self.assertEqual(
            [line for line in calls_after if line.startswith("cp ") or "import:workflow" in line],
            [line for line in calls_before if line.startswith("cp ") or "import:workflow" in line],
        )
        receipts = list((self.repo / ".n8n-local/member-gateway-recovery").rglob("recovery-receipt.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["preimage_state"], "absent")
        creation = json.loads((receipts[0].parent / "creation-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(creation["ownership"], "created_by_this_transaction")

    def test_partial_create_retry_rejects_nonmatching_existing_target(self):
        Path(str(self.docker_log) + ".workflows.json").write_text("[]", encoding="utf-8")
        imported = Path(str(self.docker_log) + ".imported.json")
        imported.unlink(missing_ok=True)
        first = self._run_import(
            self._bounded_args("-ConfirmLiveImport"),
            {"DOCKER_STUB_READBACK_FAIL": "1"},
        )
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        changed = json.loads(imported.read_text(encoding="utf-8"))
        changed["nodes"] = []
        imported.write_text(json.dumps(changed), encoding="utf-8")
        calls_before = self._docker_log_lines()
        second = self._run_import(
            self._bounded_args("-ConfirmLiveImport"),
            {"DOCKER_STUB_INCLUDE_IMPORTED": "1"},
        )
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        self.assertEqual(
            [line for line in self._docker_log_lines() if line.startswith("cp ") or "import:workflow" in line],
            [line for line in calls_before if line.startswith("cp ") or "import:workflow" in line],
        )

    def test_new_target_success_persists_absence_and_creation_receipts(self):
        Path(str(self.docker_log) + ".workflows.json").write_text("[]", encoding="utf-8")
        imported = Path(str(self.docker_log) + ".imported.json")
        imported.unlink(missing_ok=True)
        result = self._run_import(self._bounded_args("-ConfirmLiveImport"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipts = list((self.repo / ".n8n-local/member-gateway-recovery").rglob("recovery-receipt.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["preimage_state"], "absent")
        creation = json.loads((receipts[0].parent / "creation-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(creation["ownership"], "created_by_this_transaction")


if __name__ == "__main__":
    unittest.main()
