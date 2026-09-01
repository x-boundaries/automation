import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/member-gateway-tests.yml"


class MemberGatewayCiTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_has_narrow_triggers_and_read_only_permissions(self):
        self.assertIn("pull_request:", self.text)
        self.assertIn("push:", self.text)
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("permissions:\n  contents: read", self.text)
        for broad_path in (
            '"scripts/**"',
            '"tests/**"',
            '"n8n-workflows/**"',
            '"docs/**"',
            '"**"',
        ):
            self.assertNotIn(broad_path, self.text)
        self.assertIn('"member_gateway/**"', self.text)

    def test_every_checkout_binds_to_literal_event_head(self):
        head_ref = chr(36) + "{{ github.event.pull_request.head.sha || github.sha }}"
        self.assertGreaterEqual(self.text.count("ref: " + head_ref), 4)
        self.assertGreaterEqual(self.text.count("git rev-parse HEAD"), 4)
        self.assertGreaterEqual(self.text.count("EXPECTED_SHA"), 4)
        self.assertIn("fetch-depth: 0", self.text)
        self.assertIn("persist-credentials: false", self.text)

    def test_windows_full_offline_regression_provisions_tzdata_before_suite(self):
        job = self.text.split("  full-offline-regression:", 1)[1]
        setup_index = job.index("      - uses: actions/setup-python@v5")
        install_index = job.index(
            "      - name: Install tzdata (Windows Python has no system zoneinfo database)"
        )
        suite_index = job.index("run: python tests/_run_ci_full_suite.py")
        self.assertLess(setup_index, install_index)
        self.assertLess(install_index, suite_index)
        self.assertIn(
            "shell: pwsh\n        run: python -m pip install --quiet tzdata",
            job,
        )

    def test_ci_contains_no_live_or_mutating_operation(self):
        for forbidden in (
            r"\$\{\{\s*secrets\.",
            r"\bn8n_live\b",
            r"\bdocker(?:-compose)?\b",
            r"\bInvoke-RestMethod\b",
            r"\bdeployment\b",
            r"\bdeploy(?:ment)?\b",
            r"\bnpm\s+install\b",
            r"\bpip\s+install\b(?!\s+--quiet\s+tzdata(?:\s|$))",
        ):
            self.assertIsNone(re.search(forbidden, self.text, re.IGNORECASE), forbidden)
        self.assertIn("offline", self.text.lower())
        self.assertIn("member-gateway-tests.yml", self.text)


if __name__ == "__main__":
    unittest.main()
