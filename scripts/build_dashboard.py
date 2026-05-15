import csv
import datetime
import re
from pathlib import Path
from collections import defaultdict

def read_csv(filepath):
    if not Path(filepath).exists():
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)

def generate_markdown_table(data, headers, keys):
    if not data:
        return "*No data available.*\n"

    header_row = "| " + " | ".join(headers) + " |"
    separator_row = "| " + " | ".join(["---"] * len(headers)) + " |"

    rows = []
    for item in data:
        row = []
        for key in keys:
            val = item.get(key, "")
            val = str(val).replace('\n', ' ').replace('\r', '')
            row.append(val)
        rows.append("| " + " | ".join(row) + " |")

    return "\n".join([header_row, separator_row] + rows) + "\n"

def generate_task_detail_guide(tasks):
    if not tasks:
        return "*No tasks available.*\n"

    lines = []
    for t in tasks:
        task_id = t.get('TaskID', '')
        task_name = t.get('Task', '')
        priority = t.get('Priority', '')
        status = t.get('Status', '')
        ready_status = t.get('ReadyStatus', '')

        summary_title = f"<strong>{task_id} - {task_name}</strong>"
        if priority: summary_title += f" | {priority}"
        if status: summary_title += f" | {status}"
        if ready_status: summary_title += f" | {ready_status}"

        lines.append("<details>")
        lines.append(f"<summary>{summary_title}</summary>")
        lines.append("")

        category = t.get('Category', '')
        if category:
            lines.append(f"- **Category:** {category}")

        effort = t.get('Effort', '')
        if effort:
            lines.append(f"- **Effort:** {effort}")

        brief_desc = t.get('Brief Description / Goal', '')
        if brief_desc:
            lines.append(f"- **What this means:** {brief_desc}")

        next_action = t.get('NextAction', '').strip()
        if next_action:
            lines.append(f"- **What to do next:** {next_action}")
        else:
            lines.append("- **What to do next:** Not defined yet. Add a clear next action in tracker/work_tracker.csv.")

        evidence = t.get('Evidence / Output', '').strip()
        if evidence:
            lines.append(f"- **Expected output / proof:** {evidence}")
        else:
            lines.append("- **Expected output / proof:** Not defined yet.")

        depends_on = t.get('DependsOn', '').strip()
        if depends_on:
            lines.append(f"- **Depends on:** {depends_on}")

        blocked_by = t.get('BlockedBy', '').strip()
        if blocked_by:
            lines.append(f"- **Blocked by:** {blocked_by}")

        notes = t.get('Notes', '').strip()
        if notes:
            lines.append(f"- **Notes:** {notes}")

        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)

def main():
    tracker_file = Path('tracker/work_tracker.csv')
    coverage_file = Path('tracker/source_coverage.csv')
    daily_log_file = Path('tracker/daily_log.csv')
    today_md_file = Path('dashboard/today.md')
    readme_file = Path('README.md')
    dashboard_readme_file = Path('dashboard/README.md')

    tasks = read_csv(tracker_file)
    coverage = read_csv(coverage_file)
    logs = read_csv(daily_log_file)

    # Calculate metrics
    total_tasks = len(tasks)
    status_counts = defaultdict(int)

    for t in tasks:
        status_counts[t.get('Status', 'Unknown')] += 1

    done = status_counts.get('Done', 0)
    in_progress = status_counts.get('In Progress', 0)
    not_started = status_counts.get('Not Started', 0)
    blocked_count = status_counts.get('Blocked', 0)
    parked = status_counts.get('Parked', 0)

    completion_percentage = (done / total_tasks * 100) if total_tasks > 0 else 0

    # Summary metrics table
    summary_metrics_table = f"| Total | Done | In Progress | Not Started | Blocked | Parked | Completion |\n|---:|---:|---:|---:|---:|---:|---:|\n| {total_tasks} | {done} | {in_progress} | {not_started} | {blocked_count} | {parked} | {completion_percentage:.1f}% |\n"

    # Partial source coverage items
    partial_coverage = [c for c in coverage if c.get('Coverage') in ['Partial', 'None']]

    # Sort logs by date desc to get recent
    logs.sort(key=lambda x: x.get('Date', ''), reverse=True)
    recent_logs = logs[:10]

    # Stale status subset - exclude items where suggestion is the same as current status
    stale_status = [t for t in tasks if t.get('StatusSuggestion') and t.get('StatusSuggestion') != 'No Change' and t.get('StatusSuggestion') != t.get('Status')]

    # Read today.md content
    today_content = "*`dashboard/today.md` is missing. Please run `python scripts/rank_tasks.py` to generate the daily action plan.*"
    if today_md_file.exists():
        with open(today_md_file, 'r', encoding='utf-8') as f:
            today_content = f.read()

    # Generate dashboard content
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    md = [
        "<!-- GENERATED CONTENT START: Do not manually edit this block. Generated by scripts/build_dashboard.py -->",
        f"*Last generated: {now_utc}*\n",
        "## Summary Metrics\n",
        summary_metrics_table,
        today_content
    ]

    md.extend([
        "\n## Partial/Uncovered Source Items\n",
        generate_markdown_table(
            partial_coverage,
            ["Source", "Request / Wishlist Item", "Covered in tracker task(s)", "Coverage"],
            ["Source", "Request / Wishlist Item", "Covered in tracker task(s)", "Coverage"]
        ),
        "## Task Detail Guide\n",
        generate_task_detail_guide(tasks),
        "## Full Tracker\n",
        generate_markdown_table(
            tasks,
            ["TaskID", "Category", "Task", "Status", "Priority", "ReadyStatus", "DependsOn", "Effort", "NextAction", "BlockedBy"],
            ["TaskID", "Category", "Task", "Status", "Priority", "ReadyStatus", "DependsOn", "Effort", "NextAction", "BlockedBy"]
        ),
        "<!-- GENERATED CONTENT END -->"
    ])

    dashboard_content = "\n".join(md)

    # Overwrite dashboard block in root README.md
    if readme_file.exists():
        with open(readme_file, 'r', encoding='utf-8') as f:
            readme_text = f.read()

        start_marker = "<!-- DASHBOARD:START -->"
        end_marker = "<!-- DASHBOARD:END -->"

        pattern = re.compile(rf"{re.escape(start_marker)}.*?{re.escape(end_marker)}", re.DOTALL)
        if pattern.search(readme_text):
            new_readme_text = pattern.sub(f"{start_marker}\n{dashboard_content}\n{end_marker}", readme_text)
            with open(readme_file, 'w', encoding='utf-8') as f:
                f.write(new_readme_text)
        else:
            print(f"Warning: DASHBOARD markers not found in {readme_file}. Root README not updated.")

    # Also keep generating dashboard/README.md for backward compatibility
    Path('dashboard').mkdir(parents=True, exist_ok=True)
    with open(dashboard_readme_file, 'w', encoding='utf-8') as f:
        f.write("# X-Boundaries Automation Dashboard\n\n" + dashboard_content)

if __name__ == '__main__':
    main()
