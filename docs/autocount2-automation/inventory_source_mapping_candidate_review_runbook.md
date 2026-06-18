# Inventory Source Mapping Candidate Review Runbook

Use this after the inventory staging schema gap review has completed. The
source mapping candidate review reads only local manifests and source column
metadata. It does not query AutoCount, read raw ERP rows into the report, build
dashboards, load a database, schedule jobs, call an API, or write back to AC2.

## Local Command

```powershell
cd C:\XB\automation

$schemaGapRun = "C:\XB\autocount_outputs\review\inventory_staging_schema_gap_review\<run-folder>"
$manifest = "$schemaGapRun\inventory_staging_schema_gap_review_manifest.json"

python scripts\autocount_inventory_source_mapping_candidate_review.py --manifest $manifest
```

Generated review outputs are written under:

```text
C:\XB\autocount_outputs\review\inventory_source_mapping_candidate_review
```

Each review run writes:

- `inventory_source_mapping_candidate_review_manifest.json`
- `inventory_source_mapping_candidate_review_report.md`

Generated outputs must remain local and ignored.

## What It Reviews

The review summarizes safe metadata-only candidate paths for known source-column
gaps:

- `DocNo` gaps look for related header surfaces with both `DocKey` and `DocNo`.
- `SmallestQty` gaps look for quantity-like columns such as `Qty`,
  `Quantity`, `BaseQty`, `SmallestQty`, `TransferQty`, or `ReceiveQty` on
  related metadata surfaces.
- `BatchBalQty` gaps are checked against dependent staging fields. When no
  staging field depends on `BatchBalQty`, the review flags it for expected
  column review rather than treating it as dashboard-ready.

The recommendation remains:

```text
no_dashboard_until_schema_gap_resolved
```

while any candidate mapping still needs manual validation.

## Safe Evidence

The generated report is intended to be safe to paste back after reviewing for
accidental secrets. It includes only:

- status and recommendation
- source surfaces reviewed
- missing source column names
- dependent staging field names
- candidate source surface names
- candidate bridge/value column names
- candidate classification and confidence
- warning and exception counts
- generated filenames

It must not include raw CSV rows, document numbers, supplier names, item codes,
prices, costs, quantities, or other raw ERP/business identifiers.

## Safe Summary Command

```powershell
$reviewRun = "C:\XB\autocount_outputs\review\inventory_source_mapping_candidate_review\<run-folder>"
$review = Get-Content -Raw -Path (Join-Path $reviewRun 'inventory_source_mapping_candidate_review_manifest.json') | ConvertFrom-Json

[pscustomobject]@{
  run_path = $review.storage.run_path
  status = $review.status
  recommendation = $review.recommendation
  decision = $review.decision
  business_reconciliation_status = $review.business_reconciliation_status
  data_maturity = $review.data_maturity
  final_production_selected = $review.final_production_selected
  warning_count = $review.warning_count
  exception_count = $review.exception_count
}

$review.mapping_candidates |
  Select-Object gap_source_surface, missing_source_column, candidate_source_surface, candidate_classification, confidence
```

Do not paste raw staging CSV rows, supplier names, item codes, document numbers,
prices, costs, quantities, or other raw ERP/business identifiers.

## Next Step

Use this review only to decide future schema mapping and source preference
questions. Do not build dashboards, purchase recommendations, business KPIs,
joins, reconciled dimensions, DB loads, scheduler, write-back/import/API
behavior, or final mapping logic from this review.
