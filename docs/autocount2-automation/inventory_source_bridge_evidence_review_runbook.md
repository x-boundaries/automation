# Inventory Source Bridge Evidence Review Runbook

Use this after the inventory source metadata coverage review has completed. The
bridge evidence review reads the metadata coverage manifest only. It prepares a
manual-validation packet for likely `DocKey` -> `DocNo` bridge paths and
quantity-column evidence.

It does not read raw ERP rows, write SQL, load a database, build dashboards,
schedule jobs, export CSV rows, change staging builds, or select production
mappings.

## Local Command

```powershell
cd C:\XB\automation

$coverageRun = "C:\XB\autocount_outputs\review\inventory_source_metadata_coverage_review\<run-folder>"
$manifest = "$coverageRun\inventory_source_metadata_coverage_review_manifest.json"

python scripts\autocount_inventory_source_bridge_evidence_review.py --manifest $manifest
```

Generated review outputs are written under:

```text
C:\XB\autocount_outputs\review\inventory_source_bridge_evidence_review
```

Each review run writes:

- `inventory_source_bridge_evidence_review_manifest.json`
- `inventory_source_bridge_evidence_review_report.md`

Generated outputs must remain local and ignored.

## What It Reviews

The review identifies metadata-only bridge candidates that require manual
validation:

- GRN detail surfaces `dbo.vGoodsReceivedNoteDetail` and
  `dbo.vGoodsReceivedNoteSubDetail` to header surface `dbo.vGoodsReceivedNote`
- GR/GRDTL detail surface `dbo.GRDTL` to header surface `dbo.GR`
- Stock receive detail surface `dbo.vStockReceiveDetail` to header surface
  `dbo.vStockReceive`
- Stock transfer detail surface `dbo.vStockTransferDetail` to header surface
  `dbo.vStockTransfer`

The proposed bridge pattern is:

```text
detail.DocKey -> header.DocKey -> header.DocNo
```

The review also separates quantity evidence:

- direct `SmallestQty` availability on `dbo.vGoodsReceivedNoteDetail` and
  `dbo.GRDTL`
- possible quantity aliases on `dbo.vStockReceiveDetail` and
  `dbo.vStockTransferDetail`
- `BatchBalQty` on `dbo.vGoodsReceivedNoteSubDetail` as manual review only
  when no dependent staging field exists

No quantity alias is selected automatically.

## Required Status

The source metadata coverage manifest must remain blocked and unreconciled:

```text
decision = Needs reconciliation
business_reconciliation_status = not_reconciled
data_maturity = immature_pre_go_live
final_production_selected = false
recommendation = no_dashboard_until_schema_gap_resolved
```

If the source manifest is missing or has unsafe status fields, the bridge review
writes a failed output with redacted exceptions.

## Safe Summary Command

```powershell
$reviewRun = "C:\XB\autocount_outputs\review\inventory_source_bridge_evidence_review\<run-folder>"
$review = Get-Content -Raw -Path (Join-Path $reviewRun 'inventory_source_bridge_evidence_review_manifest.json') | ConvertFrom-Json

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

$review.bridge_candidates |
  Select-Object bridge_name, header_source_surface, dependent_staging_field, confidence, selection_status

$review.quantity_alias_reviews |
  Select-Object source_surface, reviewed_expected_column, candidate_classification, possible_alias_columns, selected_quantity_column

$review.expected_column_reviews |
  Select-Object gap_source_surface, missing_source_column, metadata_gap_reason, staging_mapping_status
```

Do not paste raw staging CSV rows, supplier names, item codes, document numbers,
prices, costs, quantities, credentials, connection strings, or other raw
ERP/business identifiers.

## Guardrails

- Diagnostic-only.
- Metadata/report review only.
- No SQL write-back.
- No scheduler.
- No raw ERP/business row export.
- No raw CSV row output.
- No credentials or connection strings.
- No staging build behaviour changes.
- No extraction behaviour changes.
- No final mapping selection.
- No dashboards.
- No KPIs.
- No purchase recommendations.
- No analytics joins.
- No DB loads.
- No reconciled dimensions.

## Next Step

Use this review only as a manual-validation decision packet. A future PR may
patch staging mappings only after manual validation confirms the bridge and
quantity assumptions.
