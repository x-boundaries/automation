import csv
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SGT = ZoneInfo('Asia/Singapore')

def parse_effort(effort_str):
    try:
        return float(effort_str)
    except:
        return 5.0  # Default effort if blank or unparseable

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

def rank_tasks():
    tracker_file = Path('tracker/work_tracker.csv')
    if not tracker_file.exists():
        return

    tasks = read_tracker_tasks()
    base_fieldnames = []
    with open(tracker_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        base_fieldnames = reader.fieldnames or []

    task_dict = {t['TaskID']: t for t in tasks}

    for t in tasks:
        deps = [d.strip() for d in t.get('DependsOn', '').replace(';', ',').split(',') if d.strip()]

        status = t.get('Status', 'Unknown')

        if status == 'Done':
            t['ReadyStatus'] = 'Done'
            t['BlockedBy'] = ''
        elif status == 'Parked':
            t['ReadyStatus'] = 'Parked'
            t['BlockedBy'] = ''
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

            if is_blocked:
                t['ReadyStatus'] = 'Waiting'
                t['BlockedBy'] = ','.join(blocked_by)
            else:
                existing_blockers = [
                    b.strip()
                    for b in t.get('BlockedBy', '').replace(';', ',').split(',')
                    if b.strip()
                ]
                has_manual_waiting_blocker = (
                    t.get('ReadyStatus') == 'Waiting'
                    and existing_blockers
                    and not all(
                        b in task_dict and task_dict[b].get('Status') in ['Done', 'Parked']
                        for b in existing_blockers
                    )
                )

                if t.get('ReadyStatus') != 'Blocked' and not has_manual_waiting_blocker:
                    t['ReadyStatus'] = 'Ready'
                    t['BlockedBy'] = ''

    unlocks = {}
    for t in tasks:
        deps = [d.strip() for d in t.get('DependsOn', '').replace(';', ',').split(',') if d.strip()]
        for d in deps:
            if d not in unlocks:
                unlocks[d] = []
            unlocks[d].append(t['TaskID'])

    for t in tasks:
        status = t.get('Status', 'Unknown')
        score = 0

        if status in ['Done', 'Parked']:
            score = 0
        else:
            pri = t.get('Priority', 'Low')
            if pri == 'High': score += 3000
            elif pri == 'Medium': score += 2000
            else: score += 1000

            effort = parse_effort(t.get('Effort', '5'))
            if effort == 1.0: score += 250
            elif effort == 2.0: score += 200
            elif effort == 3.0: score += 150
            elif effort == 4.0: score += 100
            else: score += 50

            if status == 'In Progress':
                score += 150

            unlocked_tasks = unlocks.get(t['TaskID'], [])
            active_unlocked = 0
            for ut_id in unlocked_tasks:
                if ut_id in task_dict and task_dict[ut_id]['Status'] not in ['Done', 'Parked']:
                    active_unlocked += 1
            unlock_bonus = min(250, active_unlocked * 25)
            score += unlock_bonus

            due_date_str = t.get('DueDate', '').strip()
            if due_date_str:
                try:
                    due_date = datetime.datetime.strptime(due_date_str, '%Y-%m-%d').date()
                    today_date = datetime.datetime.now(SGT).date()
                    days_until_due = (due_date - today_date).days
                    if days_until_due <= 0:
                        score += 300
                    elif days_until_due <= 3:
                        score += 200
                    elif days_until_due <= 7:
                        score += 100
                except ValueError:
                    pass

            if t.get('ReadyStatus') in ['Blocked', 'Waiting']:
                score -= 5000

        t['RankScore'] = max(0, int(score))

    base_rows = []
    with open(tracker_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['TaskID'] in task_dict:
                merged = task_dict[row['TaskID']]
                for field in base_fieldnames:
                    row[field] = merged.get(field, row.get(field, ''))
            base_rows.append(row)

    with open(tracker_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=base_fieldnames, lineterminator='\n')
        writer.writeheader()
        writer.writerows(base_rows)

    generate_today_md(tasks)
    save_ranked_csv(tasks)

def generate_today_md(tasks):
    actionable = [t for t in tasks if t.get('ReadyStatus') == 'Ready' and t.get('Status') not in ['Done', 'Parked']]
    actionable.sort(key=lambda x: int(x.get('RankScore', 0)), reverse=True)

    top_10 = actionable[:10]
    quick_wins = [t for t in actionable if str(t.get('Effort', '')).strip() == '1']
    blocked_waiting = [t for t in tasks if t.get('ReadyStatus') in ['Blocked', 'Waiting'] and t.get('Status') not in ['Done', 'Parked']]
    stale_status = [t for t in tasks if t.get('StatusSuggestion') and t.get('StatusSuggestion') != 'No Change' and t.get('StatusSuggestion') != t.get('Status')]

    today = datetime.datetime.now(SGT).strftime('%Y-%m-%d')

    md = [
        "# Daily Action Plan\n",
        f"**Date:** {today} SGT\n",
        "> ⚠️ **Reminder:** The agent does not auto-mark tasks as Done. Update the tracker manually when work is confirmed and evidence is provided.\n",
        "- [X-Boundaries Automation Repo](https://github.com/x-boundaries/automation)",
        "- [Main Dashboard](https://github.com/x-boundaries/automation/blob/main/dashboard/README.md)\n",
        "## 🏆 Today's Top 10 Tasks"
    ]

    top_10_by_cat = {}
    for t in top_10:
        cat = t.get('Category', 'Other')
        if not cat.strip():
            cat = 'Other'
        if cat not in top_10_by_cat:
            top_10_by_cat[cat] = []
        top_10_by_cat[cat].append(t)

    for cat, tasks_in_cat in top_10_by_cat.items():
        md.append(f"\n### {cat}")
        for t in tasks_in_cat:
            md.append(f"- **[{t['TaskID']}] {t['Task']}** (Priority: {t.get('Priority', '')}, Effort: {t.get('Effort', '')})")
            if t.get('NextAction'):
                md.append(f"  - **Next Action:** {t['NextAction']}")
            md.append(f"  - **Goal:** {t.get('Brief Description / Goal', '')}\n")

    md.append("## ⚡ Quick Wins (Effort 1)")
    if quick_wins:
        for t in quick_wins:
            md.append(f"- **[{t['TaskID']}] {t['Task']}** (Category: {t.get('Category', '')}) - {t.get('Brief Description / Goal', '')}")
    else:
        md.append("*No quick wins identified.*\n")

    md.append("## 🛑 Blocked & Waiting Tasks")
    if blocked_waiting:
        for t in blocked_waiting:
            md.append(f"- **[{t['TaskID']}] {t['Task']}** (Category: {t.get('Category', '')}, {t.get('ReadyStatus', '')}) - Blocked by: {t.get('BlockedBy', '')}")
    else:
        md.append("*No blocked or waiting tasks.*\n")

    if stale_status:
        md.append("## 🔎 Tasks Needing Status Review")
        for t in stale_status:
            md.append(f"- **[{t['TaskID']}] {t['Task']}** (Category: {t.get('Category', '')}) - Suggested: *{t.get('StatusSuggestion', '')}*")

    daily_log_file = Path('tracker/daily_log.csv')
    if daily_log_file.exists():
        with open(daily_log_file, 'r', encoding='utf-8') as f:
            log_reader = csv.DictReader(f)
            logs = list(log_reader)

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
            writer = csv.DictWriter(f, fieldnames=sorted_tasks[0].keys(), extrasaction='ignore', lineterminator='\n')
            writer.writeheader()
            writer.writerows(sorted_tasks)

if __name__ == '__main__':
    rank_tasks()
