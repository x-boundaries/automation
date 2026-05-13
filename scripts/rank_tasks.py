import csv
import datetime
from pathlib import Path

def parse_effort(effort_str):
    try:
        return float(effort_str)
    except:
        return 5.0  # Default effort if blank or unparseable

def rank_tasks():
    tracker_file = Path('tracker/work_tracker.csv')
    if not tracker_file.exists():
        return

    with open(tracker_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        tasks = list(reader)

    # Simple topological sort/dependency resolution logic
    # Also calculate readiness

    task_dict = {t['TaskID']: t for t in tasks}

    for t in tasks:
        deps = [d.strip() for d in t.get('DependsOn', '').split(',') if d.strip()]

        status = t.get('Status', 'Unknown')

        if status == 'Done':
            t['ReadyStatus'] = 'Done'
        elif status == 'Parked':
            t['ReadyStatus'] = 'Parked'
        else:
            is_blocked = False
            blocked_by = []
            for d in deps:
                if d in task_dict and task_dict[d]['Status'] not in ['Done', 'Parked']:
                    is_blocked = True
                    blocked_by.append(d)

            # Only override ReadyStatus if it's currently empty or Ready, but dependencies aren't met
            if is_blocked and t.get('ReadyStatus') not in ['Blocked', 'Waiting']:
                 t['ReadyStatus'] = 'Waiting'
                 if not t.get('BlockedBy'):
                     t['BlockedBy'] = ",".join(blocked_by)
            elif not is_blocked and t.get('ReadyStatus') not in ['Blocked', 'Waiting']:
                 t['ReadyStatus'] = 'Ready'

        # Ranking logic
        score = 0

        if status in ['Done', 'Parked']:
            score = 0
        else:
            # Base score by priority
            pri = t.get('Priority', 'Low')
            if pri == 'High': score += 3000
            elif pri == 'Medium': score += 2000
            else: score += 1000

            # Boost if in progress
            if status == 'In Progress':
                score += 500

            # Adjust by effort (lower effort = higher score, quick win bias)
            effort = parse_effort(t.get('Effort', '5'))
            score -= (effort * 10)

            # Penalize if blocked/waiting
            if t.get('ReadyStatus') in ['Blocked', 'Waiting']:
                score -= 1000

        t['RankScore'] = max(0, int(score))

    # Write back to work_tracker.csv with updated RankScore, ReadyStatus
    with open(tracker_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerows(tasks)

    # Generate today.md
    generate_today_md(tasks)

    # Save ranked list for dashboard
    save_ranked_csv(tasks)

def generate_today_md(tasks):
    # Filter actionable tasks
    actionable = [t for t in tasks if t.get('ReadyStatus') == 'Ready' and t.get('Status') not in ['Done', 'Parked']]
    actionable.sort(key=lambda x: int(x.get('RankScore', 0)), reverse=True)

    top_5 = actionable[:5]
    quick_wins = [t for t in actionable if str(t.get('Effort', '')).strip() == '1']

    blocked_waiting = [t for t in tasks if t.get('ReadyStatus') in ['Blocked', 'Waiting'] and t.get('Status') not in ['Done', 'Parked']]

    stale_status = [t for t in tasks if t.get('StatusSuggestion') and t.get('StatusSuggestion') != 'No Change' and t.get('StatusSuggestion') != t.get('Status')]

    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')

    md = [
        "# Daily Action Plan\n",
        f"**Date:** {today}\n",
        "> ⚠️ **Reminder:** The agent does not auto-mark tasks as Done. Update the tracker manually when work is confirmed and evidence is provided.\n",
        "- [X-Boundaries Automation Repo](https://github.com/x-boundaries/automation)",
        "- [Main Dashboard](https://github.com/x-boundaries/automation/blob/main/dashboard/README.md)\n",
        "## 🏆 Today's Top 5 Tasks"
    ]

    for i, t in enumerate(top_5, 1):
        md.append(f"{i}. **[{t['TaskID']}] {t['Task']}** (Priority: {t.get('Priority', '')}, Effort: {t.get('Effort', '')})")
        if t.get('NextAction'):
            md.append(f"   - **Next Action:** {t['NextAction']}")
        md.append(f"   - **Goal:** {t.get('Brief Description / Goal', '')}\n")

    md.append("## ⚡ Quick Wins (Effort 1)")
    if quick_wins:
        for t in quick_wins:
            md.append(f"- **[{t['TaskID']}] {t['Task']}** - {t.get('Brief Description / Goal', '')}")
    else:
        md.append("*No quick wins identified.*\n")

    md.append("## 🛑 Blocked & Waiting Tasks")
    if blocked_waiting:
        for t in blocked_waiting:
            md.append(f"- **[{t['TaskID']}] {t['Task']}** ({t.get('ReadyStatus', '')}) - Blocked by: {t.get('BlockedBy', '')}")
    else:
        md.append("*No blocked or waiting tasks.*\n")

    if stale_status:
        md.append("## 🔎 Tasks Needing Status Review")
        for t in stale_status:
            md.append(f"- **[{t['TaskID']}] {t['Task']}** - Suggested: *{t.get('StatusSuggestion', '')}*")

    Path('dashboard').mkdir(exist_ok=True)
    with open('dashboard/today.md', 'w', encoding='utf-8') as f:
        f.write("\n".join(md))

def save_ranked_csv(tasks):
    sorted_tasks = sorted([t for t in tasks if t.get('Status') not in ['Done', 'Parked']],
                          key=lambda x: int(x.get('RankScore', 0)), reverse=True)

    Path('dashboard').mkdir(exist_ok=True)
    with open('dashboard/ranked_tasks.csv', 'w', encoding='utf-8', newline='') as f:
        if sorted_tasks:
            writer = csv.DictWriter(f, fieldnames=sorted_tasks[0].keys())
            writer.writeheader()
            writer.writerows(sorted_tasks)

if __name__ == '__main__':
    rank_tasks()
