# Selected Surface Snapshot Extract Runbook

## Purpose

Use this extractor only after the selected-surface aggregate reconciliation has
run and the goal is to capture the current AC2 database snapshot for local
review. This is a narrow read-only raw snapshot export for selected surfaces; it
does not prove business correctness and it does not approve migration/import.

The current AC2 data may be immature, pre-go-live, test, or partial. The output
is not business-reconciled, and every run remains `Needs reconciliation` with
`final_production_selected: false`.

This script does not schedule extraction, create dashboards, build AI/RAG flows,
create warehouse models, write back to AutoCount, or extract a CoA/account
master. CoA/account master remains unresolved until a real surface is found and
reviewed separately.

## Prerequisites

- Use the dedicated AC2 read-only SQL login validated by the
  [AC2 read-only SQL login validation runbook](readonly_sql_login_runbook.md).
- Required grants are `SELECT` only on the configured surfaces.
- Do not run this with a write/admin login.
- Keep connection strings, screenshots, local configs, generated CSVs, and raw
  ERP data out of Git, docs, chat, and PRs.

## Local Config

Copy the secret-free example to the ignored local path:

```powershell
Copy-Item config\autocount_selected_surface_extract.example.json config\autocount_selected_surface_extract.local.json
```

Set the read-only connection string outside the repo:

```powershell
$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = '<stored outside Git>'
```

The config uses only `AUTOCOUNT_READONLY_SQL_CONNECTION_STRING` by default.
Do not add production connection strings to the JSON file.

Default column allowlists must match the PR #41 selected-surface profile
metadata gathered from AC2. If a local config includes a `column_inventory`
array from discovery metadata, the extractor validates configured `columns` and
`order_by` values against that supplied inventory before building SQL. The
extractor does not require a live `sys.columns` metadata check during raw
snapshot extraction.

Default local output root:

```text
C:\XB\autocount_outputs\extract\selected_surfaces
```

The script creates a timestamped run folder and writes:

- `selected_surface_extract_manifest.json`
- one CSV per enabled surface
- `selected_surface_extract_report.md`

Generated outputs and `.local` config files must never be committed.

## Run Command

From the repository root:

```powershell
python scripts\autocount_selected_surface_extract.py --config config\autocount_selected_surface_extract.local.json
```

For a bounded dry-review sample, set `max_rows` in the local config. `business_date`
and `as_of_date` are metadata only; the extractor does not filter by date unless
a future reviewed config explicitly changes the SQL design.

## Safe Manifest Summary

After a run, summarize the manifest metadata without reading or printing CSV
contents:

```powershell
$runPath = 'C:\XB\autocount_outputs\extract\selected_surfaces\selected_surface_extract_YYYYMMDD_HHMMSS_RUNID'
$manifestPath = Join-Path $runPath 'selected_surface_extract_manifest.json'
$manifest = Get-Content -Raw -Path $manifestPath | ConvertFrom-Json

[pscustomobject]@{
  run_path = $manifest.storage.run_path
  status = $manifest.status
  exception_count = $manifest.exception_count
  data_maturity = $manifest.data_maturity
  business_reconciliation_status = $manifest.business_reconciliation_status
} | Format-List

'Exported files:'
$manifest.exported_files |
  Select-Object surface_name, surface_id, schema_name, object_name, object_id, row_count,
    file_name, output_path, decision, final_production_selected, data_maturity,
    business_reconciliation_status |
  Format-Table -AutoSize -Wrap

'Skipped surfaces:'
$manifest.skipped_surfaces |
  Select-Object surface_name, surface_id, schema_name, object_name, reason |
  Format-Table -AutoSize -Wrap
```

## Default Scope

Enabled by default:

- `Debtor`
- `vDebtor`
- `Creditor`
- `vCreditor`
- `PaymentMethod`
- `PO`
- `PODTL`
- `vPurchaseOrder`
- `ARInvoice`
- `APInvoice`

Disabled by default:

- `ARInvoiceDTL`
- `APInvoiceDTL`
- `GLDTL`

CoA/account master is always skipped as unresolved. Do not promote `GLDTL`,
`GLMast`, `Accountant`, or any other unreviewed object into a CoA master in this
extractor.

## Safety Boundary

The script uses explicit column allowlists and does not use `SELECT *`. CSV cells
are formula-neutralized before writing, and Decimal/date/datetime values are
normalized for deterministic CSV and manifest output.

Raw CSVs are not safe to paste back. They can contain customer, supplier,
invoice, payment, purchase order, item, and GL-related business data.

Generally safe to paste back after reviewing for accidental secrets:

- the manifest metadata and row counts
- the markdown report warnings and run summary

Not safe to paste back:

- raw CSV contents
- generated run folders
- local `.local` config files
- connection strings
- screenshots
- ad hoc SQL output
- production database backups

## Difference From Aggregate Reconciliation

The selected-surface reconciliation script runs aggregate-only checks such as
counts and totals. This extractor writes raw selected rows to local CSV files.
It is therefore more sensitive and should only be used when the immediate goal
is a local snapshot of the current AC2 database, not proof of final business
mapping.

Matching aggregate counts are not final approval. This snapshot must still be
reviewed later against AutoCount UI/reports when the AC2 database contains real
production-ready data.

## Rerun Later

When AutoCount has real post-go-live data, rerun with the same read-only login
and a reviewed local config. Keep the new run folder under the output root,
compare row counts and selected samples locally, and update planning docs only
with safe summaries. Do not commit raw extracts.
