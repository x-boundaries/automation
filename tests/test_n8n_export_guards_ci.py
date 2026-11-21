"""Pins `.github/workflows/n8n-export-guards.yml` (#334 G2 L2).

The two repo-wide n8n export guards (tests/test_no_telegram_nodes.py, #288 T2, and
tests/test_n8n3_always_output_data_guard.py, #332 T4) must run on every pull request that changes
anything under n8n-workflows/, not only when a listed path of another workflow changes. This test
reads the workflow as text (no YAML library in CI) and fails if the trigger is narrowed, a guard is
dropped from the run line, or the run is made to pass whatever the guards say. Offline, files only.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/n8n-export-guards.yml"

TRIGGER_PATHS = (
    "n8n-workflows/**",
    "tests/test_no_telegram_nodes.py",
    "tests/test_n8n3_always_output_data_guard.py",
    "tests/test_n8n_export_guards_ci.py",
    ".github/workflows/n8n-export-guards.yml",
)
RUN_LINE = ("run: python -m unittest tests.test_no_telegram_nodes tests.test_n8n3_always_output_data_guard "
            "tests.test_n8n_export_guards_ci -v")


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

    def test_runs_both_guards_on_the_exact_head(self):
        self.assertEqual(self.lines.count(RUN_LINE), 1)
        self.assertIn("ref: ${{ github.event.pull_request.head.sha || github.sha }}", self.lines)
        self.assertIn("persist-credentials: false", self.lines)
        self.assertIn('test "${ACTUAL_SHA}" = "${EXPECTED_SHA}"', self.lines)
        self.assertIn("contents: read", self.lines)

    def test_the_run_cannot_pass_whatever_the_guards_say(self):
        for masked in ("continue-on-error", "|| true", "|| exit 0", "set +e"):
            self.assertNotIn(masked, self.text, masked)
        self.assertFalse([line for line in self.lines if line.startswith("if:")], "no step or job condition")

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
            "the menu's Restart and Start tunnel recreate with `--pull never`, so only Update pulls an image",
            "cloudflared receives only `TUNNEL_TOKEN`, not the whole `.env`",
            "**Never re-copy the setup templates over the live files.**",
            "Re-copying either one would undo these fixes.",
        ):
            self.assertIn(phrase, section)
        self.assertNotIn("CLOUDFLARED_IMAGE", section)

    def test_the_guard_modules_exist(self):
        for name in ("test_no_telegram_nodes.py", "test_n8n3_always_output_data_guard.py"):
            self.assertTrue((ROOT / "tests" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
