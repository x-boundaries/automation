"""Drift guards for the member-create-uat windows-full-suite shard matrix (#211)."""

import collections
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
WORKFLOW = ROOT / ".github/workflows/member-create-uat-tests.yml"
FULL_SUITE_RUNNER = ROOT / "tests/_run_ci_full_suite.py"
WINDOWS_JOB = "windows-full-suite"
UBUNTU_JOB = "ubuntu-focused"
LIVE_IMPORT_MODULE = "test_import_n8n_workflows_live_dry_run"
HANDOFF_MODULE = "test_member_lookup_shared_folder_handoff"
SHARD_STEP = "Full repository test suite shard (excludes the Docker/n8n live-import module)"
WINDOWS_HEAD = (
    "  windows-full-suite:\n"
    "    runs-on: windows-latest\n"
    "    strategy:\n"
    "      fail-fast: false\n"
    "      matrix:\n"
    "        shard: [focused, mg-deployment, bounded-import, rest]\n"
    "    steps:\n"
)
WINDOWS_STEPS = [
    "uses: actions/checkout@v4",
    "name: Assert literal exact-head checkout",
    "uses: actions/setup-python@v5",
    "name: Install tzdata (Windows Python has no system zoneinfo database)",
    "name: PowerShell parse check (no execution)",
    "name: " + SHARD_STEP,
]
SHARD_STEP_TEXT = (
    "      - name: " + SHARD_STEP + "\n"
    "        shell: pwsh\n"
    "        # The n8n live-import dry-run module needs a Docker/n8n environment absent on\n"
    "        # stock CI; it and the complete suite are validated locally on the dev/VM host.\n"
    "        run: python tests/_run_ci_full_suite.py --shard ${{ matrix.shard }}"
)
UBUNTU_STEP = "Focused create UAT and shared-folder handoff tests (portable components)"


