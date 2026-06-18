# Inventory Operation Raw Snapshot Runbook

## Purpose

Use this local-only extractor after aggregate validation has confirmed the
selected inventory operation surfaces are readable.

The extractor writes one raw CSV snapshot per selected surface so the team can
inspect extraction shape and prepare later staging/dashboard design. This is not
business approval, final production mapping, warehouse loading, scheduler work,
or write-back.

Every run remains `Needs reconciliation`,
`data_maturity=immature_pre_go_live`,
`business_reconciliation_status=not_reconciled`, and
`final_production_selected=false`.

## Confirmed Validation Context

The local aggregate validation run succeeded after read-only SELECT permission
was granted:

- Run path:
  `C:\XB\autocount_outputs\probe\inventory_operations_validation\inventory_operation_validation_20260618_151728_a825d672`
- Status: `success`
- Selected surfaces: 17
- Existing surfaces: 17
- Readable surfaces: 17
- Failed aggregates: 0
- Missing surfaces: 0

The current database snapshot is immature/pre-go-live/test/partial. GRN, stock
receive, and transfer surfaces were readable but empty; PO, supplier, `StockDTL`,
and `vItemBalQty` had limited rows.

## Selected Surface Scope

The extractor allowlist is limited to these validated surfaces:

- GRN header: `dbo.vGoodsReceivedNote`, `dbo.GR`
- GRN detail: `dbo.vGoodsReceivedNoteDetail`,
  `dbo.vGoodsReceivedNoteSubDetail`, `dbo.GRDTL`
- Stock receive: `dbo.vStockReceive`, `dbo.vStockReceiveDetail`
- Transfer header: `dbo.vStockTransfer`, `dbo.XFER`
- Transfer detail: `dbo.vStockTransferDetail`
- Outstanding PO / in-transit: `dbo.vPurchaseOrder`, `dbo.PO`, `dbo.PODTL`
- Supplier context: `dbo.vCreditor`, `dbo.Creditor`
- Movement/stock references: `dbo.StockDTL`, `dbo.vItemBalQty`

Transfer/GIT fields remain column evidence on `dbo.vStockTransfer`, not a
standalone selected object:

- `XFERUDF_GIT`
- `XFERUDF_RcvDate`
- `XFERUDF_RcvBy`
- `XFERUDF_UseGIT`

CoA, GL, bank opening balances, full accounting cutover, finance migration
ownership, replacing AC2 accounting, scheduler, write-back, dashboard, and
warehouse build remain out of scope.

## Local PowerShell Command

Run this only on the Windows AC2 VM. It keeps the password in the current
PowerShell process and clears it after the run.

```powershell
cd C:\XB\automation

Copy-Item config\autocount_inventory_operation_extract.example.json config\autocount_inventory_operation_extract.local.json -Force

$password = Read-Host 'Password for xb_ac2_readonly on localhost\A2006 / AED_XBOUNDARIES' -AsSecureString
$plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
)

$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = "Driver={ODBC Driver 17 for SQL Server};Server=localhost\A2006;Database=AED_XBOUNDARIES;UID=xb_ac2_readonly;PWD=$plainPassword;Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly;"

try {
  python scripts\autocount_inventory_operation_extract.py --config config\autocount_inventory_operation_extract.local.json
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
C:\XB\autocount_outputs\extract\inventory_operations
```

The folder contains:

- `inventory_operation_extract_manifest.json`
- `inventory_operation_extract_report.md`
- one `.csv` raw snapshot file per selected surface

Do not commit generated outputs. Generated outputs and `.local` config files
are ignored and must not be committed.

## Extraction Behavior

- Target validation runs before extraction.
- Only allowlisted selected surfaces are exported.
- SQL uses explicit column lists, never `SELECT *`.
- No joins or transformations are performed.
- No final dashboard metrics are computed.
- No date filter is applied by default because many selected surfaces currently
  have zero or very low row counts.
- Optional `row_limit` can be set in the local config for local test runs; the
  manifest records the row limit when used.
- Empty surfaces still produce a CSV with headers and row count `0`.
- Missing expected columns are warnings, not blockers, unless the extractor
  itself depends on them.
- CSV output uses spreadsheet formula-injection protection.
- Console output contains only status, run path, counts, row counts, and
  warnings. It does not print raw rows.

## Safe To Paste Back

After reviewing for accidental secrets, the following are safe to paste into a
PR or chat:

- run path,
- status,
- selected surface names,
- per-surface row counts,
- missing expected column warnings,
- redacted exceptions,
- manifest/report summaries.

Do not paste raw ERP/business rows, raw CSVs, screenshots, connection strings,
passwords, supplier names, item codes, document numbers, customer names,
location names, or other raw business identifiers.

## Next Step

After this PR, run local raw snapshot extraction and review only
manifest/report counts before staging/dashboard design. Do not start warehouse
loading, dashboard build, scheduler, or write-back from this PR.
