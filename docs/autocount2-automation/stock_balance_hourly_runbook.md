# AC2 hourly stock balance export runbook (#245)

Status: **offline build only.** Nothing here has run against SQL Server, the AC2 VM, Google Drive or Task Scheduler. Every step under [VM proof steps](#vm-proof-steps-v1-v7) needs a separate owner OK.

## What it does

`scripts/autocount_stock_balance_hourly.py` reads the stored on-hand stock balance from AutoCount (`dbo.ItemBatchBalQty`, the table AutoCount itself maintains) once an hour, 24/7, and writes:

- an immutable hourly folder with `stock_balance.csv` and `manifest.json`;
- a "latest" pair (`stock_balance_latest.csv`, `latest_manifest.json`) that is replaced only after the new hourly CSV re-reads to the hash that was written;
- `last_run_status.json`, updated on every run, success or failed, so a failed hour is visible in Drive.

Delivery to Google Drive is a plain file copy into a local folder that Google Drive for desktop syncs (`output.delivery_root`). Nothing calls a Drive API. The manifest says "handed to Drive sync folder", never "uploaded".

Where it runs (owner decision, 23 Oct 2026): on the AC2 VM as a Windows Task Scheduler task, reading SQL Server locally. The archive is a gitignored folder inside the automation clone on the VM, and delivery writes directly into the Drive for desktop synced folder. The settings file is JSON and cannot carry comments, so this runbook documents the placeholders used in the example settings.

How it reads:

- Fixed `SELECT` statements only; `READ COMMITTED`, `SET LOCK_TIMEOUT` (default 5000 ms), query timeout (default 60 s), one connection per attempt. No `NOLOCK`, no snapshot isolation, no database setting changes.
- Consistency: `dbo.ChangeCount` (row `ItemBatchBalQty`) is read before and after the balance read on the same connection. Equal values mean no balance write landed in between. If it keeps moving, the run retries (default 3 attempts, short backoff), then fails as `unstable_snapshot`. A lock timeout is also retried within the same attempt budget.
- Balance rows are summed by ItemCode, UOM and Location (BatchNo is always empty today; rows with a non-empty BatchNo are counted in the manifest).
- No cost column is selected. `MostRecentlyCost` and every cost table stay out. Consignment (`dbo.CSGNItemBalQty`) is out.
- `dbo.ChangeLog` is never read.

## Files

| File | Purpose |
| --- | --- |
| `scripts/autocount_stock_balance_hourly.py` | The exporter. CLI: `--config PATH` (required), `--check-latest`, `--dry-run-config`. |
| `config/autocount_stock_balance_hourly.example.json` | Committed example with fake values only. |
| `config/autocount_stock_balance_hourly.local.json` | The real settings file on the VM. Gitignored by `config/*.local.json`; never commit it. Real location labels live only here. |
| `scripts/install_autocount_stock_balance_hourly_task.ps1` | Task Scheduler installer. Not run by the build; step V7 only. |
| `sql/autocount2/stock_balance_hourly_live_check.readonly.template.sql` | Owner-run read-only live check (step V3). Aggregates only. |
| `tests/test_autocount_stock_balance_hourly.py` | Offline tests with in-memory fakes and temp folders. |

## VM prerequisites

Check these on the AC2 VM before V1 (needs owner OK, like every VM step). The scheduled task and every manual run must use the same interpreter.

- [ ] **Python 3.11** (64-bit), called by its **full path** to `python.exe` (placeholder `C:\EXAMPLE\Python311\python.exe`), never `python.exe` from `PATH`. The installer's `-PythonExe` takes that full path.
- [ ] **`pyodbc`** installed into that interpreter (not `--user`).
- [ ] **`tzdata`** installed into that interpreter. Windows Python has no zone database of its own; without `tzdata` every run fails as `timezone_unavailable`.
- [ ] **ODBC Driver 17 for SQL Server**, or the driver named in `source.odbc_driver` (the code's default is `ODBC Driver 17 for SQL Server`).

```powershell
& "C:\EXAMPLE\Python311\python.exe" -m pip install pyodbc tzdata
& "C:\EXAMPLE\Python311\python.exe" -c "import pyodbc, zoneinfo; zoneinfo.ZoneInfo('Asia/Singapore'); print('ok')"
Get-OdbcDriver -Name "ODBC Driver 17 for SQL Server" -Platform 64-bit
```

## Commands

Run from the automation clone, always with the task's full-path interpreter (see [VM prerequisites](#vm-prerequisites)):

```powershell
& "C:\EXAMPLE\Python311\python.exe" scripts\autocount_stock_balance_hourly.py --config config\autocount_stock_balance_hourly.local.json --dry-run-config
& "C:\EXAMPLE\Python311\python.exe" scripts\autocount_stock_balance_hourly.py --config config\autocount_stock_balance_hourly.local.json
& "C:\EXAMPLE\Python311\python.exe" scripts\autocount_stock_balance_hourly.py --config config\autocount_stock_balance_hourly.local.json --check-latest
```

Exit codes: run `0` success (and delivery verified or not configured), `1` failed or delivery not verified. `--check-latest`: `0` fresh, `2` stale (older than `stale_after_minutes`), `3` missing or invalid; it never exits with anything else, so invalid settings (including a malformed `timezone` such as `Asia/Singapore/`), a missing zone database or a usage error also give `3`. `--dry-run-config`: `0` valid (it also prints `auth_mode`, `scheduled_logon_type` and `run_minute`, which the installer reads), `1` invalid. Console output is a short status JSON; it never prints row data, connection strings, logins, host names or paths. The run output, `--check-latest` and `last_run_status.json` also carry `large_balance_count` (see [Large-balance warning](#large-balance-warning)); it never changes an exit code.

## Settings

Every business choice is a setting. The owner said answers may still change, so every row below "may change". Unknown keys, bad values, a bad SyncRegistration column or a missing `expected_database` fail validation before anything is read.

| Key | Default | Notes |
| --- | --- | --- |
| `timezone` | `"Asia/Singapore"` | Used for folder names, `as_of_local` and the stale check. May change. |
| `schedule.frequency` | `"hourly"` | Only `hourly` is supported. Used by the installer and the stale check. May change. |
| `schedule.run_minute` | `0` | Minute past each hour (00:00, 01:00 ... 23:00 = 24 runs a day). The installer reads it from the validated settings (there is no installer parameter for it); changing it later means registering the task again (the installer never replaces an existing task, so that needs an owner OK). No separate close-of-day file. May change. |
| `schedule.logon_type` | `"S4U"` | The Task Scheduler logon type. Must match `identity.auth_mode` (see [Read identity and scheduled logon](#read-identity-and-scheduled-logon)): `S4U` with `windows_integrated`, `Password` with `sql_login`. Any other pair fails validation. The installer takes the logon type from the validated settings. |
| `stale_after_minutes` | `70` | `--check-latest` reports stale after this. May change. |
| `source.server` | required | SQL Server instance. The example uses a fake value. |
| `source.odbc_driver` | `"ODBC Driver 17 for SQL Server"` | May change. |
| `source.expected_database` | required | `DB_NAME()` must equal it before any business query. Example: `AED_EXAMPLE_BOOK`. |
| `source.allowed_databases` | `[expected_database]` | Must contain `expected_database`. May change. |
| `source.encrypt` | `false` | ODBC `Encrypt`. Local connection on the VM. May change. |
| `source.trust_server_certificate` | `false` | ODBC `TrustServerCertificate`. May change. |
| `source.sync_registration_time_column` | `"LastFinishSyncTime"` | Exactly `LastFinishSyncTime` or `LastTransFinishSyncTime`; anything else fails validation. Which is right on live is an open question. A failing sync read fails the hour (no fallback). |
| `identity.auth_mode` | `"windows_integrated"` | `windows_integrated` (the Task Scheduler account, with `schedule.logon_type` `S4U`) or `sql_login` (with `schedule.logon_type` `Password`). Decided at V4. |
| `identity.sql_login` | `null` | Login name, only with `sql_login`. `sa` is rejected. |
| `identity.credential_target` | `null` | Windows Credential Manager generic credential target holding the password, only with `sql_login`. It must be saved by the task's own Windows account. The password is read at runtime and never stored or logged. |
| `identity.allow_privileged_identity` | `false` | `false`: refuse sysadmin or db_owner. `true`: run, set `identity.privileged=true` and a warning in every manifest. May change. |
| `read.lock_timeout_ms` | `5000` | May change. |
| `read.query_timeout_seconds` | `60` | May change. |
| `read.max_attempts` | `3` | Attempts for an unstable snapshot or a lock timeout. May change. |
| `read.retry_backoff_seconds` | `2` | May change. |
| `rows.zero_row_mode` | `"keep_rows_and_add_items_without_rows"` | Keep every balance row including zeros, and add one row per active stock-controlled item (`IsActive='T'` and `StockControl='T'`) with no balance row anywhere: Location `""`, LocationLabel `"(no balance row)"`, BalQty `0`, RowSource `no_balance_row`. Other values: `keep_rows` (no added rows), `drop_zero_rows`. May change. |
| `rows.drop_zero_rows_for_inactive_items` | `true` | Drop zero rows of inactive items (non-zero inactive rows are kept). May change. |
| `rows.uom_guard` | `"fail"` | Fail the hour (no CSV) if any balance row's UOM differs from `Item.BaseUOM`, or any item has more than one UOM, until a mapping exists. May change once a mapping is designed. |
| `location_labels` | `{}` | Location code -> label. Every code is exported; codes are never dropped or renamed. Unmapped code label = `code - Location.Description` when present, else the code; unmapped codes are listed in the manifest. Real labels only in the local file. May change. |
| `output.archive_root` | required | Local archive folder on the VM. Owner decision: a gitignored folder inside the automation clone, `<automation clone>\autocount_outputs\stock_balance_hourly` (ignored by the existing `autocount_outputs/` rules in `.gitignore`). May change. |
| `output.delivery_root` | `null` | The Google Drive for desktop synced folder; the job writes into it directly. `null` = local archive only. Folder and Google account are named at V6. Must differ from `archive_root`. |
| `output.large_balance_threshold` | `100000` | Whole number from 1 to 1000000000. Every exported row whose BalQty is strictly above it, positive or negative, is listed in the manifest as a warning; the run still succeeds and publishes (see [Large-balance warning](#large-balance-warning)). There is no off switch: the check never blocks an export, so if it is noisy, raise the threshold. May change. |
| `output.columns` | as_of_local, ItemCode, ItemDescription, ItemType, Location, LocationLabel, UOM, BalQty, IsActive, StockControl, RowSource | Order and subset of the CSV columns; must include ItemCode, Location, UOM, BalQty. To be aligned to the owner's Stock Balance by Location report at reconciliation. May change. |

## Outputs and folder layout

```
<archive_root>\
  .ac2_stockbal.lock                      (while a run is active, or left by a killed run)
  last_run_status.json                    (every run)
  stock_balance_latest.csv                (latest verified success only)
  latest_manifest.json                    (latest verified success only)
  hourly\<YYYY-MM-DD>\ac2_stockbal_<YYYYMMDDTHHMMSS+0800>_<run_id first 8>\
      stock_balance.csv                   (success only)
      manifest.json                       (every outcome)
<delivery_root>\                          (when set; same layout without the lock)
```

- Hourly files use exclusive create plus fsync and SHA-256. A rerun in the same hour makes a new folder; existing files are never touched.
- The latest pair is written with a temp file plus `os.replace`, only after the hourly CSV re-reads to its hash. `latest_manifest.json` carries `as_of_local` and `run_id`, so an old snapshot is visible.
- Delivery: the CSV is copied into `delivery_root` with exclusive create and re-hashed; then the manifest is copied and re-hashed; only then does the delivery latest pair advance. A mismatch gives delivery status `verify_failed` and the delivery latest pair is not advanced. `manifest.delivery` records the CSV copy; `last_run_status.json` records the final delivery result including the manifest copy.
- Lock: `.ac2_stockbal.lock` records the run_id, process id and start time. A run killed by the 15-minute task limit or a reboot cannot remove it. The next run clears it automatically only when it is older than 30 minutes (the 15-minute limit plus a 15-minute margin) **and** either it was written before Windows last started (a restart ends every run, whatever process later reuses its process id) or its recorded process is no longer running (on Windows, a process created after the lock was written counts as a reused process id, not the owner). That run adds a warning to its manifest naming the killed run's reference, the reason and the lock age. A younger lock, or a lock written since the last start whose process is still running, gives `already_running` and is left alone.
- CSV: UTF-8 without BOM, header row, CRLF line ends via the csv module, fixed column order from `output.columns`, every cell through `safe_csv_row` (formula prefixes neutralised; numbers stay numeric, so negative quantities are not prefixed).
- Manifest fields: job `ac2_hourly_stock_balance`, schema_version, run_id, status, failure_class, sanitized message, started/finished (+08:00), as_of_local and as_of_sql, timezone, source (surface, database, query_sha256, settings_sha256, git commit), identity flags (never names), attempts, ChangeCount before/after, row_count, CSV sha256/size, counts (zero, negative, no_balance_row, non_empty_batch, inactive_dropped, zero_dropped, null_balqty_rows), per-location rows / non_zero / negative / sum_qty, unmapped_location_codes, large_balances, uom_guard, pos_sync per Location, delivery, warnings.
- `last_run_status.json` fields: job, run_id, status, failure_class, as_of_local, started_at, finished_at, hourly_folder, delivery_status, large_balance_count (`null` for a failed run).

## Large-balance warning

Added before V5 (owner decision, 27 Oct 2026) after a stock transfer had an item code keyed into the quantity field, which gave balances in the billions at two locations. The job cannot tell a keying error from a real balance, so it warns and never blocks.

- Every exported row with `|BalQty|` strictly above `output.large_balance_threshold` (default `100000`) counts, positive or negative. A balance equal to the threshold does not.
- The manifest (hourly and `latest_manifest.json`) gets:

```json
"large_balances": {
  "threshold": 100000,
  "count": 2,
  "rows": [
    {"ItemCode": "FAKE-0201", "Location": "LOC-A", "BalQty": "123456789000"},
    {"ItemCode": "FAKE-0201", "Location": "LOC-B", "BalQty": "-123456789000"}
  ],
  "rows_truncated": false
}
```

  `rows` lists at most 20 rows, largest `|BalQty|` first; `count` is the full number and `rows_truncated` is `true` when more than 20 rows qualify. When nothing qualifies, `count` is `0` and `rows` is empty. A failed run has `large_balances: null`.
- When `count` is above 0, `warnings` gets one line: `Large balance: <count> exported row(s) with |BalQty| above <threshold>; ...`. The line names no item. A failed run drops the line, as it has no `large_balances`.
- `last_run_status.json`, the run's console output and `--check-latest` carry `large_balance_count` (`null` for a failed run, or for a latest manifest written before this check existed). Exit codes do not change. A later alert round (Telegram through n8n, see [Planned follow-up](#planned-follow-up-failure-alerts)) can read this field; no alert is sent today.
- The CSV is unchanged: the flagged rows are exported as AutoCount holds them.

What to do when `large_balance_count` is above 0:

- [ ] Open `latest_manifest.json` (or the hourly `manifest.json`) and read `large_balances.rows`.
- [ ] For each item and location, open the item's stock movement history in AutoCount (for example a stock card report for that item and location) and find the document behind the balance, typically a stock transfer, adjustment or receive whose quantity looks like a code or a typo.
- [ ] If it is a keying error, have the document corrected in AutoCount. The next hourly run picks up the corrected balance; the job never edits AutoCount.
- [ ] If the balance is real, leave it, and consider raising `output.large_balance_threshold` in the local settings file.
- [ ] Record what was found in the private mailbox, never in this public repository (item codes and document numbers are business data).

## Failure classes (what the owner sees in Drive)

A failed run writes `manifest.json` only (no CSV), never touches the latest pair, and updates `last_run_status.json` to `failed`. In Drive the owner sees `last_run_status.json` with `status: failed`, while `latest_manifest.json` still shows the last good `as_of_local`.

| failure_class | Meaning | What to do |
| --- | --- | --- |
| `config_invalid` | Settings file invalid. | Run `--dry-run-config`, fix the named setting. |
| `timezone_unavailable` | No time zone data at all (Windows Python without `tzdata`). The manifest and `last_run_status.json` are still written, with times in UTC and a `note` saying so. | Install `tzdata` into the task's interpreter (see [VM prerequisites](#vm-prerequisites)). |
| `already_running` | Another run holds the lock: it is younger than 30 minutes, or it was written since the last restart and its process is still running. | Nothing for a single hour. If it repeats for hours, clear it by hand: read `pid` in `.ac2_stockbal.lock` (archive root), run `Get-Process -Id <pid>`; if that is not the exporter's `python.exe`, or no process has that id, delete `.ac2_stockbal.lock`. If it is the exporter, leave it and report. |
| `stale_lock` | The lock file is unreadable or incomplete, or a killed run's lock could not be removed. Never removed automatically. | Check no export process is running, then delete `.ac2_stockbal.lock` in the archive root. |
| `identity_refused` | `sa` login, sysadmin/db_owner with `allow_privileged_identity` false, expected-context mismatch (`AUTOCOUNT_EXPECTED_SQL_LOGIN` / `_USER` / `_DATABASE`), or database pin mismatch. | Check the read identity and the env values. Messages never show the values. |
| `credential_unavailable` | The Credential Manager target could not be read. | Check `identity.credential_target` was saved by the task's own Windows account, and that the task uses a `Password` logon (an `S4U` task cannot read it). |
| `lock_timeout` | Lock timeout on every attempt. | Usually clears next hour. |
| `unstable_snapshot` | ChangeCount moved during every attempt. | Usually clears next hour; check if it repeats. |
| `uom_guard` | A non-base UOM or a multi-UOM item exists. | Map the UOM before the export can continue. |
| `sync_read_failed` | SyncRegistration read failed (for example the wrong time column). | Check `source.sync_registration_time_column`. |
| `source_error` | Connection or other read failure. | Check SQL Server and the driver. |
| `write_failed` | The hourly CSV or manifest could not be written or re-read. | Check disk space and folder permissions. |
| `latest_publish_failed` | Hourly data is good but the latest pair could not be replaced (`last_run_status.json` only). The pair is replaced in two steps (CSV, then manifest). If only the second step fails, the new `stock_balance_latest.csv` sits beside the previous `latest_manifest.json`, so `--check-latest` exits `3` (invalid) until the next successful hour replaces both. | Check whether a program (for example the sync client) holds the latest files open. The hourly folder is complete either way. |
| `internal_error` | Unexpected error. The message has the run reference only. | Report the run reference. |
| (no new status) | The task did not run at all: `finished_at` in `last_run_status.json` is older than about 70 minutes and no new hourly folder appears (VM off or asleep, task disabled, or for pair (b) the Windows password changed). | Check the VM is on, then `Get-ScheduledTaskInfo -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` (`LastRunTime`, `LastTaskResult`). For pair (b) after a password change, update the task's stored password (needs owner OK). The planned heartbeat round will alert on this. |

## Read identity and scheduled logon

- Owner decision (23 Oct 2026): the owner's own admin account, with any password read from Windows Credential Manager at runtime. That account is privileged (sysadmin or db_owner), so the local settings must set `allow_privileged_identity: true`. Every manifest then carries `identity.privileged: true` and a warning.
- Exactly two identity and logon pairs are supported. Which one applies is decided at V4. Settings validation refuses any other pair, and the installer registers the logon type from the validated settings.

| Pair | Settings | Task registration | How the job signs in |
| --- | --- | --- | --- |
| (a) Windows account | `identity.auth_mode: "windows_integrated"`, `schedule.logon_type: "S4U"`, no `sql_login` or `credential_target` | `LogonType S4U`: no password is stored | SQL Server sees the task's Windows account (`Trusted_Connection`). S4U gives the task no network access and no access to Credential Manager secrets, which is fine for SQL Server on the same VM. |
| (b) SQL login | `identity.auth_mode: "sql_login"`, `schedule.logon_type: "Password"`, `identity.sql_login`, `identity.credential_target` | `LogonType Password`: the installer asks for the Windows account's password with `Get-Credential` and passes it to `Register-ScheduledTask -User/-Password`. It is never echoed, logged or written to a file. | The job reads the SQL login's password from Windows Credential Manager under that same Windows account at runtime. Save the credential while signed in as that account, for example `cmdkey /generic:<credential target> /user:<sql login> /pass` (prompts for the password). |

- `sql_login` under `S4U` is refused at validation: an S4U task cannot read Credential Manager, so every hour would fail as `credential_unavailable`. `windows_integrated` under `Password` is refused too, because it would store a Windows password for no reason.
- Whatever the pair: the task runs at `RunLevel Limited`. For an admin Windows account that is a filtered token, so membership that comes only through the local Administrators group is not used. The V7 supervised run proves the pair works under the scheduled context; if it does not, do not raise the run level without a separate owner OK.
- The least-privilege option remains available: a dedicated SQL login with `SELECT` only on the six tables read (`ItemBatchBalQty`, `Item`, `ItemUOM`, `Location`, `ChangeCount`, `SyncRegistration`) and `allow_privileged_identity: false`.
- Always: `SUSER_SNAME()='sa'` is refused (case-insensitive), whatever the settings say. The `AUTOCOUNT_EXPECTED_SQL_LOGIN` / `_USER` / `_DATABASE` environment values must be set for the task's account and must match; the run fails closed otherwise.

## Reconciliation

1. Owner exports AutoCount **Stock > Stock Balance by Location** once, off-peak.
2. Compare totals by location with `per_location.sum_qty` in the manifest of an export taken close to the same time.
3. Compare 20 sample SKUs (quantity per location).
4. Align `output.columns` to the report's columns if the owner wants the same layout.
5. Record the result in the private mailbox, not in this public repository.

## VM proof steps (V1-V7)

Each step **needs owner OK**. None has been run.

- **V1** (needs owner OK): check the [VM prerequisites](#vm-prerequisites), then run against the test book `AED_TESTINGAPI` with the VM Windows user and `allow_privileged_identity: false` (expect `identity_refused`), then `true` (test book only).
- **V2** (needs owner OK): reconcile the test output to the scoping totals. The totals are confidential and kept only in the private mailbox file `mailbox/245/20261023-1623-v2-reference-totals.md`.
- **V3** (needs owner OK): owner runs `sql/autocount2/stock_balance_hourly_live_check.readonly.template.sql` off-peak. Note: the ledger-match step assumes `dbo.StockDTL` has a `BatchNo` column; if it errors there, nothing was written and the remaining steps can be rerun without it.
- **V4** (needs owner OK): choose pair (a) or (b) from [Read identity and scheduled logon](#read-identity-and-scheduled-logon) (owner's admin account: Windows account, or SQL login plus the Credential Manager target), set `identity.auth_mode` and `schedule.logon_type` to match, and run `--dry-run-config`. Or create a dedicated login and grants if the owner changes the decision.
- **V5** (needs owner OK): three manual live runs off-peak; reconcile with the owner's Stock Balance by Location export.
- **V6** (needs owner OK): name the Drive for desktop synced folder (`delivery_root`) and the Google account Drive for desktop uses on the VM.
- **V7** (needs a separate owner OK): register the hourly task, then prove it with one supervised run under the scheduled context before leaving it enabled.
  1. Set the `AUTOCOUNT_EXPECTED_SQL_LOGIN` / `_USER` / `_DATABASE` environment values for the task's Windows account (user or machine level), as the job fails closed without them.
  2. Dry run first (in an elevated Windows PowerShell 5.1, no transcript, from the root of the clone the task will run from):
     `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autocount_stock_balance_hourly_task.ps1 -UserId <Windows account> -ConfigPath <full path to the local settings file> -PythonExe <full path to python.exe> -DryRun`
     It checks `pyodbc`, runs `--dry-run-config` with that interpreter, prints the task it would register (`\XB\XB AC2 Hourly Stock Balance`, the repo root, the minute and the logon type) and stops: nothing is registered and no password is asked for. If it looks right, run the same command again without `-DryRun` to register the logon type the settings require; for pair (b) it then prompts for the Windows password.
     `-ExecutionPolicy Bypass` applies to this one command only; it does not change the VM's PowerShell settings. `-RepoRoot` is not needed (the installer uses the clone it is in). The task goes in the `\XB\` folder of Task Scheduler; use `-TaskPath "\Other\"` only if the owner asks for another folder (it must start and end with `\`). The installer refuses if a task with the same name is already in that folder, and warns if one is in the Task Scheduler root (see "Move an existing root task" below).
  3. Note `finished_at` in `<archive_root>\last_run_status.json` (if present), then run `Start-ScheduledTask -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` straight away, before the first hourly trigger.
  4. Wait until `Get-ScheduledTaskInfo -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` shows a new `LastRunTime` and the task is no longer running (at most 15 minutes).
  5. Pass only if `last_run_status.json` is newer than the noted value and shows `status: success` (and `delivery_status: handed_to_drive_sync_folder` once `delivery_root` is set). This also proves `PythonExe`, the environment values, the identity pair and the run level under the task account.
  6. Otherwise run `Disable-ScheduledTask -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` at once and report the `failure_class`. The task is left enabled only after a pass.

### Move an existing root task into `\XB\` (VM step, needs owner OK)

An earlier installer registered the task in the Task Scheduler root (`\`). Move it once, with the owner's OK, at a quiet minute well away from the task's run minute (in an elevated Windows PowerShell 5.1):

1. Export the root task: `Export-ScheduledTask -TaskPath "\" -TaskName "XB AC2 Hourly Stock Balance" > "$env:TEMP\xb245-root-task.xml"`.
2. Register it under `\XB\` from that XML. For pair (a) (S4U, no stored password): `Register-ScheduledTask -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance" -Xml (Get-Content "$env:TEMP\xb245-root-task.xml" -Raw)`. For pair (b) (Password logon), do not register from XML (that would put the password on the command line): unregister the root task (step 4), then run the installer as in V7 step 2, dry run first, and do the V7 steps 3-6 supervised run again.
3. Compare: `Export-ScheduledTask -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` must match the saved XML apart from the `<URI>`, `<Date>` and `<Author>` lines. Check the trigger, IgnoreNew, the 15 minute limit, StartWhenAvailable, the logon type and RunLevel `LeastPrivilege`.
4. Unregister the root copy: `Unregister-ScheduledTask -TaskPath "\" -TaskName "XB AC2 Hourly Stock Balance" -Confirm:$false`.
5. Check that `Get-ScheduledTask -TaskName "XB AC2 Hourly Stock Balance"` lists only the `\XB\` task, then delete the saved XML. After the next hourly run, confirm `last_run_status.json` shows a new `success`. Record the result in the private mailbox.

## Retention

- No pruning code in this build. Hourly folders accumulate (24 a day locally and, when `delivery_root` is set, in Drive).
- Local raw retention follows [Security guardrails](security_guardrails.md) (raw exports stay outside GitHub, restricted NTFS permissions, initially 90 days). Any pruning needs a later owner OK.

## Out of scope

- **#246** (daily money / buying package): the owner wants AutoCount's Advanced Dimensional report there (most detailed breakdown, for example marketplace shipping fees). New marketplace fee codes can land on a default term (believed to be "Adjustment") until someone adds the term in AC2, so the #246 report should flag unmapped codes. Cost and value belong in #246, not here.
- Dashboards: planned later as Claude artifacts shared in the Work Claude organisation account. Nothing to build now.
- Consignment stock, failure alerts (planned follow-up below), n8n, Drive API upload, pruning, scheduling, SQL logins and grants.

## Planned follow-up: failure alerts

Not built in this round, and nothing in n8n is touched. In this build a failure is visible only through files (`last_run_status.json`, the hourly manifest, `--check-latest`). Planned for a later #245 round:

- Failure alerts go to the owner by Telegram through an n8n webhook. The VM job posts a sanitized failure event (hour, `failure_class`, `run_id`), with no stock data.
- The same round can alert on `large_balance_count` above 0 in `last_run_status.json` (count only, no item codes or quantities).
- An hourly heartbeat, so n8n alerts when no run has been heard for about 70 minutes.

## Open questions

1. Drive for desktop folder and Google account: name them at V6.
2. Read identity: pair (a) Windows account with S4U, or pair (b) SQL login with a Password logon and the Credential Manager target. Decide at V4.
3. Items with no balance row: keep the added "(no balance row)" rows (default) or switch `zero_row_mode`.
4. Which SyncRegistration time column is right on live.
5. Online marketplace stock may include consignment for some outlets, so it can differ from this export; owner to confirm that is fine.
