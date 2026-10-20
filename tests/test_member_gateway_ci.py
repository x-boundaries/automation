import contextlib
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/member-gateway-tests.yml"
FULL_SUITE_RUNNER = ROOT / "tests/_run_ci_full_suite.py"
FULL_SUITE_COMMAND = "python tests/_run_ci_full_suite.py --skip-member-gateway-targeted"


def _load_full_suite_runner():
    spec = importlib.util.spec_from_file_location("_run_ci_full_suite", FULL_SUITE_RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _targeted_test_steps(runner):
    """Exact text of the targeted test steps: whole modules, no filter, condition or trailing command."""
    powershell = runner.MEMBER_GATEWAY_TARGETED_JOBS["powershell-static"]
    (tooling,) = runner.MEMBER_GATEWAY_TARGETED_JOBS["powershell-worker-tooling"]
    bounded = runner.MEMBER_GATEWAY_TARGETED_JOBS["bounded-import-security"]
    return {
        "powershell-static": {
            "Run worker and adapter offline behavioral tests": "run: >-\n          python tests/_ci_annotated_unittest.py "
            + "\n          ".join("tests." + module for module in powershell),
        },
        "powershell-worker-tooling": {
            "Run worker tooling offline tests": "run: python tests/_ci_annotated_unittest.py tests.%s" % tooling,
        },
        "bounded-import-security": {
            "Run bounded import security regressions": "run: python -m unittest tests.%s -v" % bounded[0],
            "Run welcome-email bounded import security regressions": "run: python -m unittest tests.%s -v" % bounded[1],
        },
    }


def _job_block(text, name):
    block = text.split("\n  " + name + ":\n", 1)[1]
    following = re.search(r"\n  [a-z0-9-]+:\n", block)
    return block[: following.start()] if following else block


class MemberGatewayCiTests(unittest.TestCase):
    TZDATA_COMMAND = "python -m pip install --quiet tzdata"
    # The only other permitted install: the exact-pinned PyCA AEAD used by the
    # Shopify M1 protected payload, in the gateway-tests job only.
    CRYPTO_COMMAND = 'python -m pip install --quiet "cryptography==46.0.7"'

    def _assert_exact_crypto_allowance(self, text):
        job = text.split("  gateway-tests:", 1)[1].split("\n  powershell-static:", 1)[0]
        line = "        run: " + self.CRYPTO_COMMAND
        self.assertEqual(text.count(self.CRYPTO_COMMAND), 1)
        self.assertEqual(job.count(line), 1)
        self.assertLess(job.index("      - uses: actions/setup-python@v5"), job.index(line))
        self.assertLess(job.index(line), job.index("python -m unittest discover -s member_gateway/tests"))

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
        self._assert_exact_crypto_allowance(text)
        remaining = remaining.replace("        run: " + self.CRYPTO_COMMAND, "", 1)
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

    def test_bounded_import_security_job_is_exact_offline_gate(self):
        self.assertEqual(
            self.text.count("run: python -m unittest tests.test_member_gateway_bounded_import_security -v"),
            1,
        )
        job = self.text.split("  bounded-import-security:\n", 1)[1].split("\n  n8n-offline:", 1)[0]
        self.assertIn("runs-on: windows-latest", job)
        self.assertIn("ref: ${{ github.event.pull_request.head.sha || github.sha }}", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("shell: pwsh", job)
        self.assertNotIn("secrets.", job)
        self.assertNotIn("docker", job.lower())
        self.assertNotIn("import:workflow", job)

    def _assert_parallel_full_suite_skips_only_targeted_windows_modules(self, text, runner):
        self.assertNotIn("needs:", text)
        full = _job_block(text, "full-offline-regression")
        self.assertEqual(text.count("run: python tests/_run_ci_full_suite.py"), 1)
        self.assertEqual(full.count("        run: " + FULL_SUITE_COMMAND + "\n"), 1)
        self.assertEqual(
            set(runner.MEMBER_GATEWAY_TARGETED_JOBS),
            {"powershell-static", "powershell-worker-tooling", "bounded-import-security"},
        )
        for name in ("full-offline-regression", *runner.MEMBER_GATEWAY_TARGETED_JOBS):
            job = _job_block(text, name)
            # Same OS, interpreter and asserted exact head as the parallel full suite.
            self.assertIn("    runs-on: windows-latest\n", job, name)
            self.assertIn('          python-version: "3.12"\n', job, name)
            self.assertIn("ref: ${{ github.event.pull_request.head.sha || github.sha }}", job, name)
            self.assertIn('test "${ACTUAL_SHA}" = "${EXPECTED_SHA}"', job, name)
            self.assertNotIn("continue-on-error", job, name)
            self.assertNotIn("\n    if:", job, name)
        for name, modules in runner.MEMBER_GATEWAY_TARGETED_JOBS.items():
            job = _job_block(text, name)
            self.assertEqual(tuple(re.findall(r"\btests\.(test_\w+)", job)), modules, name)
        self.assertEqual(len(runner.MEMBER_GATEWAY_TARGETED_MODULES), sum(map(len, runner.MEMBER_GATEWAY_TARGETED_JOBS.values())))
        self.assertEqual(set(_targeted_test_steps(runner)), set(runner.MEMBER_GATEWAY_TARGETED_JOBS))
        for name, steps in _targeted_test_steps(runner).items():
            job = _job_block(text, name)
            self.assertNotIn("strategy:", job, name)
            self.assertEqual(job.count("      - name: Run "), len(steps), name)
            for step_name, run in steps.items():
                marker = "      - name: " + step_name + "\n"
                self.assertEqual(job.count(marker), 1, step_name)
                step = marker + job.split(marker, 1)[1].split("\n      - ", 1)[0].rstrip("\n")
                self.assertEqual(step, marker + "        shell: pwsh\n        " + run, step_name)

    def test_full_suite_runs_in_parallel_and_skips_only_targeted_windows_modules(self):
        self._assert_parallel_full_suite_skips_only_targeted_windows_modules(self.text, _load_full_suite_runner())

    def test_parallel_full_suite_guard_rejects_drift(self):
        runner = _load_full_suite_runner()
        ps_job = _job_block(self.text, "powershell-static")
        tooling_job = _job_block(self.text, "powershell-worker-tooling")
        tooling_run = "run: python tests/_ci_annotated_unittest.py tests.test_member_gateway_worker_deployment"
        for mutated in (
            self.text.replace(tooling_run, "run: python tests/_ci_annotated_unittest.py", 1),
            self.text.replace(tooling_run, tooling_run + ".WorkerDeploymentTests", 1),
            self.text.replace(tooling_run, tooling_run + "; exit 0", 1),
            self.text.replace(tooling_run, tooling_run + " -k nothing_matches", 1),
            self.text.replace(
                "      - name: Run worker tooling offline tests\n",
                "      - name: Run worker tooling offline tests\n        if: ${{ false }}\n",
                1,
            ),
            self.text.replace(
                "      - name: Run worker tooling offline tests\n",
                "      - name: Run worker tooling offline tests\n        continue-on-error: true\n",
                1,
            ),
            self.text.replace(tooling_job, tooling_job.replace("    runs-on: windows-latest\n", "    if: false\n    runs-on: windows-latest\n", 1), 1),
            self.text.replace(tooling_job, tooling_job.replace("runs-on: windows-latest", "runs-on: ubuntu-latest"), 1),
            self.text.replace(
                tooling_job,
                tooling_job.replace(
                    "    runs-on: windows-latest\n",
                    "    runs-on: windows-latest\n    strategy:\n      matrix:\n        leg: [a]\n        exclude:\n          - leg: a\n",
                    1,
                ),
                1,
            ),
            self.text.replace(
                "          tests.test_directlocal_code_custody_ps\n",
                "          tests.test_directlocal_code_custody_ps\n          tests.test_member_gateway_worker_deployment\n",
                1,
            ),
            self.text.replace("    runs-on: windows-latest\n    steps:", "    runs-on: windows-latest\n    needs: [powershell-static]\n    steps:", 1),
            self.text.replace("          tests.test_directlocal_code_custody_ps\n", "", 1),
            self.text.replace("tests.test_member_welcome_email_bounded_import_security -v", "tests.test_member_gateway_ci -v", 1),
            self.text.replace(ps_job, ps_job.replace("runs-on: windows-latest", "runs-on: ubuntu-latest"), 1),
            self.text.replace(FULL_SUITE_COMMAND, "python tests/_run_ci_full_suite.py --skip-member-gateway-targeted --extra", 1),
            self.text.replace(
                "tests.test_member_gateway_bounded_import_security -v",
                "tests.test_member_gateway_bounded_import_security.NoSuchClass -v",
                1,
            ),
            self.text.replace(
                "python -m unittest tests.test_member_gateway_bounded_import_security -v",
                "python -m unittest -k nothing_matches tests.test_member_gateway_bounded_import_security -v",
                1,
            ),
            self.text.replace(
                "      - name: Run worker and adapter offline behavioral tests\n",
                "      - name: Run worker and adapter offline behavioral tests\n        if: ${{ false }}\n",
                1,
            ),
            self.text.replace(
                "tests.test_member_welcome_email_bounded_import_security -v",
                "tests.test_member_welcome_email_bounded_import_security -v; exit 0",
                1,
            ),
        ):
            self.assertNotEqual(mutated, self.text)
            with self.assertRaises(AssertionError):
                self._assert_parallel_full_suite_skips_only_targeted_windows_modules(mutated, runner)

    def test_shard_union_equals_accepted_original_test_set(self):
        runner = _load_full_suite_runner()
        loader = unittest.TestLoader()
        discovered = loader.discover(str(runner.TESTS_DIR))
        self.assertEqual(loader.errors, [])
        accepted_ids = {test.id() for test in runner.accepted_tests(discovered)}
        self.assertFalse(any("test_import_n8n_workflows_live_dry_run" in test_id for test_id in accepted_ids))
        kept, skipped = runner.partition(runner.accepted_tests(discovered), runner.MEMBER_GATEWAY_TARGETED_MODULES)
        kept_ids = {test.id() for test in kept}
        targeted_ids = set()
        for name, modules in runner.MEMBER_GATEWAY_TARGETED_JOBS.items():
            # Loaded independently, by module name, the way the targeted job loads them.
            shard = {test.id() for test in runner._flatten(loader.loadTestsFromNames(modules))}
            self.assertTrue(shard, name)
            self.assertEqual({test_id.split(".", 1)[0] for test_id in shard}, set(modules), name)
            targeted_ids |= shard
        self.assertEqual(loader.errors, [])
        self.assertEqual(kept_ids & targeted_ids, set())
        self.assertEqual(kept_ids | targeted_ids, accepted_ids)
        self.assertEqual({test.id() for test in skipped}, targeted_ids)

    def test_full_suite_runner_fails_closed_on_unknown_argument_or_missing_module(self):
        runner = _load_full_suite_runner()
        trivial = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(["--skip-everything"]), 2)
            with mock.patch.object(runner.unittest.TestLoader, "discover", return_value=trivial):
                self.assertEqual(runner.main([]), 0)
                self.assertEqual(runner.main([runner.SKIP_MEMBER_GATEWAY_TARGETED_FLAG]), 2)

    def test_full_suite_runner_reports_targeted_import_failure_and_still_fails_closed(self):
        runner = _load_full_suite_runner()
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_xb211_fake_targeted.py").write_text('raise ImportError("xb211 targeted import boom")\n', encoding="utf-8")
            Path(tmp, "test_xb211_fake_remainder.py").write_text(
                "import unittest\n\n\nclass Remainder(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
                encoding="utf-8",
            )
            saved_path = list(sys.path)
            try:
                with mock.patch.object(runner, "TESTS_DIR", Path(tmp)), mock.patch.object(
                    runner, "MEMBER_GATEWAY_TARGETED_MODULES", frozenset({"test_xb211_fake_targeted"})
                ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(runner.main([runner.SKIP_MEMBER_GATEWAY_TARGETED_FLAG]), 2)
            finally:
                sys.path[:] = saved_path
                for module in ("test_xb211_fake_targeted", "test_xb211_fake_remainder"):
                    sys.modules.pop(module, None)
        self.assertIn("xb211 targeted import boom", stdout.getvalue())
        self.assertIn("failing closed", stdout.getvalue())
        # The remainder (and the failed import itself) still ran before failing closed.
        self.assertIn("Ran 2 tests", stderr.getvalue())

    def test_bounded_import_files_are_narrowly_triggered(self):
        for required_path in (
            '"config/member_forms_gateway_bounded_import.v2.template.json"',
            '"n8n-workflows/scripts/import-member-forms-gateway-bounded.ps1"',
            '"scripts/install_ac2_member_gateway_worker.ps1"',
            '"scripts/launch_ac2_member_gateway_worker.ps1"',
            '"scripts/test_ac2_member_gateway_autocount_dependencies.ps1"',
            '"tests/test_member_gateway_worker_deployment.py"',
            '"tests/test_member_gateway_bounded_import_security.py"',
        ):
            self.assertEqual(self.text.count(required_path), 2, required_path)

    def test_worker_production_example_is_narrowly_triggered(self):
        required_path = '"config/ac2_member_gateway_worker.production.example.json"'
        self.assertEqual(self.text.count(required_path), 2, required_path)

    def test_crypto_allowance_is_exact_pinned_and_gateway_job_only(self):
        self._assert_exact_crypto_allowance(self.text)
        for mutated in (
            self.text.replace("cryptography==46.0.7", "cryptography", 1),
            self.text.replace("cryptography==46.0.7", "cryptography==46.0.7 requests", 1),
            self.text.replace("  full-offline-regression:\n", "  another-job:\n    steps:\n      - run: " + self.CRYPTO_COMMAND + "\n\n  full-offline-regression:\n", 1),
        ):
            with self.assertRaises(AssertionError):
                self._assert_exact_tzdata_allowance(mutated)

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
