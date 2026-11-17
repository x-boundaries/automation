# AC2 hourly stock balance export runbook (#245)

Status: **offline build only.** Nothing here has run against SQL Server, the AC2 VM, Google Drive or Task Scheduler. Every step under [VM proof steps](#vm-proof-steps-v1-v7) and [Round 6 VM proof](#round-6-vm-proof-steps-d1-d6) needs a separate owner OK.

Dashboard v2 (28 Oct 2026): every successful run also writes the item index `stock_item_index.json.gz` beside the feed, for the dashboard's item search, item detail and data-issues views (see [Item index](#item-index-schema-v1)). The item master read adds `ItemGroup` and `ItemBrand`, and the `dbo.ItemUOM` read adds `BarCode` and `MinQty`. Nothing else changes.

Buying analysis (#292 slice S2, owner "Yes, accept all", 8 Nov 2026): behind `analysis.enabled` (**default off**), the first successful run after local midnight also builds a small set of nightly buying-analysis files beside the feed (sales by item, location, day and channel; stock-state intervals; placement facts; rule-based highlights; one manifest), and the item index gains structured size, gender, colour, category and style fields (`schema_minor: 2`). Units only. A failure never blocks the hourly files. See [Buying analysis files](#buying-analysis-files-292-s2).

Purchasing and vendors (#292 slice S4, owner 10 Nov 2026): behind `analysis.purchasing.enabled` (**default off**, and it needs `analysis.enabled` too), the nightly analysis set also carries `analysis_purchasing.json.gz`: open PO units, goods received by month, supplier lead days, vendor names and the ordered vs received size split. Units only; vendor **names** are shown (owner Q1), never creditor codes, PO/GR numbers, prices or amounts. A purchasing failure never blocks the buying files or the hourly files. See [Purchasing file](#purchasing-file-292-s4).

Error alerts (29 Oct 2026, owner: errors only, no heartbeat): a failed or flagged run can POST one sanitized event to an n8n webhook, which sends one email to the operations inbox through the shared `XB Ops Alert Email` sub-workflow (6 Nov 2026, #288: email replaces Telegram; no dual-send). Off by default and shipped inactive; see [Error alerts](#error-alerts-email-through-n8n).

Round 6 (accepted design, owner "Accept all", 27 Oct 2026): a daily 00:00 close rebuilt from the stock ledger, a backfill from 1 Aug 2026, one latest pair in Drive (no hourly folders in Drive), local retention, and a compact dashboard feed. File, setting and task names keep the word "hourly" (the job still runs hourly); only the Drive folder is renamed to "daily".

## What it does

`scripts/autocount_stock_balance_hourly.py` runs once an hour, 24/7. Each run reads the stored on-hand stock balance (`dbo.ItemBatchBalQty`, the table AutoCount itself maintains) and the stock ledger (`dbo.StockDTL`, summed per item, UOM, location and day), checks that the two agree, and writes:

- locally, an immutable hourly folder with `stock_balance.csv`, `manifest.json` and `alert_record.json` (see [Per-run alert record](#per-run-alert-record); kept 48 hours once pruning is on);
- a "latest" pair (`stock_balance_latest.csv`, `latest_manifest.json`) that is replaced only after the new hourly CSV re-reads to the hash that was written;
- the dashboard feed `stock_dashboard_feed.json.gz`, beside the latest pair (see [Dashboard feed](#dashboard-feed-schema-v1));
- the item index `stock_item_index.json.gz`, beside the feed (see [Item index](#item-index-schema-v1));
- `last_run_status.json`, updated on every run, success or failed, so a failed hour is visible in Drive.

The first run after local midnight also publishes the **daily close** of the previous day (see [Daily close](#daily-close)): one CSV of non-zero closing balances plus its manifest, in `daily/<YYYY-MM>/`.

Delivery to Google Drive is a plain file copy into a local folder that Google Drive for desktop syncs (`output.delivery_root`). Drive gets only the latest pair, the feed, the item index, `last_run_status.json` and the daily files, each replaced through a temp copy that must re-read to the right hash. **No hourly folders go to Drive.** Nothing calls a Drive API. The manifest says "handed to Drive sync folder", never "uploaded".

Where it runs (owner decision, 23 Oct 2026): on the AC2 VM as a Windows Task Scheduler task, reading SQL Server locally. The archive is a gitignored folder inside the automation clone on the VM, and delivery writes directly into the Drive for desktop synced folder. The settings file is JSON and cannot carry comments, so this runbook documents the placeholders used in the example settings.

How it reads:

- Fixed `SELECT` statements only; `READ COMMITTED`, `SET LOCK_TIMEOUT` (default 5000 ms), query timeout (default 60 s), one connection per attempt. No `NOLOCK`, no snapshot isolation, no database setting changes.
- Consistency: `dbo.ChangeCount` (row `ItemBatchBalQty`) is read before and after the balance read on the same connection. Equal values mean no balance write landed in between. If it keeps moving, the run retries (default 3 attempts, short backoff), then fails as `unstable_snapshot`. A lock timeout is also retried within the same attempt budget.
- Balance rows are summed by ItemCode, UOM and Location (BatchNo is always empty today; rows with a non-empty BatchNo are counted in the manifest).
- The ledger read (`dbo.StockDTL`: `SUM(Qty)` per ItemCode, UOM, Location and `CAST(DocDate AS date)`, never filtered by date), the item master (`dbo.Item`, including the `ItemGroup` and `ItemBrand` codes), the location list (`dbo.Location`) and the item UOM rows (`dbo.ItemUOM`: `BarCode`, `ReOLevel`, `MinQty`; no `Cost` or `Price`) are read **inside the same ChangeCount window** as the balance read. `dbo.ChangeCount` has no row for `StockDTL` (R1, 28 Oct 2026); a stock posting writes both tables, so it moves the balance counter, and the per-run [ledger check](#ledger-check-every-run) catches anything the counter missed.
- No cost column is selected. `MostRecentlyCost`, `StockDTL.Cost` / `TotalCost` and every cost table stay out. Consignment (`dbo.CSGNItemBalQty`) is out. `StockDTL.DocInfo` (free text) is never read.
- `dbo.ChangeLog` is never read.
- With `analysis.enabled` on: the item master read adds the attribute columns, and the nightly analysis read runs **on its own connection after the hourly files are published** (identity checked again), so it can never fail or delay the hourly snapshot. See [Buying analysis files](#buying-analysis-files-292-s2).

## Files

| File | Purpose |
| --- | --- |
| `scripts/autocount_stock_balance_hourly.py` | The exporter. CLI: `--config PATH` (required), `--check-latest`, `--dry-run-config`, `--backfill`. |
| `config/autocount_stock_balance_hourly.example.json` | Committed example with fake values only. |
| `config/autocount_stock_balance_hourly.local.json` | The real settings file on the VM. Gitignored by `config/*.local.json`; never commit it. Real location labels live only here. |
| `scripts/install_autocount_stock_balance_hourly_task.ps1` | Task Scheduler installer. Not run by the build; step V7 only. |
| `sql/autocount2/stock_balance_hourly_live_check.readonly.template.sql` | Owner-run read-only live check (step V3). Aggregates only. |
| `scripts/autocount_stock_analysis.py` | #292 S2: pure builders for the nightly analysis files, the analysis settings check, the size and gender normalisation and the SELECT-only analysis read. No I/O. |
| `tests/test_autocount_stock_balance_hourly.py` | Offline tests with in-memory fakes and temp folders. |
| `tests/test_autocount_stock_analysis.py` | Offline tests of the analysis builders on fake rows. |
| `n8n-workflows/ac2_stock_alert.workflow.json` | The inactive n8n workflow that turns an alert event into one alert email (see [Error alerts](#error-alerts-email-through-n8n)). |
| `n8n-workflows/ops_alert_email.workflow.json` | The inactive shared `XB Ops Alert Email` sub-workflow that sends it (#288): the only Gmail node, the recipient, the subject format, and the fold and flood rules. |
| `tests/test_ac2_stock_alert_workflow.py` | Offline contract of that workflow; runs its Code node with Node against real events and checks the result against the sub-workflow's input check. |

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
& "C:\EXAMPLE\Python311\python.exe" scripts\autocount_stock_balance_hourly.py --config config\autocount_stock_balance_hourly.local.json --backfill
```

`--backfill` is a normal run (same reads, same checks, same latest pair and feed) that also writes **every** daily close from `daily_close.backfill_start` to yesterday again, replacing any that exist (`write_reason: rebuild`, revision + 1). It uses the same code path as the nightly close. Run it once at the VM step (step D2), and again only if the owner wants Drive's older daily files rebuilt from the ledger.

Exit codes: run `0` success (and delivery verified or not configured, the daily close published or not due, and the feed and the item index published), `1` failed, delivery not verified, daily close failed, feed failed or item index failed. A daily close, feed or item index failure never stops the hourly latest pair: `status` stays `success` and `daily_close_status` / `feed_status` / `item_index_status` say what failed. `--check-latest`: `0` fresh, `2` stale (older than `stale_after_minutes`), `3` missing or invalid; it never exits with anything else, so invalid settings (including a malformed `timezone` such as `Asia/Singapore/`), a missing zone database or a usage error also give `3`. `--dry-run-config`: `0` valid (it also prints `auth_mode`, `scheduled_logon_type` and `run_minute`, which the installer reads), `1` invalid. Console output is a short status JSON; it never prints row data, connection strings, logins, host names or paths. The run output, `--check-latest` and `last_run_status.json` also carry `large_balance_count` (see [Large-balance warning](#large-balance-warning)); it never changes an exit code. The run output and `last_run_status.json` carry `alert_status` (see [Error alerts](#error-alerts-email-through-n8n)), and `last_run_status.json` and the run's `alert_record.json` also carry the webhook's HTTP status; an alert, sent or lost, never changes an exit code either.

## Settings

Every business choice is a setting. The owner said answers may still change, so every row below "may change". Unknown keys, bad values, a bad SyncRegistration column or a missing `expected_database` fail validation before anything is read.

| Key | Default | Notes |
| --- | --- | --- |
| `timezone` | `"Asia/Singapore"` | Used for folder names, `as_of_local` and the stale check. May change. |
| `schedule.frequency` | `"hourly"` | Only `hourly` is supported (owner decision: hourly latest plus the 00:00 daily close in the same task). Used by the installer and the stale check. May change. |
| `schedule.run_minute` | `0` | Minute past each hour (00:00, 01:00 ... 23:00 = 24 runs a day). The installer reads it from the validated settings (there is no installer parameter for it); changing it later means registering the task again (the installer never replaces an existing task, so that needs an owner OK). The first run after local midnight (00:00 + this minute) also writes the daily close. May change. |
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
| `rows.uom_guard` | `"fail"` | Fail the hour (no CSV) if any balance row's or ledger group's UOM differs from `Item.BaseUOM` (a ledger item missing from the item master counts as different), or any item has more than one UOM, until a mapping exists. May change once a mapping is designed. |
| `rows.exclude_non_stock_items` | `true` | Leave items with `StockControl = 'F'` (not stock-controlled, for example marketplace fee codes, which made one outlet negative at R1) out of the latest CSV, the daily closes and the feed. They are still in the ledger check. Counted as `counts.non_stock_excluded` (latest rows), `ledger.non_stock_items_excluded` and `ledger.non_stock_rows_excluded`. Only an explicit `F` is excluded: an item missing from the item master is kept. May change. |
| `location_labels` | `{}` | Location code -> label. Every code is exported; codes are never dropped or renamed. Unmapped code label = `code - Location.Description` when present, else the code; unmapped codes are listed in the manifest. Real labels only in the local file. May change. |
| `output.archive_root` | required | Local archive folder on the VM. Owner decision: a gitignored folder inside the automation clone, `<automation clone>\autocount_outputs\stock_balance_hourly` (ignored by the existing `autocount_outputs/` rules in `.gitignore`). May change. |
| `output.delivery_root` | `null` | The Google Drive for desktop synced folder; the job writes into it directly. `null` = local archive only. Must differ from `archive_root`, and neither may sit inside the other (an archive inside the Drive folder would sync the hourly folders). Drive folder naming follows the company Drive layout rule `<Feed name (cadence)>`: since round 6 the folder is **`Report/AC2 Stock Balance (daily)`** (was `(hourly)`; the folder rename itself is VM step D4). Example placeholder: `D:\EXAMPLE\DriveForDesktop\Report\AC2 Stock Balance (daily)`. |
| `output.large_balance_threshold` | `100000` | Whole number from 1 to 1000000000. Every exported row whose BalQty is strictly above it, positive or negative, is listed in the manifest as a warning; the run still succeeds and publishes (see [Large-balance warning](#large-balance-warning)). There is no off switch: the check never blocks an export, so if it is noisy, raise the threshold. May change. |
| `output.columns` | as_of_local, ItemCode, ItemDescription, ItemType, Location, LocationLabel, UOM, BalQty, IsActive, StockControl, RowSource | Order and subset of the latest CSV's columns; must include ItemCode, Location, UOM, BalQty. To be aligned to the owner's Stock Balance by Location report at reconciliation. The daily close CSV has its own fixed columns (see [Daily close](#daily-close)). May change. |
| `daily_close.backfill_start` | `"2026-08-01"` | First day that gets a daily close file, as `YYYY-MM-DD`. Owner decision: the later of 1 Aug 2026 and the opening-stock date (R1: opening stock on 31 Jul, so 1 Aug). Closes before it are not written, but every close still sums the ledger **from its first row** (R1: timed sale rows from 3 Jul). May change. |
| `daily_close.restate_days` | `7` | Restate window, 1 to 31 days. A due close may replace the files of D-1 .. D-7 when the ledger changed; an older changed day is left as published and flagged (`late_changes_beyond_window`). May change. |
| `retention.prune_enabled` | `false` | Pruning of the local archive. Off until the owner OKs it at VM step D3. Never touches Drive. |
| `retention.hourly_keep_hours` | `48` | Local hourly run folders older than this (by the time in the folder name) are removed. 2 to 8784. |
| `retention.daily_keep_days` | `90` | Local daily close files whose close date is older than this are removed. Must be more than `restate_days`. Drive keeps every daily file. |
| `feed.low_stock_threshold` | `2` | Feed low stock = `0 < quantity <= threshold`, active items (owner, 28 Oct 2026: global 2 units for now). `ItemUOM.ReOLevel` is shown beside each feed low-stock row when set but does not change the feed rule. The dashboard v2 page uses the item index instead: the item's reorder level, else its minimum quantity, else this threshold (see [Item index](#item-index-schema-v1)). |
| `feed.list_cap` | `200` | Most rows listed in the feed's negatives and low-stock lists; counts are always complete. |
| `feed.movers_top` | `20` | Biggest risers and fallers listed per period (1, 7, 30 days). |
| `alert.webhook_enabled` | `false` | Error alerts on or off (see [Error alerts](#error-alerts-email-through-n8n)). Switched on only at go-live step A3 with the owner's OK. |
| `alert.webhook_url` | `null` | The n8n webhook, `https://<n8n host>/webhook/<private path>`. `https` only; no user, password, query or fragment (the secret travels only in the header). Required when enabled. Example placeholder: `https://n8n.example.com/webhook/REPLACE_WITH_ALERT_WEBHOOK_PATH`. |
| `alert.auth_header_name` | `"X-XB-Alert-Token"` | Header that carries the shared secret; must equal the header name of the n8n Header Auth credential. |
| `alert.secret_env_var` | `null` | Name of the environment variable (user or machine level, for the task's Windows account) that holds the shared secret. The way for pair (a) S4U. The value is read at runtime and never stored, printed or logged. |
| `alert.secret_credential_target` | `null` | Instead of the env var: a Windows Credential Manager generic credential target holding the secret. Only with `schedule.logon_type: Password` (an S4U task cannot read Credential Manager), so validation refuses it under S4U. Set exactly one of the two when enabled. |
| `alert.timeout_seconds` | `10` | One POST per run at most, 1 to 30 s. No retry inside the run. |
| `alert.repeat_hours` | `24` | A problem that is still there is repeated after this many hours (1 to 168). |
| `analysis.*` | off | #292 S2 nightly buying-analysis files and item attributes. See [Analysis settings](#analysis-settings). The example file holds fake codes only; the real channel, debtor, pool and placement values are filled in the VM's local settings file at VM proof step P1. |

## Outputs and folder layout

```
<archive_root>\                           (local, on the VM)
  .ac2_stockbal.lock                      (while a run is active, or left by a killed run)
  alert_state.json                        (error alerts on: which problems were last alerted, and when; local only)
  last_run_status.json                    (every run)
  stock_balance_latest.csv                (latest verified success only)
  latest_manifest.json                    (latest verified success only)
  stock_dashboard_feed.json.gz            (every successful run)
  stock_item_index.json.gz                (every successful run)
  analysis_*.json.gz, analysis_manifest.json  (analysis.enabled only: replaced by the first successful run after midnight)
  hourly\<YYYY-MM-DD>\ac2_stockbal_<YYYYMMDDTHHMMSS+0800>_<run_id first 8>\
      stock_balance.csv                   (success only; kept 48 h when pruning is on)
      manifest.json                       (every outcome; kept 48 h when pruning is on)
      alert_record.json                   (every run: alert_status and the webhook's HTTP status; local only; kept 48 h when pruning is on)
  daily\close_state.json.gz               (what the published daily files were built from; local only, never pruned)
  daily\<YYYY-MM>\stock_close_<YYYY-MM-DD>.csv            (kept 90 days when pruning is on)
  daily\<YYYY-MM>\stock_close_<YYYY-MM-DD>.manifest.json  (kept 90 days when pruning is on)

<delivery_root>\                          (Drive: "Report/AC2 Stock Balance (daily)")
  stock_balance_latest.csv                (replaced every run)
  latest_manifest.json                    (replaced every run)
  stock_dashboard_feed.json.gz            (replaced every run)
  stock_item_index.json.gz                (replaced every run)
  analysis_*.json.gz, analysis_manifest.json  (analysis.enabled only: replaced nightly, as one verified set)
  last_run_status.json                    (replaced every run)
  daily\<YYYY-MM>\stock_close_<YYYY-MM-DD>.csv            (one per day, never pruned)
  daily\<YYYY-MM>\stock_close_<YYYY-MM-DD>.manifest.json
```

- No `hourly\` folder is ever written to Drive. An hourly run that is not the daily close adds no file to Drive; it only replaces the five top-level files.
- Local hourly files use exclusive create plus fsync and SHA-256. A rerun in the same hour makes a new folder; existing files are never touched.
- Published sets (`publish_verified_set`): the latest pair (archive), the Drive latest set (CSV, manifest, feed and item index), each day's CSV + manifest (per folder), the archive feed and the archive item index are each written as a set. Every file of the set is first written to a temp name beside its target, re-read and hashed; only when **all** of them verify are they moved over the old files with `os.replace`, in order (CSV first). A bad or failed copy of any file removes all the temps and replaces nothing, so Drive keeps the previous verified pair (and feed and item index) and a day keeps its previous CSV + manifest, each pair still matching by hash. The archive latest pair is published only after the hourly CSV re-reads to its hash. `latest_manifest.json` carries `as_of_local` and `run_id`, so an old snapshot is visible.
- One limit remains, as before round 6: if a target is still held (for example by the sync client) after the move's three tries, the files already moved stay moved and the rest are dropped; the run reports `copy_failed` and `--check-latest` may say `invalid` until the next good hour.
- Delivery status `verify_failed` (hash mismatch) or `copy_failed` (write error) is in `last_run_status.json`; the run manifest records `delivery.mode: latest_only`, the files and `pending_see_last_run_status`.
- Size, measured offline with a generated fake book (50,000 ledger groups, 12,000 keys, 6,000 items): daily close CSV about 1 MB a day; feed under 10 KB; the 88-day backfill run took about 11 s and an ordinary hourly run about 1 s on the server PC. Real sizes are measured at VM step D2. Item index (dashboard v2), measured with a generated fake book of 24,000 items (19,473 listed), 12,000 item-location keys and 47,000 ledger groups, random descriptions and barcodes: 671 KB gzipped (2.7 MB raw); the backfill run still took about 10 s. Real text compresses better; the real size is measured at VM step D7.
- Lock: `.ac2_stockbal.lock` records the run_id, process id and start time. A run killed by the 15-minute task limit or a reboot cannot remove it. The next run clears it automatically only when it is older than 30 minutes (the 15-minute limit plus a 15-minute margin) **and** either it was written before Windows last started (a restart ends every run, whatever process later reuses its process id) or its recorded process is no longer running (on Windows, a process created after the lock was written counts as a reused process id, not the owner). That run adds a warning to its manifest naming the killed run's reference, the reason and the lock age. A younger lock, or a lock written since the last start whose process is still running, gives `already_running` and is left alone.
- CSV: UTF-8 without BOM, header row, CRLF line ends via the csv module, fixed column order from `output.columns`, every cell through `safe_csv_row` (formula prefixes neutralised; numbers stay numeric, so negative quantities are not prefixed).
- Manifest fields: job `ac2_hourly_stock_balance`, schema_version, run_id, status, failure_class, sanitized message, started/finished (+08:00), as_of_local and as_of_sql, timezone, source (surface, database, query_sha256, settings_sha256, git commit), identity flags (never names), attempts, ChangeCount before/after, row_count, CSV sha256/size, counts (zero, negative, no_balance_row, non_empty_batch, inactive_dropped, zero_dropped, null_balqty_rows), per-location rows / non_zero / negative / sum_qty, unmapped_location_codes, large_balances, uom_guard, pos_sync per Location, delivery, warnings.
  Round 6 adds (schema_version `2`): `ledger` (groups, rows, null_qty_rows, non_empty_batch, future_dated_rows, non_base_uom_groups, non_stock_rows_excluded, non_stock_items_excluded, items_missing_from_master, first_day, last_day), `ledger_check`, `uom_guard.ledger_non_base_uom_groups`, `counts.non_stock_excluded`, `daily_close`, `feed` (status, sha256, byte_size), `retention`, and the extra query hashes (`ledger`, `item_master`, `locations`, `item_uom`; the last was `reorder_levels` before dashboard v2). Dashboard v2 adds `item_index` (status, sha256, byte_size, counts).
- `last_run_status.json` fields: job, run_id, status, failure_class, as_of_local, started_at, finished_at, hourly_folder, delivery_status, large_balance_count (`null` for a failed run), daily_close_status (`published`, `not_due`, `failed`, `skipped_failed_run`), daily_close_failure_class, published_close_day, revised_day_count, late_change_day_count, feed_status (`published` = in the archive and, when delivery is set, in Drive; `failed` = not built, not written to the archive, or not delivered to Drive; `null` for a failed run), item_index_status (same values and rules as feed_status), analysis_status (`disabled`, `not_due`, `published`, `failed`, `skipped_failed_run`), analysis_failure_class, analysis_close_day (the close day of the analysis set in the archive), analysis_seconds (build and publish time of a published set), next_run_local, alert_status and alert_http_status (see [Error alerts](#error-alerts-email-through-n8n)). `status` and `failure_class` describe the hourly snapshot as before.
- `alert_record.json` fields (one per run, see [Per-run alert record](#per-run-alert-record)): schema `xb.ac2.stock_alert_record.v1`, job, run_id, started_at, finished_at, status, failure_class, alert_status, http_status.

## Ledger check (every run)

Owner-accepted design, section 2.2: every run proves itself before anything is published.

- The ledger summed over **all** dates must equal the stored balance (`ItemBatchBalQty`) for every ItemCode / UOM / Location key, all items included (non-stock codes too). A key only in the balance table must be 0; a key only in the ledger must net 0 (R1 found both kinds, all zero, and no shared key that differed).
- Any difference fails the whole run as `ledger_mismatch`: no hourly CSV, no latest pair, no daily close, no feed. `last_run_status.json` shows `failed`; the manifest's `ledger_check` gives counts only (keys_compared, mismatched, balance_only, balance_only_non_zero, ledger_only, ledger_only_non_zero), never items or quantities.
- What to do: rerun once (a posting that slipped past the ChangeCount window clears on the next hour). If it repeats, report the counts; do not change settings to get past it.

## Daily close

- **When:** the close of day D-1 is due when the close state has not published D-1 yet. That is the first successful run after local midnight (normally 00:00 + `run_minute`). If that run fails or the VM is off, the next run catches up and writes every missing day. A second run in the same hour finds the close already published and writes nothing (the 00:00 double start seen on 28 Oct is harmless; the installer now prevents it).
- **What:** the closing balance of a day is the sum of every ledger row whose `DocDate` is before the next local midnight, from the very first ledger row (`CAST(DocDate AS date) < D + 1`, exactly `DocDate < D+1 00:00`; R1: one document type carries real times). It is never a `DocDate = D` match and never starts at `backfill_start`.
- **Rows:** non-zero closes only (owner Q2), non-stock items left out. Columns: `close_date, ItemCode, ItemDescription, ItemType, Location, LocationLabel, UOM, CloseQty, IsActive, StockControl`. Same CSV safety as the latest CSV. Descriptions are the item master's current text. Unmapped location codes are kept, labelled `code - Location.Description`, and listed (`unmapped_location_codes` in the day manifest and in `daily_close`).
- **Day manifest** (`stock_close_<day>.manifest.json`): job `ac2_daily_stock_close`, close_date, close_rule, rows_rule, write_reason (`new`, `revised`, `rebuild`), revision, run_id, written_at, ledger_read_as_of_local, final_from (the day after the restate window ends), row_count, csv sha256 / byte_size, per_location rows / negative / sum_qty, unmapped_location_codes.
- **Nightly rebuild and restate window:** a due close rebuilds every close from the whole ledger and compares with the ledger the published files were built from (kept in `daily\close_state.json.gz`):
  - a day never published is written (`new`);
  - a changed day within D-1 .. D-`restate_days` is replaced (`revised`, revision + 1) and listed in `daily_close.revised_days` (day, rows_changed, net_qty_change);
  - a changed older day is **left as published** and listed in `daily_close.late_changes_beyond_window` (day, rows_changed, net_qty_change; at most 31 listed, the count is complete), with one `Late change: ...` warning. The flag is raised once; the state then holds the new ledger. The dashboard feed always shows the rebuilt truth.
  - Days inside the window are provisional (`daily_close.provisional_from`); a day is final from its manifest's `final_from`.
- **Missing or unreadable close state:** the next due run writes every day from `backfill_start` again (self-healing; the files then match the ledger) and records `daily_close.state` (`missing` or `unreadable`). It warns `Close state was ...` only when the state was lost: an unreadable state, or a missing state on a normal run while `daily\` already exists. The very first run (no `daily\` yet) and a `--backfill` run rewrite every day anyway, so they do not warn; `daily_close.state: missing` still shows it (VM step D2, 28 Oct 2026, was such a `--backfill`).
- **Failure:** a daily file that cannot be written or does not verify stops further day writes, keeps the old close state, and sets `daily_close_status: failed` with `daily_close_write_failed`. The hourly latest pair is still published; the next run retries the same days. Exit code `1`.
- Backfill: `--backfill` (above). The first run after the round-6 code is installed also writes every day from `backfill_start`, because no close state exists yet.

## Dashboard feed (schema v1)

`stock_dashboard_feed.json.gz` is written beside the latest pair on every successful run (local archive and Drive), gzip with a fixed header time. It is the only data the planned dashboard artifact reads (the artifact itself is a later round). Quantities only: no cost, no sales value, no customer, member or document data.

| Key | Content |
| --- | --- |
| `schema`, `schema_version` | `xb.ac2.stock_dashboard_feed`, `1`. A breaking change raises the version. |
| `job`, `run_id`, `as_of_local`, `timezone` | Same as the latest manifest. |
| `cadence_minutes`, `next_run_local`, `stale_after_minutes` | For the page's "as of ..., next sync in ..." countdown (60, the next hour at `run_minute`, 70). |
| `backfill_start`, `last_close_day`, `published_close_day`, `provisional_from` | History range; the last day the daily files were published; first provisional day. |
| `quantity_rule` | Text: units only, non-stock items excluded. |
| `locations` | `[{code, label, mapped}]` for every location seen (latest, history or labels). |
| `latest` | Per location: `units`, `skus_in_stock`, `zero`, `negative`, `low_stock` (from the latest balance rows). |
| `history` | `days` (from `backfill_start` to `last_close_day`) and per location arrays aligned to `days`: `units`, `skus_in_stock`, `negative`. |
| `movers` | `1d`, `7d`, `30d`: `from`, `to`, `changed_keys`, and `up` / `down` lists (`item, description, location, uom, change, close`), `movers_top` each, from the closes. |
| `negatives` | `count`, `rows` (`item, description, location, uom, qty, negative_since`), `truncated`. `negative_since` is the first close day of the current negative streak, `null` if it turned negative today. |
| `low_stock` | `rule`, `threshold`, `count`, `items_with_reorder_level`, `rows` (`..., qty, reorder_level`), `truncated`. |
| `anomalies` | Counts only: `ledger_check`, `large_balance_count`, `negative_rows`, `future_dated_ledger_rows`, `unmapped_location_count`, `non_stock_items_excluded`, `daily_close_status`, `revised_day_count`, `late_change_day_count`. |
| `pos_sync` | Last POS sync time per location, as in the manifest. |

Numbers are JSON numbers (whole quantities as integers). A feed that cannot be built or written sets `feed_status: failed` (`feed_publish_failed`). If it cannot be built or written to the archive, the previous feed file stays in the archive and in Drive (Drive is not sent a feed the archive lacks) and the hourly latest pair is still published. The Drive feed travels in the same set as the Drive latest pair, so a failed Drive delivery keeps the previous pair **and** the previous feed there, and `feed_status` also reads `failed`.

## Item index (schema v1)

`stock_item_index.json.gz` (dashboard v2, 28 Oct 2026) is written beside the feed on every successful run (archive and Drive), from the same verified read, gzip with a fixed header time. The private dashboard page reads it once per open or refresh for item search ("where can I get it"), item detail (per-outlet quantity, close history, last movement), low stock by reorder level, fix-up lists, downloads and the data-issues view. Quantities and item master fields only: no cost, price, document, customer or member data. The feed keeps schema v1 unchanged.

| Key | Content |
| --- | --- |
| `schema`, `schema_version` | `xb.ac2.stock_item_index`, `1`. A breaking change raises the version. |
| `job`, `run_id`, `as_of_local`, `timezone`, `quantity_rule` | As in the feed. |
| `last_close_day`, `history_from`, `history_days` | The close history range: the last 90 closes, never before `daily_close.backfill_start`. |
| `low_stock_default_threshold` | `feed.low_stock_threshold`, the page's fallback when an item has no reorder level or minimum quantity. |
| `large_balance_threshold` | `output.large_balance_threshold`, for the page's large-balance list. |
| `locations` | Location codes, referenced by index below. Labels come from the feed. |
| `item_fields`, `items` | One array per item: `code, description, barcode, group, brand, uom, active, reorder_level, min_qty`. Listed: every stock-controlled item that is active, holds a balance or moved in the history window. `barcode`, `reorder_level` and `min_qty` come from the item's base-UOM `dbo.ItemUOM` row (the UOM guard allows one UOM per item); a level of 0 or none is `null`; `group` and `brand` are the AutoCount codes. |
| `stock_fields`, `stock` | One array per item and location: `item` (index), `location` (index), `qty` (the latest balance), `last_movement` (the last ledger day up to today with any row for the key, a net-zero day included; `null` when none). Non-zero balances, plus zero balances that ever moved. |
| `history_fields`, `history` | One array per item and location: `item`, `location`, `changes` = `[[offset, close], ...]`, with offset in days from `history_from`. Offset 0 is the close on `history_from`; later entries are the days the close changed (0 when it went to zero). Closes sum from the first ledger row, exactly like the daily files. A key that is zero all through the window is left out. |
| `counts` | `items`, `inactive_without_stock_or_movement_excluded`, `non_stock_items_excluded`, `items_without_description`, `items_without_barcode`, `items_with_reorder_level`, `items_with_min_qty`, `stock_keys`, `history_keys`. Also in the run manifest under `item_index.counts`. |
| `schema_minor` (#292 S2) | Present only when `analysis.enabled` is true: `2`. The major `schema_version` stays `1`. Ten fields are **appended** to `item_fields` (every version-1 field keeps its position, and the page finds fields by name): `category` (`ItemCategory`), `size` (normalised), `size_raw`, `size_order` (sort key), `size_class` (`letter`, `one_size`, `numeric`, `footwear`, `footwear_us`, `kids`, `waist`, `range`, `volume`, `other`), `length` (waist-length sizes only), `gender` (`M`, `W`, `U`, `K` or empty), `colour`, `style_code` (`ItemClass`), `style_name` (`Desc2`), then `excluded` (#292 S3b: `true` for an item in `analysis.exclude_item_codes`). `counts` adds `items_sized` (class `other` is never counted as sized), `items_with_gender`, `items_with_category`, `items_excluded`. With analysis off the file is exactly the version-1 file. |

**Excluded items in the hourly files (#292 S3b).** An item in `analysis.exclude_item_codes` (default the AutoCount placeholder `Default Item`) is **marked, not removed**, in the item index, and the latest CSV and the feed are unchanged. Why: removing it would make the feed's location totals, negative-stock counts and the error alerts disagree with AutoCount's own balances, and would change the hourly files while analysis is still off. The page leaves marked items (and its own copy of the list) out of every view and recomputes the totals it shows from the item index.

Failure handling is the feed's: a build or archive write failure keeps the previous file in both places and sets `item_index_status: failed` (`item_index_publish_failed`, exit code `1`); the hourly latest pair is still published. The Drive copy travels in the same verified set as the Drive latest pair and the feed.

Page rules that use it (documented here so the numbers can be checked): low stock = `0 < qty <= level` per outlet for active items, where level is the item's `reorder_level`, else its `min_qty`, else `low_stock_default_threshold`, and the page says which one applied. AutoCount keeps these levels per item, not per outlet, so the page applies the item's level to each outlet's quantity.

## Buying analysis files (#292 S2)

Pre-go-live AC1 history (size mix and seasonality only) is a separate one-off file built outside this exporter: see [AC1 sales history build runbook](ac1_sales_history_runbook.md).

Design: private mailbox `mailbox/292/20261108-1530-g0r2-design.md` sections 3-5 and 9 (S2). Off by default (`analysis.enabled: false`); nothing below happens until the owner switches it on at VM proof step P3.

**When.** The analysis set is due when the archive copy or the Drive copy of `analysis_manifest.json` is missing, unreadable or older than yesterday's close (`built_from_close_day` < yesterday). In practice that is the 00:00 run; a failed build or publish is retried by the next hourly run (the daily close's self-healing rule), and `--backfill` always rebuilds. Every other run records `analysis_status: not_due`. The build runs **after** every hourly file (latest pair, feed, item index, daily files) is published, so it can never delay or block them, and its outcome is recorded only in `last_run_status.json` and the analysis manifest.

**How it reads.** One grouped `SELECT` on its own connection (identity checked again; same isolation and lock timeout): `dbo.StockDTL` summed per ItemCode, Location, day and DocType for the configured sales, returns and receipt DocTypes, `DocDate >= <window start> AND DocDate < <today 00:00>`, with `LEFT JOIN`s to the `IV`, `CN` and `DO` headers on `DocKey`. The debtor code is compared **inside SQL Server** (`CASE <debtor> WHEN ? THEN ? ... END`, parameters from `analysis.channel_by_debtor`), and only the matched channel id and a has-a-debtor flag come back: the code never reaches Python, a file or a log. No cost, price or amount column, no DocNo, no free text. Stock-state and placement come from the hourly ledger read already in memory. The query text depends only on how many debtor rules and DocTypes are set; its hash is in the run manifest (`source.queries_sha256.analysis_sales`). It sits outside the hourly ChangeCount window on purpose; it reads rows dated up to yesterday only, and the next nightly build picks up any later backdated posting.

**Files** (all latest-only; data files gzip with a fixed header time; published to the archive, then to Drive, each as one verified set with the manifest last; a reader never mixes two builds):

| File | Schema | Content |
| --- | --- | --- |
| `analysis_sales_daily.json.gz` | `xb.ac2.analysis_sales_daily` v1 | Columnar: `items`, `locations`, `channels`, `groups` dictionaries; `columns` = `item`, `location`, `day` (offset from `day0`), `channel`, `sold` (units out net of returns; **negative when returns exceed sales**, kept as it is), `returned` (units back: CN rows and positive sales rows), `group` (the item's `ItemGroup`, an index into `groups`). Window: from the first ledger row, capped at `sales_window_days` (400). `reconcile` = `[doc_type, location, month, net_out, qty_in]` for the AC3 check. Non-stock items and `exclude_item_codes` items excluded and counted (`non_stock_rows_excluded`, `excluded_item_rows`). |
| `analysis_stock_state.json.gz` | `xb.ac2.analysis_stock_state` v1 | Per item and pool: `first_stock_day`, `zero_intervals` (`[[from, to], ...]`, inclusive day offsets whose pool close was zero or negative after the pool first held stock; closes sum from the first ledger row, like the daily files), `qty_now` (yesterday's close), `opening_at_golive` (close of `golive_day`), `received` per month (receipt DocTypes, net), `group` (index into `groups`). Lets the page count in-stock and zero days for any window. |
| `analysis_placement.json.gz` | `xb.ac2.analysis_placement` v1 | Per configured pair, zero location and item, over the last 90 days: `zero_qty_now`, `holder_qty_now`, `zero_days_30`, `zero_days_90`, `open_now`, `sold_at_zero_loc_90`, `group` (index into `groups`). A day counts when the zero location's close is <= 0 after it first held the item, while the holder locations together hold >= `placement.min_hold`. Rows with no such day are left out. |
| `analysis_highlights.json.gz` | `xb.ac2.analysis_highlights` v1 | `lines` = `{rule, section, group, brand, item, inputs}`. `section` is `main` for an item whose `ItemGroup` is in `highlights.main_groups` (default `XB`), else `other` (for example consignment `CSGN`); the main section comes first. Top `highlights.top_n` per rule, brand and **group section** (main groups together, each other group on its own, so consignment never crowds out main stock); `counts` per rule before the cap, plus `lines_by_section`. Rules: `sold_out_early` (first zero within `sold_out_days` of first stock, or sell-through >= `sell_through_pct`), `open_placement` (open now with at least `placement_min_sold` units sold at the zero location in the placement window; demand rows always rank first, then most zero days; `counts.open_placement.open_below_min_sold` counts the open rows left out; `placement_min_sold: 0` turns the filter off and lists **every** open row, including rows whose net sold is negative), `size_drift` (sold vs stocked size share differs by >= `drift_pts` with >= `drift_min_units` units in the share basis, per group section, brand, category and gender, since `stock_take_day`; a size whose net sold is negative keeps its real `sold` but counts as 0 for the shares (`sold_for_share`, `n_share_units`) and carries `flags: ["net returns in window"]`; `n_sold` is the real net total), `slow_seller` (pool stock > 0, no sale in `slow_days`, first stock more than `slow_days` ago). stocked = opening at go-live (if positive) + received in the pool. Sales count only `highlights.channels`. |
| `analysis_manifest.json` | `xb.ac2.analysis_manifest` v1 | `run_id`, `as_of_local`, `built_from_close_day`, `built_at`, `files` (name, schema, version, sha256, bytes, counts), `windows_hint` (go-live, stock take, sales and placement windows), `sold_units_by_channel`, `other` (groups and units the rules put in `other`), `size_coverage` per brand (items, sized items, items with an unrecognised size, sold units, sold units of sized items; class `other` is never counted as sized), `attributes` (sized items, unknown gender values, up to 50 unrecognised size values with counts, to complete the size map), `excluded_items` (counts only: codes configured, items in the master, analysis rows and ledger movement keys left out), `main_groups`, `groups` (per `ItemGroup`: `main`, items, sales rows, sold and returned units in the window, units now per pool; a consignment group with no stock take can be negative and is shown as it is), `settings_digest` (sha256 of the analysis settings; never the values). |

**Channel rules** (settings, never code), evaluated in order for each grouped row of a sales or returns DocType:

1. an `IV`/`CN`/`DO` row whose debtor is in `channel_by_debtor` -> that channel (matched in SQL);
2. `channel_by_location[<location>][<DocType>]` -> that channel;
3. an `IV`/`CN`/`DO` row with any other non-blank debtor -> `debtor_default_channel` (`wholesale`);
4. anything else -> `other` (shown and counted in the manifest, never folded into a retail channel).

Channel ids are a closed set: `outlet`, `marketplace`, `shopify`, `wholesale`, `other`. A DO later invoiced posts no second stock row (VM data check), so OS, IV and DO all count with no double count. Internal DocTypes (adjustments, transfers, issues, receives) are not listed, so they are never sales.

**Size and gender** (`analysis.attributes`, table-driven, no description parsing): blank, a `blank_values` entry (`DEFAULT`) or a `size_blank_values` entry (packs such as `PKG` or `BOX`; the colour is kept) is "not sized"; `size_map` first (`2XL` -> `XXL`, `O.S`/`OS`/`F`/`FREE` -> `OS`, ...), then `letter_order` (with the combined `S/M`, `M/L`, `L/XL`), then numbers, prefixed numbers (`size_prefix_class`: `J8` -> `kids`, `US9.5` -> `footwear_us`) and numbers with a unit (`size_unit_class`: `140CM` takes the category class like a plain number; `20L`, `0.5L`, `21OZ` -> `volume`); a pair `A-B` (also when it is a `size_map` target) in a category whose `size_class_by_category` is `waist` becomes size `A` plus length `B`, elsewhere one `range` size, and a `length_codes` entry as `B` (`32-R`) always gives size `A` plus length `B`; numeric sizes take their class from the category, and in a `footwear` category numbers below `footwear_cm_min` (18) are US/UK sizes (`footwear_us`) while the rest stay cm (`footwear`); anything else stays as given with class `other` and is listed in the manifest. Gender: `gender_map` (`M`/`MEN` -> M, `F`/`WOMEN` -> W, `BOTH` -> U); `kids_categories` are always K.

**Failure handling.** `analysis_read_failed` (connection, identity, lock or SQL error), `analysis_build_failed` (a builder error) and `analysis_publish_failed` (an archive or Drive write or verify failure) set `analysis_status: failed` and exit code `1`, like the feed and item index. The hourly latest pair, feed, item index and daily files are already published and are never touched; the previous analysis set stays where it was not replaced; the next run retries. A failed hourly run gives `analysis_status: skipped_failed_run` and no analysis read. Error alerts are unchanged (no analysis condition yet).

**Size and time.** Measured offline on the server PC with a generated fake book (25,000 items, 142,000 ledger keys, 32,600 sales groups, 6 locations, 4 pools, 3 pairs): build 3.3 s; sales 115 KB, stock state 233 KB, placement 47 KB, highlights 24 KB gzipped. Real sizes and the 00:00 run time are measured at VM proof step P4 (target: every file under 1 MB gzipped, the 00:00 run under 15 s).

### Analysis settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `analysis.enabled` | `false` | Builds the analysis set nightly and adds the item attributes. Switched on only at VM proof step P3. |
| `analysis.golive_day` | `"2026-07-31"` | Opening stock day; `opening_at_golive` is the close of this day. |
| `analysis.stock_take_day` | `null` | The stock-take reset day: the page's default window and the `size_drift` window. Set at P1. |
| `analysis.sales_window_days` | `400` | Sales window cap, 1 to 400 days back from yesterday. |
| `analysis.exclude_item_codes` | `["Default Item"]` | Item codes left out of every analysis file (matched without regard to case) and counted in the manifest's `excluded_items`. Marked `excluded` in the item index; the feed and latest CSV are unchanged. `[]` turns it off. |
| `analysis.doc_types` | `sales` OS, IV, DO; `returns` CN; `receipts` GR, PI | DocType codes read. A DocType may be listed once. Only `IV`, `CN` and `DO` have a debtor join (fixed in code). |
| `analysis.channel_by_location` | `{}` | `location -> {DocType -> channel}`. |
| `analysis.channel_by_debtor` | `{}` | `debtor code -> channel`. Lives only in the VM's local settings file; used only as SQL parameters; never written to any output, log or message. |
| `analysis.debtor_default_channel` | `"wholesale"` | Channel for a debtor row that matched no other rule; `null` sends it to `other`. |
| `analysis.pools` | `{}` | `pool id -> locations`. Required when enabled. Locations in no pool (transit, defective) never count. |
| `analysis.placement.min_hold` | `2` | Holder units needed for a placement day. |
| `analysis.placement.pairs` | `[]` | Up to 20 `{id, label, zero_locations, holder_locations}`. Each zero location is checked on its own; the holder locations are summed. A location may not be on both sides. |
| `analysis.highlights.*` | `pool` `retail_network`, `channels` outlet + marketplace + shopify, `main_groups` `["XB"]`, `top_n` 20, `placement_min_sold` 1, `sold_out_days` 28, `sell_through_pct` 80, `drift_pts` 3, `drift_min_units` 100, `slow_days` 60 | Highlight rules (design 5.3). `pool` must be one of `analysis.pools`. `main_groups` are the `ItemGroup` codes in the `main` section; every other group gets its own section. `placement_min_sold: 0` lists every open placement row. |
| `analysis.purchasing.enabled` | `false` | #292 S4. Adds `analysis_purchasing.json.gz` to the nightly set (needs `analysis.enabled` too). Switched on only at VM proof step Q4. |
| `analysis.purchasing.stale_days` | `60` | A PO line is stale when it still has open units and its PO date is more than this many days before the close day (1-365). |
| `analysis.attributes.*` | see the script | `size_map`, `letter_order`, `blank_values`, `size_blank_values`, `size_prefix_class`, `size_unit_class`, `length_codes`, `footwear_cm_min`, `gender_map`, `kids_categories`, `size_class_by_category`. Extend the size map from the manifest's unrecognised list. |

Nested blocks are closed sets merged over the defaults; validation messages name the setting path only, never a code or value. While `analysis.purchasing.enabled` is `false` its block is left out of the analysis `settings_digest` and of the hourly manifest's `source.settings_sha256` (also printed by `--dry-run-config`), so neither changes when this code is pulled. Both change once purchasing is switched on.

### Purchasing file (#292 S4)

Design: private mailbox `mailbox/292/20261108-1530-g0r2-design.md` sections 3, 6 and 9 (S4), with the VM data check report `mailbox/292/20261108-1640-vm-g0r2-data-check-report.md` (A1-A4, B1-B3). Owner decisions: vendor names shown, units only, no PO numbers or prices (Q1); no money figures anywhere (Q2); brand = `ItemBrand`.

**How it reads.** With `analysis.purchasing.enabled` (and `analysis.enabled`), two more grouped `SELECT`s run on the analysis connection, right after the sales read, with the same window end (`DocDate < <today 00:00>`):

- **PO lines**: `dbo.PODTL` joined to `dbo.PO` on `DocKey`, summed per item, vendor and PO day: ordered (`Qty`), transferred (`TransferedQty`, the units received against the line), open (`Qty - TransferedQty` per line, never below 0), lines, open lines and over-received lines.
- **GR lines**: `dbo.GRDTL` joined to `dbo.GR` on `DocKey`, summed per item, vendor, GR day, link flag and lead days. A GR line is **linked** when it was copied from a PO: `FromDocType = 'PO'` and `FromDocNo` matches a not-cancelled `PO.DocNo` (`FromDocDtlKey` is empty in this book). Lead days = GR document date minus the linked PO document date.
- The vendor label is one name per creditor (the largest `CreditorName` the creditor carries on its PO and GR headers), so a supplier never shows as two vendors because of one retyped name. The creditor code and the PO/GR numbers are compared **inside SQL Server only** and never come back to Python, a file or a log. No price, cost, amount, tax or discount column is read. Cancelled documents (`Cancelled = 'T'`) are left out.

AutoCount facts this rests on (VM data check, 8 Nov 2026): no supplier ETA (`PODTL.DeliveryDate` copies the PO date) and no PO status column, so "open" is computed from the line quantities with a stale-age flag; consignment purchases are not stock documents and are not read; a purchase invoice (PI) that carries its own stock row is rare and is not in this file (it stays in the stock-state `received` months).

| File | Schema | Content |
| --- | --- | --- |
| `analysis_purchasing.json.gz` | `xb.ac2.analysis_purchasing` v1 | `rules` (plain-language definitions), `stale_days`, dictionaries `vendors` (names), `items` (codes), `item_brand` (index into `brands`), `brands` (the item master's `ItemBrand` only, blank when it has none: the same brand as the page's Buying tab, so an item's ordered, received and sold units sit under one brand; the buying files keep their own brand rule), `groups`. `po_rows` = `item, vendor, po_day, ordered, transferred, open, lines, open_lines, over_lines, age_days, stale, group` (one row per item, vendor and PO day; `age_days` = close day minus PO day; `stale` = open units and age above `stale_days`; the PO month is the first 7 characters of `po_day`). `gr_rows` = `item, vendor, month, received, linked, lines, group`. `lead_rows` = `group, vendor, brand, lines, positive_lines, le0_lines, p25, median, p75, max, hist` per group, vendor and brand (`hist` = `[[lead_days, lines], ...]` so the page can recompute the spread for any group mix; the quantiles use linear interpolation between closest ranks over lines with a lead above 0; lines with 0 or fewer days are counted in `le0_lines` and left out of the spread); `lead_all` = the same for every linked line. `size_rows` = `group, brand, category, gender, size, size_order, size_class, ordered, received` (sized items only; `not_sized_units` counts the rest). `vendor_rows` = `vendor, brands, items`. `reconcile_po` = `month, lines, ordered, transferred, open, open_lines` and `reconcile_gr` = `month, lines, received, linked_lines, linked_units` for the Q5 check. `counts`. |

The analysis manifest gains a `purchasing` block (only while purchasing is on): `status` (`published` or `failed`), `failure_class`, and the coverage figures: vendors, PO lines, ordered / transferred / open units, open and **stale** lines and units, over-received lines, GR lines and units, linked and unlinked GR lines and units, the **GR-to-PO link rate** (by lines and by units), lead-day lines (all, above 0, 0 or less) and `lead_coverage_pct`, stock items, items with a PO (`items_with_po_pct`), items with a GR and the share of those that also have a PO (these count stock items of the item master only; `codes_not_in_item_master` counts PO or GR codes the master does not know), and the non-stock and excluded lines left out. Non-stock items and `exclude_item_codes` items are left out of the purchasing file as they are from the other files.

**Failure handling.** A purchasing read failure (`purchasing_read_failed`: a missing column, a lock or SQL error) or build failure (`purchasing_build_failed`) leaves the purchasing file **out of that night's set**: the buying files and the manifest still publish, the manifest's `purchasing.status` is `failed` with its class, `last_run_status.json` shows `analysis_purchasing_status: failed` and `analysis_purchasing_failure_class`, and the exit code is `1`. The old purchasing file may stay in the folder, but the new manifest does not list it, so the page does not read it. While purchasing is on, the set stays due until a purchasing block is published, so the next hourly run tries again (the same self-healing rule as the rest of the analysis set). With purchasing off, nothing is read, `last_run_status.json` has no purchasing fields, the manifest has no `purchasing` block and the analysis files are byte-for-byte what they were before this change.

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
- `last_run_status.json`, the run's console output and `--check-latest` carry `large_balance_count` (`null` for a failed run, or for a latest manifest written before this check existed). Exit codes do not change. When error alerts are on, a count above 0 raises the `large_balance` alert (count only; see [Error alerts](#error-alerts-email-through-n8n)).
- The CSV is unchanged: the flagged rows are exported as AutoCount holds them.

What to do when `large_balance_count` is above 0:

- [ ] Open `latest_manifest.json` (or the hourly `manifest.json`) and read `large_balances.rows`.
- [ ] For each item and location, open the item's stock movement history in AutoCount (for example a stock card report for that item and location) and find the document behind the balance, typically a stock transfer, adjustment or receive whose quantity looks like a code or a typo.
- [ ] If it is a keying error, have the document corrected in AutoCount. The next hourly run picks up the corrected balance; the job never edits AutoCount.
- [ ] If the balance is real, leave it, and consider raising `output.large_balance_threshold` in the local settings file.
- [ ] Record what was found in the private mailbox, never in this public repository (item codes and document numbers are business data).

## Failure classes (what the owner sees in Drive)

A failed run writes `manifest.json` and `alert_record.json` only (no CSV), never touches the latest pair, and updates `last_run_status.json` to `failed`. In Drive the owner sees `last_run_status.json` with `status: failed`, while `latest_manifest.json` still shows the last good `as_of_local`.

One exception: a run that fails only at its own manifest write (`write_failed` after the hourly CSV was written) has already done the daily close and retention by then. Any due daily files are published (archive and Drive), the close state is saved, and expired local files may have been pruned; the latest pair and the feed are not touched. The daily files are complete and verified, so nothing needs undoing; the next run is `not_due` for that day.

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
| `uom_guard` | A non-base UOM or a multi-UOM item exists (balance rows or ledger groups). | Map the UOM before the export can continue. |
| `ledger_mismatch` | The ledger summed over all dates does not equal the stored balance (see [Ledger check](#ledger-check-every-run)). Nothing is published. | Wait for the next hour; if it repeats, report the `ledger_check` counts. |
| `daily_close_write_failed` (in `daily_close_failure_class`; `status` stays `success`) | A daily file could not be written or did not verify, or the close state could not be saved. The hourly latest pair and feed are still published. | Check disk space, permissions and whether the sync client holds a file. The next run retries. |
| `item_index_publish_failed` (in the manifest `item_index`; `item_index_status: failed`) | The item index could not be built, written to the archive, or delivered to Drive. The previous index stays where it was not replaced (see [Item index](#item-index-schema-v1)). | Report the run reference. |
| `feed_publish_failed` (in the manifest `feed`; `feed_status: failed`) | The feed could not be built, written to the archive, or delivered to Drive. The previous feed stays where it was not replaced (see [Dashboard feed](#dashboard-feed-schema-v1)). | Report the run reference. |
| `analysis_read_failed`, `analysis_build_failed`, `analysis_publish_failed` (in `analysis_failure_class`; `status` stays `success`) | The nightly analysis set could not be read, built or published (see [Buying analysis files](#buying-analysis-files-292-s2)). The hourly files are published; the previous analysis set stays. | The next run retries. If it repeats, report the run reference. |
| `sync_read_failed` | SyncRegistration read failed (for example the wrong time column). | Check `source.sync_registration_time_column`. |
| `source_error` | Connection or other read failure. | Check SQL Server and the driver. |
| `write_failed` | The hourly CSV or manifest could not be written or re-read. | Check disk space and folder permissions. |
| `latest_publish_failed` | Hourly data is good but the latest pair could not be replaced (`last_run_status.json` only). The pair is replaced in two steps (CSV, then manifest). If only the second step fails, the new `stock_balance_latest.csv` sits beside the previous `latest_manifest.json`, so `--check-latest` exits `3` (invalid) until the next successful hour replaces both. | Check whether a program (for example the sync client) holds the latest files open. The hourly folder is complete either way. |
| `internal_error` | Unexpected error. The message has the run reference only. | Report the run reference. |
| (no new status) | The task did not run at all: `finished_at` in `last_run_status.json` is older than about 70 minutes and no new hourly folder appears (VM off or asleep, task disabled, or for pair (b) the Windows password changed). | Check the VM is on, then `Get-ScheduledTaskInfo -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` (`LastRunTime`, `LastTaskResult`). For pair (b) after a password change, update the task's stored password (needs owner OK). No alert is sent for this (owner decision 29 Oct 2026: errors only, no heartbeat); the dashboard's status badge shows the staleness. |

## Read identity and scheduled logon

- Owner decision (23 Oct 2026): the owner's own admin account, with any password read from Windows Credential Manager at runtime. That account is privileged (sysadmin or db_owner), so the local settings must set `allow_privileged_identity: true`. Every manifest then carries `identity.privileged: true` and a warning.
- Exactly two identity and logon pairs are supported. Which one applies is decided at V4. Settings validation refuses any other pair, and the installer registers the logon type from the validated settings.

| Pair | Settings | Task registration | How the job signs in |
| --- | --- | --- | --- |
| (a) Windows account | `identity.auth_mode: "windows_integrated"`, `schedule.logon_type: "S4U"`, no `sql_login` or `credential_target` | `LogonType S4U`: no password is stored | SQL Server sees the task's Windows account (`Trusted_Connection`). S4U gives the task no network access and no access to Credential Manager secrets, which is fine for SQL Server on the same VM. |
| (b) SQL login | `identity.auth_mode: "sql_login"`, `schedule.logon_type: "Password"`, `identity.sql_login`, `identity.credential_target` | `LogonType Password`: the installer asks for the Windows account's password with `Get-Credential` and passes it to `Register-ScheduledTask -User/-Password`. It is never echoed, logged or written to a file. | The job reads the SQL login's password from Windows Credential Manager under that same Windows account at runtime. Save the credential while signed in as that account, for example `cmdkey /generic:<credential target> /user:<sql login> /pass` (prompts for the password). |

- `sql_login` under `S4U` is refused at validation: an S4U task cannot read Credential Manager, so every hour would fail as `credential_unavailable`. `windows_integrated` under `Password` is refused too, because it would store a Windows password for no reason.
- Whatever the pair: the task runs at `RunLevel Limited`. For an admin Windows account that is a filtered token, so membership that comes only through the local Administrators group is not used. The V7 supervised run proves the pair works under the scheduled context; if it does not, do not raise the run level without a separate owner OK.
- The least-privilege option remains available: a dedicated SQL login with `SELECT` only on the seven tables read (`ItemBatchBalQty`, `StockDTL`, `Item`, `ItemUOM`, `Location`, `ChangeCount`, `SyncRegistration`), plus `IV`, `CN` and `DO` when `analysis.enabled` is on, and `allow_privileged_identity: false`.
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
     `-ExecutionPolicy Bypass` applies to this one command only; it does not change the VM's PowerShell settings. `-RepoRoot` is not needed (the installer uses the clone it is in). The task goes in the `\XB\` folder of Task Scheduler; use `-TaskPath "\Other\"` only if the owner asks for another folder (it must start and end with `\`). For a folder name with a space, double the last backslash (`-TaskPath "\XB\Stock export\\"`) or avoid the space: Windows PowerShell 5.1 otherwise passes the closing `\"` as a literal quote and swallows the next argument (the installer then refuses the path, so this fails safe). The installer refuses if a task with the same name is already in that folder, and warns if one is in the Task Scheduler root (see "Replace the paused root task" below). The dry run also prints the one hourly trigger it built.
  3. Note `finished_at` in `<archive_root>\last_run_status.json` (if present), then run `Start-ScheduledTask -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` straight away, before the first hourly trigger.
  4. Wait until `Get-ScheduledTaskInfo -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` shows a new `LastRunTime` and the task is no longer running (at most 15 minutes).
  5. Pass only if `last_run_status.json` is newer than the noted value and shows `status: success` (and `delivery_status: handed_to_drive_sync_folder` once `delivery_root` is set). This also proves `PythonExe`, the environment values, the identity pair and the run level under the task account.
  6. Otherwise run `Disable-ScheduledTask -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance"` at once and report the `failure_class`. The task is left enabled only after a pass.

### Replace the paused root task (VM step D5, needs owner OK)

An earlier installer registered `XB AC2 Hourly Stock Balance` in the Task Scheduler root (`\`) with a daily 00:00 start plus a one-day hourly repeat, which started 00:00 twice (28 Oct 2026); it is now disabled. **Do not move it by exporting its XML**: that would copy the old double trigger. Replace it instead (elevated Windows PowerShell 5.1, at a quiet minute):

1. Confirm the root task is still disabled: `Get-ScheduledTask -TaskPath "\" -TaskName "XB AC2 Hourly Stock Balance"` shows `State: Disabled`.
2. Unregister it: `Unregister-ScheduledTask -TaskPath "\" -TaskName "XB AC2 Hourly Stock Balance" -Confirm:$false`. (It was registered from an elevated window, so only an elevated window can change it.)
3. Run the installer dry run as in V7 step 2. It must print `Trigger: one trigger, starts 00:00, repeats every PT1H, no end (exactly one run per hour; no daily trigger)` and no root-task warning.
4. Register for real (same command without `-DryRun`), then do the supervised run, V7 steps 3-6.
5. Check: `Get-ScheduledTask -TaskName "XB AC2 Hourly Stock Balance"` lists only the `\XB\` task; `(Get-ScheduledTask -TaskPath "\XB\" -TaskName "XB AC2 Hourly Stock Balance").Triggers` shows one trigger with `Repetition.Interval PT1H` and an empty `Repetition.Duration`. Record the result in the private mailbox.

## Round 6 VM proof steps (D1-D6)

Each step **needs owner OK**; VM Claude runs them. None has been run. Results (counts, sizes, times) go to the private mailbox only.

- **D1 update** (needs owner OK): pull the merged code into the VM clone, add the round-6 sections to the local settings file (`daily_close`, `retention` with `prune_enabled: false`, `feed`, `rows.exclude_non_stock_items: true`), point `output.delivery_root` at the renamed folder from D4 (or do D4 first), and run `--dry-run-config`.
- **D2 backfill** (needs owner OK): one supervised manual run with `--backfill` (read-only SQL, writes the archive and Drive). Pass when: `status: success`; `ledger_check.result: pass`; `daily_close.status: published` with `days_written` = days from `backfill_start` to yesterday; `published_close_day` = yesterday; no `hourly\` folder appears in Drive; the feed is in Drive beside the latest pair. Record daily CSV size, feed size and run time. Then the owner's one-off spot check (design 2.2 (3)): AutoCount stock balance report "as at" 31 Aug and 30 Sep 2026 (location totals and 20 SKUs) against `stock_close_2026-08-31.csv` and `stock_close_2026-09-30.csv`, and the known transfer correction recorded in the private mailbox (design 2.2 (4)).
- **D3 retention** (needs owner OK): set `retention.prune_enabled: true`, run once, and check `retention` in the manifest: only local hourly folders older than 48 h and daily files older than 90 days removed, `errors: 0`, nothing removed from Drive or outside the archive.
- **D4 Drive folder rename** (needs owner OK, Drive change): rename `Report/AC2 Stock Balance (hourly)` to `Report/AC2 Stock Balance (daily)` and set `output.delivery_root` to the new local path. Check Drive for desktop still syncs it.
- **D5 task** (needs owner OK, VM change): replace the paused root task with the `\XB\` task, see [Replace the paused root task](#replace-the-paused-root-task-vm-step-d5-needs-owner-ok).
- **D7 dashboard v2 update** (needs owner OK, after the dashboard v2 PR is merged): first run step 6 of `sql/autocount2/stock_balance_hourly_live_check.readonly.template.sql` (column check; all five lengths must be non-NULL), then pull main into the VM clone, run `--dry-run-config`, and one supervised manual run (no `--backfill`). Pass when `status: success`, `item_index_status: published`, `stock_item_index.json.gz` is in Drive beside the feed, and `item_index.counts` look sane. Record the item index size and run time in the private mailbox. No settings change is needed.
- **D6 first 00:00 close** (needs owner OK to leave it enabled): after the first midnight, check `last_run_status.json` from the 00:00 run: `daily_close_status: published`, `published_close_day` = the day before, exactly one task start at 00:00 (`Get-WinEvent` Task Scheduler history or one 00:xx hourly folder), and the 01:00 run shows `daily_close_status: not_due`.

## #292 S2 VM proof steps (P1-P7)

Each step **needs owner OK**; VM Claude runs them after the S2 PR is merged. None has been run. Results (values, counts, sizes, times) go to the private mailbox only, never to this repository.

- **P1 settings values** (needs owner OK, after the owner's VM data check report): fill the real `analysis` block in the VM's local settings file: `channel_by_location` and `channel_by_debtor` (data check B4), `pools` (B6), the three placement pairs (design Q5), `stock_take_day`, and `attributes` (`size_map` additions, `kids_categories`, `size_class_by_category` from B5). Keep `enabled: false` for now and run `--dry-run-config`.
- **P2 live check** (needs owner OK): run steps 7 and 8 of `sql/autocount2/stock_balance_hourly_live_check.readonly.template.sql`. Step 7: every length must be non-NULL. Keep the step 8 output privately for P5.
- **P3 switch on** (needs owner OK): pull main into the VM clone, set `analysis.enabled: true`, run `--dry-run-config`.
- **P4 first build, AC8** (needs owner OK): one supervised manual run (no `--backfill`; the first enabled run is due anyway). Pass when `status: success`, every hourly status as before, `analysis_status: published`, `analysis_close_day` = yesterday, the five analysis files in Drive beside the feed, and `item_index` shows `schema_minor: 2` with sane `items_sized`. Record each file's `bytes` (from `analysis_manifest.json`), `analysis_seconds` and the whole run time; every file must be under 1 MB and the run under 15 s, otherwise G2 decides. Check the manifest's `other` units and `unrecognised_size_values`, and extend the settings if needed.
- **P5 AC3 reconciliation** (needs owner OK): compare the sales file's `reconcile` rows with live-check step 8 per DocType, location and month (months from `day0` to yesterday); every `net_out` and `qty_in` must match. Then check that the channel units in the manifest add up to the sales and returns DocType totals.
- **P6 AC4 stock-state check** (needs owner OK): pick 20 item-pool keys (mix of pools, some with zero intervals), and for the last 30 days sum each pool's locations from the daily close CSVs (`stock_close_<day>.csv`, a missing row = 0). The days with a pool total <= 0 after `first_stock_day` must equal the days inside `zero_intervals`, key by key.
- **P7 first 00:00 build** (needs owner OK to leave it on): after the first midnight, the 00:00 run shows `analysis_status: published` with `analysis_close_day` = the day before, and the 01:00 run shows `not_due`. Rollback at any point: set `analysis.enabled: false` (the hourly files and the item index return to exactly version 1; the last analysis files stay in Drive until removed by hand with the owner's OK).

## #292 S4 VM proof steps (Q1-Q6)

Each step **needs owner OK**; VM Claude runs them in this order after the S4 PR is merged. None has been run. Results (values, counts, vendor names, sizes, times) go to the private mailbox only, never to this repository. Never edit the settings file, pull or switch branches between :55 and :05. Stop at the first step that does not pass: do its rollback, report, and do not start the next step.

The order matters: the code that knows `analysis.purchasing` is pulled **before** the block is added. Code from before this change rejects the block (`ConfigError: analysis has unknown setting(s)`), so a settings file with the block on old code fails every hourly run until the block is removed. For the same reason, a code rollback (Q1's) is only safe once the settings file has no `purchasing` block again.

- **Q1 pull the code, purchasing absent** (needs owner OK).
  - Do: run `--dry-run-config` and record its `settings_sha256`; record `git rev-parse HEAD` of the VM clone; pull main into the VM clone (not between :55 and :05). Leave the local settings file as it is (no `purchasing` block). Run `--dry-run-config` again, then let the next scheduled run complete (or one supervised manual run, no `--backfill`).
  - Pass: `--dry-run-config` prints `status: valid` with the **same** `settings_sha256` as before the pull; the run shows `status: success` and every hourly status as before; `last_run_status.json` has no `analysis_purchasing_*` field; the analysis manifest has no `purchasing` block and lists no `analysis_purchasing.json.gz` (off changes nothing).
  - Stop if: `--dry-run-config` fails, the hash differs, the run fails, or any purchasing field or file appears.
  - Rollback: `git checkout <the recorded HEAD>` in the VM clone (not between :55 and :05), run `--dry-run-config` and check the recorded hash. The settings file was not touched.
- **Q2 live check** (needs owner OK; read-only).
  - Do: run step 9 of `sql/autocount2/stock_balance_hourly_live_check.readonly.template.sql` (three SELECTs).
  - Pass: every length is non-NULL; both `Cancelled` columns have a character type (`char`, `nchar`, `varchar` or `nvarchar`); the per-value counts show only `F` and `T` for `PO` and for `GR` (a `NULL` row is allowed: it is read as not cancelled).
  - Stop if: any length is NULL, a `Cancelled` column is not a character type, or any other `Cancelled` value appears. Report it: do not add the block or switch purchasing on.
  - Rollback: none needed (SELECT only; nothing changed). The Q1 code may stay: with no `purchasing` block it behaves as before.
- **Q3 add the block, still off** (needs owner OK).
  - Do: back up the local settings file (a copy beside it with the date in its name). Add `"purchasing": {"enabled": false, "stale_days": 60}` to the `analysis` block. Run `--dry-run-config`.
  - Pass: `status: valid`, and `settings_sha256` equal to Q1's (an inert block is left out of the hash).
  - Stop if: `--dry-run-config` fails or the hash differs.
  - Rollback: restore the backup **at once** (before the next :00 run), run `--dry-run-config` and check that it prints Q1's hash.
- **Q4 switch on and first build** (needs owner OK).
  - Do: set `analysis.purchasing.enabled: true` (not between :55 and :05) and run `--dry-run-config` (`status: valid`; the hash now changes, as expected). Then one supervised manual run, no `--backfill`: with purchasing on, the analysis set is due again while the published manifest has no published `purchasing` block, so this run rebuilds it the same day.
  - Pass: `status: success`, every hourly status as before, `analysis_status: published`, `analysis_purchasing_status: published`, `analysis_purchasing.json.gz` in Drive beside the other analysis files, and the manifest lists it with a matching `sha256` and `bytes`. Record its `bytes` (under 1 MB), `analysis_seconds` and the whole run time (under 15 s). The manifest's `purchasing` block shows a link rate and stale count close to the data check (B1, B2).
  - Stop if: `--dry-run-config` fails, or the run shows `analysis_purchasing_status: failed` (report `analysis_purchasing_failure_class`), a file over 1 MB, a run over 15 s, or any hourly status that changed.
  - Rollback: set `analysis.purchasing.enabled: false` (or restore the Q3 settings) and run `--dry-run-config` (Q1's hash again). The set already published today keeps its purchasing entry until the next 00:00 build, which has no purchasing file in its manifest and leaves every other file unchanged. While it fails, the set is retried every hour (exit 1, no alert; an alert is tracked in #288), so do not leave a failing switch-on in place.
- **Q5 reconcile, AC3** (needs owner OK; read-only).
  - Do: run step 10 of the live check **straight after the Q4 build, before new purchase documents are keyed**. `TransferedQty` is live: a GR or PO keyed after the build changes 10a's transferred and open figures, also for older months. If documents were keyed in between, expect differences in the months they touch, and check those months again against the next 00:00 build (Q6).
  - Pass: every `reconcile_po` row equals 10a month by month (lines, ordered, transferred, open, open lines); every `reconcile_gr` row equals 10b (lines, received, linked lines, linked units); the manifest's `purchasing.stale_lines` and `stale_units` equal 10c (with the same `stale_days`). Then compare each `reconcile_gr` month's `received` with the `GR` rows of step 8 (`qty_in`, all locations): they differ only by GR lines whose unit of measure is not the base unit, and any difference must be reported.
  - Stop if: a month differs for a reason other than documents keyed after the build, or the step 8 comparison shows a difference. Report it; purchasing stays on only with the owner's OK.
  - Rollback: as Q4 (set `analysis.purchasing.enabled: false`).
- **Q6 first 00:00 build** (needs owner OK to leave it on).
  - Do: after the next midnight, read `last_run_status.json` from the 00:00 and 01:00 runs.
  - Pass: the 00:00 run shows `analysis_purchasing_status: published` and the 01:00 run `analysis_status: not_due`. Any month Q5 left open now matches step 10 run straight after this build.
  - Stop if: the 00:00 run shows `analysis_purchasing_status: failed` (report the failure class) or a month still differs.
  - Rollback at any point: set `analysis.purchasing.enabled: false`; the next nightly set has no purchasing file in its manifest and every other file is unchanged. A code rollback (Q1's) needs the `purchasing` block removed from the settings file first.

## Retention

- Drive keeps one latest pair, the feed, the run status and one daily file pair per day. Nothing in Drive is ever pruned or deleted by the job.
- Local archive (behind `retention.prune_enabled`, default `false`; switched on at VM step D3 with the owner's OK): hourly run folders are kept `hourly_keep_hours` (48 h), daily close files `daily_keep_days` (90 days, per [Security guardrails](security_guardrails.md)). Runs every successful hour, after the close; never fails a run (`retention` in the manifest counts what was removed, kept and any errors).
- Safety rules (all tested):
  - only inside `archive_root\hourly\` and `archive_root\daily\`, and only names the exporter makes: `<YYYY-MM-DD>\ac2_stockbal_<timestamp>_<8 hex>\` with `stock_balance.csv` / `manifest.json` / `alert_record.json`, and `<YYYY-MM>\stock_close_<YYYY-MM-DD>.csv` / `.manifest.json`;
  - the age comes from the folder or file name, never from file times;
  - anything else (other names, other folders, symlinks or junctions, anything resolving outside the archive) is kept and counted as `kept_unexpected`; a folder is removed only when it is empty;
  - the current run's folder, `daily\close_state.json.gz`, the latest pair, the feed, the analysis files (including `analysis_purchasing.json.gz`), the lock and `delivery_root` are never touched.
- Before round 6 the job also wrote hourly folders into Drive; the owner removed those on 28 Oct 2026. The job does not clean Drive.

## Out of scope

- **#246** (daily money / buying package): the owner wants AutoCount's Advanced Dimensional report there (most detailed breakdown, for example marketplace shipping fees). New marketplace fee codes can land on a default term (believed to be "Adjustment") until someone adds the term in AC2, so the #246 report should flag unmapped codes. Cost and value belong in #246, not here.
- The dashboard page: a private Claude artifact that reads `stock_dashboard_feed.json.gz` and `stock_item_index.json.gz` (and, from #292 S3, the analysis files) through the viewer's own Drive connector. Its source lives in the private mailbox, not in this repository. This build only writes the files.
- Consignment stock, a heartbeat or "still alive" alert (owner decision 29 Oct 2026: errors only), Drive API upload, Drive clean-up, SQL logins and grants.

## Error alerts (email through n8n)

Owner decision (29 Oct 2026): **errors only.** No heartbeat, no "still alive" message, no success or "resolved" message. Built off by default; the n8n workflow ships inactive. Every live step is listed under [Go-live](#alert-go-live-steps-a1-a5) and needs its own owner OK.

How it works:

1. At the end of every run (after the daily close, the feed, the item index and delivery, just before `last_run_status.json` is written) the exporter works out which alert conditions the run raises, from the same values it writes to `last_run_status.json`.
2. It checks that the shared secret is readable (every run, even with no condition; nothing is sent). If the de-duplication rule says so, it POSTs **one** sanitized event to `alert.webhook_url`: one attempt, `alert.timeout_seconds` (10 s) per socket operation, no proxy, no redirect, the shared secret in the `alert.auth_header_name` header. Only the answer's status is used; its body is never read.
3. The n8n workflow `AC2 Stock Export - Error Alert` ([`n8n-workflows/ac2_stock_alert.workflow.json`](../../n8n-workflows/ac2_stock_alert.workflow.json)) checks the Header Auth secret, rejects any event outside the strict schema (400), and calls the shared sub-workflow `XB Ops Alert Email` ([`n8n-workflows/ops_alert_email.workflow.json`](../../n8n-workflows/ops_alert_email.workflow.json)) with system `AC2-STOCK`. The sub-workflow sends one plain-text email to the operations inbox and logs the outcome in the n8n Data Table `xb_alert_email_log`. The workflow answers 200 when the email was sent (or folded, see below), 502 when the email send failed, and 500 on an unexpected error. The status map (200, 400, 403, 404, 500, 502) is the same as before, so `alert_status` means what it meant.
   - Subject: `[XB alert] AC2-STOCK ERROR run_failed:<failure_class>` for a failed run. Otherwise the active conditions joined by `+`, for example `[XB alert] AC2-STOCK WARN large_balance`. It is `ERROR` unless only `large_balance` and `late_change` are active. The subject has no time in it, so repeats share one Gmail thread.
   - The sub-workflow folds a repeat of the same subject code within 60 minutes (no email; the next email says "(+k folded since HH:MM)") and sends at most 10 `AC2-STOCK` alert emails per 60 minutes (then one `[XB alert] AC2-STOCK WARN alert_flood` email per hour). The cap counts per system, so another app's alerts never hold back or join an AC2 email, and AC2's never affect theirs. The VM's own once-a-day repeat rule below is unchanged, so a fold is rare here.
4. The exporter writes the outcome as `alert_status`, with the answer's HTTP status, in `last_run_status.json` and in the run's own `alert_record.json` (see [Per-run alert record](#per-run-alert-record)). **An alert, sent or lost, never changes the run's status, exit code or other files.**

The VM reaches n8n through the public `/webhook/` path (n8n sits behind Cloudflare Access, which leaves `/webhook/` open). n8n's `WEBHOOK_URL` is not changed.

### Conditions

| Condition | Raised when (in `last_run_status.json`) |
| --- | --- |
| `run_failed` | `status: failed`, any `failure_class` (including `ledger_mismatch`). A failed run raises only this one. |
| `daily_close_failed` | `daily_close_status: failed` |
| `delivery_failed` | `delivery_status: verify_failed` or `copy_failed` (Drive did not get the latest set) |
| `feed_failed` | `feed_status: failed` |
| `item_index_failed` | `item_index_status: failed` |
| `large_balance` | `large_balance_count` above 0 |
| `late_change` | `late_change_day_count` above 0 (only on a run that publishes a close) |

Not alerted (accepted gaps): a run that never starts (VM off, task not firing; the dashboard's status badge shows the staleness); `config_invalid` and `timezone_unavailable` from `main` (both fail while the settings are loaded, so the alert settings cannot be trusted: `alert_status: skipped_config_invalid`); `--check-latest` and `--dry-run-config`, which never alert.

### De-duplication rule

- Each condition is tracked by key (`run_failed` is keyed by its failure class, so a different failure is a different key) in `alert_state.json` in the archive root (local only, never in Drive, never pruned).
- **Send** when any active key is new, or when an active key was last sent `alert.repeat_hours` (24) or more ago. The message lists every active condition, and every active key's time is reset.
- **Suppress** otherwise (`alert_status: suppressed`): the same problem alerts once, then once a day while it lasts.
- A key that is no longer active is dropped, so if it comes back it alerts as new. Recovery itself sends nothing.
- A failed run (including `already_running` and `stale_lock`) cannot see the success-side conditions (`large_balance` and the rest), so it keeps their keys and last-sent times and drops only the other `run_failed` keys. A large balance that lasts through failed hours therefore still alerts once a day, not again after every failed hour.
- A failed send leaves the state as it was, so the next hour tries again (at most one POST an hour, no retry storm). A missing or unreadable state counts as empty (one extra alert at worst). A last-sent time that is unreadable, has no UTC offset, or lies in the future (the VM clock ran ahead, then was corrected) counts as never sent.

### Event (schema `xb.ac2.stock_alert.v1`)

Exactly these keys, closed values only: `schema`, `event` (`ac2_stock_alert`), `run_id`, `run_hour_local` (the run's hour, for example `2026-10-29T14:00:00+08:00`), `alert_kind` (`new` or `repeat`), `status`, `failure_class`, `conditions` (names from the table), `daily_close_status`, `delivery_status`, `feed_status`, `item_index_status`, `large_balance_count`, `late_change_day_count`. **No item code, quantity, location, path, message, host or login**, whatever the run found; a value outside its closed set is sent as `null` or `unknown`. Example email (fake values):

```text
Subject: [XB alert] AC2-STOCK ERROR run_failed:ledger_mismatch

AC2 stock export: ERROR

- Hourly export FAILED: ledger_mismatch (nothing new published this hour)

Hour: 2026-10-29 14:00 (UTC+08:00)
Run: 0123abcd
Details: last_run_status.json in the Drive folder; runbook docs/autocount2-automation/stock_balance_hourly_runbook.md, section "Failure classes".

--
System: AC2-STOCK | Severity: ERROR | Key: run_failed:ledger_mismatch
Times are SGT (UTC+08:00). Repeats of one system:key within 60 minutes are folded; at most 10 alert emails per system per 60 minutes.
Log: n8n Data Table xb_alert_email_log. Contract: n8n-workflows/README.md, ops_alert_email.workflow.json.
```

### alert_status values

| Value | Meaning |
| --- | --- |
| `disabled` | `alert.webhook_enabled` is false. |
| `no_alert` | The run raised no condition and the secret is readable. |
| `suppressed` | Conditions active, all alerted less than `repeat_hours` ago; the secret is readable. |
| `sent` | n8n answered 2xx. |
| `send_failed` | No answer, a timeout, a malformed answer, or a non-2xx answer (400 rejected, 403 missing or wrong secret header, 404 workflow inactive or wrong path, 500 n8n error such as an empty Header Auth credential or a failed sub-workflow call, 502 email send failed). Retried next hour. |
| `secret_unavailable` | The env var (or Credential Manager target) is missing, empty or not one printable line. Checked on **every** run while alerts are on, with or without a condition, so a clean hour shows it too. Nothing was sent. |
| `error` | Unexpected error in the alert step. Nothing changes for the run. |
| `skipped_config_invalid` | Settings invalid; no alert attempted. |

### Per-run alert record

`last_run_status.json` is replaced every run, so on its own it cannot show what the alert step did in past hours. Every run therefore also writes `alert_record.json` into its own hourly folder (`archive_root\hourly\<YYYY-MM-DD>\ac2_stockbal_...\`), beside `manifest.json`:

- **Every run, success or failed**, including a run refused by the lock and a run whose settings are invalid (`skipped_config_invalid`). It is written once (exclusive create), right after the alert step and before `last_run_status.json`; it is never replaced.
- **Local only.** It never goes to Drive (no hourly folder does). With pruning on it is removed with its folder after `retention.hourly_keep_hours` (48 h); pruning runs on successful hours.
- **Not in `manifest.json`** (owner asked 3 Nov 2026 for "alert_status in the hourly manifest"): the manifest is written once and hashed before the alert runs, because the alert reads the final delivery result, and the latest pair and Drive copy are checked against that hash. Rewriting it afterwards would break that check, so the record is a sibling file in the same folder, kept and pruned exactly like the manifest.
- Fields: `schema` (`xb.ac2.stock_alert_record.v1`), `job`, `run_id`, `started_at`, `finished_at`, `status` and `failure_class` (as in `last_run_status.json`), `alert_status` (values above), `http_status`.
- `http_status`: the webhook answer's HTTP status as a number (for example `200` with `sent`; `403`, `404`, `500` or `502` with `send_failed`), or `null` when no POST was made (`disabled`, `no_alert`, `suppressed`, `secret_unavailable`, `skipped_config_invalid`) or the POST got no usable answer (`send_failed` after a timeout, network error or malformed answer; `error` before the answer). `error` after an answer keeps that answer's status. `last_run_status.json` carries the same value as `alert_http_status`.
- **Never** the webhook URL, a header, the secret or the answer's body; no path, message, item or quantity.
- If the hourly folder cannot be written (the archive disk is full or read-only), there is no record for that run, as there is no manifest; `last_run_status.json` still shows that run until the next one. A run whose settings file cannot be read, or that names no usable `archive_root`, also writes no record (and no manifest); A5's missing-hour rule catches it. Writing the record never changes the run's status, exit code or other files.

### Alert go-live steps (A1-A5)

Each step **needs a separate owner OK**; the Controller runs A1, A2 and A4 on the server PC and VM Claude runs A3 and A5. Nothing here has been run with email (the 29 Oct A1/A2 attempt was with Telegram, which this route replaces). Results go to the private mailbox only (never the secret, the operations inbox address or the webhook path).

**Before A1:** the shared sub-workflow `XB Ops Alert Email` must be live first (#288 slice 1, each its own owner OK): the Data Table `xb_alert_email_log` created, the sub-workflow imported inactive through the local API `http://127.0.0.1:5678/api/v1`, its Gmail credential bound, the operations inbox set privately in the local import copy, one test email sent, then published so other workflows can call it. See [n8n-workflows/README.md](../../n8n-workflows/README.md) (ops_alert_email.workflow.json).

**Pasting into the VM PowerShell console:** paste with a **right-click**. Ctrl+V does not paste at a `Read-Host` prompt in Windows PowerShell (it stores a control character instead), and a right-click works at the normal prompt too, so use it for every command block below.

- **A1 n8n import, inactive** (needs owner OK): import `ac2_stock_alert.workflow.json` as a new workflow, inactive, through the local n8n API `http://127.0.0.1:5678/api/v1` on the server PC. In the local import copy only: replace `REPLACE_WITH_ALERT_WEBHOOK_PATH` with a private, unguessable path; replace `REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID` with the imported `XB Ops Alert Email` workflow; create a Header Auth credential with header name `X-XB-Alert-Token` and a new random secret that the owner generates and keeps (never pasted into chat, the repo or a mailbox file). If the 29 Oct Telegram version was imported, update that workflow instead of adding a second one.
- **A2 test event** (needs owner OK, sends one email): in the editor, listen for a test event, then from the server PC POST this fake event to the local test address `http://127.0.0.1:5678/webhook-test/<private path>` with the secret header. Pass: **one email** in the operations inbox with the subject `[XB alert] AC2-STOCK ERROR run_failed:internal_error`, reading "AC2 stock export: ERROR ... Run: 00000000", and one `sent` row in `xb_alert_email_log`; a second POST with an extra key answers 400 and sends nothing; a POST without the header answers 403 and sends nothing. A repeat of the same fake event within 60 minutes answers 200 and sends nothing (logged `folded`).

  ```json
  {"schema": "xb.ac2.stock_alert.v1", "event": "ac2_stock_alert", "run_id": "00000000000000000000000000000000", "run_hour_local": "2026-10-29T14:00:00+08:00", "alert_kind": "new", "status": "failed", "failure_class": "internal_error", "conditions": ["run_failed"], "daily_close_status": "skipped_failed_run", "delivery_status": "skipped_failed_run", "feed_status": null, "item_index_status": null, "large_balance_count": 0, "late_change_day_count": 0}
  ```

  Then **activate** the workflow (the same owner OK covers activation only if it names it).

  History: the 29 Oct A2 ran with Telegram and answered 502 ("can't parse entities"). The email route sends plain text, so that failure cannot recur; A2 runs again with email.
- **A3 VM settings and secret** (needs owner OK, VM change): signed in as the task's Windows account (pair (a)), store the secret as a user environment variable without echoing it, then add the alert block to the local settings and validate:

  ```powershell
  $s = Read-Host -AsSecureString "Alert secret"
  [Environment]::SetEnvironmentVariable("XB_AC2_STOCK_ALERT_SECRET", [System.Net.NetworkCredential]::new("", $s).Password, "User")
  ```

  At the `Alert secret` prompt, paste the secret with a **right-click**: Ctrl+V does not paste at this `Read-Host` prompt in Windows PowerShell (it stores a control character instead of the secret). The next run then shows `alert_status: secret_unavailable`, because the value is not one printable line.

  Settings: `"alert": {"webhook_enabled": true, "webhook_url": "https://<n8n host>/webhook/<private path>", "secret_env_var": "XB_AC2_STOCK_ALERT_SECRET"}` (other keys default). Open a **new** PowerShell window (the variable reaches new processes only) and run `--dry-run-config`.
- **A4 one forced-failure test** (needs owner OK, sends one email): at a quiet minute (not :00), in a new window as the task's account, make one manual run fail safely by giving only that window a wrong expected book: `$env:AUTOCOUNT_EXPECTED_SQL_DATABASE = "ALERT_TEST_WRONG_BOOK"`, then the normal run command. It stops at the identity check before any business query (`identity_refused`) and writes a failed manifest and `last_run_status.json` (Drive shows `failed` until the next hourly run). Pass: **one email** with the subject `[XB alert] AC2-STOCK ERROR run_failed:identity_refused`, reading "Hourly export FAILED: identity_refused", and `alert_status: sent`. Close that window. The next **scheduled** run must show `status: success` and `alert_status: no_alert`, and `alert_state.json` must be empty again. Because the secret is checked on every run, `no_alert` here proves the scheduled (S4U) task sees the variable; `secret_unavailable` means it does not (A4's own send ran in an interactive window, which proves nothing about the task).

  If A4 gives `send_failed`: a 403 from Cloudflare (not n8n) means its bot protection blocked the request (the job sends `User-Agent: xb-ac2-stock-alert/1`); a Cloudflare rule change needs its own owner OK. n8n itself answers 403 for a missing or wrong secret header. A 502 means the email send failed: the `send_failed` row in `xb_alert_email_log` carries the code (`http_<status>` or `gmail_error`). A 500 with no log row means the sub-workflow call itself failed (for example, it is not published). `secret_unavailable` means the window or the task does not see the variable.
- **A5 first day** (read-only): after 24 hours, read every run's `alert_record.json` from the last 24 hours (see [Per-run alert record](#per-run-alert-record)); `last_run_status.json` shows only the latest run:

  ```powershell
  Get-ChildItem "<archive_root>\hourly" -Recurse -Filter alert_record.json |
    ForEach-Object { Get-Content $_.FullName -Raw | ConvertFrom-Json } |
    Where-Object { [datetimeoffset]$_.started_at -gt [datetimeoffset]::Now.AddHours(-24) } |
    Sort-Object { [datetimeoffset]$_.started_at } |
    Format-Table started_at, status, failure_class, alert_status, http_status -AutoSize
  ```

  Pass when there is one record per scheduled hour (a missing hour means no run, an unwritable archive, or unreadable settings (check the task's last result)), every record reads `no_alert` (or `sent` / `suppressed` for a real problem), none reads `secret_unavailable`, `send_failed` or `error`, and every alert email has a record with `alert_status: sent` and a 2xx `http_status` at that hour (and a `sent` row in `xb_alert_email_log`). Outbound HTTPS from the scheduled (S4U) task is proven by the first scheduled-run record that reads `sent` with a 2xx `http_status` (A4's POST came from an interactive window). Records exist only for runs on a version with this record (3 Nov 2026 follow-up) and are kept 48 h when pruning is on.

### Rollback

- **Switch off alerts** (VM settings change, needs owner OK): set `alert.webhook_enabled` to `false`. The next run shows `alert_status: disabled`. Optionally remove the variable: `[Environment]::SetEnvironmentVariable("XB_AC2_STOCK_ALERT_SECRET", $null, "User")`, and delete `alert_state.json` in the archive root.
- **Deactivate the n8n workflow** (n8n change, needs owner OK): the VM then gets 404 and records `send_failed` every hour that has a problem; runs and exit codes are unchanged. Unpublishing `XB Ops Alert Email` instead stops every caller's email at once (AC2 then answers 500).
- **Rotate the secret**: change the Header Auth credential value in n8n and the VM variable together (A3), then repeat A4.
- **Code rollback** (to a version before the per-run alert record): the older prune removes only `stock_balance.csv` and `manifest.json`, so it leaves record-only hourly folders (holding `alert_record.json`, counted `kept_unexpected`). Delete them by hand.

### Not in this round (LATER)

- A heartbeat or "resolved" message: excluded by the owner (29 Oct 2026). Revisit only if the owner asks.
- A large-balance count that grows while the condition stays active is not re-alerted until the daily repeat.
- `config_invalid` and `timezone_unavailable` are not alerted (the alert settings themselves may be what is invalid).
- The daily repeat compares exact times, so start-up jitter can push a reminder to the next hour's run.
- If the email send answers slower than the VM's timeout, the email can still arrive after the VM recorded `send_failed`, and the next hour sends it again: one duplicate at worst.

## Open questions

1. Drive for desktop folder and Google account: name them at V6.
2. Read identity: pair (a) Windows account with S4U, or pair (b) SQL login with a Password logon and the Credential Manager target. Decide at V4.
3. Items with no balance row: keep the added "(no balance row)" rows (default) or switch `zero_row_mode`.
4. Which SyncRegistration time column is right on live.
5. Online marketplace stock may include consignment for some outlets, so it can differ from this export; owner to confirm that is fine.
