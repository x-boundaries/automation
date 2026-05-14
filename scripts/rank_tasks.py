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
        deps = [d.strip() for d in t.get('DependsOn', '').replace(';', ',').split(',') if d.strip()]

        status = t.get('Status', 'Unknown')

        if status == 'Done':
            t['ReadyStatus'] = 'Done'
        elif status == 'Parked':
            t['ReadyStatus'] = 'Parked'
        else:
            is_blocked = False
            blocked_by = []
            for d in deps:
                if d not in task_dict:
                    is_blocked = True
                    blocked_by.append(d)
                elif task_dict[d]['Status'] not in ['Done', 'Parked']:
                    is_blocked = True
                    blocked_by.append(d)

            # Only override ReadyStatus if it's currently empty or Ready, but dependencies aren't met
            # If it's blocked by manual 'Blocked' or 'Waiting' setting, keep it.
            if is_blocked and t.get('ReadyStatus') not in ['Blocked', 'Waiting']:
                 t['ReadyStatus'] = 'Waiting'
                 t['BlockedBy'] = ",".join(blocked_by)
            elif not is_blocked and t.get('ReadyStatus') not in ['Blocked', 'Waiting']:
                 t['ReadyStatus'] = 'Ready'
                 t['BlockedBy'] = ""

    # Pre-calculate what tasks are unlocked by each task
    unlocks = {}
    for t in tasks:
        deps = [d.strip() for d in t.get('DependsOn', '').replace(';', ',').split(',') if d.strip()]
        for d in deps:
            if d not in unlocks:
                unlocks[d] = []
            unlocks[d].append(t['TaskID'])

    for t in tasks:
        status = t.get('Status', 'Unknown')
        # Ranking logic
        score = 0

        if status in ['Done', 'Parked']:
            score = 0
        else:
            # PriorityScore
            pri = t.get('Priority', 'Low')
            if pri == 'High': score += 3000
            elif pri == 'Medium': score += 2000
            else: score += 1000

            # EaseScore
            effort = parse_effort(t.get('Effort', '5'))
            if effort == 1.0: score += 250
            elif effort == 2.0: score += 200
            elif effort == 3.0: score += 150
            elif effort == 4.0: score += 100
            else: score += 50

            # StatusBonus
            if status == 'In Progress':
                score += 150

            # UnlockBonus
            # Find tasks this unlocks that are not done/parked
            unlocked_tasks = unlocks.get(t['TaskID'], [])
            active_unlocked = 0
            for ut_id in unlocked_tasks:
                if ut_id in task_dict and task_dict[ut_id]['Status'] not in ['Done', 'Parked']:
                    active_unlocked += 1
            unlock_bonus = min(250, active_unlocked * 25)
            score += unlock_bonus

            # UrgencyBonus
            due_date_str = t.get('DueDate', '').strip()
            if due_date_str:
                try:
                    due_date = datetime.datetime.strptime(due_date_str, '%Y-%m-%d').date()
                    today_date = datetime.datetime.now(datetime.timezone.utc).date()
                    days_until_due = (due_date - today_date).days
                    if days_until_due <= 0:
                        score += 300
                    elif days_until_due <= 3:
                        score += 200
                    elif days_until_due <= 7:
                        score += 100
                except ValueError:
                    pass

            # Penalize if blocked/waiting so it doesn't show up top
            if t.get('ReadyStatus') in ['Blocked', 'Waiting']:
                score -= 5000

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

    # Recent daily log entries
    daily_log_file = Path('tracker/daily_log.csv')
    if daily_log_file.exists():
        with open(daily_log_file, 'r', encoding='utf-8') as f:
            log_reader = csv.DictReader(f)
            logs = list(log_reader)

        # Sort desc by date, get top 5
        logs.sort(key=lambda x: x.get('Date', ''), reverse=True)
        recent_logs = logs[:5]

        if recent_logs:
            md.append("\n## 📝 Recent Daily Log Entries")
            for log in recent_logs:
                md.append(f"- **{log.get('Date', '')} [{log.get('TaskID', '')}]** - {log.get('WhatIDid', '')}")

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
