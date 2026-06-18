# Inventory Operation Staging Build Runbook

## Purpose

Use this local package builder after raw snapshot extraction and staging
readiness review. It consumes the PR #50
`inventory_operation_extract_manifest.json` and the selected local CSV snapshot
files referenced by `surface_exports`, then emits normalized local staging CSVs.

It does not query AutoCount, does not require database credentials, does not
write to a production database, does not schedule work, and does not build
dashboards.

All outputs remain `Needs reconciliation`,
`business_reconciliation_status=not_reconciled`,
`data_maturity=immature_pre_go_live`, and
`final_production_selected=false`.

## Local PowerShell Command

```powershell
cd C:\XB\automation

$extractRun = "C:\XB\autocount_outputs\extract\inventory_operations\inventory_operation_extract_20260618_154313_a804be8d"
$manifest = "$extractRun\inventory_operation_extract_manifest.json"

python scripts\autocount_inventory_staging_build.py --manifest $manifest
```

## Output Files

The script writes a timestamped folder under:

```text
C:\XB\autocount_outputs\staging\inventory_operations
```

It emits:

- `inventory_staging_build_manifest.json`
- `inventory_staging_build_report.md`
- normalized staging CSVs such as `stg_ac2_supplier.csv`,
  `stg_ac2_purchase_order_header.csv`, and `stg_ac2_transfer_header.csv`

Generated staging outputs are local only and must not be committed.

## Safe Paste-Back Command

This command prints manifest/report-safe fields only: run path, status, staging
table row counts, warnings, exceptions, and emitted file names. It does not
paste raw staging CSV rows.

```powershell
$stagingRun = "C:\XB\autocount_outputs\staging\inventory_operations\<run-folder>"
$manifest = Get-Content -Raw -Path (Join-Path $stagingRun 'inventory_staging_build_manifest.json') | ConvertFrom-Json

[pscustomobject]@{
  run_path = $manifest.storage.run_path
  status = $manifest.status
  source_extract_run_id = $manifest.source_extract_run_id
  decision = $manifest.decision
  business_reconciliation_status = $manifest.business_reconciliation_status
  data_maturity = $manifest.data_maturity
  final_production_selected = $manifest.final_production_selected
  warning_count = @($manifest.warnings).Count
  exception_count = @($manifest.exceptions).Count
} | Format-List

'Staging table row counts:'
$manifest.staging_tables |
  Select-Object table_name, row_count, file_name |
  Format-Table -AutoSize

'Warnings:'
$manifest.warnings

'Exceptions:'
$manifest.exceptions
```

Do not paste raw staging CSV rows, raw business identifiers, screenshots,
connection strings, credentials, `.local` configs, or generated output files.

## Current Interpretation

Data remains thin. GRN, receive, and transfer sources currently have 0 rows; PO,
supplier, stock movement, and item balance reference sources have limited rows.
Dashboards are not meaningful yet.

## Next Step

After this PR, run the local staging build and review only the staging
manifest/report. Do not implement dashboard charts yet.
