import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_dashboard


class DashboardBuildTests(unittest.TestCase):
    def test_build_dashboard_markdown_uses_two_document_intake_without_legacy_tracker_fluff(self):
        source = "# Two-Document Intake\n\n## Confirmed From todo.md\n- Track SKU speed sold.\n"
        generated_at = datetime.fromisoformat("2026-06-06T17:30:00+08:00")

        dashboard = build_dashboard.build_dashboard_markdown(source, generated_at=generated_at)

        self.assertIn("# X-Boundaries Automation Dashboard", dashboard)
        self.assertIn("Last reviewed: 2026-06-06 17:30:00 SGT", dashboard)
        self.assertIn("Track SKU speed sold.", dashboard)
        self.assertIn("Source scope: `todo.md` and `XB new system 2026.xlsx` only.", dashboard)
        self.assertNotIn("Today's Top 10 Tasks", dashboard)
        self.assertNotIn("Summary Metrics", dashboard)
        self.assertNotIn("Active Task Streams", dashboard)
        self.assertNotIn("Completed & Parked Tasks", dashboard)

    def test_write_dashboard_files_replaces_root_dashboard_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "dashboard" / "source_intake.md"
            dashboard_readme = root / "dashboard" / "README.md"
            root_readme = root / "README.md"
            source_path.parent.mkdir()
            source_path.write_text(
                "# Two-Document Intake\n\n## Open Questions\n- What POS hardware is in use?\n",
                encoding="utf-8",
            )
            root_readme.write_text(
                "# X-Boundaries Automation\n\n"
                "<!-- DASHBOARD:START -->\nold tracker dashboard\n<!-- DASHBOARD:END -->\n",
                encoding="utf-8",
            )

            generated_at = datetime.fromisoformat("2026-06-06T17:30:00+08:00")

            build_dashboard.write_dashboard_files(
                source_path=source_path,
                root_readme_path=root_readme,
                dashboard_readme_path=dashboard_readme,
                generated_at=generated_at,
            )

            dashboard_text = dashboard_readme.read_text(encoding="utf-8")
            root_text = root_readme.read_text(encoding="utf-8")
            self.assertIn("What POS hardware is in use?", dashboard_text)
            self.assertIn("What POS hardware is in use?", root_text)
            self.assertNotIn("old tracker dashboard", root_text)
            self.assertIn("<!-- DASHBOARD:START -->", root_text)
            self.assertIn("<!-- DASHBOARD:END -->", root_text)


if __name__ == "__main__":
    unittest.main()
