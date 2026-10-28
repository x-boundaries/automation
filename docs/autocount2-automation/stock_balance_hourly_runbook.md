# AC2 hourly stock balance export runbook (#245)

Status: **offline build only.** Nothing here has run against SQL Server, the AC2 VM, Google Drive or Task Scheduler. Every step under [VM proof steps](#vm-proof-steps-v1-v7) and [Round 6 VM proof](#round-6-vm-proof-steps-d1-d6) needs a separate owner OK.

Round 6 (accepted design, owner "Accept all", 27 Oct 2026): a daily 00:00 close rebuilt from the stock ledger, a backfill from 1 Aug 2026, one latest pair in Drive (no hourly folders in Drive), local retention, and a compact dashboard feed. File, setting and task names keep the word "hourly" (the job still runs hourly); only the Drive folder is renamed to "daily".

## What it does

`scripts/autocount_stock_balance_hourly.py` runs once an hour, 24/7. Each run reads the stored on-hand stock balance (`dbo.ItemBatchBalQty`, the table AutoCount itself maintains) and the stock ledger (`dbo.StockDTL`, summed per item, UOM, location and day), checks that the two agree, and writes:

- locally, an immutable hourly folder with `stock_balance.csv` and `manifest.json` (kept 48 hours once pruning is on);
- a "latest" pair (`stock_balance_latest.csv`, `latest_manifest.json`) that is replaced only after the new hourly CSV re-reads to the hash that was written;
- the dashboard feed `stock_dashboard_feed.json.gz`, beside the latest pair (see [Dashboard feed](#dashboard-feed-schema-v1));
- `last_run_status.json`, updated on every run, success or failed, so a failed hour is visible in Drive.

The first run after local midnight also publishes the **daily close** of the previous day (see [Daily close](#daily-close)): one CSV of non-zero closing balances plus its manifest, in `daily/<YYYY-MM>/`.

Delivery to Google Drive is a plain file copy into a local folder that Google Drive for desktop syncs (`output.delivery_root`). Drive gets only the latest pair, the feed, `last_run_status.json` and the daily files, each replaced through a temp copy that must re-read to the right hash. **No hourly folders go to Drive.** Nothing calls a Drive API. The manifest says "handed to Drive sync folder", never "uploaded".

Where it runs (owner decision, 23 Oct 2026): on the AC2 VM as a Windows Task Scheduler task, reading SQL Server locally. The archive is a gitignored folder inside the automation clone on the VM, and delivery writes directly into the Drive for desktop synced folder. The settings file is JSON and cannot carry comments, so this runbook documents the placeholders used in the example settings.

How it reads:

- Fixed `SELECT` statements only; `READ COMMITTED`, `SET LOCK_TIMEOUT` (default 5000 ms), query timeout (default 60 s), one connection per attempt. No `NOLOCK`, no snapshot isolation, no database setting changes.
- Consistency: `dbo.ChangeCount` (row `ItemBatchBalQty`) is read before and after the balance read on the same connection. Equal values mean no balance write landed in between. If it keeps moving, the run retries (default 3 attempts, short backoff), then fails as `unstable_snapshot`. A lock timeout is also retried within the same attempt budget.
- Balance rows are summed by ItemCode, UOM and Location (BatchNo is always empty today; rows with a non-empty BatchNo are counted in the manifest).
- The ledger read (`dbo.StockDTL`: `SUM(Qty)` per ItemCode, UOM, Location and `CAST(DocDate AS date)`, never filtered by date), the item master (`dbo.Item`), the location list (`dbo.Location`) and the reorder levels (`dbo.ItemUOM.ReOLevel > 0`, information only) are read **inside the same ChangeCount window** as the balance read. `dbo.ChangeCount` has no row for `StockDTL` (R1, 28 Oct 2026); a stock posting writes both tables, so it moves the balance counter, and the per-run [ledger check](#ledger-check-every-run) catches anything the counter missed.
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

Exit codes: run `0` success (and delivery verified or not configured, the daily close published or not due, and the feed published), `1` failed, delivery not verified, daily close failed or feed failed. A daily close or feed failure never stops the hourly latest pair: `status` stays `success` and `daily_close_status` / `feed_status` say what failed. `--check-latest`: `0` fresh, `2` stale (older than `stale_after_minutes`), `3` missing or invalid; it never exits with anything else, so invalid settings (including a malformed `timezone` such as `Asia/Singapore/`), a missing zone database or a usage error also give `3`. `--dry-run-config`: `0` valid (it also prints `auth_mode`, `scheduled_logon_type` and `run_minute`, which the installer reads), `1` invalid. Console output is a short status JSON; it never prints row data, connection strings, logins, host names or paths. The run output, `--check-latest` and `last_run_status.json` also carry `large_balance_count` (see [Large-balance warning](#large-balance-warning)); it never changes an exit code.

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
| `feed.low_stock_threshold` | `2` | Low stock = `0 < quantity <= threshold`, active items (owner, 28 Oct 2026: global 2 units for now). `ItemUOM.ReOLevel` is shown beside each low-stock row when set but does not change the rule yet. |
| `feed.list_cap` | `200` | Most rows listed in the feed's negatives and low-stock lists; counts are always complete. |
| `feed.movers_top` | `20` | Biggest risers and fallers listed per period (1, 7, 30 days). |

## Outputs and folder layout

```
<archive_root>\                           (local, on the VM)
  .ac2_stockbal.lock                      (while a run is active, or left by a killed run)
  last_run_status.json                    (every run)
  stock_balance_latest.csv                (latest verified success only)
  latest_manifest.json                    (latest verified success only)
  stock_dashboard_feed.json.gz            (every successful run)
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
  last_run_status.json                    (replaced every run)
  daily\<YYYY-MM>\stock_close_<YYYY-MM-DD>.csv            (one per day, never pruned)
  daily\<YYYY-MM>\stock_close_<YYYY-MM-DD>.manifest.json
```

- No `hourly\` folder is ever written to Drive. An hourly run that is not the daily close adds no file to Drive; it only replaces the four top-level files.
- Local hourly files use exclusive create plus fsync and SHA-256. A rerun in the same hour makes a new folder; existing files are never touched.
- Published sets (`publish_verified_set`): the latest pair (archive), the Drive latest set (CSV, manifest and feed), each day's CSV + manifest (per folder) and the archive feed are each written as a set. Every file of the set is first written to a temp name beside its target, re-read and hashed; only when **all** of them verify are they moved over the old files with `os.replace`, in order (CSV first). A bad or failed copy of any file removes all the temps and replaces nothing, so Drive keeps the previous verified pair (and feed) and a day keeps its previous CSV + manifest, each pair still matching by hash. The archive latest pair is published only after the hourly CSV re-reads to its hash. `latest_manifest.json` carries `as_of_local` and `run_id`, so an old snapshot is visible.
- One limit remains, as before round 6: if a target is still held (for example by the sync client) after the move's three tries, the files already moved stay moved and the rest are dropped; the run reports `copy_failed` and `--check-latest` may say `invalid` until the next good hour.
- Delivery status `verify_failed` (hash mismatch) or `copy_failed` (write error) is in `last_run_status.json`; the run manifest records `delivery.mode: latest_only`, the files and `pending_see_last_run_status`.
- Size, measured offline with a generated fake book (50,000 ledger groups, 12,000 keys, 6,000 items): daily close CSV about 1 MB a day; feed under 10 KB; the 88-day backfill run took about 11 s and an ordinary hourly run about 1 s on the server PC. Real sizes are measured at VM step D2.
- Lock: `.ac2_stockbal.lock` records the run_id, process id and start time. A run killed by the 15-minute task limit or a reboot cannot remove it. The next run clears it automatically only when it is older than 30 minutes (the 15-minute limit plus a 15-minute margin) **and** either it was written before Windows last started (a restart ends every run, whatever process later reuses its process id) or its recorded process is no longer running (on Windows, a process created after the lock was written counts as a reused process id, not the owner). That run adds a warning to its manifest naming the killed run's reference, the reason and the lock age. A younger lock, or a lock written since the last start whose process is still running, gives `already_running` and is left alone.
- CSV: UTF-8 without BOM, header row, CRLF line ends via the csv module, fixed column order from `output.columns`, every cell through `safe_csv_row` (formula prefixes neutralised; numbers stay numeric, so negative quantities are not prefixed).
- Manifest fields: job `ac2_hourly_stock_balance`, schema_version, run_id, status, failure_class, sanitized message, started/finished (+08:00), as_of_local and as_of_sql, timezone, source (surface, database, query_sha256, settings_sha256, git commit), identity flags (never names), attempts, ChangeCount before/after, row_count, CSV sha256/size, counts (zero, negative, no_balance_row, non_empty_batch, inactive_dropped, zero_dropped, null_balqty_rows), per-location rows / non_zero / negative / sum_qty, unmapped_location_codes, large_balances, uom_guard, pos_sync per Location, delivery, warnings.
  Round 6 adds (schema_version `2`): `ledger` (groups, rows, null_qty_rows, non_empty_batch, future_dated_rows, non_base_uom_groups, non_stock_rows_excluded, non_stock_items_excluded, items_missing_from_master, first_day, last_day), `ledger_check`, `uom_guard.ledger_non_base_uom_groups`, `counts.non_stock_excluded`, `daily_close`, `feed` (status, sha256, byte_size), `retention`, and the extra query hashes (`ledger`, `item_master`, `locations`, `reorder_levels`).
- `last_run_status.json` fields: job, run_id, status, failure_class, as_of_local, started_at, finished_at, hourly_folder, delivery_status, large_balance_count (`null` for a failed run), daily_close_status (`published`, `not_due`, `failed`, `skipped_failed_run`), daily_close_failure_class, published_close_day, revised_day_count, late_change_day_count, feed_status (`published` = in the archive and, when delivery is set, in Drive; `failed` = not built, not written to the archive, or not delivered to Drive; `null` for a failed run), next_run_local. `status` and `failure_class` describe the hourly snapshot as before.

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
- **Missing or unreadable close state:** the next due run writes every day from `backfill_start` again and warns `Close state was missing ...` (self-healing; the files then match the ledger).
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
| `feed_publish_failed` (in the manifest `feed`; `feed_status: failed`) | The feed could not be built, written to the archive, or delivered to Drive. The previous feed stays where it was not replaced (see [Dashboard feed](#dashboard-feed-schema-v1)). | Report the run reference. |
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
     `-ExecutionPolicy Bypass` applies to this one command only; it does not change the VM's PowerShell settings. `-RepoRoot` is not needed (the installer uses the clone it is in). The task goes in the `\XB\` folder of Task Scheduler; use `-TaskPath "\Other\"` only if the owner asks for another folder (it must start and end with `\`). The installer refuses if a task with the same name is already in that folder, and warns if one is in the Task Scheduler root (see "Replace the paused root task" below). The dry run also prints the one hourly trigger it built.
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
- The dashboard page: a later round builds it as a private Claude artifact that reads `stock_dashboard_feed.json.gz` through the viewer's own Drive connector. This build only writes the feed.
- Consignment stock, failure alerts (planned follow-up below), n8n, Drive API upload, Drive clean-up, SQL logins and grants.

## Planned follow-up: failure alerts

Not built in this round, and nothing in n8n is touched. In this build a failure is visible only through files (`last_run_status.json`, the hourly manifest, `--check-latest`). Planned for a later #245 round:

- Failure alerts go to the owner by Telegram through an n8n webhook. The VM job posts a sanitized failure event (hour, `failure_class`, `run_id`), with no stock data.
- The same round can alert on `large_balance_count` above 0 in `last_run_status.json` (count only, no item codes or quantities), on `daily_close_status: failed` or no `published_close_day` for yesterday by 01:00, on `late_change_day_count` above 0, and on `failure_class: ledger_mismatch`.
- An hourly heartbeat, so n8n alerts when no run has been heard for about 70 minutes.

## Open questions

1. Drive for desktop folder and Google account: name them at V6.
2. Read identity: pair (a) Windows account with S4U, or pair (b) SQL login with a Password logon and the Credential Manager target. Decide at V4.
3. Items with no balance row: keep the added "(no balance row)" rows (default) or switch `zero_row_mode`.
4. Which SyncRegistration time column is right on live.
5. Online marketplace stock may include consignment for some outlets, so it can differ from this export; owner to confirm that is fine.
