# Drive mirror of `main` (weekly, read-only)

Issue: #273 (parent #117).

## What it does

Once a week (Sunday 03:00 Singapore time) the server PC copies the files of this repository's
`main` branch on GitHub into one company Google Drive folder, so people who do not use GitHub can
read the current automation work.

- **One direction only: GitHub to Drive.** GitHub stays the only place where changes are made.
  Anything edited, added or deleted in the Drive folder is put back to match `main` the next
  time `main` changes.
- **Only `main`, read-only.** It downloads from the public GitHub repository. It needs no
  password or token, never uploads, and never touches any other copy of the code on the PC.
  The source must be this repository's https address; any other address or a local folder is
  refused, and the PC's own git settings are ignored, so they cannot point it somewhere else.
- **Only the tracked files.** No `.git` folder, no local settings, no logs and no runtime data.
  Files marked `export-ignore` in `.gitattributes` are left out. Links are never copied.
- **Only its own folder.** It refuses to run unless the Drive folder holds the marker file
  `.xb-mirror-target`, and it never deletes anything outside that folder (or the marker itself).
  It checks the marker again just before copying and before every delete. The very first run
  only starts in an empty folder (just the marker). It refuses Drive's own top folders
  (`My Drive`, `Shared drives` and each shared drive, `Other computers`), drive roots, user
  profile folders and network (`\\server\...`) paths, also when reached through a link.
- **Nothing changes when `main` has not moved.** If the Drive copy already shows the newest
  commit, the run ends without touching the Drive folder.
- **No AI.** It is a plain Python script run by Windows Task Scheduler. It uses no Claude usage.

What it does **not** do: it does not copy other branches, pull requests, the private evidence
store, or anything from the PC's own working folders; it does not sync Drive changes back to
GitHub; it does not send emails or alerts (failures show in the status file and in Task Scheduler).

## Files

| File | What it is |
| --- | --- |
| `scripts/mirror_main_to_drive.py` | The mirror script. |
| `scripts/install_mirror_main_to_drive_task.ps1` | Registers the weekly task. Needs a separate owner OK. |
| `config/mirror_main_to_drive.example.json` | Settings template with placeholder folders. |
| `config/mirror_main_to_drive.local.json` | Your real settings. Gitignored: never committed. |

## Settings

