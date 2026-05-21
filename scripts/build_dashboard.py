import csv
import datetime
import re
from pathlib import Path
from collections import defaultdict
from zoneinfo import ZoneInfo

SGT = ZoneInfo('Asia/Singapore')

def read_csv(filepath):
    if not Path(filepath).exists():
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            row.pop(None, None)
            rows.append(row)
        return rows

def apply_status_updates(tasks):
    """Apply lightweight override CSVs without duplicating tracker rows.

    Files: tracker/status_updates*.csv
    Rule: match by TaskID. Non-empty cells replace base task values.
    Special value __CLEAR__ clears the target field.

    csv.DictReader puts overflow columns under key None. Ignore those so one
    accidental trailing comma cannot kill the dashboard workflow.
    """
    task_by_id = {t.get('TaskID'): t for t in tasks if t.get('TaskID')}
    for update_file in sorted(Path('tracker').glob('status_updates*.csv')):
        for update in read_csv(update_file):
            task_id = update.get('TaskID', '').strip()
            if not task_id or task_id not in task_by_id:
                continue
            target = task_by_id[task_id]
            for key, value in update.items():
                if key is None or key == 'TaskID' or value is None:
                    continue
                if not isinstance(value, str):
                    continue
                value = value.strip()
                if value == '':
                    continue
                if value == '__CLEAR__':
                    target[key] = ''
                else:
                    target[key] = value
    return tasks

def read_tracker_tasks():
    tasks = read_csv('tracker/work_tracker.csv')
    extra_dir = Path('tracker/additions')
    if extra_dir.exists():
        for extra_file in sorted(extra_dir.glob('*.csv')):
            tasks.extend(read_csv(extra_file))
    return apply_status_updates(tasks)

def build_task_details_cell(task):
    parts = []

    goal = task.get('Brief Description / Goal', '').strip()
    if goal:
        parts.append(f"**Goal:** {goal}")

    next_action = task.get('NextAction', '').strip()
    if next_action:
        parts.append(f"**Next:** {next_action}")
    else:
        parts.append("**Next:** Not defined yet.")

    evidence = task.get('Evidence / Output', '').strip()
    if evidence:
        parts.append(f"**Proof:** {evidence}")
    else:
        parts.append("**Proof:** Not defined yet.")

    notes = task.get('Notes', '').strip()
    if notes:
        parts.append(f"**Notes:** {notes}")

    cell = "<br>".join(parts)
    cell = cell.replace('\n', ' ').replace('\r', '')
    return cell

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
            val = val.replace('|', '\\|')
            row.append(val)
        rows.append("| " + " | ".join(row) + " |")

    return "\n".join([header_row, separator_row] + rows) + "\n"

def normalise_task_name(name):
    return re.sub(r'\s+', ' ', str(name).strip().lower())

def find_duplicate_tasks(tasks):
    by_name = defaultdict(list)
    by_goal = defaultdict(list)

    for task in tasks:
        name_key = normalise_task_name(task.get('Task', ''))
        goal_key = normalise_task_name(task.get('Brief Description / Goal', ''))
        if name_key:
            by_name[name_key].append(task)
        if goal_key:
            by_goal[goal_key].append(task)

    rows = []
    seen = set()
    for label, grouped in [('Task name', by_name), ('Goal', by_goal)]:
        for _, items in grouped.items():
            if len(items) <= 1:
                continue
            task_ids = ', '.join(i.get('TaskID', '') for i in items)
            task_names = ' / '.join(i.get('Task', '') for i in items)
            key = (label, task_ids)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                'Duplicate type': label,
                'TaskIDs': task_ids,
                'Tasks': task_names,
                'Recommendation': 'Review and merge/retire one row if these are not intentionally separate.'
            })
    return rows

def main():
    coverage_file = Path('tracker/source_coverage.csv')
    today_md_file = Path('dashboard/today.md')
    readme_file = Path('README.md')
    dashboard_readme_file = Path('dashboard/README.md')

    tasks = read_tracker_tasks()
    coverage = read_csv(coverage_file)

    for task in tasks:
        task['Details'] = build_task_details_cell(task)

    total_tasks = len(tasks)
    done = 0
    in_progress = 0
    not_started = 0
    blocked_count = 0
    parked = 0

    for t in tasks:
        status = t.get('Status', 'Unknown')
        ready_status = t.get('ReadyStatus', '')
        if status == 'Done':
            done += 1
        elif status == 'Parked':
            parked += 1
        elif ready_status in ['Waiting', 'Blocked']:
            blocked_count += 1
        elif status == 'In Progress':
            in_progress += 1
        else:
            not_started += 1

    completion_percentage = (done / total_tasks * 100) if total_tasks > 0 else 0

    summary_metrics_table = f"| Total | Done | In Progress | Not Started | Blocked | Parked | Completion |\n|---:|---:|---:|---:|---:|---:|---:|\n| {total_tasks} | {done} | {in_progress} | {not_started} | {blocked_count} | {parked} | {completion_percentage:.1f}% |\n"

    partial_coverage = [c for c in coverage if c.get('Coverage') in ['Partial', 'None']]
    duplicates = find_duplicate_tasks(tasks)

    today_content = "*`dashboard/today.md` is missing. Please run `python scripts/rank_tasks.py` to generate the daily action plan.*"
    if today_md_file.exists():
        with open(today_md_file, 'r', encoding='utf-8') as f:
            today_content = f.read()

    now_sgt = datetime.datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S SGT')

    md = [
        "<!-- GENERATED CONTENT START: Do not manually edit this block. Generated by scripts/build_dashboard.py -->",
        f"*Last generated: {now_sgt}*\n",
        "## Summary Metrics\n",
        summary_metrics_table,
        today_content
    ]

    md.extend([
        "\n## Duplicate Check\n",
        generate_markdown_table(
            duplicates,
            ["Duplicate type", "TaskIDs", "Tasks", "Recommendation"],
            ["Duplicate type", "TaskIDs", "Tasks", "Recommendation"]
        ),
        "\n## Partial/Uncovered Source Items\n",
        generate_markdown_table(
            partial_coverage,
            ["Source", "Request / Wishlist Item", "Covered in tracker task(s)", "Coverage"],
            ["Source", "Request / Wishlist Item", "Covered in tracker task(s)", "Coverage"]
        ),
        "## Full Tracker\n",
        generate_markdown_table(
            tasks,
            ["TaskID", "Category", "Task", "Status", "Priority", "ReadyStatus", "DependsOn", "Effort", "Details", "BlockedBy"],
            ["TaskID", "Category", "Task", "Status", "Priority", "ReadyStatus", "DependsOn", "Effort", "Details", "BlockedBy"]
        ),
        "<!-- GENERATED CONTENT END -->"
    ])

    dashboard_content = "\n".join(md)

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

    Path('dashboard').mkdir(parents=True, exist_ok=True)
    with open(dashboard_readme_file, 'w', encoding='utf-8') as f:
        f.write("# X-Boundaries Automation Dashboard\n\n" + dashboard_content)

if __name__ == '__main__':
    main()
