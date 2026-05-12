import csv
import sys
import datetime
from pathlib import Path
from collections import defaultdict

def read_csv(filepath):
    if not Path(filepath).exists():
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)

def write_csv(filepath, data, fieldnames):
    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

def calculate_rank_score(task, unlock_count):
    score = 0

    # PriorityScore
    priority = task.get('Priority', '')
    if priority == 'High': score += 3000
    elif priority == 'Medium': score += 2000
    elif priority == 'Low': score += 1000

    # EaseScore
    effort = str(task.get('Effort', '')).strip()
    if effort == '1': score += 250
    elif effort == '2': score += 200
    elif effort == '3': score += 150
    elif effort == '4': score += 100
    elif effort == '5': score += 50

    # StatusBonus
    status = task.get('Status', '')
    if status == 'In Progress': score += 150

    # UnlockBonus
    unlock_bonus = unlock_count * 25
    if unlock_bonus > 250: unlock_bonus = 250
    score += unlock_bonus

    # UrgencyBonus
    due_date_str = str(task.get('DueDate', '')).strip()
    if due_date_str:
        try:
            due_date = datetime.datetime.strptime(due_date_str, '%Y-%m-%d').date()
            today = datetime.date.today()
            days_diff = (due_date - today).days
            if days_diff <= 0: score += 300
            elif days_diff <= 3: score += 200
            elif days_diff <= 7: score += 100
        except ValueError:
            pass # Invalid date format

    return score

