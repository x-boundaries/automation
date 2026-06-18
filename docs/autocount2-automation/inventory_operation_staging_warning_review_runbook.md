# Inventory Operation Staging Warning Review Runbook

Use this after the local inventory staging audit has completed. The warning
review reads only local manifests and staging CSV headers. It does not read raw
CSV rows into the report, query AutoCount, require database credentials, build
dashboards, load a database, schedule jobs, call an API, or write back to AC2.

## Local Command

```powershell
cd C:\XB\automation

$auditRun = "C:\XB\autocount_outputs\review\inventory_staging_audit\inventory_staging_audit_20260618_164001_b3acc461"
$manifest = "$auditRun\inventory_staging_audit_manifest.json"

python scripts\autocount_inventory_staging_warning_review.py --manifest $manifest
```

Generated review outputs are written under:

```text
C:\XB\autocount_outputs\review\inventory_staging_warning_review
```

Each review run writes:

- `inventory_staging_warning_review_manifest.json`
- `inventory_staging_warning_review_report.md`

Generated outputs must remain local and ignored.

## What It Classifies

Warnings are classified into:

- `source_data_thin`
- `source_schema_gap`
- `numeric_candidate_not_computable`
- `carried_forward_disclaimer`
- `review_only_duplicate_source_overlap`

The current known staging warnings are expected to remain explainable:

- `missing_expected_columns:*` warnings for GRN, receive, and transfer detail
  surfaces are `source_schema_gap`.
- `outstanding_qty_candidate_not_numeric:dbo.PODTL` is
  `numeric_candidate_not_computable`.
- Zero-row operational movement tables are `source_data_thin`, not a failure.
- Duplicate supplier or PO source overlap is review-only evidence.

Where warning evidence would block future dashboard interpretation, it is marked
`dashboard_blocker_when_data_arrives`. This review does not build dashboards.

## Safe Evidence

The generated report is intended to be safe to paste back after reviewing for
accidental secrets. It includes only:

- table names
- expected header names
- present header names
- row counts
- warning codes
- generated filenames

It must not include raw CSV rows, document numbers, supplier names, item codes,
prices, costs, quantities, or other raw ERP/business identifiers.

## Safe Summary Command

```powershell
$reviewRun = "C:\XB\autocount_outputs\review\inventory_staging_warning_review\<run-folder>"
$review = Get-Content -Raw -Path (Join-Path $reviewRun 'inventory_staging_warning_review_manifest.json') | ConvertFrom-Json

[pscustomobject]@{
  run_path = $review.storage.run_path
  status = $review.status
  readiness_conclusion = $review.readiness_conclusion
  decision = $review.decision
  business_reconciliation_status = $review.business_reconciliation_status
  data_maturity = $review.data_maturity
  final_production_selected = $review.final_production_selected
  warning_count = $review.warning_count
  exception_count = $review.exception_count
}

$review.row_counts.PSObject.Properties |
  Sort-Object Name |
  ForEach-Object {
    [pscustomobject]@{
      staging_table = $_.Name
      row_count = $_.Value
    }
  }

$review.warning_classifications |
  Select-Object warning_code, classification, dashboard_impact
```

Do not paste raw staging CSV rows, supplier names, item codes, document numbers,
prices, costs, quantities, or other raw ERP/business identifiers.

## Next Step

Review the warning classifications only. Keep the readiness conclusion as
`staging_warning_review_ready_data_thin` while current AC2 data remains
immature/pre-go-live/test/partial. Do not build dashboards, purchase
recommendations, business KPIs, joins, reconciled dimensions, DB loads,
scheduler, or write-back/import/API behavior from this review.
