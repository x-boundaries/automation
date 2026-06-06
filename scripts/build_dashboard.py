import datetime
import re
from pathlib import Path
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
START_MARKER = "<!-- DASHBOARD:START -->"
END_MARKER = "<!-- DASHBOARD:END -->"
DEFAULT_SOURCE_PATH = Path("dashboard/source_intake.md")
DEFAULT_DASHBOARD_README_PATH = Path("dashboard/README.md")
DEFAULT_ROOT_README_PATH = Path("README.md")


def sgt_timestamp(value=None):
    if value is None:
        value = datetime.datetime.now(SGT)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=SGT)
    else:
        value = value.astimezone(SGT)
    return value.strftime("%Y-%m-%d %H:%M:%S SGT")


def build_dashboard_markdown(source_markdown, generated_at=None):
    source = source_markdown.strip()
    reviewed_at = sgt_timestamp(generated_at)
    return "\n".join(
        [
            "# X-Boundaries Automation Dashboard",
            "",
            f"*Last reviewed: {reviewed_at}*",
            "",
            "Source scope: `todo.md` and `XB new system 2026.xlsx` only.",
            "",
            source,
            "",
        ]
    )


def replace_dashboard_block(readme_text, dashboard_markdown):
    replacement = f"{START_MARKER}\n{dashboard_markdown.rstrip()}\n{END_MARKER}"
    pattern = re.compile(rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}", re.DOTALL)
    if pattern.search(readme_text):
        return pattern.sub(replacement, readme_text)
    return f"{readme_text.rstrip()}\n\n{replacement}\n"


def write_dashboard_files(
    source_path=DEFAULT_SOURCE_PATH,
    root_readme_path=DEFAULT_ROOT_README_PATH,
    dashboard_readme_path=DEFAULT_DASHBOARD_README_PATH,
    generated_at=None,
):
    source_text = Path(source_path).read_text(encoding="utf-8")
    dashboard_markdown = build_dashboard_markdown(source_text, generated_at=generated_at)

    dashboard_readme = Path(dashboard_readme_path)
    dashboard_readme.parent.mkdir(parents=True, exist_ok=True)
    dashboard_readme.write_text(dashboard_markdown, encoding="utf-8")

    root_readme = Path(root_readme_path)
    if root_readme.exists():
        readme_text = root_readme.read_text(encoding="utf-8")
    else:
        readme_text = "# X-Boundaries Automation\n"
    root_readme.write_text(replace_dashboard_block(readme_text, dashboard_markdown), encoding="utf-8")


def main():
    if not DEFAULT_SOURCE_PATH.exists():
        raise FileNotFoundError(
            f"{DEFAULT_SOURCE_PATH} is missing. Add the two-document intake summary before building the dashboard."
        )
    write_dashboard_files()


if __name__ == "__main__":
    main()
