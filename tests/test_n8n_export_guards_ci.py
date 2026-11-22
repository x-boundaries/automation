"""Pins `.github/workflows/n8n-export-guards.yml` (#334 G2 L2).

The two repo-wide n8n export guards (tests/test_no_telegram_nodes.py, #288 T2, and
tests/test_n8n3_always_output_data_guard.py, #332 T4) must run on every pull request that changes
anything under n8n-workflows/, not only when a listed path of another workflow changes. This test
reads the workflow as text (no YAML library in CI) and fails if the trigger is narrowed, a guard is
dropped from the run line, or the run is made to pass whatever the guards say. It also pins the
second job, live-import-dry-run (#316 G2 L7): the stubbed live-import helper contract test on a Windows
runner, failing when any of its tests is skipped. Offline, files only.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/n8n-export-guards.yml"

TRIGGER_PATHS = (
    "n8n-workflows/**",
    "n8n-workflow-policy.json",
    "tests/test_import_n8n_workflows_live_dry_run.py",
    "tests/test_no_telegram_nodes.py",
    "tests/test_n8n3_always_output_data_guard.py",
    "tests/test_n8n_export_guards_ci.py",
    ".github/workflows/n8n-export-guards.yml",
)
RUN_LINE = ("run: python -m unittest tests.test_no_telegram_nodes tests.test_n8n3_always_output_data_guard "
            "tests.test_n8n_export_guards_ci -v")
JOBS = ("guards", "live-import-dry-run")
DRY_RUN_LINES = (
    "python -m unittest tests.test_import_n8n_workflows_live_dry_run -v 2>&1 | tr -d '\\r' | tee live-import-dry-run.log",
    'grep -qx "OK" live-import-dry-run.log',
)
EXACT_HEAD_LINES = (
    "ref: ${{ github.event.pull_request.head.sha || github.sha }}",
    "persist-credentials: false",
    'test -n "${EXPECTED_SHA}"',
    'test "${ACTUAL_SHA}" = "${EXPECTED_SHA}"',
)


class N8nExportGuardsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.lines = [line.strip() for line in self.text.splitlines()]

    def test_triggers_on_any_n8n_export_change(self):
        on_block = self.text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        listed = re.findall(r'^\s+- "([^"]+)"$', on_block, re.MULTILINE)
        self.assertEqual(tuple(listed), TRIGGER_PATHS)
        self.assertIn("  pull_request:\n    paths:\n", on_block)
        self.assertNotIn("paths-ignore", self.text)
        self.assertNotIn("branches", on_block)

    def jobs(self) -> dict:
        body = self.text.split("\njobs:\n", 1)[1]
        parts = re.split(r"(?m)^  ([a-z][a-z-]*):\n", body)
        self.assertEqual(parts[0], "")
        return dict(zip(parts[1::2], parts[2::2]))

    def test_runs_both_guards_on_the_exact_head(self):
        self.assertEqual(self.lines.count(RUN_LINE), 1)
        jobs = self.jobs()
        self.assertEqual(tuple(jobs), JOBS)
        self.assertIn(RUN_LINE, [line.strip() for line in jobs["guards"].splitlines()])
        for name, job in jobs.items():
            lines = [line.strip() for line in job.splitlines()]
            for line in EXACT_HEAD_LINES:
                self.assertEqual(lines.count(line), 1, (name, line))

    def test_the_live_import_dry_run_job_runs_on_windows_and_fails_on_a_skip(self):
        # #316 G2 L7. The module skips itself off Windows or without PowerShell or node, and a skipped run
        # still ends "OK (skipped=N)"; only a bare "OK" line passes, so a skip turns the job red.
        job = self.jobs()["live-import-dry-run"]
        lines = [line.strip() for line in job.splitlines() if line.strip()]
        self.assertEqual(lines[0], "runs-on: windows-latest")
        self.assertEqual(lines[-3:], ["run: |", *DRY_RUN_LINES])
        self.assertEqual(lines[-4], "shell: bash")  # GitHub's bash shell runs with -e and pipefail
        self.assertIn('node-version: "20"', lines)
        for line in DRY_RUN_LINES:
            self.assertEqual(self.lines.count(line), 1, line)

    def test_permissions_are_exactly_contents_read(self):
        # #337 G2 L1 (C5): one top-level block, nothing more than contents: read, and no job-level block.
        self.assertEqual(self.text.count("permissions:"), 1)
        self.assertIn("\npermissions:\n  contents: read\n\njobs:\n", self.text)

    def test_no_secret_is_referenced(self):
        # #337 G2 L1 (C11): the guards need no secret and no token.
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("GITHUB_TOKEN", self.text)

    def test_the_run_cannot_pass_whatever_the_guards_say(self):
        for masked in ("continue-on-error", "|| true", "|| exit 0", "set +e"):
            self.assertNotIn(masked, self.text, masked)
        # #337 G2 L1 (C3): "- if: ..." written as the first key of a step is a condition too.
        conditions = [line for line in self.lines if line.removeprefix("- ").lstrip().startswith("if:")]
        self.assertFalse(conditions, "no step or job condition")

    def test_production_instance_pins_are_recorded(self):
        # #332 F1 and F4: the production instance pins live in one place, n8n-workflows/README.md, which this job
        # tests on every change under n8n-workflows/. Values only; no private path or setting.
        readme = (ROOT / "n8n-workflows" / "README.md").read_text(encoding="utf-8")
        start = readme.index("## Production n8n instance pins (#332)")
        section = readme[start:readme.index("## Current Workflow Exports", start)]
        for phrase in (
            "| `N8N_IMAGE` | `docker.n8n.io/n8nio/n8n:2.48.5` |",
            "| `N8N_RUNNERS_TASK_TIMEOUT` | `300` |",
            "Change `N8N_IMAGE` only on purpose, for a 3.x upgrade, after a backup and an approved G-plan in #332.",
            "backups made before 21 Nov 2026 carry `N8N_IMAGE=docker.n8n.io/n8nio/n8n:stable`",
            "After a restore from one, check `N8N_IMAGE` and the running n8n version",
            # Controller note (hardening report, 22 Nov 2026): cloudflared stays unpinned by owner choice; the
            # compose and menu fixes; never re-copy the templates over the live files.
            "cloudflared is **not** pinned, on purpose: it stays on `cloudflare/cloudflared:latest`",
            "Do not add a cloudflared pin.",
            "the production compose fails closed when `N8N_IMAGE` is missing (`${N8N_IMAGE:?...}`, no `:stable` "
            "fallback)",
            # G2 #350 L1: which menu actions pull an image.
            "the menu's Restart and Start tunnel recreate with `--pull never`, so Restart and Start tunnel never pull; "
            "Update and Restore do (Restore uses the backup's `N8N_IMAGE`, see below)",
            "plain Start pulls only when the image is missing locally",
            "cloudflared receives only `TUNNEL_TOKEN`, not the whole `.env`",
            "**Never re-copy the setup templates over the live files.**",
            "Re-copying either one would undo these fixes.",
        ):
            self.assertIn(phrase, section)
        self.assertNotIn("CLOUDFLARED_IMAGE", section)
        self.assertNotIn("so only Update pulls an image", section)

    def test_energygrid_monitor_status_is_email(self):
        # G2 #350 (pre-existing): the live run monitor moved to email at live step E1 (20 Nov 2026, #288).
        readme = (ROOT / "n8n-workflows" / "README.md").read_text(encoding="utf-8")
        self.assertIn("sends its alerts by email through `XB Ops Alert Email` since live step E1 on 20 Nov 2026 (#288); it "
                      "no longer uses Telegram", readme)
        self.assertNotIn("still sends by Telegram", readme)

    def test_the_guard_modules_exist(self):
        for name in ("test_no_telegram_nodes.py", "test_n8n3_always_output_data_guard.py"):
            self.assertTrue((ROOT / "tests" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
