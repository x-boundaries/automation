# Inventory Operation Staging Audit Runbook

Use this after the local inventory staging build has completed. The audit reads
only the `inventory_staging_build_manifest.json` and the local staging CSV files
referenced by that manifest.

It does not query AutoCount, require database credentials, build dashboards,
load a database, schedule jobs, call an API, or write back to AC2.

## Local Command

```powershell
cd C:\XB\automation

$stagingRun = "C:\XB\autocount_outputs\staging\inventory_operations\inventory_staging_build_20260618_162235_47682208"
$manifest = "$stagingRun\inventory_staging_build_manifest.json"

python scripts\autocount_inventory_staging_audit.py --manifest $manifest
```

Generated review outputs are written under:

```text
C:\XB\autocount_outputs\review\inventory_staging_audit
```

Each audit run writes:

- `inventory_staging_audit_manifest.json`
- `inventory_staging_audit_report.md`

Generated outputs must remain local and ignored.

## What It Checks

- All 11 expected staging CSV files exist.
- Each staging CSV has the expected headers.
- Every staging CSV keeps lineage headers:
  - `source_surface`
  - `source_extract_run_id`
  - `source_row_number`
- The staging build manifest remains:
  - `decision = Needs reconciliation`
  - `business_reconciliation_status = not_reconciled`
  - `data_maturity = immature_pre_go_live`
  - `final_production_selected = false`
- Zero-row operational movement tables are classified as `data_thin`, not as
  dashboard evidence.
- Duplicate staging source overlap is reported as a review warning only.
- `missing_expected_columns:*` warnings are carried forward from the staging
  build manifest.

## Safe Summary Command

After reviewing for accidental secrets, the following summary is generally safe
to paste back. It prints counts and filenames only; it does not print raw CSV
contents or raw rows.

```powershell
$auditRun = "C:\XB\autocount_outputs\review\inventory_staging_audit\<run-folder>"
$audit = Get-Content -Raw -Path (Join-Path $auditRun 'inventory_staging_audit_manifest.json') | ConvertFrom-Json

[pscustomobject]@{
  run_path = $audit.storage.run_path
  status = $audit.status
  audit_decision = $audit.audit_decision
  decision = $audit.decision
  business_reconciliation_status = $audit.business_reconciliation_status
  data_maturity = $audit.data_maturity
  final_production_selected = $audit.final_production_selected
  warning_count = $audit.warning_count
  exception_count = $audit.exception_count
}

$audit.row_counts.PSObject.Properties |
  Sort-Object Name |
  ForEach-Object {
    [pscustomobject]@{
      staging_table = $_.Name
      row_count = $_.Value
    }
  }

[pscustomobject]@{
  missing_files = @($audit.missing_files).Count
  missing_headers = @($audit.missing_headers).Count
  generated_files = @(
    'inventory_staging_audit_manifest.json',
    'inventory_staging_audit_report.md'
  ) -join ', '
}
```

Do not paste raw staging CSV rows, supplier names, item codes, document numbers,
or other raw ERP/business identifiers.

## Next Step

Review the local audit manifest and report only. Do not build dashboards,
purchase recommendations, business KPIs, joins, reconciled dimensions, DB
loads, scheduler, or write-back/import/API behavior from this audit.
