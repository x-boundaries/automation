from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from energygrid_bill_downloader import cli
from energygrid_bill_downloader.cli import build_parser, main
from energygrid_bill_downloader.errors import (
    ACTION_REQUIRED,
    DOWNLOAD_FAILED,
    LOGIN_FAILED,
    NO_NEW_BILLS,
    PORTAL_LAYOUT_CHANGED,
    AppError,
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
        self.assertIn("$_ -match '^energygrid-bill-downloader/'", ownership_text)
        self.assertIn(
            "$_ -eq '.github/workflows/energygrid-bill-downloader-tests.yml'", ownership_text
        )
        # The n8n error handler export and its focused test are EnergyGrid-owned, so either
        # one alone arms the guard. `n8n-workflows/README.md` is a shared companion: it
        # triggers the workflow and is permitted, but it never confers ownership.
        self.assertIn(
            "$_ -eq 'n8n-workflows/energygrid_download_error_handler.workflow.json'",
            ownership_text,
        )
        self.assertIn("$_ -eq 'tests/test_energygrid_n8n_error_handler.py'", ownership_text)
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
            "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED",
        }
        self.assertTrue(replacements.issubset(committed))
        self.assertTrue(replacements.isdisjoint(cli.RETIRED_SUPPORT_REFS))

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


if __name__ == "__main__":
    unittest.main()
