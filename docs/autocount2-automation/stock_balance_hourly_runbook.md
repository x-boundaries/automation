# AC2 hourly stock balance export runbook (#245)

Status: **offline build only.** Nothing here has run against SQL Server, the AC2 VM, Google Drive or Task Scheduler. Every step under [VM proof steps](#vm-proof-steps-v1-v7) and [Round 6 VM proof](#round-6-vm-proof-steps-d1-d6) needs a separate owner OK.

Dashboard v2 (28 Oct 2026): every successful run also writes the item index `stock_item_index.json.gz` beside the feed, for the dashboard's item search, item detail and data-issues views (see [Item index](#item-index-schema-v1)). The item master read adds `ItemGroup` and `ItemBrand`, and the `dbo.ItemUOM` read adds `BarCode` and `MinQty`. Nothing else changes.

Error alerts (29 Oct 2026, owner: errors only, no heartbeat): a failed or flagged run can POST one sanitized event to an n8n webhook, which sends one Telegram message to the owner. Off by default and shipped inactive; see [Error alerts](#error-alerts-telegram-through-n8n).

Round 6 (accepted design, owner "Accept all", 27 Oct 2026): a daily 00:00 close rebuilt from the stock ledger, a backfill from 1 Aug 2026, one latest pair in Drive (no hourly folders in Drive), local retention, and a compact dashboard feed. File, setting and task names keep the word "hourly" (the job still runs hourly); only the Drive folder is renamed to "daily".

## What it does

`scripts/autocount_stock_balance_hourly.py` runs once an hour, 24/7. Each run reads the stored on-hand stock balance (`dbo.ItemBatchBalQty`, the table AutoCount itself maintains) and the stock ledger (`dbo.StockDTL`, summed per item, UOM, location and day), checks that the two agree, and writes:

- locally, an immutable hourly folder with `stock_balance.csv` and `manifest.json` (kept 48 hours once pruning is on);
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

## Files

| File | Purpose |
| --- | --- |
| `scripts/autocount_stock_balance_hourly.py` | The exporter. CLI: `--config PATH` (required), `--check-latest`, `--dry-run-config`, `--backfill`. |
| `config/autocount_stock_balance_hourly.example.json` | Committed example with fake values only. |
| `config/autocount_stock_balance_hourly.local.json` | The real settings file on the VM. Gitignored by `config/*.local.json`; never commit it. Real location labels live only here. |
| `scripts/install_autocount_stock_balance_hourly_task.ps1` | Task Scheduler installer. Not run by the build; step V7 only. |
| `sql/autocount2/stock_balance_hourly_live_check.readonly.template.sql` | Owner-run read-only live check (step V3). Aggregates only. |
| `tests/test_autocount_stock_balance_hourly.py` | Offline tests with in-memory fakes and temp folders. |
| `n8n-workflows/ac2_stock_alert.workflow.json` | The inactive n8n workflow that turns an alert event into one Telegram message (see [Error alerts](#error-alerts-telegram-through-n8n)). |
| `tests/test_ac2_stock_alert_workflow.py` | Offline contract of that workflow; runs its Code node with Node against real events. |

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

Exit codes: run `0` success (and delivery verified or not configured, the daily close published or not due, and the feed and the item index published), `1` failed, delivery not verified, daily close failed, feed failed or item index failed. A daily close, feed or item index failure never stops the hourly latest pair: `status` stays `success` and `daily_close_status` / `feed_status` / `item_index_status` say what failed. `--check-latest`: `0` fresh, `2` stale (older than `stale_after_minutes`), `3` missing or invalid; it never exits with anything else, so invalid settings (including a malformed `timezone` such as `Asia/Singapore/`), a missing zone database or a usage error also give `3`. `--dry-run-config`: `0` valid (it also prints `auth_mode`, `scheduled_logon_type` and `run_minute`, which the installer reads), `1` invalid. Console output is a short status JSON; it never prints row data, connection strings, logins, host names or paths. The run output, `--check-latest` and `last_run_status.json` also carry `large_balance_count` (see [Large-balance warning](#large-balance-warning)); it never changes an exit code. The run output and `last_run_status.json` carry `alert_status` (see [Error alerts](#error-alerts-telegram-through-n8n)); an alert, sent or lost, never changes an exit code either.

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
| `alert.webhook_enabled` | `false` | Error alerts on or off (see [Error alerts](#error-alerts-telegram-through-n8n)). Switched on only at go-live step A3 with the owner's OK. |
| `alert.webhook_url` | `null` | The n8n webhook, `https://<n8n host>/webhook/<private path>`. `https` only; no user, password, query or fragment (the secret travels only in the header). Required when enabled. Example placeholder: `https://n8n.example.com/webhook/REPLACE_WITH_ALERT_WEBHOOK_PATH`. |
| `alert.auth_header_name` | `"X-XB-Alert-Token"` | Header that carries the shared secret; must equal the header name of the n8n Header Auth credential. |
| `alert.secret_env_var` | `null` | Name of the environment variable (user or machine level, for the task's Windows account) that holds the shared secret. The way for pair (a) S4U. The value is read at runtime and never stored, printed or logged. |
| `alert.secret_credential_target` | `null` | Instead of the env var: a Windows Credential Manager generic credential target holding the secret. Only with `schedule.logon_type: Password` (an S4U task cannot read Credential Manager), so validation refuses it under S4U. Set exactly one of the two when enabled. |
| `alert.timeout_seconds` | `10` | One POST per run at most, 1 to 30 s. No retry inside the run. |
| `alert.repeat_hours` | `24` | A problem that is still there is repeated after this many hours (1 to 168). |

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
  hourly\<YYYY-MM-DD>\ac2_stockbal_<YYYYMMDDTHHMMSS+0800>_<run_id first 8>\
      stock_balance.csv                   (success only; kept 48 h when pruning is on)
      manifest.json                       (every outcome; kept 48 h when pruning is on)
  daily\close_state.json.gz               (what the published daily files were built from; local only, never pruned)
  daily\<YYYY-MM>\stock_close_<YYYY-MM-DD>.csv            (kept 90 days when pruning is on)
  daily\<YYYY-MM>\stock_close_<YYYY-MM-DD>.manifest.json  (kept 90 days when pruning is on)

<delivery_root>\                          (Drive: "Report/AC2 Stock Balance (daily)")
  stock_balance_latest.csv                (replaced every run)
  latest_manifest.json                    (replaced every run)
  stock_dashboard_feed.json.gz            (replaced every run)
  stock_item_index.json.gz                (replaced every run)
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
- `last_run_status.json` fields: job, run_id, status, failure_class, as_of_local, started_at, finished_at, hourly_folder, delivery_status, large_balance_count (`null` for a failed run), daily_close_status (`published`, `not_due`, `failed`, `skipped_failed_run`), daily_close_failure_class, published_close_day, revised_day_count, late_change_day_count, feed_status (`published` = in the archive and, when delivery is set, in Drive; `failed` = not built, not written to the archive, or not delivered to Drive; `null` for a failed run), item_index_status (same values and rules as feed_status), next_run_local, alert_status (see [Error alerts](#error-alerts-telegram-through-n8n)). `status` and `failure_class` describe the hourly snapshot as before.

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

Failure handling is the feed's: a build or archive write failure keeps the previous file in both places and sets `item_index_status: failed` (`item_index_publish_failed`, exit code `1`); the hourly latest pair is still published. The Drive copy travels in the same verified set as the Drive latest pair and the feed.

Page rules that use it (documented here so the numbers can be checked): low stock = `0 < qty <= level` per outlet for active items, where level is the item's `reorder_level`, else its `min_qty`, else `low_stock_default_threshold`, and the page says which one applied. AutoCount keeps these levels per item, not per outlet, so the page applies the item's level to each outlet's quantity.

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
- `last_run_status.json`, the run's console output and `--check-latest` carry `large_balance_count` (`null` for a failed run, or for a latest manifest written before this check existed). Exit codes do not change. When error alerts are on, a count above 0 raises the `large_balance` alert (count only; see [Error alerts](#error-alerts-telegram-through-n8n)).
- The CSV is unchanged: the flagged rows are exported as AutoCount holds them.

What to do when `large_balance_count` is above 0:

- [ ] Open `latest_manifest.json` (or the hourly `manifest.json`) and read `large_balances.rows`.
- [ ] For each item and location, open the item's stock movement history in AutoCount (for example a stock card report for that item and location) and find the document behind the balance, typically a stock transfer, adjustment or receive whose quantity looks like a code or a typo.
- [ ] If it is a keying error, have the document corrected in AutoCount. The next hourly run picks up the corrected balance; the job never edits AutoCount.
- [ ] If the balance is real, leave it, and consider raising `output.large_balance_threshold` in the local settings file.
- [ ] Record what was found in the private mailbox, never in this public repository (item codes and document numbers are business data).

## Failure classes (what the owner sees in Drive)

A failed run writes `manifest.json` only (no CSV), never touches the latest pair, and updates `last_run_status.json` to `failed`. In Drive the owner sees `last_run_status.json` with `status: failed`, while `latest_manifest.json` still shows the last good `as_of_local`.

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
- The least-privilege option remains available: a dedicated SQL login with `SELECT` only on the seven tables read (`ItemBatchBalQty`, `StockDTL`, `Item`, `ItemUOM`, `Location`, `ChangeCount`, `SyncRegistration`) and `allow_privileged_identity: false`.
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

## Retention

- Drive keeps one latest pair, the feed, the run status and one daily file pair per day. Nothing in Drive is ever pruned or deleted by the job.
- Local archive (behind `retention.prune_enabled`, default `false`; switched on at VM step D3 with the owner's OK): hourly run folders are kept `hourly_keep_hours` (48 h), daily close files `daily_keep_days` (90 days, per [Security guardrails](security_guardrails.md)). Runs every successful hour, after the close; never fails a run (`retention` in the manifest counts what was removed, kept and any errors).
- Safety rules (all tested):
  - only inside `archive_root\hourly\` and `archive_root\daily\`, and only names the exporter makes: `<YYYY-MM-DD>\ac2_stockbal_<timestamp>_<8 hex>\` with `stock_balance.csv` / `manifest.json`, and `<YYYY-MM>\stock_close_<YYYY-MM-DD>.csv` / `.manifest.json`;
  - the age comes from the folder or file name, never from file times;
  - anything else (other names, other folders, symlinks or junctions, anything resolving outside the archive) is kept and counted as `kept_unexpected`; a folder is removed only when it is empty;
  - the current run's folder, `daily\close_state.json.gz`, the latest pair, the feed, the lock and `delivery_root` are never touched.
- Before round 6 the job also wrote hourly folders into Drive; the owner removed those on 28 Oct 2026. The job does not clean Drive.

## Out of scope

- **#246** (daily money / buying package): the owner wants AutoCount's Advanced Dimensional report there (most detailed breakdown, for example marketplace shipping fees). New marketplace fee codes can land on a default term (believed to be "Adjustment") until someone adds the term in AC2, so the #246 report should flag unmapped codes. Cost and value belong in #246, not here.
- The dashboard page: a private Claude artifact that reads `stock_dashboard_feed.json.gz` and `stock_item_index.json.gz` through the viewer's own Drive connector. Its source lives in the private mailbox, not in this repository. This build only writes the files.
- Consignment stock, a heartbeat or "still alive" alert (owner decision 29 Oct 2026: errors only), Drive API upload, Drive clean-up, SQL logins and grants.

## Error alerts (Telegram through n8n)

Owner decision (29 Oct 2026): **errors only.** No heartbeat, no "still alive" message, no success or "resolved" message. Built off by default; the n8n workflow ships inactive. Every live step is listed under [Go-live](#alert-go-live-steps-a1-a5) and needs its own owner OK.

How it works:

1. At the end of every run (after the daily close, the feed, the item index and delivery, just before `last_run_status.json` is written) the exporter works out which alert conditions the run raises, from the same values it writes to `last_run_status.json`.
2. If the de-duplication rule says so, it POSTs **one** sanitized event to `alert.webhook_url`: one attempt, `alert.timeout_seconds` (10 s), no proxy, no redirect, the shared secret in the `alert.auth_header_name` header.
3. The n8n workflow `AC2 Stock Export - Error Alert` ([`n8n-workflows/ac2_stock_alert.workflow.json`](../../n8n-workflows/ac2_stock_alert.workflow.json)) checks the Header Auth secret, rejects any event outside the strict schema (400), sends one plain-text Telegram message to the owner chat through the existing Telegram credential, and answers 200 (502 when Telegram refuses, 500 on an unexpected error).
4. The exporter writes the outcome as `alert_status`. **An alert, sent or lost, never changes the run's status, exit code or files.**

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

Not alerted (accepted gaps): a run that never starts (VM off, task not firing; the dashboard's status badge shows the staleness); `config_invalid` from `main` (the alert settings cannot be trusted, so `alert_status: skipped_config_invalid`); `--check-latest` and `--dry-run-config`, which never alert.

### De-duplication rule

- Each condition is tracked by key (`run_failed` is keyed by its failure class, so a different failure is a different key) in `alert_state.json` in the archive root (local only, never in Drive, never pruned).
- **Send** when any active key is new, or when an active key was last sent `alert.repeat_hours` (24) or more ago. The message lists every active condition, and every active key's time is reset.
- **Suppress** otherwise (`alert_status: suppressed`): the same problem alerts once, then once a day while it lasts.
- A key that is no longer active is dropped, so if it comes back it alerts as new. Recovery itself sends nothing.
- A failed send leaves the state as it was, so the next hour tries again (at most one POST an hour, no retry storm). A missing or unreadable state counts as empty (one extra alert at worst).

### Event (schema `xb.ac2.stock_alert.v1`)

Exactly these keys, closed values only: `schema`, `event` (`ac2_stock_alert`), `run_id`, `run_hour_local` (the run's hour, for example `2026-10-29T14:00:00+08:00`), `alert_kind` (`new` or `repeat`), `status`, `failure_class`, `conditions` (names from the table), `daily_close_status`, `delivery_status`, `feed_status`, `item_index_status`, `large_balance_count`, `late_change_day_count`. **No item code, quantity, location, path, message, host or login**, whatever the run found; a value outside its closed set is sent as `null` or `unknown`. Example message (fake values):

```text
AC2 stock export: ERROR

- Hourly export FAILED: ledger_mismatch (nothing new published this hour)

Hour: 2026-10-29 14:00 (UTC+08:00)
Run: 0123abcd
Details: last_run_status.json in the Drive folder; runbook section "Failure classes".
```

### alert_status values

| Value | Meaning |
| --- | --- |
| `disabled` | `alert.webhook_enabled` is false. |
| `no_alert` | The run raised no condition. |
| `suppressed` | Conditions active, all alerted less than `repeat_hours` ago. |
| `sent` | n8n answered 2xx. |
| `send_failed` | No answer, a timeout, or a non-2xx answer (400 rejected, 403 or 401 secret mismatch, 404 workflow inactive or wrong path, 502 Telegram refused). Retried next hour. |
| `secret_unavailable` | The env var (or Credential Manager target) is missing, empty or not one printable line. Nothing was sent. |
| `error` | Unexpected error in the alert step. Nothing changes for the run. |
| `skipped_config_invalid` | Settings invalid; no alert attempted. |

### Alert go-live steps (A1-A5)

Each step **needs a separate owner OK**; the Controller runs A1, A2 and A4 on the server PC and VM Claude runs A3 and A5. Nothing here has been run. Results go to the private mailbox only (never the secret, the chat id or the webhook path).

- **A1 n8n import, inactive** (needs owner OK): import `ac2_stock_alert.workflow.json` as a new workflow, inactive, through the local n8n API or editor on the server PC. In the local import copy only: replace `REPLACE_WITH_ALERT_WEBHOOK_PATH` with a private, unguessable path; replace `REPLACE_WITH_TELEGRAM_CHAT_ID` with the owner chat; bind the existing Telegram credential; create a Header Auth credential with header name `X-XB-Alert-Token` and a new random secret that the owner generates and keeps (never pasted into chat, the repo or a mailbox file).
- **A2 test event** (needs owner OK, sends one Telegram message): in the editor, listen for a test event, then from the server PC POST this fake event to the local test address `http://127.0.0.1:5678/webhook-test/<private path>` with the secret header. Pass: one Telegram message reading "AC2 stock export: ERROR ... Run: 00000000"; a second POST with an extra key answers 400 and sends nothing; a POST without the header answers 401 or 403 and sends nothing.

  ```json
  {"schema": "xb.ac2.stock_alert.v1", "event": "ac2_stock_alert", "run_id": "00000000000000000000000000000000", "run_hour_local": "2026-10-29T14:00:00+08:00", "alert_kind": "new", "status": "failed", "failure_class": "internal_error", "conditions": ["run_failed"], "daily_close_status": "skipped_failed_run", "delivery_status": "skipped_failed_run", "feed_status": null, "item_index_status": null, "large_balance_count": 0, "late_change_day_count": 0}
  ```

  Then **activate** the workflow (the same owner OK covers activation only if it names it).
- **A3 VM settings and secret** (needs owner OK, VM change): signed in as the task's Windows account (pair (a)), store the secret as a user environment variable without echoing it, then add the alert block to the local settings and validate:

  ```powershell
  $s = Read-Host -AsSecureString "Alert secret"
  [Environment]::SetEnvironmentVariable("XB_AC2_STOCK_ALERT_SECRET", [System.Net.NetworkCredential]::new("", $s).Password, "User")
  ```

  Settings: `"alert": {"webhook_enabled": true, "webhook_url": "https://<n8n host>/webhook/<private path>", "secret_env_var": "XB_AC2_STOCK_ALERT_SECRET"}` (other keys default). Open a **new** PowerShell window (the variable reaches new processes only) and run `--dry-run-config`.
- **A4 one forced-failure test** (needs owner OK, sends one Telegram message): at a quiet minute (not :00), in a new window as the task's account, make one manual run fail safely by giving only that window a wrong expected book: `$env:AUTOCOUNT_EXPECTED_SQL_DATABASE = "ALERT_TEST_WRONG_BOOK"`, then the normal run command. It stops at the identity check before any business query (`identity_refused`) and writes a failed manifest and `last_run_status.json` (Drive shows `failed` until the next hourly run). Pass: one Telegram message "Hourly export FAILED: identity_refused", and `alert_status: sent`. Close that window. The next scheduled run must show `status: success` and `alert_status: no_alert`, and `alert_state.json` must be empty again.

  If A4 gives `send_failed`: a 403 from Cloudflare (not n8n) means its bot protection blocked the request (the job sends `User-Agent: xb-ac2-stock-alert/1`); a Cloudflare rule change needs its own owner OK. `secret_unavailable` means the window or the task does not see the variable.
- **A5 first day** (read-only): after 24 hours, check that `alert_status` reads `no_alert` (or `sent` / `suppressed` for a real problem) on the hourly runs, and that no Telegram message arrived without a matching `alert_status: sent`.

### Rollback

- **Switch off alerts** (VM settings change, needs owner OK): set `alert.webhook_enabled` to `false`. The next run shows `alert_status: disabled`. Optionally remove the variable: `[Environment]::SetEnvironmentVariable("XB_AC2_STOCK_ALERT_SECRET", $null, "User")`, and delete `alert_state.json` in the archive root.
- **Deactivate the n8n workflow** (n8n change, needs owner OK): the VM then gets 404 and records `send_failed` every hour that has a problem; runs and exit codes are unchanged.
- **Rotate the secret**: change the Header Auth credential value in n8n and the VM variable together (A3), then repeat A4.

### Not in this round (LATER)

- A heartbeat or "resolved" message: excluded by the owner (29 Oct 2026). Revisit only if the owner asks.
- A large-balance count that grows while the condition stays active is not re-alerted until the daily repeat.
- `config_invalid` is not alerted (the alert settings themselves may be what is invalid).

## Open questions

1. Drive for desktop folder and Google account: name them at V6.
2. Read identity: pair (a) Windows account with S4U, or pair (b) SQL login with a Password logon and the Credential Manager target. Decide at V4.
3. Items with no balance row: keep the added "(no balance row)" rows (default) or switch `zero_row_mode`.
4. Which SyncRegistration time column is right on live.
5. Online marketplace stock may include consignment for some outlets, so it can differ from this export; owner to confirm that is fine.
