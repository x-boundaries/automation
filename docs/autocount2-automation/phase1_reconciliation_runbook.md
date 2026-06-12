# AutoCount 2 Phase 1 Reconciliation Runbook

## Purpose

This runbook defines the Phase 1 stock reconciliation kit for the confirmed
AutoCount 2.2 target. Keep exact server, database, and app-build details in
ignored local config/run notes where practical; version-controlled docs should
prefer this generic confirmed-target label unless repo access is limited to
trusted admins.

The goal is to validate candidate SQL surfaces against AutoCount UI/report
outputs before creating final wrapper views, granting a final read-only login,
or scheduling extraction.

Use the [Phase 1 AC2 stock extraction smoke runbook](phase1_stock_extract_smoke_runbook.md)
only for the repeatable smoke extraction-validation workflow. The smoke runbook
does not approve final SQL surfaces or scheduling.

## Why Extraction Is Not Scheduled Yet

Do not schedule extraction yet because the current login is discovery-only. The
confirmed probe showed no direct permission risks, but the login maps to `dbo`
and has risky role memberships. A final extractor login must be read-only and
must be validated after wrapper views are approved.

No SQL surface is selected for Phase 1 in this branch. Every candidate remains a
reconciliation candidate until the UI/report comparisons below pass.

## Run The Reconciliation Script On The VM

Generated reconciliation outputs should stay under
`C:\XB\autocount_outputs\reconcile`. Older folders such as
`C:\XB\autocount_phase1_reconcile_outputs` are legacy/manual paths and should
be avoided going forward.

1. Copy the example config to an ignored local config file and review it:

   ```powershell
   Copy-Item config\autocount_phase1_reconcile.example.json config\autocount_phase1_reconcile.local.json
   ```

2. Set the connection string only in the environment. Do not paste it into Git,
   docs, screenshots, or chat:

   ```powershell
   $env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = '<ODBC connection string stored outside Git>'
   ```

3. Run a dry run first. This validates config and writes placeholders without
   opening SQL:

   ```powershell
   python scripts\autocount_phase1_reconcile.py --config config\autocount_phase1_reconcile.local.json --dry-run
   ```

4. Run the aggregate reconciliation pass only when using an approved manual
   discovery session:

   ```powershell
   python scripts\autocount_phase1_reconcile.py --config config\autocount_phase1_reconcile.local.json --output-root C:\XB\autocount_outputs\reconcile
   ```

The script creates one timestamped folder per run under the output root and
writes only safe summaries:

- `phase1_reconcile_manifest.json`
- `phase1_reconcile_report.md`
- `object_counts.csv`
- `column_inventory.csv`
- `column_coverage.csv`
- `date_ranges.csv`
- `location_counts.csv`
- `stock_balance_summary.csv`
- `movement_summary.csv`

## Interpret The Outputs

Use the outputs as evidence for or against candidate wrapper-view design. They
are not final extraction outputs.

- `object_counts.csv`: row count by shortlisted object.
- `column_inventory.csv`: metadata-only list of object columns, data types, and
  nullable flags where available. It must not contain row values.
- `column_coverage.csv`: required extractor-contract columns present/missing by
  object and contract.
- `date_ranges.csv`: min/max `DocDate` only for objects where that column exists.
- `location_counts.csv`: aggregate counts by `Location` only where that column
  exists.
- `stock_balance_summary.csv`: aggregate `BalQty` totals/min/max only for
  balance-like objects where `BalQty` exists.
- `movement_summary.csv`: aggregate `StockDTL` count, date range, quantity, and
  cost totals where the fields exist.

The script must not output raw business rows, item descriptions, customer names,
supplier names, addresses, phone numbers, free-text remarks, credentials, or
connection strings.

## Findings from first non-dry-run AC2 reconciliation

The first confirmed non-dry-run pass against the approved local AC2 target
completed with `dry_run: false`, `safe_outputs_only: true`,
`raw_rows_exported: false`, and no warnings. Keep these findings as aggregate
evidence only; do not commit the generated output folder or raw CSVs.

