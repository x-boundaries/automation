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


    def test_invalid_cli_returns_contract_exit_code(self) -> None:
        self.assertEqual(main([]), 64)


if __name__ == "__main__":
    unittest.main()
