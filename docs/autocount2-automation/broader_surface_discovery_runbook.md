# Broader AC2 Surface Discovery Runbook

## Purpose

Use this runbook to discover broader AutoCount 2 / AC2 candidate SQL surfaces
safely before any extraction scope is expanded beyond the stock smoke workflow.
The discovery kit reads SQL Server metadata only and groups heuristic candidate
tables/views for customer, supplier, GL/accounting, AR/AP, locations, payment
methods, purchase order / outstanding PO, stock-in-transit, and stock reference
follow-up planning.

This does not approve final extraction mapping. It does not schedule extraction.
It does not export raw ERP rows. It does not create dashboards, AI/RAG flows, or
warehouse models.

Every candidate remains `Needs reconciliation` until it is compared with
AutoCount UI/report outputs and approved for a future extraction design.

## Required Account

Run this only with the dedicated read-only SQL login validated by the
[AC2 read-only SQL login validation runbook](readonly_sql_login_runbook.md).
Do not use an account with write/admin permissions. Do not paste the connection
string into Git, docs, screenshots, or chat.

Set the connection string outside the repo:

```powershell
$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = '<stored outside Git>'
```

The config loader accepts UTF-8 with BOM for PowerShell-edited JSON.

## Local Config And Output

Copy the secret-free config template to the ignored local config path:

```powershell
Copy-Item config\autocount_broader_surface_discovery.example.json config\autocount_broader_surface_discovery.local.json
```

The safe output folder is:

```text
C:\XB\autocount_outputs\probe\broader_surfaces
```

Generated manifests and reports stay local. Do not commit generated outputs or
`.local` config files.

## Operator Flow

1. Validate the dedicated read-only login.
2. Rerun the AC2 stock smoke extraction using that read-only login.
3. Run broader surface discovery:

   ```powershell
   python scripts\autocount_broader_surface_discovery.py --config config\autocount_broader_surface_discovery.local.json
   ```

4. Review the local candidate manifest/report under
   `C:\XB\autocount_outputs\probe\broader_surfaces`.
5. Use the metadata-only results to decide future extraction scope.

## Candidate Scoring And Shortlists

Broad AC2 metadata discovery can return hundreds of candidate objects per
group. The manifest and report include a `top_candidates` shortlist for each
candidate group to make review more practical. The shortlist is produced from
metadata only by scoring object names, column names, known AutoCount naming
patterns, likely header/detail table pairs, and obvious false-positive
patterns.

Scoring is only a ranking aid. A high score does not approve extraction, does
not select a SQL surface for production use, and does not replace reconciliation
against AutoCount UI/report outputs. Every shortlisted candidate remains
`Needs reconciliation`, and `final_production_selected` remains `false`.

Review shortlisted debtor/customer, creditor/supplier, GL/accounting, AR/AP,
payment, PO, stock-in-transit, stock reference, and location candidates with
Ingenious/Mike and finance or operations stakeholders where relevant. Do not
treat `top_candidates` as a final production mapping.

## What The Script Does

The script:

- Connects using only `AUTOCOUNT_READONLY_SQL_CONNECTION_STRING`.
- Reads SQL catalog metadata for visible tables/views and columns.
- Optionally captures approximate object row counts from SQL Server metadata.
- Matches candidate groups by object names and column names.
- Scores matched candidates to create a bounded metadata-only shortlist for
  human review.
- Marks all candidate groups and matched surfaces as `Needs reconciliation`.
- Redacts password/PWD/token/API-key-like fragments from exception text.
- Writes a local JSON manifest and Markdown report.

The script does not run `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`,
`ALTER`, `DROP`, or `TRUNCATE`. It does not use `SELECT *` to return ERP rows.

## Reconciliation Required

Debtor/customer, creditor/supplier, GL/accounting, AR/AP, payment method,
purchase order / outstanding PO, and stock-in-transit-related surfaces require
reconciliation with AutoCount UI/report outputs before actual extraction.

Known stock smoke surfaces are included only as reference follow-up context, not
as final production mapping.

Keep stock-in-transit interpretation and supplier prepayment accounting
treatment unresolved pending finance / Ingenious sign-off.

## Safe To Paste Back

After review for accidental secrets, these are generally safe to paste back for
planning:

- `broader_surface_discovery_manifest.json`
- `broader_surface_discovery_report.md`

Do not paste raw ERP rows, screenshots, connection strings, local config files,
customer/supplier lists, payment details, invoice lines, database backups, or
ad hoc SQL output.
