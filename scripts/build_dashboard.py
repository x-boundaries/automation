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
    ranked_file = Path('dashboard/ranked_tasks.csv')
    daily_log_file = Path('tracker/daily_log.csv')

    tasks = read_csv(tracker_file)
    coverage = read_csv(coverage_file)
    ranked_tasks = read_csv(ranked_file)
    logs = read_csv(daily_log_file)

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
    blocked_count = status_counts.get('Blocked', 0)
    parked = status_counts.get('Parked', 0)

    completion_percentage = (done / total_tasks * 100) if total_tasks > 0 else 0

    # Subsets of tasks
    actionable_tasks = [t for t in ranked_tasks if t.get('ReadyStatus') == 'Ready']
    top_5 = actionable_tasks[:5]
    quick_wins = [t for t in actionable_tasks if str(t.get('Effort', '')).strip() == '1']
    blocked_waiting = [t for t in tasks if t.get('ReadyStatus') in ['Blocked', 'Waiting']]
    stale_status = [t for t in tasks if t.get('StatusSuggestion') and t.get('StatusSuggestion') != 'No Change']

    # Sort logs by date desc to get recent
    logs.sort(key=lambda x: x.get('Date', ''), reverse=True)
    recent_logs = logs[:10]

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
        f"- **Blocked:** {blocked_count}",
        f"- **Parked:** {parked}",
        f"- **Completion:** {completion_percentage:.1f}%\n",
        "## Today's Top 5 Tasks\n",
        generate_markdown_table(
            top_5,
            ["TaskID", "Category", "Task", "Priority", "Effort", "NextAction"],
            ["TaskID", "Category", "Task", "Priority", "Effort", "NextAction"]
        ),
        "## Quick Wins (Effort 1)\n",
        generate_markdown_table(
            quick_wins,
            ["TaskID", "Task", "Priority"],
            ["TaskID", "Task", "Priority"]
        ),
        "## Blocked / Waiting Tasks\n",
        generate_markdown_table(
            blocked_waiting,
            ["TaskID", "Task", "ReadyStatus", "BlockedBy", "Notes"],
            ["TaskID", "Task", "ReadyStatus", "BlockedBy", "Notes"]
        ),
        "## Tasks with Stale Status / Suggested Status\n",
        generate_markdown_table(
            stale_status,
            ["TaskID", "Task", "Current Status", "Suggested Status"],
            ["TaskID", "Task", "Status", "StatusSuggestion"]
        ),
        "## Recent Daily Log Entries\n",
        generate_markdown_table(
            recent_logs,
            ["Date", "TaskID", "What I Did", "Confidence", "Suggested Status"],
            ["Date", "TaskID", "WhatIDid", "Confidence", "SuggestedStatus"]
        ),
        "## Ranked Open Tasks (Full List)\n",
        generate_markdown_table(
            ranked_tasks,
            ["TaskID", "Category", "Task", "RankScore", "Priority", "Effort", "Status"],
            ["TaskID", "Category", "Task", "RankScore", "Priority", "Effort", "Status"]
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
            ["TaskID", "No", "Category", "Task", "Brief Description / Goal", "Status", "Priority", "DependsOn", "Effort", "DueDate"],
            ["TaskID", "No", "Category", "Task", "Brief Description / Goal", "Status", "Priority", "DependsOn", "Effort", "DueDate"]
        )
    ]

    # Ensure dashboard directory exists
    Path('dashboard').mkdir(parents=True, exist_ok=True)

    # Write to README.md
    with open('dashboard/README.md', 'w', encoding='utf-8') as f:
        f.write("\n".join(md))

if __name__ == '__main__':
    main()
