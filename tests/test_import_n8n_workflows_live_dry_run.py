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
  const args = process.argv.slice(1);
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
    process.stderr.write('No workflows found with specified filters\n');
    process.exit(1);
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
        (repo / ".git").mkdir(parents=True)
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
        return repo

    def _run_import(self, extra_args):
        env = os.environ.copy()
        env["PATH"] = str(self.stub_dir) + os.pathsep + env["PATH"]
        env["DOCKER_STUB_LOG"] = str(self.docker_log)
        # NODE_OPTIONS treats backslashes inside quotes as escapes; node accepts
        # forward slashes on Windows.
        env["NODE_OPTIONS"] = f'--require "{self.preload_path.as_posix()}"'
        env.pop("N8N_WORKFLOW_HOOK_SCRIPT", None)
        env.pop("N8N_WORKFLOW_HOOK_AUTOLOAD", None)
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


if __name__ == "__main__":
    unittest.main()
