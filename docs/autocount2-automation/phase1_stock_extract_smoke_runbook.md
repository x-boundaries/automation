# Phase 1 AC2 Stock Extraction Smoke Runbook

## Purpose

Use this runbook to repeat the verified AC2 stock extraction smoke test on the
Windows VM. The purpose is to prove AC2 stock data can be extracted into a
local CSV archive safely, with a manifest that can be reviewed without
committing raw ERP data.

This is not scheduled extraction. It does not approve SQL write-back, final
wrapper views, or final production-selected SQL surfaces.

## Prerequisites

- Python 3.9 or newer.
- Python package: `pyodbc`.
- Microsoft ODBC Driver 17 for SQL Server.
- Windows timezone package for `ZoneInfo("Asia/Singapore")`: `tzdata`.
- `AUTOCOUNT_READONLY_SQL_CONNECTION_STRING` set outside the repository.

Install Python packages on the VM:

```powershell
python -m pip install pyodbc tzdata
```

If timezone initialization fails on Windows, install or reinstall `tzdata`:

```powershell
python -m pip install tzdata
```

## Standard Local Output Root

All generated AutoCount outputs should stay under:

```text
C:\XB\autocount_outputs
```

Workflow subfolders:

```text
C:\XB\autocount_outputs\probe
C:\XB\autocount_outputs\reconcile
C:\XB\autocount_outputs\extract\stock
```

Older ad hoc folders such as `C:\XB\autocount_stock_extract_outputs`,
`C:\XB\autocount_phase1_reconcile_outputs`, and
`C:\XB\autocount_probe_outputs` are legacy/manual paths and should be avoided
going forward.

Raw CSVs stay local and must not be committed. Paste or share only reviewed
manifests and safe summaries, such as `run_manifest.json`.

## Prepare Local Config

Copy the secret-free smoke template to the ignored local config path:

```powershell
Copy-Item config\autocount_stock_extract.ac2_smoke.example.json config\autocount_stock_extract.local.json
```

Keep the SQL connection string outside Git:

```powershell
$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = '<ODBC connection string stored outside Git>'
```

PowerShell may save JSON as UTF-8 with a BOM. The extractor now reads JSON
config files with BOM tolerance, so local edits from PowerShell should load.

## Dry Run

Validate the config and extraction window without opening SQL or writing CSVs:

```powershell
python scripts\autocount_stock_extract.py --config config\autocount_stock_extract.local.json --business-date 2026-06-04 --dry-run
```

Expected window:

```text
business_date: 2026-06-04
movement_from: 2026-06-01T00:00:00+08:00
movement_to: 2026-06-05T00:00:00+08:00
storage_batch_id: ac2_stock_2026-06-04
```

## Real Smoke Extraction

Run the verified smoke date only after confirming the environment variable is
set outside the repo:

```powershell
python scripts\autocount_stock_extract.py --config config\autocount_stock_extract.local.json --business-date 2026-06-04
```

The expected manifest path under the standardized output root is:

```text
C:\XB\autocount_outputs\extract\stock\ac2_stock_2026-06-04\run_manifest.json
```

Verified smoke run result:

- `status`: `success`
- `exception_count`: `0`
- `business_date`: `2026-06-04`
- `movement_from`: `2026-06-01T00:00:00+08:00`
- `movement_to`: `2026-06-05T00:00:00+08:00`
- `storage_batch_id`: `ac2_stock_2026-06-04`
- `stock_master`: 21,831 rows
- `stock_balance`: 1 row
- `stock_movement`: 2 rows

`stock_balance` currently uses `dbo.vItemBalQty` for smoke validation and the
verified run returned only 1 row. Do not treat it as a final balance mapping.

## Data Handling

The smoke run generates:

- `stock_master.csv`
- `stock_balance.csv`
- `stock_movement.csv`
- `run_manifest.json`

Do not commit generated CSVs, local configs, `.env` files, credentials,
screenshots, production connection strings, or raw ERP/business data.

`stock_master.csv` contains raw ERP item descriptions and barcodes. Extractor
CSVs are spreadsheet-formula-neutralized before writing, but they remain
sensitive business data and must stay local. Paste/share only reviewed
`run_manifest.json` content unless an approved operator asks for a safe summary.

## Before Scheduling

Final scheduled extraction still requires:

- Dedicated read-only SQL login.
- Approved wrapper views or explicit approval for the direct smoke profile.
- Reconciliation sign-off against AutoCount UI/report outputs.
- Operator approval for scheduling.

No SQL write-back is allowed by this runbook.