Safe aggregate findings:

- Master candidate counts: `dbo.Item`, `dbo.ItemUOM`, `dbo.vItem`, and
  `dbo.vItemUOM` each reported 21,831 rows.
- Location setup reported 7 rows: `GIT`, `HQ`, `XB01`, `XB02`, `XB03`,
  `XB04`, and `XB05`.
- Location-bearing activity in the safe summaries was limited to `XB01`: one
  `vItemBalQty` row, one `ItemBatchBalQty` row, and two `StockDTL` rows.
- Stock balance totals reconciled to aggregate `BalQty = -2` across
  `vItemBalQty`, `vItemUOMBalQty`, `ItemBatchBalQty`, and
  `vItemBatchBalQty`.
- Movement summary reported two `StockDTL` rows, total `Qty = -2`, total
  `Cost = 66.16`, and a movement date range from
  `2026-06-04T12:18:07.493000` to `2026-06-04T12:25:02.990000`.
- Stock adjustment, stock receive, stock transfer, stock issue, stock write-off,
  and goods-received-note candidate header/detail views currently reported 0
  rows in the aggregate pass.

These aggregate facts support the working interpretation that the AC2
environment has setup/master data loaded but is not yet in live transactional
use. This is not approval for scheduled extraction or any SQL surface selection.

> Warning: the current wrapper SQL template must not be executed until it is
> updated for confirmed columns/sign semantics and manually reviewed.

## CSV Safety

All generated reconciliation CSVs are spreadsheet-formula-neutralised before
write. Formula-like text values, including location or metadata fields beginning
with `=`, `+`, `-`, or `@` after leading whitespace/control characters, should
open as text in spreadsheet tools. These files are still operational evidence;
keep generated folders outside Git and review before sharing.

## Compare Inside AutoCount UI

Compare the safe summary outputs with these AutoCount UI/report surfaces:

1. Stock item listing count.
2. Location setup count.
3. Stock balance/status report totals by date/location/UOM as applicable.
4. Stock card/movement report totals and date ranges.
5. The two `StockDTL` rows dated 2026-06-04, verified inside AutoCount UI/report
   output without copying raw row values into the repository.

Record only pass/fail notes and safe aggregate counts in docs. Do not paste raw
rows or screenshots into Git.

## Use Wrapper-View Templates After Reconciliation

`sql/autocount2/phase1_wrapper_views.template.sql` is a draft template only. Do
not execute it as-is. After reconciliation, adjust the view definitions for the
confirmed columns, sign conventions, UOM handling, cancellation/posting rules,
and date filters.

Prefer approved wrapper views over direct table extraction. Wrapper views should
normalize the final extractor contract while isolating downstream code from raw
AutoCount table/view details.

## Create And Use A Read-Only Login After Approval

`sql/autocount2/read_only_login.template.sql` is a template only. Use it after
wrapper views are approved to create/map a dedicated account and grant `SELECT`
only to approved wrapper views.

The final login must not have write/admin permissions such as `db_owner`,
`db_datawriter`, `db_ddladmin`, `db_securityadmin`, `db_accessadmin`, or
`db_backupoperator`. Re-run the SQL probe with the final login before scheduling
any extraction.

## Safe To Paste Back Into ChatGPT/Codex

These are generally safe if reviewed for accidental secrets first:

- `phase1_reconcile_manifest.json`
- `phase1_reconcile_report.md`
- Aggregate CSVs from the reconciliation script
- Manual notes with only counts, totals, date ranges, missing-column names, and
  pass/fail reconciliation status

## Must Not Be Pasted Or Committed

Do not paste or commit:

- ODBC connection strings, passwords, API keys, or `.env` values
- Local config files such as `config/autocount_phase1_reconcile.local.json`
- Raw SQL probe output containing business rows
- Raw reconciliation extracts or ad hoc `SELECT TOP` output
- Item descriptions, customer names, supplier names, addresses, phone numbers,
  remarks, screenshots, database backups, or production connection strings
- Any SQL write-back script or scheduled extraction configuration
