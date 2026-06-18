# Inventory Source Metadata Coverage Review Runbook

Use this after the inventory source mapping candidate review has completed. The
metadata coverage review reads local manifests and source column metadata. It
does not read raw ERP rows into the report, build dashboards, load a database,
schedule jobs, call an API, or write back to AC2.

## Local Command

```powershell
cd C:\XB\automation

$mappingRun = "C:\XB\autocount_outputs\review\inventory_source_mapping_candidate_review\<run-folder>"
$manifest = "$mappingRun\inventory_source_mapping_candidate_review_manifest.json"

python scripts\autocount_inventory_source_metadata_coverage_review.py --manifest $manifest
```

Generated review outputs are written under:

```text
C:\XB\autocount_outputs\review\inventory_source_metadata_coverage_review
```

Each review run writes:

- `inventory_source_metadata_coverage_review_manifest.json`
- `inventory_source_metadata_coverage_review_report.md`

Generated outputs must remain local and ignored.

## Optional Metadata Probe

When existing extract manifests do not include enough related surface metadata,
operators may run the script with the catalog-only probe mode:

```powershell
$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = "<read-only SQL Server connection string>"
python scripts\autocount_inventory_source_metadata_coverage_review.py --manifest $manifest --use-sql-metadata-probe
```

The probe mode reads SQL Server catalog metadata only from `sys.objects`,
`sys.schemas`, `sys.columns`, and `sys.types`. It does not select raw
ERP/business rows. Do not paste connection strings, credentials, screenshots, or
raw SQL output into GitHub.

## What It Reviews

The review summarizes safe metadata-only coverage for unresolved source mapping
gaps:

- surfaces with `DocNo`
- surfaces with `DocKey`
- surfaces with both `DocNo` and `DocKey`
- GRN header-looking surfaces
- stock receive header-looking surfaces
- stock transfer header-looking surfaces
- quantity-like columns such as `Qty`, `Quantity`, `BaseQty`, `SmallestQty`,
  `ReceiveQty`, `TransferQty`, `TransferredQty`, `BatchBalQty`, or `BalQty`
- unresolved mapping gaps that still need more metadata or manual validation

The recommendation remains:

```text
no_dashboard_until_schema_gap_resolved
```

while any mapping question remains unresolved.

## Safe Evidence

The generated report is intended to be safe to paste back after reviewing for
accidental secrets. It includes only:

- status and recommendation
- reviewed source surface names
- source column names
- `DocNo` / `DocKey` candidate classifications
- quantity-like column candidate classifications
- unresolved metadata gap reasons
- warning and exception counts
- generated filenames

It must not include raw CSV rows, document numbers, supplier names, item codes,
prices, costs, quantities, or other raw ERP/business identifiers.

## Safe Summary Command

```powershell
$reviewRun = "C:\XB\autocount_outputs\review\inventory_source_metadata_coverage_review\<run-folder>"
$review = Get-Content -Raw -Path (Join-Path $reviewRun 'inventory_source_metadata_coverage_review_manifest.json') | ConvertFrom-Json

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

$review.docno_doc_key_candidates |
  Select-Object source_surface, candidate_classification, confidence

$review.quantity_column_candidates |
  Select-Object source_surface, quantity_like_columns, confidence

$review.unresolved_metadata_gaps |
  Select-Object gap_source_surface, missing_source_column, candidate_classification, metadata_gap_reason
```

Do not paste raw staging CSV rows, supplier names, item codes, document numbers,
prices, costs, quantities, or other raw ERP/business identifiers.

## Next Step

Use this review only to decide what metadata to inspect before a future mapping
candidate review. Do not build dashboards, purchase recommendations, business
KPIs, joins, reconciled dimensions, DB loads, scheduler, write-back/import/API
behavior, or final mapping logic from this review.
