# Pinned clones for the AC2 server's scheduled tasks

Every scheduled task on the AC2 server (the AutoCount VM) runs its code from its own fixed copy of this repository, a **pinned clone**. A pinned clone changes only through the approved steps below. (#117, 24 Nov 2026.)

## Why

A task that runs from a shared checkout runs whatever that checkout holds at the time. On the AC2 server, `C:\XB\automation` was such a checkout, and a chat start-up hook fast-forwarded it to `origin/main` every time a chat started in that folder (22 and 24 Nov 2026). So any merge reached the live hourly stock export as soon as someone opened a chat there, with no approval step.

## The rule

- Each task runs from `C:\XB\golive\automation-<purpose>-<sha7>`:
  - `<purpose>` is a short lowercase label, for example `stock` or `arap`;
  - `<sha7>` is the first 7 characters of the commit it holds.
- A pinned clone:
  - has a **detached HEAD** at that commit, so no branch is checked out and nothing that pulls or fast-forwards a branch can move it;
  - has **no changed tracked files**, and none hidden from `git status` by `assume-unchanged` or `skip-worktree`;
  - sits directly in `C:\XB\golive`.
- One task, one clone. Updating one task never moves another.
- Settings files stay where they are, outside the clones. A new clone has no local settings.
- Untracked files are not checked (Python writes `__pycache__` there). Never copy files into a pinned clone.
- Chats on the AC2 server never start inside `C:\XB\automation` or a pinned clone. They work from `C:\XB\vm-work`.

`scripts/xb_pinned_clone.ps1` checks the rule (`Get-XbPinnedCloneProblem`). Every installer for an AC2 server task refuses to register from a folder that fails the check, and its dry run prints `Pinned clone: yes.` or the reason.

### Settings still in `C:\XB\automation\config` (until they are moved)

The hourly stock export and AR/AP aging read their settings from gitignored `*.local.json` files in `C:\XB\automation\config`. Their move onto pinned clones keeps those settings paths as they are.

Until a separate approved step moves the settings out, nobody may do any of these to `C:\XB\automation`:
- run `git clean -x` (or `-X`);
- re-clone or replace it;
- archive, move or delete it.

Any of them deletes the settings both live jobs read. A normal `git pull` or fast-forward does not touch ignored files.

The installers covered:
- `install_autocount_stock_balance_hourly_task.ps1`
- `install_autocount_arap_aging_task.ps1`
- `install_autocount_stock_extract_task.ps1`
- `install_member_sync_task.ps1`
- `install_member_sync_summary_task.ps1`
- `install_member_benefits_task.ps1`
- `install_member_tags_task.ps1`
- `install_member_removals_task.ps1`
- `install_member_pos_exceptions_task.ps1`

The server PC's own tasks (daily health check, Drive mirror) are not covered here.

## Update one job (new pinned clone, then re-point)

Each update needs the owner's OK for that task and that commit. Run in Windows PowerShell 5.1 as administrator, from `C:\XB\vm-work`. Never use the AutoCount `sa` login.

1. **Pick the commit.** Use the merged `main` commit the owner approved. Write down the full SHA and its first 7 characters.

   Also write down the commit the task runs today (OLD). If the task runs from a folder that is not a pinned clone (for example `C:\XB\automation`), make a pinned clone at OLD as well, `C:\XB\golive\automation-<purpose>-<OLD sha7>`, with step 2. It is the rollback target, so a rollback never lands on a folder that moves by itself.

2. **Make the clone.**

   ```powershell
   git clone --no-checkout https://github.com/x-boundaries/automation.git C:\XB\golive\automation-<purpose>-<sha7>
   git -C C:\XB\golive\automation-<purpose>-<sha7> checkout --detach <full sha>
   ```

3. **Check the clone.** This must print nothing:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -Command ". C:\XB\golive\automation-<purpose>-<sha7>\scripts\xb_pinned_clone.ps1; Get-XbPinnedCloneProblem -RepoRoot C:\XB\golive\automation-<purpose>-<sha7>"
   ```

4. **Check the job with the new code.** Use the task's own `python.exe` and settings file, and the job's settings check. These checks read no data:
   - the stock and AR/AP exports use `--dry-run-config`;
   - the member tools use `check-config`.

5. **Dry run the re-point.** Nothing changes. Check the output:
   - only the script path and the folder move to the new clone;
   - the program and the other arguments stay the same.

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\XB\golive\automation-<purpose>-<sha7>\scripts\xb_repoint_task_clone.ps1 -TaskName "<task name>" -NewRepoRoot C:\XB\golive\automation-<purpose>-<sha7>
   ```

   The script refuses, changing nothing, in these cases:
   - another argument is a relative path;
   - the program is a bare name such as `python.exe`;
   - the script is missing from the new clone.

   Those tasks are reinstalled instead (see "New task" below).

6. **Re-point in a quiet window.** Pick a time when the task is not running, and is not due in the next few minutes. Add `-BackupDir C:\XB\vm-work\task-backups -Apply` to the command in step 5. The script then:
   - saves the task's XML to the backup folder;
   - changes only the action;
   - reads the task back: the action must be as planned, and the triggers, account and settings unchanged.

   A Password-logon task asks for that account's Windows password. An S4U task asks for nothing.

   If the readback reports a difference, run the rollback line it prints. Task Scheduler might rewrite the triggers, account or settings XML in another form even when nothing really changed; the tool cannot tell the two apart, so it always stops there. Then report the difference.

   Right after a successful re-point, prove the rollback plans correctly: run the printed rollback line **without** `-Apply`. Its "New arguments" and "New folder" must equal the action from before the re-point.

7. **Watch one run.** At the next scheduled time, check that the run used the new folder and ended well:
   - Task Scheduler's last result is `0`;
   - the job's own status file reports success for that run. The hourly stock export uses `last_run_status.json`.

8. **Keep the old code.** Leave the previous clone, and the backup XML, in place as the rollback. Remove an old clone only with the owner's OK, after the new one has run cleanly for at least 7 days.

## Roll back

Take a quiet window first.

- **First choice: back to the previous pinned clone,** for example the clone at OLD from step 1. Repeat step 6 with `-NewRepoRoot <previous clone>`.
- **Last resort: back to the exact earlier action,** even when that is `C:\XB\automation`:

  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File <pinned clone>\scripts\xb_repoint_task_clone.ps1 -TaskName "<task name>" -RestoreFromXml <backup xml> -BackupDir C:\XB\vm-work\task-backups -Apply
  ```

  The re-point step prints this line. It refuses a backup made for another task, a backup with more than one action, and any change of program. Back on `C:\XB\automation`, the task again runs whatever that folder holds when it next moves.

## New task

Run the task's installer from inside the pinned clone, for example:

```powershell
C:\XB\golive\automation-<purpose>-<sha7>\scripts\install_<job>_task.ps1
```

`RepoRoot` defaults to that clone. Run the installer's dry run first; it must print `Pinned clone: yes.`. Each installer's runbook gives the rest of its steps and its own owner OK.
