from __future__ import annotations

from pathlib import Path
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

        scope_guard = block_for(lines, "foreach ($file in $files) {", 10)
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

    def test_invalid_cli_returns_contract_exit_code(self) -> None:
        self.assertEqual(main([]), 64)


if __name__ == "__main__":
    unittest.main()
