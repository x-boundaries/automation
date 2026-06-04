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
  take, and assembly rows if exposed by vendor-approved views.

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
   - Replace the example `dbo.vw_AutoCount...` view names with vendor-approved views or SQL.
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

5. Install Python dependencies on the VM:

   ```powershell
   py -m pip install pyodbc
   ```

   The VM also needs Microsoft ODBC Driver for SQL Server installed.

6. Validate config without connecting to SQL:

   ```powershell
   py scripts\autocount_stock_extract.py --config D:\AutoCountStockExtract\autocount_stock_extract.local.json --dry-run
   ```

7. Run a manual extraction for a known business date:

   ```powershell
   py scripts\autocount_stock_extract.py --config D:\AutoCountStockExtract\autocount_stock_extract.local.json --business-date 2026-06-04
   ```

8. Register the daily scheduled task:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_autocount_stock_extract_task.ps1 `
     -PythonExe "py" `
     -ConfigPath "D:\AutoCountStockExtract\autocount_stock_extract.local.json" `
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
row counts, status, storage paths, and exception summaries only. It does not
include raw stock rows or credentials.

## Reconciliation Checks

Before trusting dashboard numbers:

- Compare `stock_balance.csv` totals against AutoCount Stock Balance/Status for the same date.
- Re-run one business date twice and confirm no duplicate rows are created.
- Enter or identify a backdated adjustment in a test/sandbox company and confirm the 3-day overlap window catches it.
- Confirm the SQL login cannot insert, update, delete, execute posting procedures, or alter schema.
- Confirm archive files are stored only in approved secure storage, not GitHub.

## Notes For Vendor Ingenious/Mike

Please confirm one of these approved extraction paths:

- SQL views for stock master, daily stock balance, stock movement/stock card, and stock documents.
- AutoCount REST/API endpoints such as `/stock/read`, `/stock/balance/read`, `/stock/card/read`, `/stock/status`, and stock document reads.
- AutoCount .NET/report helper output for stock status, stock balance/cost, and stock aging.

The extractor is intentionally read-only. Phase 1 should not post stock,
adjustment, transfer, or accounting entries back into AutoCount.
