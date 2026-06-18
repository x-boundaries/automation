# Inventory Operation Staging Schema Gap Review Runbook

Use this after the local inventory staging warning review has completed. The
schema gap review reads only local manifests and staging CSV headers. It does
not read raw CSV rows into the report, query AutoCount, require database
credentials, build dashboards, load a database, schedule jobs, call an API, or
write back to AC2.

## Local Command

```powershell
cd C:\XB\automation

$warningRun = "C:\XB\autocount_outputs\review\inventory_staging_warning_review\inventory_staging_warning_review_20260618_170812_7299ed51"
$manifest = "$warningRun\inventory_staging_warning_review_manifest.json"

python scripts\autocount_inventory_staging_schema_gap_review.py --manifest $manifest
```

Generated review outputs are written under:

```text
C:\XB\autocount_outputs\review\inventory_staging_schema_gap_review
```

Each review run writes:

- `inventory_staging_schema_gap_review_manifest.json`
- `inventory_staging_schema_gap_review_report.md`

Generated outputs must remain local and ignored.

## What It Reviews

The review summarizes dashboard blockers from the warning review manifest and
keeps them in a decision queue:

- `source_schema_gap` blockers identify source surfaces that need source column
  mapping, alternative-surface review, or safe deferral until data arrives.
- `numeric_candidate_not_computable` blockers identify staging candidate fields
  that need numeric type mapping before outstanding PO dashboards can be trusted.
- duplicate source overlap warnings identify supplier and purchase order header
  staging tables that will need future dimension/source-preference dedupe rules.
- zero-row operational movement tables remain `safe_until_data_arrives`.

The recommendation remains:

```text
no_dashboard_until_schema_gap_resolved
```

when schema gaps or numeric candidate issues remain.

## Safe Evidence

The generated report is intended to be safe to paste back after reviewing for
accidental secrets. It includes only:

- status and review conclusion
- source surfaces reviewed
- staging tables reviewed
- missing expected header names
- present header names
- expected source column names, when upstream metadata preserved them
- present source column names, when upstream metadata preserved them
- missing source column names and dependent staging fields, when available
- explicit `missing_source_columns_unknown` /
  `requires_source_column_mapping_evidence` labels when the warning payload does
  not preserve exact source-column detail
- duplicate source overlap warnings
- recommendation
- warning and exception counts
- generated filenames

It must not include raw CSV rows, document numbers, supplier names, item codes,
prices, costs, quantities, or other raw ERP/business identifiers.

## Safe Summary Command

```powershell
$reviewRun = "C:\XB\autocount_outputs\review\inventory_staging_schema_gap_review\<run-folder>"
$review = Get-Content -Raw -Path (Join-Path $reviewRun 'inventory_staging_schema_gap_review_manifest.json') | ConvertFrom-Json

[pscustomobject]@{
  run_path = $review.storage.run_path
  status = $review.status
  review_conclusion = $review.review_conclusion
  recommendation = $review.recommendation
  decision = $review.decision
  business_reconciliation_status = $review.business_reconciliation_status
  data_maturity = $review.data_maturity
  final_production_selected = $review.final_production_selected
  warning_count = $review.warning_count
  exception_count = $review.exception_count
}

$review.source_schema_gaps |
  Select-Object source_surface, staging_table, classification, source_column_gap_classification, missing_source_columns_unknown

$review.numeric_candidate_reviews |
  Select-Object source_surface, staging_table, classification

$review.duplicate_overlap_reviews |
  Select-Object staging_table, classification
```

Do not paste raw staging CSV rows, supplier names, item codes, document numbers,
prices, costs, quantities, or other raw ERP/business identifiers.

## Next Step

Use this review to decide future schema mapping, source preference, and dedupe
rules only. Do not build dashboards, purchase recommendations, business KPIs,
joins, reconciled dimensions, DB loads, scheduler, or write-back/import/API
behavior from this review.
