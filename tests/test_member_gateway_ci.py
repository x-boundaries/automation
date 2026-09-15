import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/member-gateway-tests.yml"


class MemberGatewayCiTests(unittest.TestCase):
    TZDATA_COMMAND = "python -m pip install --quiet tzdata"

    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def _assert_exact_tzdata_allowance(self, text):
        job = text.split("  full-offline-regression:", 1)[1]
        command_line = re.compile(r"(?m)^[ \t]*run:[ \t]*" + re.escape(self.TZDATA_COMMAND) + r"[ \t]*$")
        self.assertEqual(command_line.findall(job), ["        run: " + self.TZDATA_COMMAND])
        setup_index = job.index("      - uses: actions/setup-python@v5")
        install_index = job.index("        run: " + self.TZDATA_COMMAND)
        suite_index = job.index("run: python tests/_run_ci_full_suite.py")
        self.assertLess(setup_index, install_index)
        self.assertLess(install_index, suite_index)
        remaining = text.replace("        run: " + self.TZDATA_COMMAND, "", 1)
        self.assertIsNone(re.search(r"\bpip\s+install\b", remaining, re.IGNORECASE))

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
        for required_path in (
            '"schemas/member_gateway_source_cursor.v1.schema.json"',
            '"schemas/member_gateway_operator_status.v1.schema.json"',
            '"schemas/member_gateway_operator_reconciliation.v1.schema.json"',
            '"member_gateway/migrations/0004_forms_ingest_cursor.sql"',
        ):
            self.assertEqual(self.text.count(required_path), 2, required_path)

    def test_every_checkout_binds_to_literal_event_head(self):
        head_ref = chr(36) + "{{ github.event.pull_request.head.sha || github.sha }}"
        self.assertGreaterEqual(self.text.count("ref: " + head_ref), 4)
        self.assertGreaterEqual(self.text.count("git rev-parse HEAD"), 4)
        self.assertGreaterEqual(self.text.count("EXPECTED_SHA"), 4)
        self.assertIn("fetch-depth: 0", self.text)
        self.assertIn("persist-credentials: false", self.text)

    def test_windows_full_offline_regression_provisions_tzdata_before_suite(self):
        self._assert_exact_tzdata_allowance(self.text)

    def test_tzdata_allowance_rejects_appended_package(self):
        mutated = self.text.replace(self.TZDATA_COMMAND, self.TZDATA_COMMAND + " requests", 1)
        with self.assertRaises(AssertionError):
            self._assert_exact_tzdata_allowance(mutated)

    def test_tzdata_allowance_rejects_second_install(self):
        mutated = self.text.replace(
            "  full-offline-regression:\n",
            "  gateway-tests:\n    steps:\n      - run: " + self.TZDATA_COMMAND + "\n\n  full-offline-regression:\n",
            1,
        )
        with self.assertRaises(AssertionError):
            self._assert_exact_tzdata_allowance(mutated)

    def test_tzdata_allowance_rejects_install_in_another_job(self):
        mutated = self.text.replace(
            "  full-offline-regression:\n",
            "  another-job:\n    steps:\n      - run: " + self.TZDATA_COMMAND + "\n\n  full-offline-regression:\n",
            1,
        )
        with self.assertRaises(AssertionError):
            self._assert_exact_tzdata_allowance(mutated)

    def test_tzdata_allowance_rejects_altered_authorised_command(self):
        mutated = self.text.replace(self.TZDATA_COMMAND, "python -m pip install tzdata", 1)
        with self.assertRaises(AssertionError):
            self._assert_exact_tzdata_allowance(mutated)

    def test_ci_contains_no_live_or_mutating_operation(self):
        for forbidden in (
            r"\$\{\{\s*secrets\.",
            r"\bn8n_live\b",
            r"\bdocker(?:-compose)?\b",
            r"\bInvoke-RestMethod\b",
            r"\bdeployment\b",
            r"\bdeploy(?:ment)?\b",
            r"\bnpm\s+install\b",
        ):
            self.assertIsNone(re.search(forbidden, self.text, re.IGNORECASE), forbidden)
        self._assert_exact_tzdata_allowance(self.text)
        self.assertIn("offline", self.text.lower())
        self.assertIn("member-gateway-tests.yml", self.text)


if __name__ == "__main__":
    unittest.main()
