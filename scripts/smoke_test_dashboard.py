import os
import subprocess
import sys
from pathlib import Path


REQUIRED_TEXT = [
    "Source scope: `todo.md` and `XB new system 2026.xlsx` only.",
    "Track SKU speed sold",
    "What The Excel Workbook Is Asking For",
    "Open Questions To Clarify",
]

FORBIDDEN_TEXT = [
    "Today's Top 10 Tasks",
    "Summary Metrics",
    "Active Task Streams",
    "Completed & Parked Tasks",
    "ranked_tasks.csv",
    "work_tracker.csv",
]


def fail(message):
    print(f"FAIL: {message}")
    sys.exit(1)


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def run_test():
    if not Path("dashboard/source_intake.md").exists():
        fail("dashboard/source_intake.md not found.")

    print("Running build_dashboard.py...")
    result = subprocess.run([sys.executable, "scripts/build_dashboard.py"], capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"build_dashboard.py failed.\n{result.stderr}")

    for path in ("dashboard/README.md", "README.md"):
        if not Path(path).exists():
            fail(f"{path} was not generated.")

    dashboard_text = read_text("dashboard/README.md")
    root_text = read_text("README.md")
    combined_text = dashboard_text + "\n" + root_text

    for text in REQUIRED_TEXT:
        if text not in combined_text:
            fail(f"Expected text missing: {text}")

    for text in FORBIDDEN_TEXT:
        if text in combined_text:
            fail(f"Legacy dashboard text still present: {text}")

    print("SUCCESS: Two-document dashboard smoke test passed.")
    sys.exit(0)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[1])
    run_test()
