# AutoCount Stock Extraction Runbook

## Phase 1 Smoke Architecture

The current Phase 1 workflow is manual, read-only smoke extraction on the
AutoCount SQL Server VM. Operators run the extractor manually for approved
smoke dates, review `run_manifest.json`, and keep raw CSVs local.

Windows Task Scheduler is future production reference only. No scheduled
extraction is approved yet.

```text
Manual operator run on the VM
  -> scripts/autocount_stock_extract.py
  -> read-only AutoCount SQL views or approved AutoCount API/report outputs
  -> secure archive batch outside GitHub
  -> reviewed run_manifest.json for safe handoff
```

n8n is not required for storage. If it is used later, keep it as an optional
notification/orchestration layer that receives only `run_manifest.json`, not raw
stock rows.

For the verified AC2 smoke workflow, use the
[Phase 1 AC2 stock extraction smoke runbook](autocount2-automation/phase1_stock_extract_smoke_runbook.md).
For the current inventory intelligence objective and parked accounting scope,
see the
[AutoCount 2.0 inventory intelligence scope](autocount2-automation/inventory_intelligence_scope.md).

## What Gets Extracted

Daily:

- `stock_master.csv`: item/UOM/barcode/group/brand/category/active status.
- `stock_balance.csv`: end-of-day item/location/UOM quantity and cost snapshot.
- `stock_movement.csv`: date-ranged stock card or movement rows.
- `stock_documents.csv`: adjustment, receive, issue, transfer, write-off, stock
  take, and assembly rows if exposed by approved read-only views or official
  API/report outputs.

Weekly later:

- Stock aging snapshot, once the daily movement and balance outputs reconcile.

## Files Added

- `scripts/autocount_stock_extract.py`: read-only extractor CLI.
- `scripts/install_autocount_stock_extract_task.ps1`: Windows Task Scheduler installer.
- `config/autocount_stock_extract.ac2_smoke.example.json`: secret-free AC2 smoke extraction-validation profile.
- `config/autocount_stock_extract.example.json`: safe example config with placeholder SQL views.
- `tests/test_autocount_stock_extract.py`: unit tests for extraction windows, manifests, archive writes, and failure notifications.

## Production Setup On The VM

1. Run the [AutoCount SQL Server local probe](autocount_sql_probe.md) first.
   Use its metadata-only candidate report to choose real read-only views,
   official API/report outputs, or validated SQL queries before replacing the
   placeholder `dbo.vw_AutoCount...` examples in the stock extractor config.
   Do not commit probe outputs or raw sample rows.

2. Create the standard local output folder outside the repo:

   ```powershell
   New-Item -ItemType Directory -Force C:\XB\autocount_outputs\extract\stock
   ```

3. Copy `config/autocount_stock_extract.example.json` to:

   ```text
   config\autocount_stock_extract.local.json
   ```

4. Edit the local config on the VM only:

   - Keep `archive_root` under `C:\XB\autocount_outputs\extract\stock`.
   - Replace the example `dbo.vw_AutoCount...` view names with approved read-only views, official API/report outputs, or SQL validated in local sandbox testing.
   - Leave `notification.webhook_url` blank unless a webhook receiver is ready.

