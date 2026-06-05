# AutoCount Stock Extraction Runbook

## Recommended Architecture

Run the extractor on the AutoCount SQL Server VM with Windows Task Scheduler.

```text
Windows Task Scheduler
  -> scripts/autocount_stock_extract.py
  -> read-only AutoCount SQL views or approved AutoCount API/report outputs
  -> secure archive batch outside GitHub
  -> optional webhook/email/dashboard refresh
```

n8n is not required for storage. If it is used later, keep it as an optional
notification/orchestration layer that receives only `run_manifest.json`, not raw
stock rows.

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
- `config/autocount_stock_extract.example.json`: safe example config with placeholder SQL views.
- `tests/test_autocount_stock_extract.py`: unit tests for extraction windows, manifests, archive writes, and failure notifications.

## Production Setup On The VM

1. Create a secure runtime folder outside the repo, for example:

   ```powershell
   New-Item -ItemType Directory -Force D:\AutoCountStockExtract
   New-Item -ItemType Directory -Force D:\AutoCountStockArchive
   ```

2. Copy `config/autocount_stock_extract.example.json` to:

   ```text
   D:\AutoCountStockExtract\autocount_stock_extract.local.json
   ```

3. Edit the local config on the VM only:

   - Keep `archive_root` outside GitHub.
   - Replace the example `dbo.vw_AutoCount...` view names with approved read-only views, official API/report outputs, or SQL validated in local sandbox testing.
   - Leave `notification.webhook_url` blank unless a webhook receiver is ready.

4. Use a read-only SQL connection. Prefer Windows authentication with a dedicated read-only Windows account. If SQL authentication is required, store it only on the VM, never in this repo.

   Example user-level environment variable:

   ```powershell
   [Environment]::SetEnvironmentVariable(
     "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
     "Driver={ODBC Driver 18 for SQL Server};Server=YOUR-SERVER;Database=YOUR-AUTOCOUNT-DB;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly;",
     "User"
   )
   ```

5. Install Python on the VM.

   - Minimum: Python 3.9, because the extractor uses the standard-library `zoneinfo` module.
   - Recommended: Python 3.11 or newer on the Windows VM.
   - For scheduled production use, pass the full Python executable path to the installer script rather than relying on the interactive user's `PATH`.

6. Install Python dependencies on the VM:

   ```powershell
   py -3.11 -m pip install pyodbc
   ```

   The VM also needs Microsoft ODBC Driver for SQL Server installed.

7. Validate config without connecting to SQL:

   ```powershell
   py -3.11 scripts\autocount_stock_extract.py --config D:\AutoCountStockExtract\autocount_stock_extract.local.json --dry-run
   ```

8. Run a manual extraction for a known business date:

   ```powershell
   py -3.11 scripts\autocount_stock_extract.py --config D:\AutoCountStockExtract\autocount_stock_extract.local.json --business-date 2026-06-04
   ```

9. Register the daily scheduled task.

   Recommended production posture:

   - Use a dedicated Windows/task account with least privilege.
   - Grant that account read-only SQL/API access and write access only to the approved archive/reporting folders.
   - Keep passwords and connection strings outside GitHub.
   - The installer defaults to `LogonType S4U`, which avoids storing a password and does not require an interactive logged-in session, but it is best suited to local resources. If the task must access network paths or other resources that require delegated credentials, configure the task account securely through Windows Task Scheduler or an approved secret-handling process.
   - `LogonType Interactive` is for development/testing only and may require the user to be logged in.

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_autocount_stock_extract_task.ps1 `
     -PythonExe "C:\Python311\python.exe" `
     -ConfigPath "D:\AutoCountStockExtract\autocount_stock_extract.local.json" `
     -UserId ".\svc_autocount_extract" `
     -LogonType S4U `
     -StartTime "02:00"
   ```

## Output Contract

Each run writes one idempotent batch folder:

```text
D:\AutoCountStockArchive\
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
    "archive_path": "D:\\AutoCountStockArchive\\ac2_stock_2026-06-04",
    "dataset_files": {
      "stock_master": "D:\\AutoCountStockArchive\\ac2_stock_2026-06-04\\stock_master.csv"
    },
    "files": {
      "stock_master": {
        "path": "D:\\AutoCountStockArchive\\ac2_stock_2026-06-04\\stock_master.csv",
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
