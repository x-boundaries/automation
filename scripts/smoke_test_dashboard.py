import os
import sys
import subprocess
import csv
from pathlib import Path

def run_test():
    tracker_path = 'tracker/work_tracker.csv'

    # 1. Check tracker exists
    if not os.path.exists(tracker_path):
        print("FAIL: tracker/work_tracker.csv not found.")
        sys.exit(1)

    # 2. Check required ranking columns exist
    with open(tracker_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        headers = next(reader)
        required = ['RankScore', 'ReadyStatus', 'BlockedBy']
        missing = [req for req in required if req not in headers]
        if missing:
            print(f"FAIL: Missing ranking columns: {missing}")
            sys.exit(1)

    # 3. Run rank_tasks.py
    print("Running rank_tasks.py...")
    res1 = subprocess.run([sys.executable, 'scripts/rank_tasks.py'], capture_output=True, text=True)
    if res1.returncode != 0:
        print(f"FAIL: rank_tasks.py failed.\n{res1.stderr}")
        sys.exit(1)

    # Check outputs of rank_tasks.py
    if not os.path.exists('dashboard/today.md'):
        print("FAIL: dashboard/today.md was not generated.")
        sys.exit(1)
    if not os.path.exists('dashboard/ranked_tasks.csv'):
        print("FAIL: dashboard/ranked_tasks.csv was not generated.")
        sys.exit(1)

    # 4. Run build_dashboard.py
    print("Running build_dashboard.py...")
    res2 = subprocess.run([sys.executable, 'scripts/build_dashboard.py'], capture_output=True, text=True)
    if res2.returncode != 0:
        print(f"FAIL: build_dashboard.py failed.\n{res2.stderr}")
        sys.exit(1)

    # Optional: check if dashboard updated successfully
    if not os.path.exists('dashboard/README.md'):
        print("FAIL: dashboard/README.md was not generated.")
        sys.exit(1)

    print("SUCCESS: Smoke test passed.")
    sys.exit(0)

if __name__ == '__main__':
    run_test()
