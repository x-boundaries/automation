from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from energygrid_bill_downloader import cli
from energygrid_bill_downloader import portal as portal_module
from energygrid_bill_downloader.cli import build_parser, main
from energygrid_bill_downloader.config import RuntimeConfig
from energygrid_bill_downloader.errors import (
    ACTION_REQUIRED,
    DOWNLOAD_FAILED,
    LOGIN_FAILED,
    NO_NEW_BILLS,
    PORTAL_LAYOUT_CHANGED,
    AppError,
    ConfigError,
    DependencyError,
    DownloadError,
    LayoutChangedError,
    LoginError,
)


class CliTests(unittest.TestCase):
    def test_only_locked_commands_and_overrides_are_exposed(self) -> None:
        args = build_parser().parse_args(
            [
                "list",
                "--config",
                "C:/private/energygrid.json",
                "--headed",
                "--timeout-seconds",
                "7",
            ]
        )
        self.assertEqual(args.command, "list")
        self.assertTrue(args.headed)
        self.assertEqual(args.timeout_seconds, 7)
    def test_root_readme_keeps_existing_surface_and_adds_isolated_link(self) -> None:
        readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Current Automation Surfaces", readme)
        self.assertIn("[energygrid-bill-downloader/](energygrid-bill-downloader/README.md)", readme)
        self.assertIn("does not change the AutoCount integration surfaces", readme)
        self.assertIn("This repository contains AutoCount 2 automation work:", readme)
        self.assertIn("It also contains the isolated Energy@Grid bill downloader", readme)
        self.assertNotIn("AutoCount 2 automation work only:", readme)
        gitignore = (Path(__file__).parents[2] / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("\n/_MandarinGallery/\n", gitignore)
        self.assertNotIn("*.pdf", gitignore)




    def test_energygrid_workflow_proves_committed_delta_and_disposable_browser_cache(self) -> None:
        workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "energygrid-bill-downloader-tests.yml").read_text(
            encoding="utf-8"
        )
        lines = workflow.splitlines()

        def indentation(line: str) -> int:
            return len(line) - len(line.lstrip(" "))

        def block_for(source: list[str], marker: str, line_indent: int) -> list[str]:
            start = next(
                index for index, line in enumerate(source)
                if indentation(line) == line_indent and line.strip() == marker
            )
            end = next(
                (
                    index
                    for index in range(start + 1, len(source))
                    if source[index].strip() and indentation(source[index]) <= line_indent
                ),
                len(source),
            )
            return source[start:end]

        job_lines = block_for(lines, "synthetic-windows:", 2)
        pull_request = block_for(lines, "pull_request:", 2)
        self.assertEqual(
            block_for(pull_request, "paths:", 4),
            [
                "    paths:",
                '      - "energygrid-bill-downloader/**"',
                '      - "scripts/energygrid_one_shot_supervisor.ps1"',
                '      - ".github/workflows/energygrid-bill-downloader-tests.yml"',
                "      # EnergyGrid n8n error handler (#141): the source-controlled export, its focused",
                "      # offline test, and the directory README that documents it. Exact entries, not a",
                "      # broad n8n-workflows/** or tests/** glob, which would widen the reviewed trigger",
                "      # surface beyond what this workflow owns.",
                '      - "n8n-workflows/energygrid_download_error_handler.workflow.json"',
                '      - "tests/test_energygrid_n8n_error_handler.py"',
                '      - "n8n-workflows/README.md"',
                '      - "README.md"',
                '      - ".gitignore"',
            ],
        )

        scope_guard = block_for(lines, "foreach ($file in $files) {", 12)
        scope_text = "\n".join(scope_guard)
        self.assertIn("$file -notmatch '^energygrid-bill-downloader/'", scope_text)
        self.assertEqual(
            [line.strip() for line in scope_guard if "$file -ne " in line],
            [
                "$file -ne 'scripts/energygrid_one_shot_supervisor.ps1' -and",
                "$file -ne '.github/workflows/energygrid-bill-downloader-tests.yml' -and",
                "$file -ne 'n8n-workflows/energygrid_download_error_handler.workflow.json' -and",
                "$file -ne 'tests/test_energygrid_n8n_error_handler.py' -and",
                "$file -ne 'n8n-workflows/README.md' -and",
                "$file -ne 'README.md' -and",
                "$file -ne '.gitignore') {",
            ],
        )
        self.assertNotIn("$file -notmatch '.*'", scope_text)
        self.assertNotIn("*.pdf", scope_text)

        # DL-XB-134-001: ownership is decided from EnergyGrid-owned paths ONLY. The shared
        # companions still trigger the workflow, so they must not appear in the classification -
        # otherwise an unrelated pull request editing a shared file is judged as if it were an
        # EnergyGrid change, which is the contradiction this repair removes.
        ownership = block_for(lines, "$owned = @($files | Where-Object {", 10)
        ownership_text = " ".join(ownership)
        self.assertEqual(
            [line.strip() for line in ownership],
            [
                "$owned = @($files | Where-Object {",
                "$_ -match '^energygrid-bill-downloader/' -or",
                "$_ -eq 'scripts/energygrid_one_shot_supervisor.ps1' -or",
                "$_ -eq '.github/workflows/energygrid-bill-downloader-tests.yml' -or",
                "$_ -eq 'n8n-workflows/energygrid_download_error_handler.workflow.json' -or",
                "$_ -eq 'tests/test_energygrid_n8n_error_handler.py'",
            ],
        )
        # The n8n error handler export and its focused test are EnergyGrid-owned, so either
        # one alone arms the guard. `n8n-workflows/README.md` is a shared companion: it
        # triggers the workflow and is permitted, but it never confers ownership.
        self.assertNotIn("README.md", ownership_text)
        self.assertNotIn(".gitignore", ownership_text)
        self.assertIn("if ($owned.Count -eq 0) {", workflow)

        # The whitespace check stays UNCONDITIONAL: it must precede the applicability branch.
        self.assertLess(
            next(i for i, l in enumerate(lines) if l.strip() == "git diff --check $base HEAD"),
            next(i for i, l in enumerate(lines) if l.strip().startswith("$owned = @(")),
            "git diff --check must run before the EnergyGrid applicability decision",
        )
        job_level_env = (
            block_for(job_lines, "env:", 4)
            if any(indentation(line) == 4 and line.strip() == "env:" for line in job_lines)
            else []
        )
        self.assertNotIn("runner.temp", "\n".join(job_level_env))

        browser_path = "PLAYWRIGHT_BROWSERS_PATH: ${{ runner.temp }}/energygrid-playwright"
        expected_env = ["        env:", f"          {browser_path}"]
        for step_name in (
            "Provision Chromium with official Playwright mechanism",
            "Project synthetic test suite",
        ):
            step = block_for(job_lines, f"- name: {step_name}", 6)
            self.assertEqual(block_for(step, "env:", 8), expected_env)
        self.assertIn("ref: ${{ github.event.pull_request.head.sha || github.sha }}", workflow)
        self.assertIn("EXPECTED_SHA", workflow)
        self.assertIn("$base = (git merge-base HEAD origin/main).Trim()", workflow)
        self.assertIn("git diff --check $base HEAD", workflow)
        self.assertNotIn("git diff --check\n", workflow)
        self.assertIn("git diff --name-only $base HEAD", workflow)
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH: ${{ runner.temp }}/energygrid-playwright", workflow)
        self.assertNotIn("ENERGYGRID_USERNAME:", workflow)
        self.assertNotIn("ENERGYGRID_PASSWORD:", workflow)
        self.assertNotIn("secrets.", workflow)

    # ---- DL-XB-134-001: shared-trigger scope applicability ---- #
    #
    # The workflow triggers on shared `README.md` and `.gitignore`, so an unrelated pull request
    # that legitimately edits one of them runs this workflow too. Ownership therefore has to be
    # decided from EnergyGrid-owned paths only, and the strict scope guard applied solely when
    # the candidate really does own an EnergyGrid change. The three cases below execute the
    # COMMITTED decision logic rather than restating the rule, so the guard and its tests cannot
    # drift apart.

    def _scope_decision_script(self) -> str:
        """Return the committed scope-guard decision logic with git data-gathering removed.

        Only the three `git`-reading lines are dropped; every classification and guard line is
        kept verbatim from the committed workflow, which is what binds these cases to it.
        """
        workflow = (
            Path(__file__).parents[2] / ".github" / "workflows"
            / "energygrid-bill-downloader-tests.yml"
        ).read_text(encoding="utf-8")
        lines = workflow.splitlines()
        start = next(
            index for index, line in enumerate(lines)
            if line.strip() == "- name: Scope and whitespace check"
        )
        run_at = next(
            index for index in range(start, len(lines)) if lines[index].strip() == "run: |"
        )
        body: list[str] = []
        for line in lines[run_at + 1:]:
            if line.strip() and (len(line) - len(line.lstrip(" "))) <= 8:
                break
            body.append(line[10:])
        decision = [
            line for line in body
            if not line.startswith("$base = ")
            and not line.startswith("git diff --check ")
            and not line.startswith("$files = @(git ")
        ]
        script = "\n".join(decision)
        self.assertIn("$owned", script, "the committed guard must classify EnergyGrid ownership")
        self.assertIn("Out-of-scope changed path", script)
        return script

    def _run_scope_case(self, changed: list[str]) -> subprocess.CompletedProcess[str]:
        """Execute the committed decision logic against a synthetic changed-file set."""
        # `pwsh` is the shell the workflow declares and is what runs on the Windows runner.
        # Windows PowerShell is accepted as an equivalent local host because the guard uses
        # only constructs both editions share, so these cases stay executable off-runner
        # instead of silently skipping.
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:  # pragma: no cover - no PowerShell host available
            self.skipTest("a PowerShell host is required to run the committed scope guard")
        listing = ",".join("'{}'".format(path) for path in changed)
        script = "$files = @({})\n{}".format(listing, self._scope_decision_script())
        return subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, check=False,
        )

    def test_shared_trigger_only_change_is_not_judged_by_the_energygrid_scope_guard(self) -> None:
        """Case A: `.gitignore` triggered the workflow, but nothing EnergyGrid-owned changed."""
        result = self._run_scope_case([".gitignore", "scripts/member_create_uat_approval.py"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Out-of-scope changed path", result.stdout + result.stderr)
        self.assertIn("not applicable", result.stdout)

    def test_readme_only_shared_trigger_does_not_confer_energygrid_ownership(self) -> None:
        """Case A, second shared companion: `README.md` alone must not arm the guard either."""
        result = self._run_scope_case(["README.md", "docs/autocount2-automation/some_runbook.md"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Out-of-scope changed path", result.stdout + result.stderr)

    def test_energygrid_owned_change_with_an_unrelated_path_still_fails_closed(self) -> None:
        """Case B: a real EnergyGrid change may not smuggle an unrelated path in with it."""
        result = self._run_scope_case([
            "energygrid-bill-downloader/energygrid_bill_downloader/cli.py",
            "scripts/member_create_uat_approval.py",
        ])
        self.assertNotEqual(result.returncode, 0, "an out-of-scope path must fail closed")
        self.assertIn("Out-of-scope changed path", result.stdout + result.stderr)
        self.assertIn("scripts/member_create_uat_approval.py", result.stdout + result.stderr)

    def test_workflow_only_change_with_an_unrelated_path_still_fails_closed(self) -> None:
        """Case B via the other ownership arm: the workflow file itself also arms the guard."""
        result = self._run_scope_case([
            ".github/workflows/energygrid-bill-downloader-tests.yml",
            "scripts/member_create_uat_approval.py",
        ])
        self.assertNotEqual(result.returncode, 0, "an out-of-scope path must fail closed")
        self.assertIn("Out-of-scope changed path", result.stdout + result.stderr)

    def test_energygrid_owned_change_with_permitted_shared_companions_is_accepted(self) -> None:
        """Case C: subtree plus the two permitted shared files remains in scope."""
        result = self._run_scope_case([
            "energygrid-bill-downloader/energygrid_bill_downloader/cli.py",
            "energygrid-bill-downloader/tests/test_cli.py",
            ".gitignore",
            "README.md",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Out-of-scope changed path", result.stdout + result.stderr)

    # ---- #141: the n8n error handler arm of the same ownership rule ---- #

    def test_n8n_error_handler_change_with_its_test_and_directory_readme_is_accepted(self) -> None:
        """The three authorised handler paths must not become false out-of-scope failures."""
        result = self._run_scope_case([
            "n8n-workflows/energygrid_download_error_handler.workflow.json",
            "tests/test_energygrid_n8n_error_handler.py",
            "n8n-workflows/README.md",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Out-of-scope changed path", result.stdout + result.stderr)

    def test_n8n_error_handler_change_with_an_unrelated_path_still_fails_closed(self) -> None:
        """The new ownership arm arms the guard just as the subtree arm does."""
        result = self._run_scope_case([
            "n8n-workflows/energygrid_download_error_handler.workflow.json",
            "scripts/member_create_uat_approval.py",
        ])
        self.assertNotEqual(result.returncode, 0, "an out-of-scope path must fail closed")
        self.assertIn("Out-of-scope changed path", result.stdout + result.stderr)

    def test_n8n_directory_readme_alone_does_not_confer_energygrid_ownership(self) -> None:
        """It triggers the workflow as a shared companion, but owns nothing on its own."""
        result = self._run_scope_case([
            "n8n-workflows/README.md",
            "n8n-workflows/member_create_uat_result_mapping.workflow.json",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Out-of-scope changed path", result.stdout + result.stderr)
        self.assertIn("not applicable", result.stdout)

    def test_invalid_cli_returns_contract_exit_code(self) -> None:
        self.assertEqual(main([]), 64)


# ---- DL-XB-141-OBS-001: terminal failure evidence ---- #
#
# Before this repair a caught AppError produced a coarse stdout line and nothing
# in the JSONL, so a failed run left no local record of WHERE it failed. These
# cases drive the committed handler and mapping rather than restating them, and
# they hold the privacy line: the raw exception message is never an output.


def stub_portal(error: BaseException | None = None, bills: tuple = ()):
    """Return a portal class that satisfies the CLI's contract without a browser.

    No Playwright, no network, no credentials: `login` either succeeds or raises
    the failure under test, which is the only behaviour these cases need.
    """

    class StubPortal:
        def __init__(self, config, headed: bool = False) -> None:
            self.config = config

        def __enter__(self) -> "StubPortal":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def login(self) -> None:
            if error is not None:
                raise error

        def inventory(self, safety_ceiling: int) -> list:
            return list(bills)

    return StubPortal


class TerminalFailureEvidenceTests(unittest.TestCase):
    HOSTILE_FRAGMENTS = (
        "hunter2",
        "tok_live_abcd1234",
        "C:/private/energygrid/secrets.json",
        "https://portal.example.invalid/session",
        "2026-05-01_account_a.pdf",
    )
    HOSTILE_MESSAGE = (
        "unmapped future failure: password=hunter2 token=tok_live_abcd1234 "
        "config C:/private/energygrid/secrets.json at "
        "https://portal.example.invalid/session downloading 2026-05-01_account_a.pdf"
    )

    def write_config(self, root: Path) -> Path:
        """Write a synthetic config whose private roots are all inside `root`."""
        (root / "archive").mkdir()
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    # Never contacted: every case replaces the portal class.
                    "portal_url": "http://127.0.0.1:1/synthetic",
                    "account_identity": "SYNTHETIC-INTENDED-ACCOUNT",
                    "archive_root": str(root / "archive"),
                    "state_path": str(root / "state" / "state.sqlite3"),
                    "temp_root": str(root / "temp"),
                    "log_root": str(root / "logs"),
                    "timeout_seconds": 5,
                    "max_attempts": 2,
                    "inventory_safety_ceiling": 50,
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def run_cli(self, root: Path, portal_cls, command: str = "run") -> tuple[int, str]:
        config_path = self.write_config(root)
        original = cli.PlaywrightPortal
        cli.PlaywrightPortal = portal_cls
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                exit_code = cli.main([command, "--config", str(config_path)])
        finally:
            cli.PlaywrightPortal = original
        return exit_code, buffer.getvalue()

    @staticmethod
    def log_text(root: Path) -> str:
        return "".join(
            path.read_text(encoding="utf-8") for path in sorted((root / "logs").glob("run-*.jsonl"))
        )

    def events(self, root: Path) -> list[dict]:
        return [json.loads(line) for line in self.log_text(root).splitlines() if line.strip()]

    def phases(self, root: Path) -> list[str]:
        return [event["phase"] for event in self.events(root)]

    def terminal_events(self, root: Path) -> list[dict]:
        return [event for event in self.events(root) if event["phase"] == cli.RUN_FAILED_PHASE]

    # ---- B: the handler itself ---- #

    def test_caught_app_error_logs_one_terminal_event_and_preserves_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exit_code, stdout = self.run_cli(
                root,
                stub_portal(LayoutChangedError("post-activation Login control is hidden or disabled")),
            )

            self.assertEqual(exit_code, 20)
            self.assertEqual(
                json.loads(stdout),
                {"status": PORTAL_LAYOUT_CHANGED, "error_class": PORTAL_LAYOUT_CHANGED},
            )

            terminal = self.terminal_events(root)
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["status"], PORTAL_LAYOUT_CHANGED)
            self.assertEqual(terminal[0]["support_ref"], "EG_LOGIN_POST_ACTIVATION_NOT_READY")
            self.assertEqual(
                set(terminal[0]), {"run_id", "phase", "status", "support_ref"},
                "the terminal event carries no payload beyond the locked public-safe fields",
            )

            # It records a login that started and never completed, and the raw
            # structural message stays out of both surfaces.
            self.assertIn("login_start", self.phases(root))
            self.assertNotIn("login_complete", self.phases(root))
            self.assertNotIn("hidden or disabled", self.log_text(root))
            self.assertNotIn("hidden or disabled", stdout)

    def test_login_failure_and_retryable_failure_keep_their_own_status_and_exit(self) -> None:
        for error, status, expected_exit, expected_ref in (
            (LoginError("portal rejected the login"), LOGIN_FAILED, 20, "EG_LOGIN_PORTAL_REJECTED"),
            (LoginError("runtime credentials are unavailable"), LOGIN_FAILED, 20, "EG_LOGIN_CREDENTIALS_UNAVAILABLE"),
            (DownloadError("browser download timed out"), DOWNLOAD_FAILED, 10, cli.UNCLASSIFIED_SUPPORT_REF),
            # One per-step layout reference, end to end: the finer reference
            # changes nothing about the status or the exit code it travels with.
            (
                LayoutChangedError("portal navigation did not complete"),
                PORTAL_LAYOUT_CHANGED,
                20,
                "EG_LOGIN_NAVIGATION_FAILED",
            ),
            (
                LayoutChangedError("Billing Manager entry did not appear after login"),
                PORTAL_LAYOUT_CHANGED,
                20,
                "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED",
            ),
            (
                LayoutChangedError("login submit dispatch outcome uncertain"),
                PORTAL_LAYOUT_CHANGED,
                20,
                "EG_LOGIN_SUBMIT_DISPATCH_UNCERTAIN",
            ),
        ):
            with self.subTest(status=status, exit_code=expected_exit):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    exit_code, stdout = self.run_cli(root, stub_portal(error))

                    self.assertEqual(exit_code, expected_exit)
                    self.assertEqual(json.loads(stdout)["status"], status)
                    terminal = self.terminal_events(root)
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0]["status"], status)
                    self.assertEqual(terminal[0]["support_ref"], expected_ref)

    # ---- C: unknown and hostile future messages ---- #

    def test_unknown_hostile_message_is_classified_generically_and_never_echoed(self) -> None:
        error = AppError(self.HOSTILE_MESSAGE, status=PORTAL_LAYOUT_CHANGED, exit_code=20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exit_code, stdout = self.run_cli(root, stub_portal(error))

            self.assertEqual(exit_code, 20)
            self.assertEqual(json.loads(stdout)["status"], PORTAL_LAYOUT_CHANGED)

            terminal = self.terminal_events(root)
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["support_ref"], cli.UNCLASSIFIED_SUPPORT_REF)

            log_text = self.log_text(root)
            for fragment in self.HOSTILE_FRAGMENTS:
                self.assertNotIn(fragment, log_text, fragment)
                self.assertNotIn(fragment, stdout, fragment)
            self.assertNotIn(self.HOSTILE_MESSAGE, log_text)
            self.assertNotIn(self.HOSTILE_MESSAGE, stdout)

    def test_unknown_message_never_reaches_the_log_file_name(self) -> None:
        error = AppError(self.HOSTILE_MESSAGE, status=PORTAL_LAYOUT_CHANGED, exit_code=20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_cli(root, stub_portal(error))
            for path in (root / "logs").iterdir():
                for fragment in self.HOSTILE_FRAGMENTS:
                    self.assertNotIn(fragment, path.name)

    # ---- D: the evidence write itself fails ---- #

    def test_failure_to_write_the_terminal_event_preserves_the_canonical_result(self) -> None:
        original_event = cli.SafeLogger.event

        def failing_event(self, phase, status=None, **fields):
            if phase == cli.RUN_FAILED_PHASE:
                raise OSError("synthetic log write failure")
            return original_event(self, phase, status=status, **fields)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli.SafeLogger.event = failing_event
            try:
                exit_code, stdout = self.run_cli(
                    root,
                    stub_portal(LayoutChangedError("Flutter semantics placeholder remained after activation")),
                )
            finally:
                cli.SafeLogger.event = original_event

            # The lost evidence line must not become a different outcome.
            self.assertEqual(exit_code, 20)
            self.assertEqual(json.loads(stdout)["status"], PORTAL_LAYOUT_CHANGED)
            self.assertEqual(self.terminal_events(root), [])
            self.assertNotIn("placeholder remained", self.log_text(root))
            self.assertNotIn("synthetic log write failure", stdout)

    # ---- E: failing before a logger exists ---- #

    def test_failure_before_the_logger_exists_does_not_invent_a_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = cli.main(["run", "--config", str(root / "absent.json")])
            stdout = buffer.getvalue()

            self.assertEqual(exit_code, 64)
            self.assertEqual(
                json.loads(stdout),
                {"status": ACTION_REQUIRED, "error_class": "CONFIG_OR_DEPENDENCY"},
            )
            # No log root was established, so none may be fabricated to carry evidence.
            self.assertEqual(list(root.iterdir()), [])
            self.assertNotIn("absent.json", stdout)
            self.assertNotIn("Traceback", stdout)

    # ---- F: the successful path is untouched ---- #

    def test_successful_run_keeps_its_phases_and_adds_no_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exit_code, stdout = self.run_cli(root, stub_portal())

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout)["status"], NO_NEW_BILLS)

            phases = self.phases(root)
            self.assertEqual(phases[:2], ["login_start", "login_complete"])
            self.assertEqual(phases[-1], "run_complete")
            self.assertEqual(self.terminal_events(root), [])

    # ---- G: exactly one terminal event per caught AppError ---- #

    def test_one_caught_app_error_produces_exactly_one_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_cli(root, stub_portal(LayoutChangedError("login submit dispatch outcome uncertain")))

            phases = self.phases(root)
            self.assertEqual(
                phases.count(cli.RUN_FAILED_PHASE), 1,
                "a single caught AppError must not be recorded twice",
            )
            self.assertNotIn("run_complete", phases)
            self.assertEqual(
                self.terminal_events(root)[0]["support_ref"],
                "EG_LOGIN_SUBMIT_DISPATCH_UNCERTAIN",
            )


class SupportReferenceContractTests(unittest.TestCase):
    """The reference vocabulary itself must stay bounded and disclosure-free."""

    IDENTIFIER = re.compile(r"\A[A-Z][A-Z0-9_]{0,63}\Z")

    def test_every_reference_is_a_bounded_ascii_identifier(self) -> None:
        for message, ref in cli.SUPPORT_REFS_BY_MESSAGE.items():
            with self.subTest(ref=ref):
                self.assertRegex(ref, self.IDENTIFIER)
                self.assertTrue(ref.isascii())
                # A reference must be a code, not a restatement of the message.
                self.assertNotIn(ref.casefold(), message.casefold())
        self.assertRegex(cli.UNCLASSIFIED_SUPPORT_REF, self.IDENTIFIER)

    def test_known_messages_map_to_distinct_non_generic_references(self) -> None:
        refs = list(cli.SUPPORT_REFS_BY_MESSAGE.values())
        self.assertEqual(len(refs), len(set(refs)), "each known failure needs its own reference")
        self.assertNotIn(cli.UNCLASSIFIED_SUPPORT_REF, refs)

    def test_retired_references_stay_mapped_and_stay_out_of_the_live_vocabulary(self) -> None:
        """A retired reference reads old evidence; it never classifies new work.

        Keeping the mapping is what lets an operator interpret a log written
        before the login steps were told apart. Declaring it retired is what
        stops a future step from quietly reusing the coarse reference instead
        of earning its own.
        """
        committed = set(cli.SUPPORT_REFS_BY_MESSAGE.values())
        self.assertTrue(cli.RETIRED_SUPPORT_REFS.issubset(committed))
        self.assertIn("EG_LOGIN_REQUIRED_CONTROL_UNRESOLVED", cli.RETIRED_SUPPORT_REFS)
        self.assertIn("EG_LOGIN_SUBMIT_FAILED", cli.RETIRED_SUPPORT_REFS)
        # DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001. Billing Manager is no
        # longer an authentication oracle, so no login step waits on it.
        self.assertIn("EG_LOGIN_BILLING_MANAGER_WAIT_FAILED", cli.RETIRED_SUPPORT_REFS)
        for ref in cli.RETIRED_SUPPORT_REFS:
            with self.subTest(ref=ref):
                self.assertRegex(ref, self.IDENTIFIER)
        # The per-step references that replaced it are all live, and distinct.
        replacements = {
            "EG_LOGIN_NAVIGATION_FAILED",
            "EG_LOGIN_SEMANTICS_ACTIVATION_DISPATCH_FAILED",
            "EG_LOGIN_ENTRY_CLICK_FAILED",
            "EG_LOGIN_USERNAME_FILL_FAILED",
            "EG_LOGIN_PASSWORD_FILL_FAILED",
            "EG_LOGIN_SUBMIT_NOT_APPEAR",
            "EG_LOGIN_SUBMIT_AMBIGUOUS",
            "EG_LOGIN_SUBMIT_NOT_READY",
            "EG_LOGIN_SUBMIT_UNRESOLVED",
            "EG_LOGIN_SUBMIT_DISPATCH_UNCERTAIN",
            # What replaced the retired Billing Manager login wait: the login
            # half now names the authentication contract, and the navigation
            # half names the business route separately -- since DL-XB-199 the
            # live EB Bill tab rather than the retired Billing Manager link.
            "EG_LOGIN_AUTHENTICATION_UNPROVED",
            "EG_NAV_EB_BILL_TAB_NOT_READY",
        }
        self.assertTrue(replacements.issubset(committed))
        self.assertTrue(replacements.isdisjoint(cli.RETIRED_SUPPORT_REFS))

    def test_the_retired_billing_manager_wait_stays_readable_but_unreachable(self) -> None:
        """Old evidence still reads; no current build can record it again."""
        error = LayoutChangedError("Billing Manager entry did not appear after login")
        self.assertEqual(
            cli.support_ref_for(error), "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED"
        )
        self.assertIn("EG_LOGIN_BILLING_MANAGER_WAIT_FAILED", cli.RETIRED_SUPPORT_REFS)
        self.assertNotIn(
            "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED", cli.NAVIGATION_SUPPORT_REFS
        )

    def test_the_navigation_half_is_declared_mapped_and_separate(self) -> None:
        """Business navigation has its own references, disjoint from login."""
        committed = set(cli.SUPPORT_REFS_BY_MESSAGE.values())
        self.assertTrue(cli.NAVIGATION_SUPPORT_REFS.issubset(committed))
        self.assertTrue(
            cli.NAVIGATION_SUPPORT_REFS.isdisjoint(cli.RETIRED_SUPPORT_REFS)
        )
        self.assertEqual(
            cli.NAVIGATION_SUPPORT_REFS,
            {
                # DL-XB-199 (G2-076). The live single surface: the exact EB
                # Bill tab, the account witness, one Search and the one results
                # table. Each is reachable only from the production inventory.
                "EG_NAV_EB_BILL_TAB_NOT_READY",
                "EG_NAV_EB_BILL_TAB_DISPATCH_UNCERTAIN",
                "EG_NAV_EB_BILL_TAB_UNPROVED",
                "EG_NAV_ACCOUNT_WITNESS_UNPROVED",
                "EG_NAV_ACCOUNT_WITNESS_AMBIGUOUS",
                "EG_NAV_SEARCH_NOT_READY",
                "EG_NAV_SEARCH_DISPATCH_UNCERTAIN",
                "EG_NAV_PAGE_TOPOLOGY",
                "EG_NAV_RESULTS_UNSETTLED",
                "EG_NAV_RESULTS_HEADER_ONLY",
                "EG_NAV_RESULTS_PAGINATION_PRESENT",
                "EG_NAV_RESULTS_ROWCOUNT_CONTRADICTORY",
                "EG_NAV_RESULTS_ROW_IDENTITY_INVALID",
                "EG_NAV_RESULTS_SAFETY_CEILING",
                "EG_NAV_RESULTS_INVENTORY_CONSUMED",
            },
        )
        self.assertTrue(cli.NAVIGATION_SUPPORT_REFS.isdisjoint(cli.DIAGNOSTIC_EMS_ENTRY_SUPPORT_REFS))
        self.assertTrue(
            cli.NAVIGATION_SUPPORT_REFS.isdisjoint(cli.NAVIGATION_DIAGNOSTIC_ALLOWED_SUPPORT_REFS),
            "no production reference can appear in a navigation diagnostic document",
        )
        for ref in cli.NAVIGATION_SUPPORT_REFS:
            with self.subTest(ref=ref):
                self.assertRegex(ref, self.IDENTIFIER)
                self.assertTrue(
                    ref.startswith("EG_NAV_"),
                    "a navigation failure never borrows the login vocabulary",
                )

    def test_the_ems_entry_references_are_mapped_exactly_and_stay_navigation(self) -> None:
        """The application entry has its own two references, and no others.

        Since DL-XB-199 production never actuates EMS, so only the separate
        navigation diagnostic's one EMS dispatch can raise them.
        """
        for message, ref in (
            (
                "EMS application entry control is not ready",
                "EG_NAV_EMS_ENTRY_NOT_READY",
            ),
            (
                "EMS application entry dispatch outcome uncertain",
                "EG_NAV_EMS_ENTRY_DISPATCH_UNCERTAIN",
            ),
        ):
            with self.subTest(ref=ref):
                self.assertEqual(
                    cli.support_ref_for(LayoutChangedError(message)), ref
                )
                self.assertIn(ref, cli.DIAGNOSTIC_EMS_ENTRY_SUPPORT_REFS)
                self.assertIn(ref, cli.NAVIGATION_DIAGNOSTIC_ALLOWED_SUPPORT_REFS)
                self.assertNotIn(ref, cli.NAVIGATION_SUPPORT_REFS)
                self.assertNotIn(ref, cli.RETIRED_SUPPORT_REFS)
                self.assertFalse(ref.startswith("EG_LOGIN_"))
        # The wording is owned by portal.py, so a reword there fails here
        # rather than silently degrading to the generic reference.
        self.assertEqual(
            portal_module.NAV_EMS_ENTRY_NOT_READY_MESSAGE,
            "EMS application entry control is not ready",
        )
        self.assertEqual(
            portal_module.NAV_EMS_ENTRY_UNCERTAIN_MESSAGE,
            "EMS application entry dispatch outcome uncertain",
        )

    def test_the_retired_link_route_references_stay_readable_but_unreachable(self) -> None:
        """DL-XB-199 retired the Billing Manager / EB Bill link route."""
        retired = {
            "Billing Manager navigation control is not ready": "EG_NAV_BILLING_MANAGER_NOT_READY",
            "Billing Manager navigation dispatch outcome uncertain": "EG_NAV_BILLING_MANAGER_DISPATCH_UNCERTAIN",
            "EB Bill navigation control is not ready": "EG_NAV_EB_BILL_NOT_READY",
            "EB Bill navigation dispatch outcome uncertain": "EG_NAV_EB_BILL_DISPATCH_UNCERTAIN",
            "EB Bill results route was not proven": "EG_NAV_RESULTS_ROUTE_UNPROVED",
        }
        for message, ref in retired.items():
            with self.subTest(ref=ref):
                self.assertEqual(cli.support_ref_for(LayoutChangedError(message)), ref)
                self.assertIn(ref, cli.RETIRED_SUPPORT_REFS)
                self.assertNotIn(ref, cli.NAVIGATION_SUPPORT_REFS)
                self.assertNotIn(ref, cli.NAVIGATION_DIAGNOSTIC_ALLOWED_SUPPORT_REFS)
        # The producing constants are gone, so nothing in portal.py can raise them.
        for name in (
            "NAV_BILLING_MANAGER_NOT_READY_MESSAGE",
            "NAV_BILLING_MANAGER_UNCERTAIN_MESSAGE",
            "NAV_EB_BILL_NOT_READY_MESSAGE",
            "NAV_EB_BILL_UNCERTAIN_MESSAGE",
            "NAV_RESULTS_ROUTE_UNPROVED_MESSAGE",
        ):
            self.assertFalse(hasattr(portal_module, name), name)
        source = Path(portal_module.__file__).read_text(encoding="utf-8")
        for message in retired:
            self.assertNotIn(message, source)

    def test_the_navigation_diagnostic_allowlist_is_unchanged(self) -> None:
        """The diagnostic's own allowlist stays exactly what it was."""
        login = {ref for ref in cli.SUPPORT_REFS_BY_MESSAGE.values() if ref.startswith("EG_LOGIN_")}
        self.assertEqual(
            cli.NAVIGATION_DIAGNOSTIC_ALLOWED_SUPPORT_REFS,
            login
            | {
                "EG_NAV_EMS_ENTRY_NOT_READY",
                "EG_NAV_EMS_ENTRY_DISPATCH_UNCERTAIN",
                "EG_NAV_DIAGNOSTIC_PAGE_TOPOLOGY",
                "EG_NAV_DIAGNOSTIC_FRAME_TOPOLOGY",
                "EG_NAV_DIAGNOSTIC_CROSS_ORIGIN",
                "EG_NAV_DIAGNOSTIC_OBSERVATION_UNREADABLE",
                "EG_NAV_DIAGNOSTIC_OUTPUT_REJECTED",
                "EG_LOGIN_DIAGNOSTIC_UNCLASSIFIED",
            },
        )
        self.assertEqual(
            cli.NAVIGATION_DIAGNOSTIC_SUPPORT_REFS,
            {
                "EG_NAV_DIAGNOSTIC_PAGE_TOPOLOGY",
                "EG_NAV_DIAGNOSTIC_FRAME_TOPOLOGY",
                "EG_NAV_DIAGNOSTIC_CROSS_ORIGIN",
                "EG_NAV_DIAGNOSTIC_OBSERVATION_UNREADABLE",
                "EG_NAV_DIAGNOSTIC_OUTPUT_REJECTED",
            },
        )

    def test_historical_submit_message_remains_mapped_but_retired(self) -> None:
        error = LayoutChangedError("login submission did not complete")
        self.assertEqual(cli.support_ref_for(error), "EG_LOGIN_SUBMIT_FAILED")
        self.assertIn("EG_LOGIN_SUBMIT_FAILED", cli.RETIRED_SUPPORT_REFS)

    def test_classification_is_exact_so_a_longer_future_message_stays_generic(self) -> None:
        known = "post-activation Login control did not appear"
        self.assertEqual(
            cli.support_ref_for(LayoutChangedError(known)), "EG_LOGIN_POST_ACTIVATION_NOT_APPEAR"
        )
        for near_miss in (known + " within the configured timeout", "  " + known, known.upper()):
            with self.subTest(near_miss=near_miss):
                self.assertEqual(
                    cli.support_ref_for(LayoutChangedError(near_miss)), cli.UNCLASSIFIED_SUPPORT_REF
                )


# ---- DL-XB-141-RUNTIME-005-SOURCE-DURABILITY-A1: the bounded diagnostic command ---- #
#
# The diagnostic is a separate command with its own closed argument surface, its
# own closed result document, and its own exit codes. These cases hold that it
# cannot be widened by an argument, cannot create a filesystem artefact, and
# cannot leak a private value.

# The same bounded-reference shape SupportReferenceContractTests enforces.
SUPPORT_REFERENCE_PATTERN = re.compile(r"\A[A-Z][A-Z0-9_]{0,63}\Z")

DIAGNOSTIC_SENTINEL_USERNAME = "synthetic-diagnostic-user"
DIAGNOSTIC_SENTINEL_PASSWORD = "synthetic-diagnostic-hunter2"


def diagnostic_witnesses(**overrides):
    """A pre- or post-submit observation in the shape the portal produces."""
    observation = portal_module.unobserved_login_witnesses(
        include_url=overrides.pop("include_url", False)
    )
    observation["hosts"] = {tag: 0 for tag in portal_module.DIAGNOSTIC_HOST_TAGS}
    observation["semantics_placeholder"] = {"count": 0, "present": False}
    for name in ("billing_manager", "username", "password", "ems", "rejection"):
        observation[name] = {"count": 0, "visible": False}
    for name in ("login", "enable_accessibility"):
        observation[name] = {"count": 0, "visible": False, "actionable": False}
    observation["visible_alert"] = False
    if "url_changed" in observation:
        observation["url_changed"] = False
    observation.update(overrides)
    return observation


def diagnostic_portal(
    result=None,
    error: BaseException | None = None,
    recorder: list | None = None,
):
    """A portal class that records how it was constructed and returns a result.

    No Playwright, no browser, no network and no credential: the diagnostic
    contract under test is the CLI's, not the portal's.
    """

    class StubDiagnosticPortal:
        def __init__(self, config, headed: bool = False) -> None:
            self.config = config
            if recorder is not None:
                recorder.append(headed)

        def __enter__(self) -> "StubDiagnosticPortal":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def login_diagnostic(self):
            if error is not None:
                raise error
            return result

        def login(self) -> None:
            raise AssertionError("the diagnostic must never run the login path")

        def inventory(self, safety_ceiling: int) -> list:
            raise AssertionError("the diagnostic must never reach inventory")

    return StubDiagnosticPortal


def complete_result(
    classification="SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS",
    authentication_outcome=portal_module.AUTHENTICATION_UNPROVED,
):
    return portal_module.LoginDiagnosticResult(
        classification=classification,
        submit_dispatched=True,
        submit_outcome=portal_module.SUBMIT_DISPATCHED,
        pre_submit=diagnostic_witnesses(),
        post_submit=diagnostic_witnesses(include_url=True),
        failure=None,
        authentication_outcome=authentication_outcome,
    )


class LoginDiagnosticCliTests(unittest.TestCase):
    """The command surface, the closed document, and the exit-code mapping."""

    # ---- argument surface ---- #

    def test_the_diagnostic_accepts_only_a_config_argument(self) -> None:
        args = build_parser().parse_args(["login-diagnostic", "--config", "C:/private/eg.json"])
        self.assertEqual(args.command, "login-diagnostic")
        self.assertEqual(vars(args).keys(), {"command", "config"})

    def test_the_diagnostic_rejects_every_other_argument(self) -> None:
        for extra in (
            ["--headed"],
            ["--archive-root", "C:/private/archive"],
            ["--state-path", "C:/private/state.sqlite3"],
            ["--temp-root", "C:/private/temp"],
            ["--log-root", "C:/private/logs"],
            ["--timeout-seconds", "9"],
            ["--max-attempts", "3"],
            ["--portal-url", "http://example.invalid"],
            ["extra-positional"],
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigError):
                    build_parser().parse_args(
                        ["login-diagnostic", "--config", "C:/private/eg.json", *extra]
                    )

    def test_the_diagnostic_requires_its_config_argument(self) -> None:
        with self.assertRaises(ConfigError):
            build_parser().parse_args(["login-diagnostic"])

    def test_run_and_list_keep_their_existing_option_surface(self) -> None:
        """The new command must not have narrowed the two established ones."""
        for command in ("run", "list"):
            with self.subTest(command=command):
                args = build_parser().parse_args(
                    [command, "--config", "C:/private/eg.json", "--headed"]
                )
                self.assertTrue(args.headed)

    def test_the_command_allowlist_keeps_existing_operations_and_adds_the_direct_diagnostic(self) -> None:
        actions = [
            action
            for action in build_parser()._subparsers._group_actions
            if hasattr(action, "choices")
        ]
        self.assertEqual(
            list(actions[0].choices),
            [
                "run",
                "list",
                "login-diagnostic",
                "navigation-diagnostic",
                "download-preflight-diagnostic",
            ],
        )

    # ---- the run itself ---- #

    def write_diagnostic_config(self, root: Path) -> Path:
        (root / "archive").mkdir()
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    # Never contacted: every case replaces the portal class.
                    "portal_url": "http://127.0.0.1:1/synthetic",
                    "account_identity": "SYNTHETIC-INTENDED-ACCOUNT",
                    "archive_root": str(root / "archive"),
                    "state_path": str(root / "state" / "state.sqlite3"),
                    "temp_root": str(root / "temp"),
                    "log_root": str(root / "logs"),
                    "timeout_seconds": 5,
                    "max_attempts": 2,
                    "inventory_safety_ceiling": 50,
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def run_diagnostic(self, root: Path, portal_cls, config_path: Path | None = None):
        if config_path is None:
            config_path = self.write_diagnostic_config(root)
        original = cli.PlaywrightPortal
        cli.PlaywrightPortal = portal_cls
        out = io.StringIO()
        err = io.StringIO()
        environment = {
            "ENERGYGRID_USERNAME": DIAGNOSTIC_SENTINEL_USERNAME,
            "ENERGYGRID_PASSWORD": DIAGNOSTIC_SENTINEL_PASSWORD,
        }
        previous = {name: os.environ.get(name) for name in environment}
        os.environ.update(environment)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exit_code = cli.main(["login-diagnostic", "--config", str(config_path)])
        finally:
            cli.PlaywrightPortal = original
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        return exit_code, out.getvalue(), err.getvalue()

    def test_the_portal_is_always_constructed_headed(self) -> None:
        recorder: list = []
        with tempfile.TemporaryDirectory() as name:
            exit_code, _out, _err = self.run_diagnostic(
                Path(name), diagnostic_portal(complete_result(), recorder=recorder)
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(recorder, [True], "headed is a property, not a choice")

    def test_the_diagnostic_creates_no_filesystem_artefact(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config_path = self.write_diagnostic_config(root)
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.run_diagnostic(root, diagnostic_portal(complete_result()), config_path)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        self.assertEqual(before, after, "no state, log, temp or archive artefact")
        for absent in ("logs", "temp", "state"):
            self.assertNotIn(absent, after)

    def test_the_run_path_machinery_is_unreachable(self) -> None:
        """Preflight, the logger, temp cleanup, the state store and reconcile never run."""

        def explode(*_args, **_kwargs):
            raise AssertionError("the diagnostic reached the run path")

        with tempfile.TemporaryDirectory() as name:
            with mock.patch.object(cli, "SafeLogger", explode), \
                    mock.patch.object(cli, "StateStore", explode), \
                    mock.patch.object(cli, "cleanup_stale_owned_temp", explode), \
                    mock.patch.object(cli, "reconcile_inventory", explode), \
                    mock.patch.object(RuntimeConfig, "preflight", explode):
                exit_code, out, _err = self.run_diagnostic(
                    Path(name), diagnostic_portal(complete_result())
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(out)["status"], cli.DIAGNOSTIC_COMPLETE)

    # ---- the document ---- #

    def document(self, out: str) -> dict:
        self.assertEqual(len(out.strip().splitlines()), 1, "exactly one document")
        return json.loads(out)

    def test_a_classified_run_is_complete_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_diagnostic(
                Path(name), diagnostic_portal(complete_result())
            )
        document = self.document(out)
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["schema"], "energygrid.login_diagnostic.v2")
        self.assertEqual(document["status"], "DIAGNOSTIC_COMPLETE")
        self.assertEqual(
            document["classification"], "SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS"
        )
        # A historical shell classification is diagnostically complete and
        # still says nothing about authentication.
        self.assertEqual(document["authentication_outcome"], "AUTHENTICATION_UNPROVED")
        self.assertEqual(document["navigation_status"], "NOT_TESTED")
        self.assertIs(document["submit_dispatched"], True)
        self.assertEqual(document["submit_outcome"], "DISPATCHED")
        self.assertNotIn("support_ref", document, "a complete result carries no reference")
        self.assertEqual(
            set(document),
            {
                "schema",
                "status",
                "classification",
                "authentication_outcome",
                "navigation_status",
                "submit_dispatched",
                "submit_outcome",
                "pre_submit",
                "post_submit",
            },
        )

    # ---- DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001: the v2 additions ---- #

    def test_the_active_schema_is_v2_and_v1_is_historical_only(self) -> None:
        """v1 stays readable as documentation; no build emits it."""
        self.assertEqual(cli.LOGIN_DIAGNOSTIC_SCHEMA, "energygrid.login_diagnostic.v2")
        self.assertIn(
            "energygrid.login_diagnostic.v1", cli.HISTORICAL_LOGIN_DIAGNOSTIC_SCHEMAS
        )
        self.assertNotIn(
            cli.LOGIN_DIAGNOSTIC_SCHEMA, cli.HISTORICAL_LOGIN_DIAGNOSTIC_SCHEMAS
        )
        with tempfile.TemporaryDirectory() as name:
            _exit_code, out, _err = self.run_diagnostic(
                Path(name), diagnostic_portal(complete_result())
            )
        for historical in cli.HISTORICAL_LOGIN_DIAGNOSTIC_SCHEMAS:
            self.assertNotIn(historical, out)

    def test_a_clean_authenticated_landing_is_reported_as_authenticated(self) -> None:
        result = complete_result(
            "AUTHENTICATED_LANDING_PROVEN",
            authentication_outcome=portal_module.AUTHENTICATED,
        )
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_diagnostic(
                Path(name), diagnostic_portal(result)
            )
        document = self.document(out)
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["classification"], "AUTHENTICATED_LANDING_PROVEN")
        self.assertEqual(document["authentication_outcome"], "AUTHENTICATED")
        self.assertEqual(document["navigation_status"], "NOT_TESTED")

    def test_a_clean_rejection_is_reported_as_rejected(self) -> None:
        result = complete_result(
            "VISIBLE_ALERT", authentication_outcome=portal_module.REJECTED
        )
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_diagnostic(
                Path(name), diagnostic_portal(result)
            )
        document = self.document(out)
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["classification"], "VISIBLE_ALERT")
        self.assertEqual(document["authentication_outcome"], "REJECTED")

    def test_the_authentication_outcome_vocabulary_is_closed(self) -> None:
        """Exactly three values, and an unauthorised one is never emitted."""
        self.assertEqual(
            portal_module.AUTHENTICATION_OUTCOMES,
            ("AUTHENTICATED", "REJECTED", "AUTHENTICATION_UNPROVED"),
        )
        for outcome in portal_module.AUTHENTICATION_OUTCOMES:
            with self.subTest(outcome=outcome):
                document = cli.login_diagnostic_document(
                    status=cli.DIAGNOSTIC_COMPLETE,
                    classification="VISIBLE_ALERT",
                    submit_dispatched=True,
                    submit_outcome=portal_module.SUBMIT_DISPATCHED,
                    pre_submit=diagnostic_witnesses(),
                    post_submit=diagnostic_witnesses(include_url=True),
                    authentication_outcome=outcome,
                )
                self.assertEqual(document["authentication_outcome"], outcome)
        rogue = complete_result(authentication_outcome="SOME_FUTURE_OUTCOME")
        with tempfile.TemporaryDirectory() as name:
            _exit_code, out, _err = self.run_diagnostic(
                Path(name), diagnostic_portal(rogue)
            )
        document = self.document(out)
        self.assertEqual(document["authentication_outcome"], "AUTHENTICATION_UNPROVED")
        self.assertNotIn("SOME_FUTURE_OUTCOME", out)

    def test_navigation_is_never_tested_on_any_emitted_document(self) -> None:
        """The invariant holds on every result shape the command can emit."""
        cases = (
            diagnostic_portal(complete_result()),
            diagnostic_portal(complete_result(None)),
            diagnostic_portal(error=LoginError("portal rejected the login")),
            diagnostic_portal(
                error=DependencyError("Playwright Python is not installed")
            ),
            diagnostic_portal(error=RuntimeError("unmapped future failure")),
        )
        for portal_cls in cases:
            with self.subTest(portal=portal_cls):
                with tempfile.TemporaryDirectory() as name:
                    _exit_code, out, _err = self.run_diagnostic(Path(name), portal_cls)
                document = self.document(out)
                self.assertEqual(document["navigation_status"], "NOT_TESTED")
                self.assertIn(
                    document["authentication_outcome"],
                    portal_module.AUTHENTICATION_OUTCOMES,
                )

    def test_every_accepted_classification_is_emitted_verbatim(self) -> None:
        for classification in portal_module.LOGIN_DIAGNOSTIC_CLASSIFICATIONS:
            with self.subTest(classification=classification):
                with tempfile.TemporaryDirectory() as name:
                    exit_code, out, _err = self.run_diagnostic(
                        Path(name), diagnostic_portal(complete_result(classification))
                    )
                document = self.document(out)
                self.assertEqual(exit_code, 0)
                self.assertEqual(document["classification"], classification)
                self.assertEqual(document["status"], "DIAGNOSTIC_COMPLETE")

    def test_an_unclassified_run_fails_closed_with_its_own_reference(self) -> None:
        unclassified = portal_module.LoginDiagnosticResult(
            classification=None,
            submit_dispatched=True,
            submit_outcome=portal_module.SUBMIT_DISPATCH_UNCERTAIN,
            pre_submit=diagnostic_witnesses(),
            post_submit=diagnostic_witnesses(include_url=True),
            failure=None,
        )
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_diagnostic(
                Path(name), diagnostic_portal(unclassified)
            )
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(document["status"], ACTION_REQUIRED)
        self.assertIsNone(document["classification"])
        self.assertEqual(document["submit_outcome"], "DISPATCH_UNCERTAIN")
        self.assertEqual(document["support_ref"], "EG_LOGIN_DIAGNOSTIC_UNCLASSIFIED")

    def test_an_unauthorised_classification_is_never_emitted(self) -> None:
        """The emitted vocabulary stays closed even against an unknown value."""
        rogue = complete_result("SOME_FUTURE_CLASSIFICATION")
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_diagnostic(Path(name), diagnostic_portal(rogue))
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertIsNone(document["classification"])
        self.assertNotIn("SOME_FUTURE_CLASSIFICATION", out)

    def test_a_pre_dispatch_failure_reports_its_step_reference(self) -> None:
        failed = portal_module.LoginDiagnosticResult(
            classification=None,
            submit_dispatched=False,
            submit_outcome=portal_module.SUBMIT_NOT_DISPATCHED,
            pre_submit=diagnostic_witnesses(),
            post_submit=diagnostic_witnesses(include_url=True),
            failure=LayoutChangedError("login submit control did not appear"),
        )
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_diagnostic(Path(name), diagnostic_portal(failed))
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertIs(document["submit_dispatched"], False)
        self.assertEqual(document["submit_outcome"], "NOT_DISPATCHED")
        self.assertEqual(document["support_ref"], "EG_LOGIN_SUBMIT_NOT_APPEAR")

    def test_a_raised_application_error_still_emits_one_bounded_document(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_diagnostic(
                Path(name),
                diagnostic_portal(error=LoginError("runtime credentials are unavailable")),
            )
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(document["status"], ACTION_REQUIRED)
        self.assertEqual(document["support_ref"], "EG_LOGIN_CREDENTIALS_UNAVAILABLE")
        self.assertEqual(document["submit_outcome"], "NOT_DISPATCHED")

    def test_a_dependency_failure_is_a_contract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_diagnostic(
                Path(name),
                diagnostic_portal(error=DependencyError("Playwright Python is not installed")),
            )
        document = self.document(out)
        self.assertEqual(exit_code, 64)
        self.assertEqual(document["schema"], "energygrid.login_diagnostic.v2")
        self.assertEqual(document["status"], ACTION_REQUIRED)
        self.assertEqual(document["authentication_outcome"], "AUTHENTICATION_UNPROVED")
        self.assertEqual(document["navigation_status"], "NOT_TESTED")

    def test_an_unreadable_configuration_is_a_contract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            missing = root / "absent.json"
            exit_code, out, _err = self.run_diagnostic(
                root, diagnostic_portal(complete_result()), missing
            )
        document = self.document(out)
        self.assertEqual(exit_code, 64)
        self.assertEqual(document["status"], ACTION_REQUIRED)
        self.assertIsNone(document["classification"])

    def test_the_diagnostic_never_returns_the_retryable_code(self) -> None:
        """`10` belongs to the download and network band, which is unreachable here."""
        cases = (
            diagnostic_portal(complete_result()),
            diagnostic_portal(error=LoginError("runtime credentials are unavailable")),
            diagnostic_portal(error=DownloadError("browser download failed")),
        )
        for index, portal_cls in enumerate(cases):
            with self.subTest(case=index):
                with tempfile.TemporaryDirectory() as name:
                    exit_code, _out, _err = self.run_diagnostic(Path(name), portal_cls)
                self.assertIn(exit_code, (0, 20, 64))
                self.assertNotEqual(exit_code, 10)

    # ---- privacy ---- #

    def test_no_private_or_free_form_value_reaches_the_document(self) -> None:
        hostile = portal_module.LoginDiagnosticResult(
            classification=None,
            submit_dispatched=False,
            submit_outcome=portal_module.SUBMIT_NOT_DISPATCHED,
            pre_submit=diagnostic_witnesses(),
            post_submit=diagnostic_witnesses(include_url=True),
            failure=LayoutChangedError(
                "unmapped future failure: password=hunter2 at "
                "https://portal.example.invalid/session for SYNTHETIC-INTENDED-ACCOUNT"
            ),
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            exit_code, out, err = self.run_diagnostic(root, diagnostic_portal(hostile))
            emitted = out + err
            self.assertNotIn(str(root), emitted)
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(document["support_ref"], cli.UNCLASSIFIED_SUPPORT_REF)
        for fragment in (
            "hunter2",
            "https://",
            "portal.example.invalid",
            "SYNTHETIC-INTENDED-ACCOUNT",
            "unmapped future failure",
            DIAGNOSTIC_SENTINEL_USERNAME,
            DIAGNOSTIC_SENTINEL_PASSWORD,
        ):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, emitted)

    def test_the_sentinel_credentials_never_reach_any_output_surface(self) -> None:
        for portal_cls in (
            diagnostic_portal(complete_result()),
            diagnostic_portal(error=LoginError("portal rejected the login")),
        ):
            with tempfile.TemporaryDirectory() as name:
                _exit_code, out, err = self.run_diagnostic(Path(name), portal_cls)
            for fragment in (DIAGNOSTIC_SENTINEL_USERNAME, DIAGNOSTIC_SENTINEL_PASSWORD):
                with self.subTest(fragment=fragment):
                    self.assertNotIn(fragment, out + err)

    def test_every_document_value_is_an_identifier_a_count_or_a_boolean(self) -> None:
        allowed = set(portal_module.LOGIN_DIAGNOSTIC_CLASSIFICATIONS) | {
            cli.LOGIN_DIAGNOSTIC_SCHEMA,
            cli.DIAGNOSTIC_COMPLETE,
            cli.NAVIGATION_NOT_TESTED,
            ACTION_REQUIRED,
            *portal_module.SUBMIT_OUTCOMES,
            *portal_module.AUTHENTICATION_OUTCOMES,
        }
        with tempfile.TemporaryDirectory() as name:
            _exit_code, out, _err = self.run_diagnostic(
                Path(name), diagnostic_portal(complete_result())
            )
        document = self.document(out)

        def check(value) -> None:
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
                return
            if isinstance(value, str):
                self.assertTrue(
                    value in allowed or SUPPORT_REFERENCE_PATTERN.match(value),
                    "%r is neither a closed identifier nor a bounded reference" % value,
                )
                return
            self.assertIsInstance(value, (bool, int, type(None)))

        check(document)

    # ---- the unexpected-exception boundary ---- #

    def test_an_unexpected_exception_still_emits_one_bounded_document(self) -> None:
        """The last resort is a closed document, never a traceback and exit 1."""
        hostile = RuntimeError(
            "synthetic private hostile value: password=hunter2 at "
            "https://portal.example.invalid/session for SYNTHETIC-INTENDED-ACCOUNT"
        )
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, err = self.run_diagnostic(
                Path(name), diagnostic_portal(error=hostile)
            )
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(document["status"], ACTION_REQUIRED)
        self.assertIsNone(document["classification"])
        self.assertIs(document["submit_dispatched"], False)
        self.assertEqual(
            document["submit_outcome"], portal_module.SUBMIT_NOT_DISPATCHED
        )
        self.assertEqual(document["support_ref"], cli.UNCLASSIFIED_SUPPORT_REF)
        self.assertEqual(
            document["pre_submit"], portal_module.unobserved_login_witnesses()
        )
        self.assertEqual(
            document["post_submit"],
            portal_module.unobserved_login_witnesses(include_url=True),
        )
        self.assertEqual(err, "", "no traceback, and no stderr surface at all")
        for fragment in (
            "synthetic private hostile value",
            "hunter2",
            "https://",
            "portal.example.invalid",
            "SYNTHETIC-INTENDED-ACCOUNT",
            "Traceback",
            "RuntimeError",
            DIAGNOSTIC_SENTINEL_USERNAME,
            DIAGNOSTIC_SENTINEL_PASSWORD,
        ):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, out + err)

    def test_process_control_is_never_converted_into_a_diagnostic_result(self) -> None:
        """`KeyboardInterrupt` and `SystemExit` are process control, not an outcome."""
        for raised in (KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(raised=type(raised).__name__):
                with tempfile.TemporaryDirectory() as name:
                    with self.assertRaises(type(raised)):
                        self.run_diagnostic(
                            Path(name), diagnostic_portal(error=raised)
                        )

    def test_the_boundary_leaves_no_document_behind_when_it_propagates(self) -> None:
        """A propagated interrupt must not also have emitted a result."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config_path = self.write_diagnostic_config(root)
            out = io.StringIO()
            err = io.StringIO()
            original = cli.PlaywrightPortal
            cli.PlaywrightPortal = diagnostic_portal(error=KeyboardInterrupt())
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(KeyboardInterrupt):
                        cli.main(["login-diagnostic", "--config", str(config_path)])
            finally:
                cli.PlaywrightPortal = original
        self.assertEqual(out.getvalue(), "", "no document is emitted for an interrupt")
        self.assertEqual(err.getvalue(), "")

    def test_the_diagnostic_reference_is_a_bounded_ascii_identifier(self) -> None:
        reference = cli.DIAGNOSTIC_UNCLASSIFIED_SUPPORT_REF
        self.assertEqual(reference, "EG_LOGIN_DIAGNOSTIC_UNCLASSIFIED")
        self.assertRegex(reference, SUPPORT_REFERENCE_PATTERN)
        self.assertTrue(reference.isascii())
        self.assertNotIn(reference, cli.SUPPORT_REFS_BY_MESSAGE.values())
        self.assertNotIn(reference, cli.RETIRED_SUPPORT_REFS)


def navigation_document_control(
    role: str,
    name: str,
    *,
    count: int | str | None = 0,
    visible: bool | None = False,
    enabled: bool | None = False,
    trial_actionable: bool | None = False,
) -> dict:
    return {
        "role": role,
        "name": name,
        "count": count,
        "visible": visible,
        "enabled": enabled,
        "trial_actionable": trial_actionable,
    }


def navigation_document_pre_ems() -> dict:
    return {
        "context_pages": 1,
        "bound_page_frames": 1,
        "ems": navigation_document_control(
            "button", "EMS", count=1, visible=True, enabled=True, trial_actionable=True
        ),
    }


def navigation_document_post_ems() -> dict:
    controls = {
        key: navigation_document_control(role, name)
        for role, name, key in portal_module.NAVIGATION_DIAGNOSTIC_CONTROL_SPECS
    }
    controls["link_billing_manager"] = navigation_document_control(
        "link", "Billing Manager", count=1, visible=True, enabled=True, trial_actionable=True
    )
    return {
        "context_pages": 1,
        "bound_page_frames": 1,
        "route_changed": True,
        "same_origin": True,
        "eb_bill_route_proven": False,
        "controls": controls,
    }


def navigation_complete_result(
    result: str = portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_LINK_READY,
) -> portal_module.NavigationDiagnosticResult:
    return portal_module.NavigationDiagnosticResult(
        result=result,
        status=portal_module.NAVIGATION_DIAGNOSTIC_COMPLETE_STATE,
        authentication_proven=True,
        ems_dispatch_attempted=True,
        ems_dispatch_uncertain=False,
        pre_ems=navigation_document_pre_ems(),
        post_ems=navigation_document_post_ems(),
    )


def navigation_diagnostic_portal(
    result: portal_module.NavigationDiagnosticResult | None = None,
    error: BaseException | None = None,
    recorder: list[bool] | None = None,
):
    class StubNavigationDiagnosticPortal:
        def __init__(self, config, headed: bool = False) -> None:
            self.config = config
            if recorder is not None:
                recorder.append(headed)

        def __enter__(self) -> "StubNavigationDiagnosticPortal":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def navigation_diagnostic(self):
            if error is not None:
                raise error
            return result

        def login(self) -> None:
            raise AssertionError("CLI must call the diagnostic handler, not login directly")

        def inventory(self, *_args, **_kwargs):
            raise AssertionError("navigation diagnostic reached inventory")

        def download(self, *_args, **_kwargs):
            raise AssertionError("navigation diagnostic reached download")

    return StubNavigationDiagnosticPortal


class NavigationDiagnosticCliTests(unittest.TestCase):
    def write_config(self, root: Path) -> Path:
        (root / "archive").mkdir()
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "portal_url": "http://127.0.0.1:1/synthetic",
                    "account_identity": "SYNTHETIC-INTENDED-ACCOUNT",
                    "archive_root": str(root / "archive"),
                    "state_path": str(root / "state" / "state.sqlite3"),
                    "temp_root": str(root / "temp"),
                    "log_root": str(root / "logs"),
                    "timeout_seconds": 5,
                    "max_attempts": 2,
                    "inventory_safety_ceiling": 50,
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def run_navigation(self, root: Path, portal_cls, config_path: Path | None = None):
        config_path = config_path or self.write_config(root)
        original = cli.PlaywrightPortal
        cli.PlaywrightPortal = portal_cls
        out = io.StringIO()
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exit_code = main(["navigation-diagnostic", "--config", str(config_path)])
        finally:
            cli.PlaywrightPortal = original
        return exit_code, out.getvalue(), err.getvalue()

    @staticmethod
    def document(output: str) -> dict:
        lines = output.strip().splitlines()
        if len(lines) != 1:
            raise AssertionError(f"expected one JSON document, got {len(lines)}")
        return json.loads(lines[0])

    def test_the_new_command_accepts_only_config_and_is_fixed_headed(self) -> None:
        args = build_parser().parse_args(
            ["navigation-diagnostic", "--config", "C:/private/eg.json"]
        )
        self.assertEqual(vars(args).keys(), {"command", "config"})
        for extra in (
            ["--headed"],
            ["--archive-root", "C:/private/archive"],
            ["--timeout-seconds", "9"],
            ["extra-positional"],
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigError):
                    build_parser().parse_args(
                        ["navigation-diagnostic", "--config", "C:/private/eg.json", *extra]
                    )
        recorder: list[bool] = []
        with tempfile.TemporaryDirectory() as name:
            exit_code, _out, _err = self.run_navigation(
                Path(name), navigation_diagnostic_portal(navigation_complete_result(), recorder=recorder)
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(recorder, [True])

    def test_the_early_branch_never_runs_production_preflight_or_state_machinery(self) -> None:
        def explode(*_args, **_kwargs):
            raise AssertionError("navigation diagnostic reached production run machinery")

        with tempfile.TemporaryDirectory() as name:
            with mock.patch.object(cli, "SafeLogger", explode), \
                    mock.patch.object(cli, "StateStore", explode), \
                    mock.patch.object(cli, "cleanup_stale_owned_temp", explode), \
                    mock.patch.object(cli, "reconcile_inventory", explode), \
                    mock.patch.object(RuntimeConfig, "preflight", explode):
                exit_code, out, err = self.run_navigation(
                    Path(name), navigation_diagnostic_portal(navigation_complete_result())
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertEqual(self.document(out)["status"], "COMPLETE")

    def test_a_complete_document_has_the_fixed_schema_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, err = self.run_navigation(
                Path(name), navigation_diagnostic_portal(navigation_complete_result())
            )
        document = self.document(out)
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertEqual(document["schema"], cli.NAVIGATION_DIAGNOSTIC_SCHEMA)
        self.assertEqual(document["status"], "COMPLETE")
        self.assertEqual(
            set(document),
            {
                "schema",
                "status",
                "result",
                "authentication_proven",
                "ems_dispatch_attempted",
                "ems_dispatch_uncertain",
                "pre_ems",
                "post_ems",
            },
        )
        self.assertEqual(
            document["result"],
            portal_module.NAVIGATION_DIAGNOSTIC_POST_EMS_BILLING_MANAGER_LINK_READY,
        )

    def test_uncertain_dispatch_uses_the_existing_bounded_navigation_reference(self) -> None:
        failed = portal_module.NavigationDiagnosticResult(
            result=portal_module.NAVIGATION_DIAGNOSTIC_EMS_DISPATCH_UNCERTAIN,
            status=portal_module.NAVIGATION_DIAGNOSTIC_ACTION_REQUIRED_STATE,
            authentication_proven=True,
            ems_dispatch_attempted=True,
            ems_dispatch_uncertain=True,
            pre_ems=navigation_document_pre_ems(),
            post_ems=portal_module.unobserved_navigation_post_ems(),
            failure=LayoutChangedError(portal_module.NAV_EMS_ENTRY_UNCERTAIN_MESSAGE),
        )
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, _err = self.run_navigation(
                Path(name), navigation_diagnostic_portal(failed)
            )
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(document["support_ref"], "EG_NAV_EMS_ENTRY_DISPATCH_UNCERTAIN")

    def test_invalid_evidence_is_replaced_by_fully_unobserved_output_rejected(self) -> None:
        rogue = navigation_complete_result()
        rogue = portal_module.NavigationDiagnosticResult(
            result=rogue.result,
            status=rogue.status,
            authentication_proven=True,
            ems_dispatch_attempted=True,
            ems_dispatch_uncertain=False,
            pre_ems={"hostile": "password=hunter2 https://evil.invalid"},
            post_ems=rogue.post_ems,
        )
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, err = self.run_navigation(
                Path(name), navigation_diagnostic_portal(rogue)
            )
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(err, "")
        self.assertEqual(document["result"], portal_module.NAVIGATION_DIAGNOSTIC_OUTPUT_REJECTED)
        self.assertEqual(document["support_ref"], "EG_NAV_DIAGNOSTIC_OUTPUT_REJECTED")
        self.assertEqual(document["pre_ems"], portal_module.unobserved_navigation_pre_ems())
        encoded = json.dumps(document, sort_keys=True)
        self.assertNotIn("hunter2", encoded)
        self.assertNotIn("evil.invalid", encoded)

    def test_configuration_failure_exits_64_and_emits_one_document(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            missing = root / "missing.json"
            exit_code, out, err = self.run_navigation(
                root, navigation_diagnostic_portal(navigation_complete_result()), missing
            )
        document = self.document(out)
        self.assertEqual(exit_code, 64)
        self.assertEqual(err, "")
        self.assertEqual(document["result"], portal_module.NAVIGATION_DIAGNOSTIC_CONFIGURATION_FAILED)
        self.assertEqual(document["status"], "ACTION_REQUIRED")
        self.assertEqual(document["support_ref"], cli.DIAGNOSTIC_UNCLASSIFIED_SUPPORT_REF)

    def test_recursive_strings_stay_inside_the_closed_public_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _exit_code, out, _err = self.run_navigation(
                Path(name), navigation_diagnostic_portal(navigation_complete_result())
            )
        document = self.document(out)
        allowed = {
            cli.NAVIGATION_DIAGNOSTIC_SCHEMA,
            "COMPLETE",
            "ACTION_REQUIRED",
            ">1",
            "button",
            "link",
            "EMS",
            "Billing Manager",
            "EB Bill",
            *portal_module.NAVIGATION_DIAGNOSTIC_RESULT_IDENTIFIERS,
            *cli.SUPPORT_REFS_BY_MESSAGE.values(),
        }

        def check(value) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    check(nested)
                return
            if isinstance(value, str):
                self.assertIn(value, allowed)
                return
            self.assertIsInstance(value, (bool, int, type(None)))

        check(document)

    def test_the_new_command_never_returns_retryable_exit_10(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            exit_code, _out, _err = self.run_navigation(
                Path(name), navigation_diagnostic_portal(error=DownloadError("private download failure"))
            )
        self.assertIn(exit_code, (20, 64))
        self.assertNotEqual(exit_code, 10)


class NavigationDiagnosticRepair1OutputValidationTests(NavigationDiagnosticCliTests):
    @staticmethod
    def valid_document() -> dict:
        return cli.navigation_diagnostic_document(navigation_complete_result())

    @staticmethod
    def malformed_result(**overrides) -> portal_module.NavigationDiagnosticResult:
        valid = navigation_complete_result()
        values = {
            "result": valid.result,
            "status": valid.status,
            "authentication_proven": valid.authentication_proven,
            "ems_dispatch_attempted": valid.ems_dispatch_attempted,
            "ems_dispatch_uncertain": valid.ems_dispatch_uncertain,
            "pre_ems": valid.pre_ems,
            "post_ems": valid.post_ems,
            "failure": valid.failure,
        }
        values.update(overrides)
        return portal_module.NavigationDiagnosticResult(**values)

    def assert_rejected_output(self, result: portal_module.NavigationDiagnosticResult) -> None:
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, err = self.run_navigation(
                Path(name), navigation_diagnostic_portal(result)
            )
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(err, "")
        self.assertEqual(document["schema"], cli.NAVIGATION_DIAGNOSTIC_SCHEMA)
        self.assertEqual(document["status"], "ACTION_REQUIRED")
        self.assertEqual(document["result"], portal_module.NAVIGATION_DIAGNOSTIC_OUTPUT_REJECTED)
        self.assertEqual(document["support_ref"], "EG_NAV_DIAGNOSTIC_OUTPUT_REJECTED")
        self.assertEqual(document["authentication_proven"], False)
        self.assertEqual(document["ems_dispatch_attempted"], False)
        self.assertEqual(document["ems_dispatch_uncertain"], False)
        self.assertEqual(document["pre_ems"], portal_module.unobserved_navigation_pre_ems())
        self.assertEqual(document["post_ems"], portal_module.unobserved_navigation_post_ems())

    def test_count_contract_rejects_list_dict_float_and_string_impostors(self) -> None:
        for value in ([], {}, 1.0, "1"):
            with self.subTest(value=repr(value)):
                document = self.valid_document()
                document["pre_ems"]["context_pages"] = value
                self.assertFalse(cli._valid_navigation_document(document))
                self.assert_rejected_output(
                    self.malformed_result(
                        pre_ems=document["pre_ems"], post_ems=document["post_ems"]
                    )
                )

    def test_boolean_contract_rejects_numeric_float_string_list_dict_and_custom_values(self) -> None:
        class Truthy:
            def __bool__(self):
                return True

        for value in (0, 1, 0.0, 1.0, "true", [], {}, Truthy()):
            with self.subTest(value=repr(value)):
                self.assertFalse(cli._valid_navigation_boolean(value))
                self.assert_rejected_output(
                    self.malformed_result(authentication_proven=value)
                )

    def test_exact_booleans_and_valid_nested_document_remain_accepted(self) -> None:
        document = self.valid_document()
        self.assertTrue(cli._valid_navigation_document(document))
        self.assertTrue(cli._valid_navigation_boolean(True))
        self.assertTrue(cli._valid_navigation_boolean(False))
        self.assertEqual(cli.navigation_diagnostic_document(navigation_complete_result()), document)

    def test_wrong_containers_missing_keys_unexpected_keys_and_bad_support_refs_reject(self) -> None:
        wrong_outer = self.valid_document()
        wrong_outer["post_ems"] = []
        self.assertFalse(cli._valid_navigation_document(wrong_outer))

        missing = self.valid_document()
        del missing["pre_ems"]["ems"]
        self.assertFalse(cli._valid_navigation_document(missing))

        unexpected = self.valid_document()
        unexpected["post_ems"]["unexpected"] = False
        self.assertFalse(cli._valid_navigation_document(unexpected))

        for support_ref in ("bad-ref", "EG_NAV_DIAGNOSTIC_NOT_ALLOWED", 1, [], {}):
            with self.subTest(support_ref=repr(support_ref)):
                action_required = self.valid_document()
                action_required["status"] = "ACTION_REQUIRED"
                action_required["result"] = portal_module.NAVIGATION_DIAGNOSTIC_EMS_NOT_READY
                action_required["support_ref"] = support_ref
                self.assertFalse(cli._valid_navigation_document(action_required))

        class RogueDict(dict):
            pass

        self.assertFalse(cli._valid_navigation_document(RogueDict(self.valid_document())))

    def test_validator_is_total_for_malformed_result_fields(self) -> None:
        class Hostile:
            def __eq__(self, _other):
                raise RuntimeError("private malformed value")

        result = self.malformed_result(result=Hostile(), pre_ems=Hostile())
        document = cli.navigation_diagnostic_document(result)
        self.assertEqual(document["result"], portal_module.NAVIGATION_DIAGNOSTIC_OUTPUT_REJECTED)
        self.assertTrue(cli._valid_navigation_document(document))


class NavigationDiagnosticRepair2OutputValidationTests(NavigationDiagnosticCliTests):
    @staticmethod
    def valid_document() -> dict:
        return cli.navigation_diagnostic_document(navigation_complete_result())

    @staticmethod
    def replace_key(mapping: dict, key: str, replacement) -> None:
        mapping[replacement] = mapping.pop(key)

    def assert_rejected_emission(self, document: dict) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            emitted = cli.emit_navigation_diagnostic(document)
        parsed = self.document(out.getvalue())
        expected = portal_module.unobserved_navigation_post_ems()
        self.assertEqual(emitted, parsed)
        self.assertEqual(emitted, cli._unobserved_navigation_document())
        self.assertEqual(parsed["result"], portal_module.NAVIGATION_DIAGNOSTIC_OUTPUT_REJECTED)
        self.assertEqual(parsed["post_ems"], expected)

    def test_every_public_mapping_boundary_rejects_subclass_and_non_string_keys(self) -> None:
        class StringSubclass(str):
            pass

        cases = []
        document = self.valid_document()
        self.replace_key(document, "schema", StringSubclass("schema"))
        cases.append(document)

        document = self.valid_document()
        self.replace_key(document["pre_ems"], "context_pages", StringSubclass("context_pages"))
        cases.append(document)

        document = self.valid_document()
        self.replace_key(document["post_ems"], "route_changed", StringSubclass("route_changed"))
        cases.append(document)

        document = self.valid_document()
        self.replace_key(document["post_ems"]["controls"], "link_eb_bill", StringSubclass("link_eb_bill"))
        cases.append(document)

        document = self.valid_document()
        self.replace_key(
            document["post_ems"]["controls"]["link_eb_bill"],
            "role",
            StringSubclass("role"),
        )
        cases.append(document)

        document = self.valid_document()
        self.replace_key(document, "schema", object())
        cases.append(document)

        for malformed in cases:
            with self.subTest(keys=tuple(malformed)):
                self.assertFalse(cli._valid_navigation_document(malformed))
                self.assert_rejected_emission(malformed)

    def test_exact_builtin_mapping_keys_and_values_remain_accepted(self) -> None:
        document = self.valid_document()
        self.assertTrue(cli._valid_navigation_document(document))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            emitted = cli.emit_navigation_diagnostic(document)
        self.assertEqual(emitted, document)
        self.assertEqual(self.document(out.getvalue()), document)

    def test_serialization_failure_returns_the_final_rejected_document_exit_code(self) -> None:
        real_dumps = json.dumps
        with tempfile.TemporaryDirectory() as name:
            config_path = self.write_config(Path(name))
            json_proxy = mock.Mock(wraps=cli.json)
            serialization_calls = 0

            def dumps_side_effect(*args, **kwargs):
                nonlocal serialization_calls
                if serialization_calls == 0:
                    serialization_calls += 1
                    raise TypeError("malformed serialization")
                return real_dumps(*args, **kwargs)

            json_proxy.dumps.side_effect = dumps_side_effect
            with mock.patch.object(cli, "json", json_proxy):
                exit_code, out, err = self.run_navigation(
                    Path(name),
                    navigation_diagnostic_portal(navigation_complete_result()),
                    config_path,
                )
        document = self.document(out)
        self.assertEqual(exit_code, 20)
        self.assertEqual(err, "")
        self.assertEqual(document, cli._unobserved_navigation_document())


class NavigationDiagnosticRunbookTests(unittest.TestCase):
    def test_future_live_authority_boundary_covers_every_invariant(self) -> None:
        runbook = (Path(__file__).parents[1] / "docs" / "runbook.md").read_text(
            encoding="utf-8"
        )
        runbook = " ".join(runbook.split())
        required = (
            "exact reviewed Repair-1 H/T/base",
            "exact merged authority",
            "exact reviewed diagnostic command",
            "exactly one OS process/session",
            "success/failure/crash/interruption/configuration failure",
            "at most one EMS dispatch",
            "zero Billing Manager",
            "zero EB Bill",
            "zero production `run`",
            "zero Scheduler action",
            "fixed diagnostic schema",
            "screenshots",
            "traces",
            "HAR files",
            "storage-state exports",
            "raw URLs",
            "portal text",
            "customer/private evidence",
            "automatic retry under the same authority",
            "grants no live authority",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runbook)


# ---- DL-XB-199 G2-083 / G3-084: the no-Download pre-dispatch diagnostic ---- #
#
# The CLI half of the contract: a closed `--config`-only surface, a fixed
# headless portal, one exact closed document, the exit mapping, the early
# return that never reaches the run machinery, and fail-closed output.


def preflight_evidence(**overrides) -> portal_module.DownloadPreflightEvidence:
    values = {
        "row_ordinal": 1,
        "reason_code": "CONTROL_NOT_ENABLED",
        "last_checkpoint": "CONTROL_ENABLED",
        "window_expired": True,
        "not_ready_looks": 7,
        "elapsed_bucket": "LT_60S",
        "control": portal_module.DownloadPreflightControl(
            count_bucket=1, visible=True, enabled=False
        ),
        "snapshot": None,
    }
    values.update(overrides)
    return portal_module.DownloadPreflightEvidence(**values)


def row_download_count_evidence(**scan_overrides) -> portal_module.DownloadPreflightEvidence:
    """The G3-090-shaped RESULTS_ROW_DOWNLOAD_COUNT failure with its v2 surface_scan."""

    scan = {
        "offending_surface_row_ordinal": 3,
        "offending_download_count_bucket": 0,
        "surface_row_count_equal_frozen": True,
        "offending_witness_looks": 7,
        "offending_row_changed_between_looks": False,
    }
    scan.update(scan_overrides)
    return preflight_evidence(
        reason_code="RESULTS_ROW_DOWNLOAD_COUNT",
        last_checkpoint="RESULTS_SURFACE",
        control=portal_module.DownloadPreflightControl(),
        surface_scan=portal_module.DownloadPreflightSurfaceScan(**scan),
    )


def preflight_portal(
    *,
    rows: int = 3,
    result=None,
    login_error: BaseException | None = None,
    inventory_error: BaseException | None = None,
    preflight_error: BaseException | None = None,
    enter_error: BaseException | None = None,
    recorder: dict | None = None,
):
    """A portal class that satisfies the diagnostic CLI contract without a browser."""

    record = recorder if recorder is not None else {}
    record.setdefault("headed", [])
    record.setdefault("calls", [])

    class StubPreflightPortal:
        def __init__(self, config, headed: bool = False) -> None:
            record["headed"].append(headed)

        def __enter__(self) -> "StubPreflightPortal":
            if enter_error is not None:
                raise enter_error
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            record["calls"].append("close")
            return None

        def login(self) -> None:
            record["calls"].append("login")
            if login_error is not None:
                raise login_error

        def inventory(self, safety_ceiling: int) -> list:
            record["calls"].append("inventory")
            if inventory_error is not None:
                raise inventory_error
            return [portal_module.InvoiceRow(ordinal=index, binding=object()) for index in range(rows)]

        def download_preflight(self, handles) -> object:
            record["calls"].append(("download_preflight", len(handles)))
            if preflight_error is not None:
                raise preflight_error
            if result is not None:
                return result
            return portal_module.DownloadPreflightDiagnosticResult(rows_passed=len(handles))

        def download(self, row, destination) -> str:
            raise AssertionError("the diagnostic must never dispatch a Download")

        def login_diagnostic(self):
            raise AssertionError("wrong diagnostic")

        def navigation_diagnostic(self):
            raise AssertionError("wrong diagnostic")

    return StubPreflightPortal


class DownloadPreflightDiagnosticCliTests(unittest.TestCase):
    COMMAND = "download-preflight-diagnostic"

    def write_config(self, root: Path) -> Path:
        (root / "archive").mkdir()
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "portal_url": "http://127.0.0.1:1/synthetic",
                    "account_identity": "SYNTHETIC-INTENDED-ACCOUNT",
                    "archive_root": str(root / "archive"),
                    "state_path": str(root / "state" / "state.sqlite3"),
                    "temp_root": str(root / "temp"),
                    "log_root": str(root / "logs"),
                    "timeout_seconds": 5,
                    "max_attempts": 2,
                    "inventory_safety_ceiling": 50,
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def run_preflight(self, root: Path, portal_cls, config_path: Path | None = None):
        config_path = config_path or self.write_config(root)
        original = cli.PlaywrightPortal
        cli.PlaywrightPortal = portal_cls
        out, err = io.StringIO(), io.StringIO()
        environment = {
            "ENERGYGRID_USERNAME": DIAGNOSTIC_SENTINEL_USERNAME,
            "ENERGYGRID_PASSWORD": DIAGNOSTIC_SENTINEL_PASSWORD,
        }
        previous = {name: os.environ.get(name) for name in environment}
        os.environ.update(environment)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exit_code = main([self.COMMAND, "--config", str(config_path)])
        finally:
            cli.PlaywrightPortal = original
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        return exit_code, out.getvalue(), err.getvalue()

    def document(self, out: str) -> dict:
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1, "exactly one document")
        document = json.loads(lines[0])
        self.assertEqual(set(document), set(cli._PREFLIGHT_DOCUMENT_KEYS))
        self.assertEqual(document["schema"], "energygrid.download_preflight_diagnostic.v2")
        self.assertIs(document["download_dispatched"], False)
        return document

    def outcome(self, portal_cls):
        with tempfile.TemporaryDirectory() as name:
            exit_code, out, err = self.run_preflight(Path(name), portal_cls)
        self.assertEqual(err, "", "stderr stays empty")
        return exit_code, self.document(out)

    # ---- argument surface ---- #

    def test_the_command_accepts_only_a_config_argument(self) -> None:
        args = build_parser().parse_args([self.COMMAND, "--config", "C:/private/eg.json"])
        self.assertEqual(args.command, self.COMMAND)
        self.assertEqual(vars(args).keys(), {"command", "config"})
        for extra in (
            ["--headed"],
            ["--archive-root", "C:/private/archive"],
            ["--state-path", "C:/private/state.sqlite3"],
            ["--temp-root", "C:/private/temp"],
            ["--log-root", "C:/private/logs"],
            ["--timeout-seconds", "9"],
            ["--max-attempts", "3"],
            ["--attempt", "1"],
            ["--portal-url", "http://example.invalid"],
            ["extra-positional"],
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigError):
                    build_parser().parse_args([self.COMMAND, "--config", "C:/private/eg.json", *extra])
        with self.assertRaises(ConfigError):
            build_parser().parse_args([self.COMMAND])

    def test_the_portal_is_always_headless(self) -> None:
        recorder: dict = {}
        exit_code, _document = self.outcome(preflight_portal(recorder=recorder))
        self.assertEqual(exit_code, 0)
        self.assertEqual(recorder["headed"], [False], "headless is a property, not a choice")
        self.assertEqual(
            recorder["calls"], ["login", "inventory", ("download_preflight", 3), "close"]
        )

    # ---- result and exit paths ---- #

    def test_all_rows_passed_is_complete_and_exits_zero(self) -> None:
        exit_code, document = self.outcome(preflight_portal(rows=3))
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            document,
            {
                "schema": cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_SCHEMA,
                "status": cli.DIAGNOSTIC_COMPLETE,
                "result": "PREFLIGHT_ALL_ROWS_PASSED",
                "support_ref": None,
                "download_dispatched": False,
                "inventory_count": 3,
                "rows_passed": 3,
                "failure": None,
            },
        )

    def test_a_typed_row_failure_is_complete_and_names_its_closed_reason(self) -> None:
        result = portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=preflight_evidence())
        exit_code, document = self.outcome(preflight_portal(rows=3, result=result))
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["status"], cli.DIAGNOSTIC_COMPLETE)
        self.assertEqual(document["result"], "PREFLIGHT_ROW_FAILED")
        self.assertIsNone(document["support_ref"])
        self.assertEqual((document["inventory_count"], document["rows_passed"]), (3, 1))
        self.assertEqual(
            document["failure"],
            {
                "row_ordinal": 1,
                "reason_code": "CONTROL_NOT_ENABLED",
                "last_checkpoint": "CONTROL_ENABLED",
                "window_expired": True,
                "not_ready_looks": 7,
                "elapsed_bucket": "LT_60S",
                "control": {
                    "count_bucket": 1,
                    "visible": True,
                    "enabled": False,
                    "trial_actionability": "NOT_REACHED",
                },
                "snapshot": None,
                "surface_scan": None,
            },
        )

    def test_a_snapshot_mismatch_carries_only_the_bounded_comparison(self) -> None:
        evidence = preflight_evidence(
            row_ordinal=0,
            reason_code="SNAPSHOT_MISMATCH",
            last_checkpoint="SNAPSHOT_COMPARE",
            window_expired=False,
            not_ready_looks=0,
            elapsed_bucket="LT_250MS",
            control=portal_module.DownloadPreflightControl(),
            snapshot=portal_module.DownloadPreflightSnapshot(
                row_count_equal=True, header_equal=True, rows_equal_as_set=True, changed_row_count=2
            ),
        )
        result = portal_module.DownloadPreflightDiagnosticResult(rows_passed=0, failure=evidence)
        exit_code, document = self.outcome(preflight_portal(rows=3, result=result))
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            document["failure"]["snapshot"],
            {"row_count_equal": True, "header_equal": True, "rows_equal_as_set": True, "changed_row_count": 2},
        )

    def test_every_failure_to_observe_is_action_required_with_its_exit_code(self) -> None:
        cases = (
            (preflight_portal(enter_error=DependencyError("Playwright Chromium could not be started")),
             "CONFIGURATION_FAILED", 64, "APP_ERROR_UNCLASSIFIED"),
            (preflight_portal(login_error=LoginError("portal rejected the login")),
             "LOGIN_FAILED", 20, "EG_LOGIN_PORTAL_REJECTED"),
            (preflight_portal(login_error=LayoutChangedError("login submit control did not appear")),
             "LOGIN_FAILED", 20, "EG_LOGIN_SUBMIT_NOT_APPEAR"),
            (preflight_portal(inventory_error=LayoutChangedError(portal_module.RESULTS_HEADER_ONLY_MESSAGE)),
             "INVENTORY_FAILED", 20, "EG_NAV_RESULTS_HEADER_ONLY"),
            (preflight_portal(login_error=RuntimeError("private https://portal.example.invalid hunter2")),
             "UNEXPECTED_FAILURE", 20, "APP_ERROR_UNCLASSIFIED"),
            (preflight_portal(preflight_error=AppError("private ACCT-778899")),
             "UNEXPECTED_FAILURE", 20, "APP_ERROR_UNCLASSIFIED"),
            (preflight_portal(preflight_error=RuntimeError("private ACCT-778899")),
             "UNEXPECTED_FAILURE", 20, "APP_ERROR_UNCLASSIFIED"),
        )
        for portal_cls, result, exit_expected, support_ref in cases:
            with self.subTest(result=result, support_ref=support_ref):
                exit_code, document = self.outcome(portal_cls)
                self.assertEqual(exit_code, exit_expected)
                self.assertEqual(document["status"], ACTION_REQUIRED)
                self.assertEqual(document["result"], result)
                self.assertEqual(document["support_ref"], support_ref)
                self.assertIsNone(document["inventory_count"])
                self.assertIsNone(document["rows_passed"])
                self.assertIsNone(document["failure"])

    def test_an_empty_inventory_is_action_required_and_never_inspected(self) -> None:
        recorder: dict = {}
        exit_code, document = self.outcome(preflight_portal(rows=0, recorder=recorder))
        self.assertEqual(exit_code, 20)
        self.assertEqual(document["result"], "INVENTORY_EMPTY")
        self.assertEqual((document["inventory_count"], document["rows_passed"]), (0, 0))
        self.assertIsNone(document["support_ref"])
        self.assertNotIn(("download_preflight", 0), recorder["calls"])

    def test_an_unreadable_configuration_exits_64_with_one_document(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config_path = root / "missing.json"
            exit_code, out, err = self.run_preflight(root, preflight_portal(), config_path)
        document = self.document(out)
        self.assertEqual(exit_code, 64)
        self.assertEqual(err, "")
        self.assertEqual(document["result"], "CONFIGURATION_FAILED")
        self.assertIn(document["support_ref"], cli.DOWNLOAD_PREFLIGHT_ALLOWED_SUPPORT_REFS)

    def test_a_malformed_portal_result_is_rejected_wholesale(self) -> None:
        malformed = (
            {"rows_passed": 3},
            portal_module.DownloadPreflightDiagnosticResult(rows_passed=2),
            portal_module.DownloadPreflightDiagnosticResult(rows_passed=3, download_dispatched=True),
            portal_module.DownloadPreflightDiagnosticResult(rows_passed=True),
            portal_module.DownloadPreflightDiagnosticResult(
                rows_passed=0, failure=preflight_evidence(row_ordinal=0, reason_code="PRIVATE ACCT-778899")
            ),
            portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=preflight_evidence(row_ordinal=2)),
            portal_module.DownloadPreflightDiagnosticResult(
                rows_passed=1, failure=preflight_evidence(last_checkpoint="CONTROL_ACTIONABLE")
            ),
            portal_module.DownloadPreflightDiagnosticResult(
                rows_passed=1, failure=preflight_evidence(window_expired=False)
            ),
            portal_module.DownloadPreflightDiagnosticResult(
                rows_passed=1,
                failure=preflight_evidence(
                    snapshot=portal_module.DownloadPreflightSnapshot(True, True, True, 0)
                ),
            ),
            portal_module.DownloadPreflightDiagnosticResult(
                rows_passed=1, failure=preflight_evidence(elapsed_bucket="12345ms")
            ),
            portal_module.DownloadPreflightDiagnosticResult(
                rows_passed=1, failure=preflight_evidence(not_ready_looks=10_000)
            ),
            portal_module.DownloadPreflightDiagnosticResult(
                rows_passed=1,
                failure=preflight_evidence(
                    control=portal_module.DownloadPreflightControl(count_bucket=0, visible=True)
                ),
            ),
        )
        for result in malformed:
            with self.subTest(result=repr(result)[:80]):
                exit_code, document = self.outcome(preflight_portal(rows=3, result=result))
                self.assertEqual(exit_code, 20)
                self.assertEqual(
                    document,
                    {
                        "schema": cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_SCHEMA,
                        "status": ACTION_REQUIRED,
                        "result": "OUTPUT_REJECTED",
                        "support_ref": None,
                        "download_dispatched": False,
                        "inventory_count": None,
                        "rows_passed": None,
                        "failure": None,
                    },
                )
                self.assertNotIn("ACCT", json.dumps(document))

    def test_the_validator_is_exact_closed_and_total(self) -> None:
        valid = cli.download_preflight_document(
            3, portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=preflight_evidence())
        )
        self.assertTrue(cli._valid_preflight_document(valid))

        class SubDict(dict):
            pass

        def mutated(change):
            document = json.loads(json.dumps(valid))
            change(document)
            return document

        rejected = (
            mutated(lambda d: d.update(extra=1)),
            mutated(lambda d: d.pop("support_ref")),
            mutated(lambda d: d.update(schema="energygrid.download_preflight_diagnostic.v1")),
            mutated(lambda d: d.update(schema="energygrid.download_preflight_diagnostic.v3")),
            mutated(lambda d: d["failure"].pop("surface_scan")),
            mutated(lambda d: d.update(status="ACTION_REQUIRED")),
            mutated(lambda d: d.update(download_dispatched=0)),
            mutated(lambda d: d.update(support_ref="EG_NAV_RESULTS_HEADER_ONLY")),
            mutated(lambda d: d.update(inventory_count=3.0)),
            mutated(lambda d: d.update(rows_passed=True)),
            mutated(lambda d: d["failure"].update(row_ordinal=1.0)),
            mutated(lambda d: d["failure"].update(extra="x")),
            mutated(lambda d: d["failure"]["control"].update(visible="yes")),
            mutated(lambda d: d["failure"]["control"].update(count_bucket=2)),
            mutated(lambda d: d["failure"]["control"].update(trial_actionability="MAYBE")),
            mutated(lambda d: d["failure"].update(control=SubDict(d["failure"]["control"]))),
            SubDict(valid),
            None,
            [],
            "document",
        )
        for document in rejected:
            with self.subTest(document=repr(document)[:80]):
                self.assertFalse(cli._valid_preflight_document(document))
        # A failure object is admitted by exactly one result.
        for result in cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_RESULTS:
            if result == "PREFLIGHT_ROW_FAILED":
                continue
            with self.subTest(result=result):
                self.assertFalse(
                    cli._valid_preflight_document(mutated(lambda d, r=result: d.update(result=r)))
                )

    # ---- DL-XB-199 G3-092: schema v2 failure.surface_scan ---- #

    def test_the_v2_failure_object_gains_exactly_surface_scan(self) -> None:
        self.assertEqual(cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_SCHEMA, "energygrid.download_preflight_diagnostic.v2")
        self.assertEqual(
            cli._PREFLIGHT_DOCUMENT_KEYS,
            {"schema", "status", "result", "support_ref", "download_dispatched", "inventory_count", "rows_passed", "failure"},
            "the top-level document keys are unchanged",
        )
        self.assertEqual(
            cli._PREFLIGHT_FAILURE_KEYS,
            {"row_ordinal", "reason_code", "last_checkpoint", "window_expired", "not_ready_looks",
             "elapsed_bucket", "control", "snapshot", "surface_scan"},
        )
        self.assertEqual(
            cli._PREFLIGHT_SURFACE_SCAN_KEYS,
            {"offending_surface_row_ordinal", "offending_download_count_bucket", "surface_row_count_equal_frozen",
             "offending_witness_looks", "offending_row_changed_between_looks"},
        )

    def test_a_row_download_count_failure_carries_its_surface_scan(self) -> None:
        result = portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=row_download_count_evidence())
        exit_code, document = self.outcome(preflight_portal(rows=4, result=result))
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["result"], "PREFLIGHT_ROW_FAILED")
        failure = document["failure"]
        self.assertEqual(failure["row_ordinal"], 1, "the handle ordinal keeps its meaning")
        self.assertEqual(
            failure["surface_scan"],
            {
                "offending_surface_row_ordinal": 3,
                "offending_download_count_bucket": 0,
                "surface_row_count_equal_frozen": True,
                "offending_witness_looks": 7,
                "offending_row_changed_between_looks": False,
            },
        )
        for bucket in (0, ">1"):
            with self.subTest(bucket=bucket):
                evidence = row_download_count_evidence(offending_download_count_bucket=bucket, offending_witness_looks=1)
                document = cli.download_preflight_document(
                    4, portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=evidence)
                )
                self.assertEqual(document["failure"]["surface_scan"]["offending_download_count_bucket"], bucket)

    def test_the_surface_scan_validator_is_exact_closed_and_fails_closed(self) -> None:
        valid = cli.download_preflight_document(
            4, portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=row_download_count_evidence())
        )
        self.assertTrue(cli._valid_preflight_document(valid))

        class SubDict(dict):
            pass

        def mutated(change):
            document = json.loads(json.dumps(valid))
            change(document)
            return document

        def scan(**changes):
            return mutated(lambda d: d["failure"]["surface_scan"].update(changes))

        def drop(key):
            return mutated(lambda d: d["failure"]["surface_scan"].pop(key))

        ceiling = cli.MAX_INVENTORY_CEILING
        rejected = {
            "surface_scan null for its own reason": mutated(lambda d: d["failure"].update(surface_scan=None)),
            "surface_scan missing": mutated(lambda d: d["failure"].pop("surface_scan")),
            "surface_scan not a mapping": mutated(lambda d: d["failure"].update(surface_scan=[3, 0])),
            "surface_scan subclass": mutated(lambda d: d["failure"].update(surface_scan=SubDict(d["failure"]["surface_scan"]))),
            "extra key": scan(extra=1),
            "row text key": scan(row_text="PRIVATE-ROW ACCT-778899"),
            **{f"missing {key}": drop(key) for key in cli._PREFLIGHT_SURFACE_SCAN_KEYS},
            "ordinal bool": scan(offending_surface_row_ordinal=True),
            "ordinal float": scan(offending_surface_row_ordinal=3.0),
            "ordinal string": scan(offending_surface_row_ordinal="3"),
            "ordinal negative": scan(offending_surface_row_ordinal=-1),
            "ordinal at ceiling": scan(offending_surface_row_ordinal=ceiling),
            "ordinal null": scan(offending_surface_row_ordinal=None),
            "bucket one": scan(offending_download_count_bucket=1),
            "bucket two": scan(offending_download_count_bucket=2),
            "bucket string zero": scan(offending_download_count_bucket="0"),
            "bucket false": scan(offending_download_count_bucket=False),
            "bucket float": scan(offending_download_count_bucket=0.0),
            "bucket >=1": scan(offending_download_count_bucket=">=1"),
            "bucket null": scan(offending_download_count_bucket=None),
            "row_count_equal int": scan(surface_row_count_equal_frozen=1),
            "row_count_equal null": scan(surface_row_count_equal_frozen=None),
            "changed string": scan(offending_row_changed_between_looks="false"),
            "changed null": scan(offending_row_changed_between_looks=None),
            "witness zero": scan(offending_witness_looks=0),
            "witness above not_ready_looks": scan(offending_witness_looks=8),
            "witness bool": scan(offending_witness_looks=True),
            "witness float": scan(offending_witness_looks=7.0),
            "witness null": scan(offending_witness_looks=None),
            "surface_scan on another reason": mutated(
                lambda d: d["failure"].update(reason_code="RESULTS_DOWNLOAD_COUNT_MISMATCH")
            ),
        }
        for label, document in rejected.items():
            with self.subTest(case=label):
                self.assertFalse(cli._valid_preflight_document(document))
        # Every legitimate domain edge is admitted.
        for label, document in {
            "ordinal zero": scan(offending_surface_row_ordinal=0),
            "ordinal ceiling-1": scan(offending_surface_row_ordinal=ceiling - 1),
            "bucket >1": scan(offending_download_count_bucket=">1"),
            "witness one": scan(offending_witness_looks=1),
            "row count differs, changed": scan(surface_row_count_equal_frozen=False, offending_row_changed_between_looks=True),
        }.items():
            with self.subTest(admitted=label):
                self.assertTrue(cli._valid_preflight_document(document))
        # Every other reason requires surface_scan null, and a mapping there fails closed.
        a_scan = valid["failure"]["surface_scan"]
        other = cli.download_preflight_document(
            3, portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=preflight_evidence())
        )
        self.assertIsNone(other["failure"]["surface_scan"])
        polluted = json.loads(json.dumps(other))
        polluted["failure"]["surface_scan"] = dict(a_scan)
        self.assertFalse(cli._valid_preflight_document(polluted))

    def test_a_malformed_surface_scan_from_the_portal_is_rejected_wholesale(self) -> None:
        malformed = (
            row_download_count_evidence(offending_download_count_bucket=1),
            row_download_count_evidence(offending_witness_looks=0),
            row_download_count_evidence(offending_witness_looks=8),
            row_download_count_evidence(offending_surface_row_ordinal=True),
            row_download_count_evidence(offending_surface_row_ordinal=cli.MAX_INVENTORY_CEILING),
            preflight_evidence(reason_code="RESULTS_ROW_DOWNLOAD_COUNT", last_checkpoint="RESULTS_SURFACE",
                               control=portal_module.DownloadPreflightControl()),
            preflight_evidence(surface_scan=row_download_count_evidence().surface_scan),
        )
        for evidence in malformed:
            with self.subTest(evidence=repr(evidence.surface_scan)[:80]):
                result = portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=evidence)
                exit_code, document = self.outcome(preflight_portal(rows=4, result=result))
                self.assertEqual(exit_code, 20)
                self.assertEqual(document["result"], "OUTPUT_REJECTED")
                self.assertIsNone(document["failure"])

    def test_a_v1_validator_rejects_every_v2_document(self) -> None:
        """Compatibility negative control: v2 never passes the v1 validation rules.

        The published M1 validator delegates to the checkout's
        `_valid_preflight_document`; restoring the two v1 constants reproduces
        the v1 rules. Each v1 gate rejects a v2 document on its own, so an old
        bundle can never admit a new document and must be rebuilt.
        """
        v1_schema = "energygrid.download_preflight_diagnostic.v1"
        v1_failure_keys = cli._PREFLIGHT_FAILURE_KEYS - {"surface_scan"}
        documents = (
            cli.download_preflight_document(3, portal_module.DownloadPreflightDiagnosticResult(rows_passed=3)),
            cli.download_preflight_document(
                3, portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=preflight_evidence())
            ),
            cli.download_preflight_document(
                4, portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=row_download_count_evidence())
            ),
        )
        for document in documents:
            self.assertTrue(cli._valid_preflight_document(document))
            with self.subTest(result=document["result"], gate="v1 schema and keys"):
                with mock.patch.object(cli, "DOWNLOAD_PREFLIGHT_DIAGNOSTIC_SCHEMA", v1_schema), \
                        mock.patch.object(cli, "_PREFLIGHT_FAILURE_KEYS", v1_failure_keys):
                    self.assertFalse(cli._valid_preflight_document(document))
            with self.subTest(result=document["result"], gate="v1 schema only"):
                with mock.patch.object(cli, "DOWNLOAD_PREFLIGHT_DIAGNOSTIC_SCHEMA", v1_schema):
                    self.assertFalse(cli._valid_preflight_document(document))
            if document["failure"] is not None:
                with self.subTest(result=document["result"], gate="v1 failure keys only"):
                    relabelled = dict(document, schema=cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_SCHEMA)
                    with mock.patch.object(cli, "_PREFLIGHT_FAILURE_KEYS", v1_failure_keys):
                        self.assertFalse(cli._valid_preflight_document(relabelled))
        # And the v2 validator does not admit a v1-shaped document either.
        v1_document = json.loads(json.dumps(documents[1]))
        v1_document["schema"] = v1_schema
        v1_document["failure"].pop("surface_scan")
        self.assertFalse(cli._valid_preflight_document(v1_document))

    def test_a_serialisation_failure_emits_the_fixed_rejected_document(self) -> None:
        valid = cli.download_preflight_document(3, portal_module.DownloadPreflightDiagnosticResult(rows_passed=3))
        out = io.StringIO()
        with mock.patch.object(cli.json, "dumps", side_effect=[TypeError("private"), json.dumps(cli._preflight_rejected_document(), sort_keys=True)]):
            with contextlib.redirect_stdout(out):
                emitted = cli.emit_download_preflight_diagnostic(valid)
        self.assertEqual(emitted["result"], "OUTPUT_REJECTED")
        self.assertEqual(json.loads(out.getvalue())["result"], "OUTPUT_REJECTED")
        self.assertEqual(cli._download_preflight_exit_code(emitted), 20)

    # ---- isolation from the run path ---- #

    def test_the_run_path_machinery_is_unreachable(self) -> None:
        def explode(*_args, **_kwargs):
            raise AssertionError("the diagnostic reached the run path")

        with tempfile.TemporaryDirectory() as name:
            with mock.patch.object(cli, "SafeLogger", explode), \
                    mock.patch.object(cli, "StateStore", explode), \
                    mock.patch.object(cli, "cleanup_stale_owned_temp", explode), \
                    mock.patch.object(cli, "reconcile_inventory", explode), \
                    mock.patch.object(RuntimeConfig, "preflight", explode):
                exit_code, out, err = self.run_preflight(Path(name), preflight_portal())
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        self.assertEqual(self.document(out)["result"], "PREFLIGHT_ALL_ROWS_PASSED")

    def test_the_diagnostic_creates_no_filesystem_artefact(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config_path = self.write_config(root)
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            for portal_cls in (
                preflight_portal(),
                preflight_portal(result=portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=preflight_evidence())),
                preflight_portal(login_error=LoginError("portal rejected the login")),
            ):
                self.run_preflight(root, portal_cls, config_path)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        self.assertEqual(before, after, "no state, log, temp or archive artefact")

    def test_the_diagnostic_never_returns_the_retryable_code(self) -> None:
        for portal_cls in (
            preflight_portal(login_error=DownloadError("synthetic network failure")),
            preflight_portal(inventory_error=DownloadError("synthetic network failure")),
            preflight_portal(preflight_error=DownloadError("synthetic network failure")),
        ):
            exit_code, document = self.outcome(portal_cls)
            self.assertNotEqual(exit_code, 10)
            self.assertIn(exit_code, (0, 20, 64))

    def test_process_control_is_never_converted_into_a_result(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(KeyboardInterrupt):
                self.run_preflight(Path(name), preflight_portal(login_error=KeyboardInterrupt()))

    # ---- privacy ---- #

    def test_no_private_or_free_form_value_reaches_any_output(self) -> None:
        hostile = "private ACCT-778899 https://portal.example.invalid 2026-05-01_account_a.pdf"
        closed = (
            set(cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_RESULTS)
            | set(portal_module.DOWNLOAD_PREFLIGHT_REASONS)
            | set(portal_module.DOWNLOAD_PREFLIGHT_CHECKPOINTS)
            | set(portal_module.DOWNLOAD_PREFLIGHT_ELAPSED_BUCKETS)
            | set(portal_module.DOWNLOAD_PREFLIGHT_TRIAL_OUTCOMES)
            | set(cli.DOWNLOAD_PREFLIGHT_ALLOWED_SUPPORT_REFS)
            | {cli.DOWNLOAD_PREFLIGHT_DIAGNOSTIC_SCHEMA, cli.DIAGNOSTIC_COMPLETE, ACTION_REQUIRED, ">1"}
        )

        def strings(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, str):
                yield value

        for portal_cls in (
            preflight_portal(),
            preflight_portal(result=portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=preflight_evidence())),
            preflight_portal(result=portal_module.DownloadPreflightDiagnosticResult(rows_passed=1, failure=row_download_count_evidence(offending_download_count_bucket=">1"))),
            preflight_portal(login_error=AppError(hostile)),
            preflight_portal(inventory_error=LayoutChangedError(hostile)),
            preflight_portal(preflight_error=RuntimeError(hostile)),
        ):
            with tempfile.TemporaryDirectory() as name:
                exit_code, out, err = self.run_preflight(Path(name), portal_cls)
            self.assertEqual(err, "")
            for fragment in ("ACCT-778899", "portal.example.invalid", "account_a", "SYNTHETIC-INTENDED-ACCOUNT",
                             DIAGNOSTIC_SENTINEL_USERNAME, DIAGNOSTIC_SENTINEL_PASSWORD, "Traceback", name):
                self.assertNotIn(fragment, out)
            self.assertTrue(set(strings(self.document(out))) <= closed)

    def test_the_existing_support_reference_vocabulary_is_unchanged(self) -> None:
        self.assertTrue(cli.DOWNLOAD_PREFLIGHT_ALLOWED_SUPPORT_REFS.isdisjoint(cli.RETIRED_SUPPORT_REFS))
        self.assertTrue(
            cli.DOWNLOAD_PREFLIGHT_ALLOWED_SUPPORT_REFS
            <= set(cli.SUPPORT_REFS_BY_MESSAGE.values()) | {cli.UNCLASSIFIED_SUPPORT_REF}
        )
        for message in (
            portal_module.RESULTS_SURFACE_CHANGED_MESSAGE,
            portal_module.RESULTS_LATCHED_MESSAGE,
            portal_module.RESULTS_ROW_HANDLE_MESSAGE,
        ):
            with self.subTest(message=message):
                self.assertNotIn(message, cli.SUPPORT_REFS_BY_MESSAGE, "download-time messages stay status-only")


if __name__ == "__main__":
    unittest.main()
