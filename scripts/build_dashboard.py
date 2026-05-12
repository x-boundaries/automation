import csv
import datetime
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
            # replace newlines in fields with spaces to avoid breaking table formatting
            val = str(val).replace('\n', ' ').replace('\r', '')
            row.append(val)
        rows.append("| " + " | ".join(row) + " |")

    return "\n".join([header_row, separator_row] + rows) + "\n"

def main():
    tracker_file = Path('tracker/work_tracker.csv')
    coverage_file = Path('tracker/source_coverage.csv')

    tasks = read_csv(tracker_file)
    coverage = read_csv(coverage_file)

    # Calculate metrics
    total_tasks = len(tasks)
    status_counts = defaultdict(int)
    category_counts = defaultdict(int)

    for t in tasks:
        status_counts[t.get('Status', 'Unknown')] += 1
        category_counts[t.get('Category', 'Unknown')] += 1

    done = status_counts.get('Done', 0)
    in_progress = status_counts.get('In Progress', 0)
    not_started = status_counts.get('Not Started', 0)
    blocked = status_counts.get('Blocked', 0)
    parked = status_counts.get('Parked', 0)

    completion_percentage = (done / total_tasks * 100) if total_tasks > 0 else 0

    # Subsets of tasks
    high_priority_open = [t for t in tasks if t.get('Priority') == 'High' and t.get('Status') not in ('Done', 'Parked')]
    recently_completed = [t for t in tasks if t.get('Status') == 'Done'] # We don't have completed dates to sort, but just filter them
    # For a real "recently completed" we would sort by date, but since they might be blank, we just show them

    # Generate content
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    md = [
        "# X-Boundaries Automation Dashboard\n",
        f"*Last generated: {now_utc}*\n",
        "## Summary Metrics\n",
        f"- **Total tasks:** {total_tasks}",
        f"- **Done:** {done}",
        f"- **In Progress:** {in_progress}",
        f"- **Not Started:** {not_started}",
        f"- **Blocked:** {blocked}",
        f"- **Parked:** {parked}",
        f"- **Completion:** {completion_percentage:.1f}%\n",
        "## Tasks by Status\n",
        generate_markdown_table(
            [{"Status": k, "Count": v} for k, v in status_counts.items()],
            ["Status", "Count"],
            ["Status", "Count"]
        ),
        "## Tasks by Category\n",
        generate_markdown_table(
            [{"Category": k, "Count": v} for k, v in category_counts.items()],
            ["Category", "Count"],
            ["Category", "Count"]
        ),
        "## High-Priority Open Tasks\n",
        generate_markdown_table(
            high_priority_open,
            ["No", "Category", "Task", "Status"],
            ["No", "Category", "Task", "Status"]
        ),
        "## Recently Completed Tasks\n",
        generate_markdown_table(
            recently_completed,
            ["No", "Category", "Task", "Evidence / Output"],
            ["No", "Category", "Task", "Evidence / Output"]
        ),
        "## Source Coverage Summary\n",
        generate_markdown_table(
            coverage,
            ["Source", "Request / Wishlist Item", "Covered in tracker task(s)", "Coverage"],
            ["Source", "Request / Wishlist Item", "Covered in tracker task(s)", "Coverage"]
        ),
        "## Full Tracker\n",
        generate_markdown_table(
            tasks,
            ["No", "Category", "Task", "Brief Description / Goal", "Status", "Priority", "Source", "Started", "Completed", "Evidence / Output", "Notes"],
            ["No", "Category", "Task", "Brief Description / Goal", "Status", "Priority", "Source", "Started", "Completed", "Evidence / Output", "Notes"]
        )
    ]

    # Ensure dashboard directory exists
    Path('dashboard').mkdir(parents=True, exist_ok=True)

    # Write to README.md
    with open('dashboard/README.md', 'w', encoding='utf-8') as f:
        f.write("\n".join(md))

if __name__ == '__main__':
    main()
