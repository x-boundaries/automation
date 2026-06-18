# Inventory Operation Aggregate Validation Runbook

## Purpose

Use this local validation pack after the selected inventory operation profile
draft and before any selected raw snapshot extraction design.

The validator checks selected AutoCount 2.0 SQL surfaces with aggregate-only
queries:

- selected surface exists,
- object type,
- readable through `xb_ac2_readonly`,
- `COUNT_BIG(1)` row count,
- required/expected columns present or missing,
- nullable/blank counts for critical columns,
- distinct counts for safe key columns,
- min/max dates for safe date columns,
- basic consistency checks for future join keys.

It does not export raw ERP/business rows, sample rows, top-N values, distinct
values, raw CSVs, screenshots, connection strings, or credentials.

Every result remains `Needs reconciliation`,
`data_maturity=immature_pre_go_live`,
`business_reconciliation_status=not_reconciled`, and
`final_production_selected=false`.

## Confirmed Target

Run only on the confirmed AutoCount 2.0 target:

- Server: `localhost\A2006`
- Database: `AED_XBOUNDARIES`
- AutoCount DB/app version: `2.2.94`
- SQL login/user: `xb_ac2_readonly`

The config rejects legacy/wrong targets:

- `localhost\SQLEXPRESS / AED_XBoundaries`
- `localhost\SQLEXPRESS / A893478`

## Selected Surface Scope

The example config validates the selected PR #48 inventory operation draft:

- GRN header: `dbo.vGoodsReceivedNote`, `dbo.GR`
- GRN detail: `dbo.vGoodsReceivedNoteDetail`,
  `dbo.vGoodsReceivedNoteSubDetail`, `dbo.GRDTL`
- Stock receive: `dbo.vStockReceive`, `dbo.vStockReceiveDetail`
- Transfer header: `dbo.vStockTransfer`, `dbo.XFER`
- Transfer detail: `dbo.vStockTransferDetail`
- Outstanding PO / in-transit: `dbo.vPurchaseOrder`, `dbo.PO`, `dbo.PODTL`
- Supplier context: `dbo.vCreditor`, `dbo.Creditor`
- Already referenced stock movement/balance candidates:
  `dbo.StockDTL`, `dbo.vItemBalQty`

Transfer/GIT fields are validated only as column evidence on
`dbo.vStockTransfer`:

- `XFERUDF_GIT`
- `XFERUDF_RcvDate`
- `XFERUDF_RcvBy`
- `XFERUDF_UseGIT`

CoA, GL, bank opening balances, full accounting cutover, finance migration
ownership, replacing AC2 accounting, scheduler, write-back, dashboard, and
warehouse build remain out of scope.

## Local PowerShell Command

Run this on the Windows AC2 VM. It keeps the password in the current PowerShell
process only and clears it after the run.

```powershell
cd C:\XB\automation

Copy-Item config\autocount_inventory_operation_validate.example.json config\autocount_inventory_operation_validate.local.json -Force

$password = Read-Host 'Password for xb_ac2_readonly on localhost\A2006 / AED_XBOUNDARIES' -AsSecureString
$plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
)

$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = "Driver={ODBC Driver 17 for SQL Server};Server=localhost\A2006;Database=AED_XBOUNDARIES;UID=xb_ac2_readonly;PWD=$plainPassword;Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly;"

try {
  python scripts\autocount_inventory_operation_validate.py --config config\autocount_inventory_operation_validate.local.json
} finally {
  Remove-Item Env:\AUTOCOUNT_READONLY_SQL_CONNECTION_STRING -ErrorAction SilentlyContinue
  Remove-Variable plainPassword -ErrorAction SilentlyContinue
  Remove-Variable password -ErrorAction SilentlyContinue
}
```

If the VM does not have `python` on `PATH`, replace `python` with the full
Python executable path used for the other AutoCount scripts.

## Output Files

Each run writes a timestamped local folder under:

```text
C:\XB\autocount_outputs\probe\inventory_operations_validation
```

The folder contains:

- `inventory_operation_validation_manifest.json`
- `inventory_operation_validation_report.md`

Generated outputs and `.local` config files are ignored and must not be
committed.

## Failure Behavior

- Target validation failure: `failed`.
- Connection failure: `failed`.
- Selected surface missing: continue with `success_with_warnings`; mark the
  surface missing.
- Surface unreadable: continue with `success_with_warnings`; mark unreadable.
- Aggregate failure on one surface: continue with `success_with_warnings`; mark
  that surface failed.
- All critical selected surfaces missing: `failed`.

## Safe To Paste Back

After reviewing for accidental secrets, the following aggregate-only fields are
safe to paste into a PR or chat:

- status and run path,
- database context,
- selected surface exists/readable flags,
- object type,
- row counts,
- expected/missing column names,
- blank counts,
- distinct counts without values,
- min/max date summaries,
- warnings and redacted exceptions.

Do not paste raw ERP/business rows, raw CSVs, screenshots, connection strings,
passwords, supplier names, item codes, document numbers, customer names,
location names, or distinct key values.

## Next Step

After this PR, run the local aggregate validation on the AC2 VM and use the
results to decide whether selected raw snapshot extraction is safe to design
next.