Copy `config/mirror_main_to_drive.example.json` to `config/mirror_main_to_drive.local.json` and
fill in real folders. All folders must be full paths (for example starting with `C:\` or `G:\`).

| Setting | Meaning |
| --- | --- |
| `repo_url` | The public GitHub address. Keep the default. Any other address, a local folder, or an address with a user name or password is refused. |
| `branch` | Must be `main`. |
| `cache_dir` | A local folder **outside Drive** for the downloaded copy, for example `C:\XB\drive_mirror\cache`. A scratch folder `cache.mirror-staging` is made next to it. |
| `target_dir` | The Drive for desktop folder that receives the copy. |
| `log_dir` | A local folder **outside Drive** for `last_run_status.json` and `mirror_main_to_drive.log`. |

The cache, staging and log folders must not be inside the Drive folder, and the Drive folder must
not be inside them. Network paths (`\\server\share\...`) and `\\?\` paths are refused for all
three. Keys starting with `_` are treated as comments.

## Setup (once)

1. Make sure Google Drive for desktop is installed and signed in to the company account on the
   server PC, and that `git` and Python 3 are installed.
2. In Drive, create one new empty folder for the copy, for example
   `G:\My Drive\automation (read-only copy of main)`. Never use `My Drive` itself, `Shared drives`
   or a shared drive's top folder: those are refused. Use a new folder: everything inside it
   that is not in `main` is deleted, so the first run refuses a folder that holds anything but
   the marker (`reason` `first_run_target_not_empty`).
3. Inside that folder, create an empty file named exactly `.xb-mirror-target`. This is the
   marker that tells the script the folder is its own. In PowerShell:
   `New-Item -ItemType File -Path "G:\My Drive\<your folder>\.xb-mirror-target"`
4. Share the Drive folder with staff as **Viewer** only, never Editor. Anything staff save,
   edit or add inside it is deleted on the next update, and their own files must never live
   there. Do not put Drive shortcuts inside it either: how shortcuts appear on Windows has not
   been checked yet, and if they show as plain folders their contents would be deleted.
5. Fill in `config/mirror_main_to_drive.local.json` (see Settings).
6. Do one manual run (owner OK needed, because it writes to Drive):
   `python scripts\mirror_main_to_drive.py --config config\mirror_main_to_drive.local.json`
7. Open `_MIRROR_INFO.txt` in the Drive folder. Check that `commit:` matches the newest commit on
   GitHub `main` and that the files are there.
8. With a separate owner OK, register the weekly task (fill in the real python path and user):
   `powershell -NoProfile -File scripts\install_mirror_main_to_drive_task.ps1 -PythonExe "C:\path\to\python.exe" -UserId "PC-NAME\user-name"`
   The task runs the script from the clone the installer is run from, so run it from a clone
   that stays on `main` (not a working clone used for other branches).
   The task runs only while that user is logged on, because Drive for desktop drive letters
   exist only in the signed-in session. If the PC was off or logged out on Sunday, it runs at
   the next logon.

A manual run (step 6) can be done any time, for example after a big merge. It is safe to repeat.
To force a full re-copy even though `main` has not moved, delete `_MIRROR_INFO.txt` in the Drive
folder, then do a manual run.

## Reading the status

- **In Drive:** `_MIRROR_INFO.txt` says which commit the copy is, its commit date, when it was
  copied (+08:00), and the source address. If it is missing, a copy was interrupted and the
  next run redoes it.
- **On the PC:** `log_dir\last_run_status.json` is rewritten on every run, and one line per run
  is added to `log_dir\mirror_main_to_drive.log`. The Drive folder also gets
  `last_run_status.json` when the copy was updated or a run failed after the folder check
  passed; a run with no change leaves the Drive folder untouched. So the Drive status time only
  changes when `main` moves: to see that the weekly run is alive, look at the PC log or Task
  Scheduler.
- **`running`:** a run writes `status` `running` to the PC status file as soon as it starts, and
  to the Drive status file just before it starts copying. If `running` is still there long after
  the run should have ended (30-minute limit), that run was stopped part way (time limit,
  restart, power cut). The next run reports it and redoes the copy.
- **Quick check (changes nothing):**
  `python scripts\mirror_main_to_drive.py --config config\mirror_main_to_drive.local.json --status`
  prints a `verdict` (`OK`, `RUNNING`, `UNFINISHED`, `FAILED` or `NEVER_RUN`), the last run, the
  lock and the commit in the Drive info file. It exits 0 for `OK` and `RUNNING`, 1 otherwise.
- **In Task Scheduler:** "Last Run Result" is the exit code below.

| `status` | Exit code | `failure_class` | What to do |
| --- | --- | --- | --- |
| `ok` | 0 | - | The copy was updated. |
| `no_change` | 0 | - | `main` has not moved. Nothing to do. |
| `failed` | 2 | `CONFIG_INVALID` | Fix the settings file (`reason` says which setting). |
| `failed` | 3 | `TARGET_GUARD` | Check the Drive folder: Drive signed in, marker present, right folder (`reason` says why). `first_run_target_not_empty`: empty the folder (or pick a new one) before the first run. `marker_missing` or `target_changed` during a run: the run stopped before deleting anything more. |
| `failed` | 4 | `FETCH_FAILED` | Usually no internet or GitHub down. The next run retries. |
| `failed` | 5 | `EXPORT_FAILED` | Ask a developer; the Drive copy was not changed. |
| `failed` | 6 | `SYNC_FAILED` | The copy stopped part way (for example Drive was full or offline). The next run redoes it. |
| `failed` | 7 | `LOCKED` | This run changed nothing. `reason` `lock_held`: another run started less than 35 minutes ago and may still be going; wait. `reason` `previous_run_unfinished`: an earlier run was stopped part way (for example by a restart) and left its lock; the Drive copy may be incomplete. The first run more than 2 hours after that takes over the lock and redoes the copy. To redo it sooner, check no run is going (`--status`), delete `cache_dir\mirror.lock`, then do a manual run. |
| `failed` | 1 | `UNEXPECTED` | Ask a developer. |

Status and log lines hold only fixed messages and short reason codes: no passwords, tokens or
local folder paths.

## How to stop it

- **Pause:** open Task Scheduler, find `XB Drive Mirror of main (weekly)`, right-click, Disable.
- **Remove:** right-click the task, Delete (owner OK needed). The Drive folder stays as it is.
- **Stop it touching a folder at once:** delete the `.xb-mirror-target` marker from the Drive
  folder. Every later run then refuses with `TARGET_GUARD` and changes nothing, and a run that
  is going at that moment stops before its next delete.
