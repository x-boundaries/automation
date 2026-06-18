# Inventory Operation Staging Readiness Runbook

## Purpose

Use this review after the local inventory operation raw snapshot extractor has
run. The readiness script consumes only
`inventory_operation_extract_manifest.json` metadata and produces a staging
readiness report.

It does not require a database connection, does not query AutoCount, and does
not read raw CSV rows.

All outputs remain `Needs reconciliation`,
`business_reconciliation_status=not_reconciled`,
`data_maturity=immature_pre_go_live`, and
`final_production_selected=false`.

## Required Input

Provide the path to the PR #50 extractor manifest:

- `inventory_operation_extract_manifest.json`

The script intentionally reads the PR #50 field `surface_exports`.

Manifest fields used:

- `row_counts`
- `schema_metadata`
- `surface_exports`
- `selected_surfaces`
- `warnings`
- guardrail fields

## Local PowerShell Command

```powershell
cd C:\XB\automation

$extractRun = "C:\XB\autocount_outputs\extract\inventory_operations\inventory_operation_extract_20260618_154313_a804be8d"
$manifest = "$extractRun\inventory_operation_extract_manifest.json"

python scripts\autocount_inventory_staging_readiness.py --manifest $manifest
```

If the VM does not have `python` on `PATH`, replace `python` with the full
Python executable path used for the other AutoCount scripts.

## Output Files

The script writes a timestamped folder under:

```text
C:\XB\autocount_outputs\review\inventory_staging_readiness
```

Output files:

- `inventory_staging_readiness_manifest.json`
- `inventory_staging_readiness_report.md`

Generated review outputs must remain local and ignored.

## Safe Paste-Back Command

This command prints only manifest/report-safe fields, including status,
readiness decision, row count summary, and warning summary. It does not paste
raw CSV rows.

```powershell
$reviewRun = "C:\XB\autocount_outputs\review\inventory_staging_readiness\<run-folder>"
$review = Get-Content -Raw -Path (Join-Path $reviewRun 'inventory_staging_readiness_manifest.json') | ConvertFrom-Json

[pscustomobject]@{
  status = $review.status
  readiness_decision = $review.staging_readiness_decision
  source_extract_status = $review.source_extract.status
  zero_row_ready_count = @($review.zero_row_ready_surfaces).Count
  non_empty_count = @($review.non_empty_surfaces).Count
  warning_count = @($review.warning_summary).Count
  decision = $review.decision
  business_reconciliation_status = $review.business_reconciliation_status
  data_maturity = $review.data_maturity
  final_production_selected = $review.final_production_selected
} | Format-List

'Row count summary:'
$review.surface_readiness |
  Select-Object object_id, readiness_status, row_count, export_status, schema_available |
  Format-Table -AutoSize

'Warning summary:'
$review.warning_summary
```

Do not paste raw CSV rows, raw business identifiers, screenshots, connection
strings, credentials, `.local` configs, or generated raw output files.

## Expected Current Interpretation

Based on the local PR #50 extractor run:

- GRN, stock receive, and stock transfer surfaces are ready but empty.
- PO, supplier, `StockDTL`, and `vItemBalQty` surfaces are ready with limited
  data.
- Dashboard analytics are not meaningful yet because GRN/receive/transfer
  surfaces currently have 0 rows.
- All surfaces remain not reconciled and not production approved.

## Next Step

Run the local staging readiness review first. Proceed to staging loader design
only if readiness is clean; do not build warehouse loading, dashboards,
scheduler, write-back, or import/API behavior from this review.