5. Use a read-only SQL connection. Prefer Windows authentication with a dedicated read-only Windows account. If SQL authentication is required, store it only on the VM, never in this repo.

   SQL source mode also requires the expected SQL execution context described in
   [Expected SQL Execution Context Guard](#expected-sql-execution-context-guard).
   Extraction fails closed without it.

   Example user-level environment variable:

   ```powershell
   [Environment]::SetEnvironmentVariable(
     "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
     "Driver={ODBC Driver 17 for SQL Server};Server=YOUR-SERVER;Database=YOUR-AUTOCOUNT-DB;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly;",
     "User"
   )
   ```

6. Install Python on the VM.

   - Minimum: Python 3.9, because the extractor uses the standard-library `zoneinfo` module.
   - Recommended: Python 3.11 or newer on the Windows VM.
   - Windows Python may require the `tzdata` package for `ZoneInfo("Asia/Singapore")`.
   - For scheduled production use, pass the full Python executable path to the installer script rather than relying on the interactive user's `PATH`.

7. Install Python dependencies on the VM:

   ```powershell
   py -3.11 -m pip install pyodbc tzdata
   ```

   The VM also needs Microsoft ODBC Driver 17 for SQL Server installed. If the
   extractor reports missing timezone data on Windows, run:

   ```powershell
   python -m pip install tzdata
   ```

8. Validate config without connecting to SQL:

   ```powershell
   py -3.11 scripts\autocount_stock_extract.py --config config\autocount_stock_extract.local.json --dry-run
   ```

9. Run a manual extraction for a known business date:

   ```powershell
   py -3.11 scripts\autocount_stock_extract.py --config config\autocount_stock_extract.local.json --business-date 2026-06-04
   ```

10. Future production reference: register the daily scheduled task.

   Hard gate: do not register Task Scheduler yet. Scheduling requires all of
   the following approvals first:

   - Dedicated read-only SQL login.
   - Reconciliation/sign-off against AutoCount UI/report outputs.
   - Approved wrapper views or explicit approval for the direct smoke profile.
   - Operator approval.

   Keep the instructions below only as future production reference.

   Recommended production posture:

   - Use a dedicated Windows/task account with least privilege.
   - Grant that account read-only SQL/API access and write access only to the approved archive/reporting folders.
   - Keep passwords and connection strings outside GitHub.
   - The installer defaults to `LogonType S4U`, which avoids storing a password and does not require an interactive logged-in session, but it is best suited to local resources. If the task must access network paths or other resources that require delegated credentials, configure the task account securely through Windows Task Scheduler or an approved secret-handling process.
   - `LogonType Interactive` is for development/testing only and may require the user to be logged in.

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_autocount_stock_extract_task.ps1 `
     -PythonExe "C:\Python311\python.exe" `
     -ConfigPath "config\autocount_stock_extract.local.json" `
     -UserId ".\svc_autocount_extract" `
     -LogonType S4U `
     -StartTime "02:00"
   ```

## Expected SQL Execution Context Guard

`Trusted_Connection=yes` proves only that integrated authentication was used. It does not
identify which Windows account, SQL login, database user, or database the session actually
resolved to. A connection-string label is not proof of a read-only identity.

SQL source mode therefore refuses to run any business query until the expected execution
context is supplied locally and matches what the server reports on the same connection.

### Required Environment Variable Names

Set these three on the VM. Only the NAMES belong in this repository:

| Variable name | Must contain |
|---|---|
| `AUTOCOUNT_EXPECTED_SQL_LOGIN` | the SQL login the extraction process must resolve to |
| `AUTOCOUNT_EXPECTED_SQL_USER` | the mapped database user in the AutoCount database |
| `AUTOCOUNT_EXPECTED_SQL_DATABASE` | the explicit AutoCount database the extractor must be bound to |

### Handling Rules

- The values are private operational identity values. Never commit them, never paste them into
  documentation, pull requests, issue comments, chat, logs, or screenshots.
- The values must correspond to the intended dedicated future task identity, its one-to-one
  mapped database user, and the explicit target database — not to an interactive administrator
  account.
- Comparison is exact, so each value must match what the server reports character for
  character. Read the three values yourself on the VM using
  `SELECT SUSER_SNAME(), USER_NAME(), DB_NAME();` while connected as the intended identity, and
  keep the result local.
- Set them for the account that will actually run the extractor. Values placed only in a
  different account's user scope will not be visible to an unattended task.

### Behaviour

- If any of the three is missing or blank, SQL extraction fails closed before opening a
  connection. No business query runs and no run manifest is written.
- The extractor reads `SUSER_SNAME()`, `USER_NAME()`, and `DB_NAME()` on the same connection
  and cursor that will run the dataset query, immediately before that query. Every dataset
  opens its own connection and is asserted independently, so a context proven on one connection
  never authorises a query on another.
- On any mismatch, the run aborts before the business query. It does not retry, does not fall
  back to the interactive or administrative account, and does not continue to the remaining
  datasets.
- Failure messages name only the contract class that failed, for example
  `Expected SQL execution context is not configured: ...` or
  `SQL execution context mismatch: login`. They never include observed or expected logins,
  users, databases, server names, connection strings, or credentials. To diagnose a mismatch,
  compare the values locally on the VM.
- Sample source mode and `--dry-run` do not require these variables. `--dry-run` remains
  non-connecting and non-persisting.

Current production extraction and Windows Task Scheduler activation remain unapproved under
issue #149. This guard is a prerequisite control, not an approval.

## Output Contract

Each run writes one idempotent batch folder:

```text
C:\XB\autocount_outputs\extract\stock\
  ac2_stock_2026-06-04\
    stock_master.csv
    stock_balance.csv
    stock_movement.csv
    stock_documents.csv
    run_manifest.json
```

Re-running the same `business_date` replaces that date's dataset files instead
of appending duplicate rows.

`run_manifest.json` is safe to send to notification tools because it contains
the per-run `run_id`, row counts, status, storage paths, CSV byte sizes,
SHA-256 hashes, and exception summaries only. It does not include raw stock
rows or credentials.

Example manifest storage shape:

```json
{
  "run_id": "2decb5fe-64d7-4db0-bcbe-a5b3df8574a0",
  "storage_batch_id": "ac2_stock_2026-06-04",
  "storage": {
    "archive_path": "C:\\XB\\autocount_outputs\\extract\\stock\\ac2_stock_2026-06-04",
    "dataset_files": {
      "stock_master": "C:\\XB\\autocount_outputs\\extract\\stock\\ac2_stock_2026-06-04\\stock_master.csv"
    },
    "files": {
      "stock_master": {
        "path": "C:\\XB\\autocount_outputs\\extract\\stock\\ac2_stock_2026-06-04\\stock_master.csv",
        "byte_size": 1204,
        "sha256": "example-placeholder-not-a-real-file-hash"
      }
    }
  }
}
```

## Reconciliation Checks

Before trusting dashboard numbers:

- Compare `stock_balance.csv` totals against AutoCount Stock Balance/Status for the same date.
- Re-run one business date twice and confirm no duplicate rows are created.
- Enter or identify a backdated adjustment in a test/sandbox company and confirm the 3-day overlap window catches it.
- Confirm the SQL login cannot insert, update, delete, execute posting procedures, or alter schema.
- Confirm archive files are stored only in approved secure storage, not GitHub.
- Confirm `run_manifest.json` includes a unique `run_id`, row counts, byte sizes, and SHA-256 hashes for generated CSV files.

## Integration Surface Verification Checklist

Confirm one of these approved extraction paths before production use:

- Official AutoCount .NET/API or report helper output for stock master, stock status, stock balance/cost, movement/stock card, and stock aging.
- Locally approved read-only SQL views for stock master, daily stock balance, stock movement/stock card, and stock documents.
- Local sandbox tests against the installed AutoCount 2.0 build and SQL Server 2019 instance.
- Read-only permission tests proving the automation account cannot insert, update, delete, execute posting procedures, or alter schema in the AutoCount production database.
- Reconciliation tests comparing extractor output with AutoCount UI/report totals for the same business date.

Optional later: ask the AutoCount vendor or implementation partner to review the chosen extraction surface. This is a useful validation step, not a blocker for this local documentation spike.

The extractor is intentionally read-only. Phase 1 should not post stock,
adjustment, transfer, or accounting entries back into AutoCount.

## After Running The SQL Probe

Use only safe summaries from the local probe outputs. Do not commit or paste raw
sample rows, generated probe folders, local configs, credentials, connection
strings, or production database names.

1. Update the decision pack:

   - Add safe SQL Server/database context to
     [extraction_surface_decision.md](autocount2-automation/extraction_surface_decision.md).
   - Summarise `permission_risks.csv` and `role_risks.csv`.
   - Mark each candidate as `Selected for Phase 1`, `Needs reconciliation`,
     `Rejected`, or `Unknown`.

2. Choose stock candidates:

   - Start with item/product/stock master candidates for `stock_master`.
   - Use stock balance/status candidates only if they expose as-at quantity and
     value semantics that can reconcile to AutoCount reports.
   - Use stock movement/stock card candidates only if date-window totals,
     document references, and signs can reconcile to AutoCount reports.
   - Use stock document/transfer/adjustment candidates only after
     cancelled/voided and transfer handling is clear.

3. Update the local extractor config on the VM:

   - Copy `config/autocount_stock_extract.from_probe.example.json` to a local
     ignored VM path `config\autocount_stock_extract.local.json`.
   - Replace only placeholder view names that have been selected in
     [phase1_extraction_mapping.md](autocount2-automation/phase1_extraction_mapping.md).
   - Keep `archive_root` outside GitHub.
   - Keep the SQL connection string in
     `AUTOCOUNT_READONLY_SQL_CONNECTION_STRING`.
   - Do not add passwords or production connection strings to JSON.

4. Run reconciliation before scheduling:

   - Follow
     [reconciliation_checklist.md](autocount2-automation/reconciliation_checklist.md).
   - Confirm item counts, active/inactive counts, stock balance by location,
     stock balance by item, stock value/cost totals, movement date-window
     totals, cancelled/voided handling, and backdated movement handling.
   - Confirm the SQL login has no direct write/schema/security risks and no
     risky database roles such as `db_owner`, `db_datawriter`, or
     `db_ddladmin`.
   - Schedule the extractor only after manual runs and reconciliation pass for
     the selected business dates.
