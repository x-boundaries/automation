# Inventory Pipeline Local Runbook

Use this to run the current approved read-only inventory pipeline chain and
emit one human-safe local summary. The runner orchestrates existing local phase
scripts and writes a final manifest/report with paths, row counts, warning
counts, exception counts, and the final decision.

It does not build dashboards, issue purchase recommendations, create KPIs,
join raw snapshots, create reconciled dimensions, load a database, schedule
jobs, call a write API, or write back to AC2.

## Local Command

```powershell
cd C:\XB\automation

python scripts\autocount_inventory_pipeline_local_run.py
```

Generated final summary outputs are written under:

```text
C:\XB\autocount_outputs\review\inventory_pipeline_local_run
```

Each pipeline local run writes:

- `inventory_pipeline_local_run_manifest.json`
- `inventory_pipeline_local_run_report.md`

Generated outputs must remain local and ignored.

## Existing Extract Manifest Mode

Use this mode when a raw selected inventory operation extract already exists
and you want to avoid re-querying AutoCount while testing the later local
staging/review phases.

```powershell
cd C:\XB\automation

$extractRun = "C:\XB\autocount_outputs\extract\inventory_operations\<run-folder>"
$manifest = "$extractRun\inventory_operation_extract_manifest.json"

python scripts\autocount_inventory_pipeline_local_run.py --existing-extract-manifest $manifest
```

In this mode, the runner skips raw extraction and starts from the staging build.

## Pipeline Phases

The local runner executes or reuses these phase manifests:

1. selected inventory operation raw extract
2. inventory staging build
3. inventory staging audit
4. inventory staging warning review
5. inventory staging schema gap review

The final summary includes:

- status
- final decision and final recommendation
- source extract run path
- staging build run path
- audit run path
- warning review run path
- schema gap review run path
- row counts by staging table
- warning counts by phase
- exception counts by phase
- generated filenames

It does not print phase warning details, exception bodies, raw CSV rows, raw
document numbers, supplier names, item codes, prices, costs, quantities, or
other raw ERP/business identifiers.

## Decision Rules

- If any phase has exceptions or a missing phase manifest, final decision is
  `pipeline_blocked_by_exceptions`.
- If the schema gap review recommends
  `no_dashboard_until_schema_gap_resolved`, final decision remains
  `no_dashboard_until_schema_gap_resolved`.
- If all phases pass and only thin movement data remains, final decision may be
  `pipeline_ready_for_reconciliation_data_thin`.

All final outputs remain:

- `decision = Needs reconciliation`
- `business_reconciliation_status = not_reconciled`
- `data_maturity = immature_pre_go_live`
- `final_production_selected = false`

## Safe Summary Command

After reviewing for accidental secrets, the following summary is generally safe
to paste back. It prints run paths, counts, and generated filenames only.

```powershell
$pipelineRun = "C:\XB\autocount_outputs\review\inventory_pipeline_local_run\<run-folder>"
$pipeline = Get-Content -Raw -Path (Join-Path $pipelineRun 'inventory_pipeline_local_run_manifest.json') | ConvertFrom-Json

[pscustomobject]@{
  run_path = $pipeline.storage.run_path
  status = $pipeline.status
  final_decision = $pipeline.final_decision
  final_recommendation = $pipeline.final_recommendation
  source_extract_run_path = $pipeline.source_extract_run_path
  staging_build_run_path = $pipeline.staging_build_run_path
  audit_run_path = $pipeline.audit_run_path
  warning_review_run_path = $pipeline.warning_review_run_path
  schema_gap_review_run_path = $pipeline.schema_gap_review_run_path
  decision = $pipeline.decision
  business_reconciliation_status = $pipeline.business_reconciliation_status
  data_maturity = $pipeline.data_maturity
  final_production_selected = $pipeline.final_production_selected
}

$pipeline.row_counts_by_staging_table.PSObject.Properties |
  Sort-Object Name |
  ForEach-Object {
    [pscustomobject]@{
      staging_table = $_.Name
      row_count = $_.Value
    }
  }

$pipeline.warning_counts_by_phase.PSObject.Properties |
  Sort-Object Name |
  ForEach-Object {
    [pscustomobject]@{
      phase = $_.Name
      warning_count = $_.Value
    }
  }

$pipeline.exception_counts_by_phase.PSObject.Properties |
  Sort-Object Name |
  ForEach-Object {
    [pscustomobject]@{
      phase = $_.Name
      exception_count = $_.Value
    }
  }
```

Do not paste raw CSV rows, supplier names, item codes, document numbers, prices,
costs, quantities, screenshots, credentials, production connection strings, or
other raw ERP/business identifiers.

## Next Step

Use this local summary only to decide whether the read-only staging/review
pipeline can proceed to future reconciliation planning. Keep dashboards,
purchase recommendations, business KPIs, joins, reconciled dimensions, DB
loads, scheduler, write-back/import/API behavior, and CoA/GL/bank/accounting
migration parked.