def _load_full_suite_runner():
    spec = importlib.util.spec_from_file_location("_run_ci_full_suite", FULL_SUITE_RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _job_block(text, name):
    block = "  " + name + ":\n" + text.split("\n  " + name + ":\n", 1)[1]
    following = re.search(r"\n(?:  #[^\n]*\n)*  [a-z0-9-]+:\n", block)
    return block[: following.start() + 1] if following else block


def _step(job, name):
    marker = "      - name: " + name + "\n"
    if job.count(marker) != 1:
        raise AssertionError("step %r appears %d times" % (name, job.count(marker)))
    return marker + job.split(marker, 1)[1].split("\n      - ", 1)[0].rstrip("\n")


def _ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _ids(test)
        else:
            yield test.id()


class MemberCreateUatCiTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def _assert_shard_matrix(self, text, runner):
        self.assertEqual(sorted(re.findall(r"(?m)^  ([a-z0-9-]+):\n", text.split("\njobs:\n", 1)[1])), [UBUNTU_JOB, WINDOWS_JOB])
        self.assertNotIn("needs:", text)
        self.assertEqual(text.count("python tests/_run_ci_full_suite.py"), 1)
        windows = _job_block(text, WINDOWS_JOB)
        # Exact job header: same runner, no job-level condition, every leg runs to completion.
        self.assertTrue(windows.startswith(WINDOWS_HEAD), windows[: len(WINDOWS_HEAD)])
        self.assertEqual(re.findall(r"(?m)^    ([a-z-]+):", windows), ["runs-on", "strategy", "steps"])
        legs = re.search(r"(?m)^        shard: \[([a-z, -]+)\]$", windows).group(1).split(", ")
        self.assertEqual(tuple(legs), runner.CREATE_UAT_SHARDS)
        self.assertEqual(tuple(runner.CREATE_UAT_SHARD_GROUPS) + (runner.REST_SHARD,), runner.CREATE_UAT_SHARDS)
        self.assertEqual(re.findall(r"(?m)^      - ((?:name|uses): .*)$", windows), WINDOWS_STEPS)
        for job in (windows, _job_block(text, UBUNTU_JOB)):
            for forbidden in (r"(?m)^\s*if:", "continue-on-error", r"\binclude:", r"\bexclude:", r"exit 0", r"\$LASTEXITCODE"):
                self.assertIsNone(re.search(forbidden, job), forbidden)
            self.assertIn('          python-version: "3.12"\n', job)
        self.assertNotIn("python -m unittest", windows)
        self.assertEqual(_step(windows, SHARD_STEP), SHARD_STEP_TEXT)
        self.assertIn("if ($failed) { exit 1 }", _step(windows, "PowerShell parse check (no execution)"))
        # The Windows focused shard stays the portable focused set minus the handoff module.
        ubuntu_modules = runner.CREATE_UAT_SHARD_GROUPS["focused"] + (HANDOFF_MODULE,)
        self.assertTrue(
            _step(_job_block(text, UBUNTU_JOB), UBUNTU_STEP).endswith(
                "        run: |\n          python -m unittest " + " ".join("tests." + module for module in ubuntu_modules)
            )
        )

    def test_windows_full_suite_is_an_exact_shard_matrix(self):
        self._assert_shard_matrix(self.text, _load_full_suite_runner())

    def test_shard_matrix_guard_rejects_drift(self):
        runner = _load_full_suite_runner()
        windows = _job_block(self.text, WINDOWS_JOB)
        legs = "        shard: [focused, mg-deployment, bounded-import, rest]\n"
        run = "run: python tests/_run_ci_full_suite.py --shard ${{ matrix.shard }}"
        handoff = "tests.test_member_lookup_shared_folder_handoff"
        for mutated in (
            self.text.replace(legs, "        shard: [focused, mg-deployment, bounded-import]\n", 1),
            self.text.replace(legs, "        shard: [focused, bounded-import, mg-deployment, rest]\n", 1),
            self.text.replace(legs, legs + "        include:\n          - shard: rest\n", 1),
            self.text.replace(legs, legs + "        exclude:\n          - shard: rest\n", 1),
            self.text.replace("      fail-fast: false\n", "      fail-fast: true\n", 1),
            self.text.replace("      fail-fast: false\n", "", 1),
            self.text.replace(windows, windows.replace("    runs-on: windows-latest\n", "    if: false\n    runs-on: windows-latest\n", 1), 1),
            self.text.replace(windows, windows.replace("runs-on: windows-latest", "runs-on: ubuntu-latest"), 1),
            self.text.replace(windows, windows.rstrip("\n") + "\n    continue-on-error: true\n", 1),
            self.text.replace("      - name: " + SHARD_STEP + "\n", "      - name: " + SHARD_STEP + "\n        if: ${{ false }}\n", 1),
            self.text.replace("      - name: " + SHARD_STEP + "\n", "      - name: " + SHARD_STEP + "\n        continue-on-error: true\n", 1),
            self.text.replace(run, run + "; exit 0", 1),
            self.text.replace(run, run + " || true", 1),
            self.text.replace(run, run + " -k nothing_matches", 1),
            self.text.replace(run, "run: python tests/_run_ci_full_suite.py --shard rest", 1),
            self.text.replace(run, "run: python tests/_run_ci_full_suite.py", 1),
            self.text.replace(
                "          if ($failed) { exit 1 }\n      - name: " + SHARD_STEP,
                "          if ($failed) { exit 1 }\n      - name: Extra\n        run: python -m unittest tests.test_member_create_uat_contract\n      - name: " + SHARD_STEP,
                1,
            ),
            self.text.replace(handoff, handoff + ".NoSuchClass", 1),
            self.text.replace("          python -m unittest tests.test_member_create_uat_contract", "          python -m unittest -k nothing tests.test_member_create_uat_contract", 1),
            self.text.replace(handoff + "\n", handoff + "; exit 0\n", 1),
            self.text.replace("        run: |\n          python -m unittest", "        continue-on-error: true\n        run: |\n          python -m unittest", 1),
        ):
            self.assertNotEqual(mutated, self.text)
            with self.assertRaises((AssertionError, AttributeError)):
                self._assert_shard_matrix(mutated, runner)

    def test_shards_partition_the_accepted_set_test_by_test(self):
        runner = _load_full_suite_runner()
        # Independent reference: a plain discover, minus the one environment-gated module.
        plain_loader = unittest.TestLoader()
        plain = [test_id for test_id in _ids(plain_loader.discover(str(ROOT / "tests"))) if test_id.split(".", 1)[0] != LIVE_IMPORT_MODULE]
        self.assertEqual(plain_loader.errors, [])
        self.assertTrue(plain)
        loader = unittest.TestLoader()
        accepted = runner.accepted_tests(loader.discover(str(runner.TESTS_DIR)))
        self.assertEqual(loader.errors, [])
        union = collections.Counter()
        for shard in runner.CREATE_UAT_SHARDS:
            selected, required = runner.shard_tests(accepted, shard)
            ids = [test.id() for test in selected]
            self.assertTrue(ids, shard)
            union.update(ids)
            if shard == runner.REST_SHARD:
                self.assertEqual(required, frozenset())
                continue
            modules = runner.CREATE_UAT_SHARD_GROUPS[shard]
            self.assertEqual(required, frozenset(modules), shard)
            # Loaded independently, by module name, these are exactly the shard's tests.
            by_name = sorted(_ids(unittest.TestLoader().loadTestsFromNames(modules)))
            self.assertEqual(sorted(ids), by_name, shard)
            self.assertEqual({test_id.split(".", 1)[0] for test_id in ids}, set(modules), shard)
        self.assertEqual(union, collections.Counter(plain))
        self.assertEqual(max(union.values()), 1)
        named = [module for modules in runner.CREATE_UAT_SHARD_GROUPS.values() for module in modules]
        self.assertEqual(len(named), len(set(named)))

    def _run_in_tmp(self, modules, groups, argv, fake_run=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        runner = _load_full_suite_runner()
        patches = [mock.patch.object(runner, "CREATE_UAT_SHARD_GROUPS", groups)]
        if fake_run is not None:
            patches.append(mock.patch.object(runner.unittest, "TextTestRunner", fake_run))
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in modules.items():
                Path(tmp, name + ".py").write_text(body, encoding="utf-8")
            saved_path = list(sys.path)
            try:
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(runner, "TESTS_DIR", Path(tmp)))
                    for patch in patches:
                        stack.enter_context(patch)
                    stack.enter_context(contextlib.redirect_stdout(stdout))
                    stack.enter_context(contextlib.redirect_stderr(stderr))
                    code = runner.main(argv)
            finally:
                sys.path[:] = saved_path
                for name in modules:
                    sys.modules.pop(name, None)
        return code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def _passing(*names):
        return "import unittest\n\n\nclass T(unittest.TestCase):\n" + "".join("    def %s(self):\n        pass\n" % name for name in names)

    def test_runner_runs_exactly_the_requested_shard(self):
        modules = {
            "test_xb211s_a": self._passing("test_a1", "test_a2"),
            "test_xb211s_b": self._passing("test_b1"),
            "test_xb211s_c": self._passing("test_c1"),
            "test_xb211s_d": self._passing("test_d1"),
            "test_xb211s_e": self._passing("test_e1", "test_e2"),
        }
        groups = {"focused": ("test_xb211s_a",), "mg-deployment": ("test_xb211s_b",), "bounded-import": ("test_xb211s_c", "test_xb211s_d")}
        expected = {
            "focused": ["test_xb211s_a.T.test_a1", "test_xb211s_a.T.test_a2"],
            "mg-deployment": ["test_xb211s_b.T.test_b1"],
            "bounded-import": ["test_xb211s_c.T.test_c1", "test_xb211s_d.T.test_d1"],
            "rest": ["test_xb211s_e.T.test_e1", "test_xb211s_e.T.test_e2"],
        }
        for shard, ids in expected.items():
            captured = []

            class FakeRunner:
                def __init__(self, *args, **kwargs):
                    pass

                def run(self, suite):
                    captured.append(sorted(_ids(suite)))
                    return mock.Mock(wasSuccessful=lambda: True)

            code, _, _ = self._run_in_tmp(modules, groups, ["--shard", shard], FakeRunner)
            self.assertEqual((code, captured), (0, [ids]), shard)

    def test_runner_fails_closed_on_bad_arguments(self):
        runner = _load_full_suite_runner()
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
            runner.unittest.TestLoader, "discover", side_effect=AssertionError("must not discover")
        ):
            for argv in (
                ["--shard"],
                ["--shard", "nope"],
                ["--shard", "rest", "rest"],
                ["--shard", "rest", "-k", "x"],
                ["--shard", "rest", runner.SKIP_MEMBER_GATEWAY_TARGETED_FLAG],
                [runner.SKIP_MEMBER_GATEWAY_TARGETED_FLAG, "--shard", "rest"],
                ["--shard=rest"],
                ["--shard", "focused.SomeClass"],
            ):
                self.assertEqual(runner.main(argv), 2, argv)

    def test_runner_fails_closed_on_missing_narrowed_or_empty_shard(self):
        modules = {"test_xb211s_a": self._passing("test_a1"), "test_xb211s_e": self._passing("test_e1")}
        base = {"mg-deployment": ("test_xb211s_e",), "bounded-import": ()}
        for groups, shard in (
            (dict(base, focused=("test_xb211s_a", "test_xb211s_missing")), "focused"),
            (dict(base, focused=("test_xb211s_a.T",)), "focused"),
            (dict(base, focused=("test_xb211s_a",)), "rest"),
        ):
            code, stdout, stderr = self._run_in_tmp(modules, groups, ["--shard", shard])
            self.assertEqual(code, 2, (groups, shard))
            self.assertIn("failing closed", stdout)

    def test_named_shard_import_failure_fails_both_its_leg_and_rest(self):
        modules = {
            "test_xb211s_boom": 'raise ImportError("xb211 shard import boom")\n',
            "test_xb211s_e": self._passing("test_e1"),
        }
        groups = {"focused": ("test_xb211s_boom",), "mg-deployment": (), "bounded-import": ()}
        code, stdout, _ = self._run_in_tmp(modules, groups, ["--shard", "focused"])
        self.assertEqual(code, 2)
        self.assertIn("xb211 shard import boom", stdout)
        # The failed import is not attributed to its module, so the rest leg runs and fails on it.
        code, _, stderr = self._run_in_tmp(modules, groups, ["--shard", "rest"])
        self.assertEqual(code, 1)
        self.assertIn("Ran 2 tests", stderr)

    def test_skip_member_gateway_targeted_path_is_unchanged(self):
        runner = _load_full_suite_runner()
        trivial = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(runner.unittest.TestLoader, "discover", return_value=trivial):
                self.assertEqual(runner.main([]), 0)
                self.assertEqual(runner.main([runner.SKIP_MEMBER_GATEWAY_TARGETED_FLAG]), 2)


if __name__ == "__main__":
    unittest.main()
