from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

from energygrid_bill_downloader.cli import build_parser, main


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

    def test_invalid_cli_returns_contract_exit_code(self) -> None:
        self.assertEqual(main([]), 64)


if __name__ == "__main__":
    unittest.main()