def main():
    tracker_file = Path('tracker/work_tracker.csv')
    daily_log_file = Path('tracker/daily_log.csv')
    dashboard_dir = Path('dashboard')
    dashboard_dir.mkdir(exist_ok=True)

    tasks = read_csv(tracker_file)
    logs = read_csv(daily_log_file)

    # Get fieldnames
    with open(tracker_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        fieldnames = next(reader)

    task_map = {t['TaskID']: t for t in tasks if t.get('TaskID')}

    # 1. Status suggestions from logs
    latest_logs = {}
    for log in logs:
        tid = log.get('TaskID')
        if not tid: continue
        if tid not in latest_logs or log.get('Date', '') >= latest_logs[tid].get('Date', ''):
            latest_logs[tid] = log

    for t in tasks:
        tid = t.get('TaskID')
        t['StatusSuggestion'] = ''
        if tid in latest_logs:
            suggestion = latest_logs[tid].get('SuggestedStatus', '')
            if suggestion and suggestion != 'No Change':
                t['StatusSuggestion'] = suggestion

    # 2. Build dependency graph
    dependents = defaultdict(list)
    prereqs = defaultdict(list)

    for t in tasks:
        tid = t.get('TaskID')
        if not tid: continue
        depends_on = [x.strip() for x in t.get('DependsOn', '').split(';') if x.strip()]
        for req in depends_on:
            if req in task_map:
                prereqs[tid].append(req)
                dependents[req].append(tid)

    # Calculate unlock count
    def get_open_dependents_count(tid):
        count = 0
        for dep in dependents.get(tid, []):
            if task_map[dep].get('Status') not in ['Done', 'Parked']:
                count += 1
        return count

    # 3. Calculate ReadyStatus & BlockedBy
    for t in tasks:
        tid = t.get('TaskID')
        if not tid: continue

        status = t.get('Status', '')
        if status == 'Done':
            t['ReadyStatus'] = 'Done'
            t['BlockedBy'] = ''
            continue
        if status == 'Parked':
            t['ReadyStatus'] = 'Parked'
            t['BlockedBy'] = ''
            continue

        incomplete_prereqs = []
        for req in prereqs.get(tid, []):
            req_task = task_map.get(req)
            if req_task and req_task.get('Status') != 'Done':
                incomplete_prereqs.append(req)

        if incomplete_prereqs:
            t['ReadyStatus'] = 'Waiting' # Or Blocked if explicitly marked
            if status == 'Blocked':
                t['ReadyStatus'] = 'Blocked'
            t['BlockedBy'] = ';'.join(incomplete_prereqs)
        else:
            t['ReadyStatus'] = 'Ready'
            if status == 'Blocked': # It could be manually blocked
                t['ReadyStatus'] = 'Blocked'
            t['BlockedBy'] = ''

    # 4. Calculate initial RankScore
    for t in tasks:
        tid = t.get('TaskID')
        if not tid: continue

        # We only assign 0 for Done or Parked. Waiting/Blocked might still have a score
        # but they are filtered from action lists. However, instructions say:
        # "Prerequisites must rank before tasks that depend on them."
        # Waiting/blocked tasks shouldn't appear as actionable, but calculating their score
        # helps bubble up prerequisites.
        if t['ReadyStatus'] in ['Done', 'Parked']:
            t['RankScore'] = '0'
        else:
            unlock_count = get_open_dependents_count(tid)
            score = calculate_rank_score(t, unlock_count)
            t['RankScore'] = str(score)

    # 5. Adjust RankScore so prerequisites rank higher than dependents
    changed = True
    while changed:
        changed = False
        for t in tasks:
            tid = t.get('TaskID')
            if not tid or t['ReadyStatus'] in ['Done', 'Parked']: continue

            my_score = int(t.get('RankScore') or 0)

            max_dep_score = 0
            for dep in dependents.get(tid, []):
                dep_task = task_map.get(dep)
                if dep_task and dep_task['ReadyStatus'] not in ['Done', 'Parked']:
                    dep_score = int(dep_task.get('RankScore') or 0)
                    if dep_score > max_dep_score:
                        max_dep_score = dep_score

            if max_dep_score >= my_score and max_dep_score > 0:
                t['RankScore'] = str(max_dep_score + 10)
                changed = True

    # Rewrite tracker
    write_csv(tracker_file, tasks, fieldnames)

    # 6. Generate dashboard/ranked_tasks.csv
    # Open tasks only (not Done, not Parked)
    open_tasks = [t for t in tasks if t['ReadyStatus'] not in ['Done', 'Parked']]
    # Sort by RankScore desc
    open_tasks.sort(key=lambda x: int(x.get('RankScore') or 0), reverse=True)
    write_csv('dashboard/ranked_tasks.csv', open_tasks, fieldnames)

    # 7. Generate dashboard/today.md
    today_md_path = 'dashboard/today.md'

    # Categories for today.md
    # Hard rule: "Done tasks should not appear in today's action list."
    # Hard rule: "Parked tasks should not appear unless manually reactivated."
    # Hard rule: "Blocked tasks should appear only in the blocker section."
    # Hard rule: "Tasks with unmet prerequisites should be marked Waiting, not actionable."

    actionable_tasks = [t for t in open_tasks if t['ReadyStatus'] == 'Ready']
    top_5 = actionable_tasks[:5]
    quick_wins = [t for t in actionable_tasks if str(t.get('Effort', '')).strip() == '1']
    blocked_waiting = [t for t in tasks if t['ReadyStatus'] in ['Blocked', 'Waiting']]
    status_review = [t for t in tasks if t.get('StatusSuggestion', '') != '']

    lines = [
        "# Daily Action Plan",
        "",
        "**Date:** " + datetime.datetime.now().strftime('%Y-%m-%d'),
        "",
        "> ⚠️ **Reminder:** The agent does not auto-mark tasks as Done. Update the tracker manually when work is confirmed and evidence is provided.",
        "",
        "- [X-Boundaries Automation Repo](https://github.com/x-boundaries/automation)",
        "- [Main Dashboard](https://github.com/x-boundaries/automation/blob/main/dashboard/README.md)",
        "",
        "## 🏆 Today's Top 5 Tasks",
    ]

    if top_5:
        for i, t in enumerate(top_5, 1):
            lines.append(f"{i}. **[{t['TaskID']}] {t['Task']}** (Priority: {t.get('Priority', 'N/A')}, Effort: {t.get('Effort', 'N/A')})")
            if t.get('NextAction'):
                lines.append(f"   - **Next Action:** {t['NextAction']}")
            lines.append(f"   - **Goal:** {t.get('Brief Description / Goal', 'N/A')}")
            lines.append("")
    else:
        lines.append("*No ready tasks available.*")
        lines.append("")

    lines.append("## ⚡ Quick Wins (Effort 1)")
    if quick_wins:
        for t in quick_wins:
            lines.append(f"- **[{t['TaskID']}] {t['Task']}**")
    else:
        lines.append("*No quick wins identified.*")
    lines.append("")

    lines.append("## 🛑 Blocked & Waiting Tasks")
    if blocked_waiting:
        for t in blocked_waiting:
            lines.append(f"- **[{t['TaskID']}] {t['Task']}** ({t['ReadyStatus']}) - Blocked by: {t.get('BlockedBy', 'N/A')}")
    else:
        lines.append("*No blocked or waiting tasks.*")
    lines.append("")

    lines.append("## 🔎 Tasks Needing Status Review")
    if status_review:
        for t in status_review:
            lines.append(f"- **[{t['TaskID']}] {t['Task']}** - Suggested: *{t['StatusSuggestion']}*")
    else:
        lines.append("*No tasks need status review.*")
    lines.append("")

    with open(today_md_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))

if __name__ == '__main__':
    main()
