# AC2 hourly stock balance export runbook (#245)

Status: **offline build only.** Nothing here has run against SQL Server, the AC2 VM, Google Drive or Task Scheduler. Every step under [VM proof steps](#vm-proof-steps-v1-v7) needs a separate owner OK.

## What it does

`scripts/autocount_stock_balance_hourly.py` reads the stored on-hand stock balance from AutoCount (`dbo.ItemBatchBalQty`, the table AutoCount itself maintains) once an hour, 24/7, and writes:

- an immutable hourly folder with `stock_balance.csv` and `manifest.json`;
- a "latest" pair (`stock_balance_latest.csv`, `latest_manifest.json`) that is replaced only after the new hourly CSV re-reads to the hash that was written;
- `last_run_status.json`, updated on every run, success or failed, so a failed hour is visible in Drive.

Delivery to Google Drive is a plain file copy into a local folder that Google Drive for desktop syncs (`output.delivery_root`). Nothing calls a Drive API. The manifest says "handed to Drive sync folder", never "uploaded".

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

## Commands

```powershell
python scripts\autocount_stock_balance_hourly.py --config config\autocount_stock_balance_hourly.local.json --dry-run-config
python scripts\autocount_stock_balance_hourly.py --config config\autocount_stock_balance_hourly.local.json
python scripts\autocount_stock_balance_hourly.py --config config\autocount_stock_balance_hourly.local.json --check-latest
```

Exit codes: run `0` success (and delivery verified or not configured), `1` failed or delivery not verified. `--check-latest`: `0` fresh, `2` stale (older than `stale_after_minutes`), `3` missing or invalid. `--dry-run-config`: `0` valid, `1` invalid. Console output is a short status JSON; it never prints row data, connection strings, logins, host names or paths.

## Settings

Every business choice is a setting. The owner said answers may still change, so every row below "may change". Unknown keys, bad values, a bad SyncRegistration column or a missing `expected_database` fail validation before anything is read.

| Key | Default | Notes |
| --- | --- | --- |
| `timezone` | `"Asia/Singapore"` | Used for folder names, `as_of_local` and the stale check. May change. |
| `schedule.frequency` | `"hourly"` | Only `hourly` is supported. Used by the installer and the stale check. May change. |
| `schedule.run_minute` | `0` | Minute past each hour (00:00, 01:00 ... 23:00 = 24 runs a day). No separate close-of-day file. May change. |
| `stale_after_minutes` | `70` | `--check-latest` reports stale after this. May change. |
| `source.server` | required | SQL Server instance. The example uses a fake value. |
| `source.odbc_driver` | `"ODBC Driver 17 for SQL Server"` | May change. |
| `source.expected_database` | required | `DB_NAME()` must equal it before any business query. Example: `AED_EXAMPLE_BOOK`. |
| `source.allowed_databases` | `[expected_database]` | Must contain `expected_database`. May change. |
| `source.encrypt` | `false` | ODBC `Encrypt`. Local connection on the VM. May change. |
| `source.trust_server_certificate` | `false` | ODBC `TrustServerCertificate`. May change. |
| `source.sync_registration_time_column` | `"LastFinishSyncTime"` | Exactly `LastFinishSyncTime` or `LastTransFinishSyncTime`; anything else fails validation. Which is right on live is an open question. A failing sync read fails the hour (no fallback). |
| `identity.auth_mode` | `"windows_integrated"` | `windows_integrated` (the Task Scheduler account) or `sql_login`. Open question. |
| `identity.sql_login` | `null` | Login name, only with `sql_login`. `sa` is rejected. |
| `identity.credential_target` | `null` | Windows Credential Manager generic credential target holding the password, only with `sql_login`. The password is read at runtime and never stored or logged. |
| `identity.allow_privileged_identity` | `false` | `false`: refuse sysadmin or db_owner. `true`: run, set `identity.privileged=true` and a warning in every manifest. May change. |
| `read.lock_timeout_ms` | `5000` | May change. |
| `read.query_timeout_seconds` | `60` | May change. |
| `read.max_attempts` | `3` | Attempts for an unstable snapshot or a lock timeout. May change. |
| `read.retry_backoff_seconds` | `2` | May change. |
| `rows.zero_row_mode` | `"keep_rows_and_add_items_without_rows"` | Keep every balance row including zeros, and add one row per active stock-controlled item (`IsActive='T'` and `StockControl='T'`) with no balance row anywhere: Location `""`, LocationLabel `"(no balance row)"`, BalQty `0`, RowSource `no_balance_row`. Other values: `keep_rows` (no added rows), `drop_zero_rows`. May change. |
| `rows.drop_zero_rows_for_inactive_items` | `true` | Drop zero rows of inactive items (non-zero inactive rows are kept). May change. |
| `rows.uom_guard` | `"fail"` | Fail the hour (no CSV) if any balance row's UOM differs from `Item.BaseUOM`, or any item has more than one UOM, until a mapping exists. May change once a mapping is designed. |
| `location_labels` | `{}` | Location code -> label. Every code is exported; codes are never dropped or renamed. Unmapped code label = `code - Location.Description` when present, else the code; unmapped codes are listed in the manifest. Real labels only in the local file. May change. |
| `output.archive_root` | required | Local archive folder on the VM. |
| `output.delivery_root` | `null` | Local folder synced by Google Drive for desktop. `null` = local archive only. Folder still to be chosen. Must differ from `archive_root`. |
| `output.columns` | as_of_local, ItemCode, ItemDescription, ItemType, Location, LocationLabel, UOM, BalQty, IsActive, StockControl, RowSource | Order and subset of the CSV columns; must include ItemCode, Location, UOM, BalQty. To be aligned to the owner's Stock Balance by Location report at reconciliation. May change. |

## Outputs and folder layout

```
<archive_root>\
  .ac2_stockbal.lock                      (only while a run is active)
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
- CSV: UTF-8 without BOM, header row, CRLF line ends via the csv module, fixed column order from `output.columns`, every cell through `safe_csv_row` (formula prefixes neutralised; numbers stay numeric, so negative quantities are not prefixed).
- Manifest fields: job `ac2_hourly_stock_balance`, schema_version, run_id, status, failure_class, sanitized message, started/finished (+08:00), as_of_local and as_of_sql, timezone, source (surface, database, query_sha256, settings_sha256, git commit), identity flags (never names), attempts, ChangeCount before/after, row_count, CSV sha256/size, counts (zero, negative, no_balance_row, non_empty_batch, inactive_dropped, zero_dropped, null_balqty_rows), per-location rows / non_zero / negative / sum_qty, unmapped_location_codes, uom_guard, pos_sync per Location, delivery.

## Failure classes (what the owner sees in Drive)

A failed run writes `manifest.json` only (no CSV), never touches the latest pair, and updates `last_run_status.json` to `failed`. In Drive the owner sees `last_run_status.json` with `status: failed`, while `latest_manifest.json` still shows the last good `as_of_local`.

| failure_class | Meaning | What to do |
| --- | --- | --- |
| `config_invalid` | Settings file invalid. | Run `--dry-run-config`, fix the named setting. |
| `already_running` | Another run holds the lock. | Nothing; the next hour runs normally. |
| `stale_lock` | A lock older than 60 minutes exists (a run was killed). Never removed automatically. | Check no export process is running, then delete `.ac2_stockbal.lock` in the archive root. |
| `identity_refused` | `sa` login, sysadmin/db_owner with `allow_privileged_identity` false, expected-context mismatch (`AUTOCOUNT_EXPECTED_SQL_LOGIN` / `_USER` / `_DATABASE`), or database pin mismatch. | Check the read identity and the env values. Messages never show the values. |
| `credential_unavailable` | The Credential Manager target could not be read. | Check `identity.credential_target` for the task's Windows account. |
| `lock_timeout` | Lock timeout on every attempt. | Usually clears next hour. |
| `unstable_snapshot` | ChangeCount moved during every attempt. | Usually clears next hour; check if it repeats. |
| `uom_guard` | A non-base UOM or a multi-UOM item exists. | Map the UOM before the export can continue. |
| `sync_read_failed` | SyncRegistration read failed (for example the wrong time column). | Check `source.sync_registration_time_column`. |
| `source_error` | Connection or other read failure. | Check SQL Server and the driver. |
| `write_failed` | The hourly CSV or manifest could not be written or re-read. | Check disk space and folder permissions. |
| `latest_publish_failed` | Hourly data is good but the latest pair could not be replaced (`last_run_status.json` only). | Check whether a program holds the latest files open. |
| `internal_error` | Unexpected error. The message has the run reference only. | Report the run reference. |

## Read identity (open question)

- Recommended: a dedicated least-privilege SQL login, `SELECT` only on the six tables read (`ItemBatchBalQty`, `Item`, `ItemUOM`, `Location`, `ChangeCount`, `SyncRegistration`), with `auth_mode` `sql_login` and the password in Windows Credential Manager. Creating it is step V4.
- Alternative the owner mentioned: their own admin account (Windows integrated). That account is sysadmin on the VM, so it needs `allow_privileged_identity: true`; every manifest then carries `identity.privileged: true` and a warning.
- Always: `SUSER_SNAME()='sa'` is refused (case-insensitive), whatever the settings say. The `AUTOCOUNT_EXPECTED_SQL_LOGIN` / `_USER` / `_DATABASE` environment values must be set for the task's account and must match; the run fails closed otherwise.

## Reconciliation

1. Owner exports AutoCount **Stock > Stock Balance by Location** once, off-peak.
2. Compare totals by location with `per_location.sum_qty` in the manifest of an export taken close to the same time.
3. Compare 20 sample SKUs (quantity per location).
4. Align `output.columns` to the report's columns if the owner wants the same layout.
5. Record the result in the private mailbox, not in this public repository.

## VM proof steps (V1-V7)

Each step **needs owner OK**. None has been run.

- **V1** (needs owner OK): run against the test book `AED_TESTINGAPI` with the VM Windows user and `allow_privileged_identity: false` (expect `identity_refused`), then `true` (test book only).
- **V2** (needs owner OK): reconcile the test output to the scoping totals (12,156 balance rows, SUM 26,090).
- **V3** (needs owner OK): owner runs `sql/autocount2/stock_balance_hourly_live_check.readonly.template.sql` off-peak. Note: the ledger-match step assumes `dbo.StockDTL` has a `BatchNo` column; if it errors there, nothing was written and the remaining steps can be rerun without it.
- **V4** (needs owner OK): create the chosen read identity and grants.
- **V5** (needs owner OK): three manual live runs off-peak; reconcile with the owner's Stock Balance by Location export.
- **V6** (needs owner OK): pick the Drive folder (`delivery_root`) and confirm the Google Drive for desktop account on the VM.
- **V7** (needs a separate owner OK): register the hourly task with `scripts/install_autocount_stock_balance_hourly_task.ps1`.

## Retention

- No pruning code in this build. Hourly folders accumulate (24 a day locally and, when `delivery_root` is set, in Drive).
- Local raw retention follows [Security guardrails](security_guardrails.md) (raw exports stay outside GitHub, restricted NTFS permissions, initially 90 days). Any pruning needs a later owner OK.

## Out of scope

- **#246** (daily money / buying package): the owner wants AutoCount's Advanced Dimensional report there (most detailed breakdown, for example marketplace shipping fees). New marketplace fee codes can land on a default term (believed to be "Adjustment") until someone adds the term in AC2, so the #246 report should flag unmapped codes. Cost and value belong in #246, not here.
- Dashboards: planned later as Claude artifacts shared in the Work Claude organisation account. Nothing to build now.
- Consignment stock, alert emails or webhooks, n8n delivery, Drive API upload, pruning, scheduling, SQL logins and grants.

## Open questions

1. Read identity: dedicated least-privilege login (recommended) or the owner's admin account (needs `allow_privileged_identity: true`).
2. Drive folder (`delivery_root`) and which Google Drive for desktop account on the VM.
3. Who gets failure alerts (none in this build; failures are visible through files only).
4. Items with no balance row: keep the added "(no balance row)" rows (default) or switch `zero_row_mode`.
5. Location labels: to confirm with the owner (kept in the local settings file only).
6. Which SyncRegistration time column is right on live.
7. Online marketplace stock may include consignment for some outlets, so it can differ from this export; owner to confirm that is fine.
