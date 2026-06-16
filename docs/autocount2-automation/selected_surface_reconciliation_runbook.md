# Selected Surface Reconciliation Runbook

## Purpose

Use this runbook after the
[Broader AC2 surface discovery runbook](broader_surface_discovery_runbook.md)
has produced `selected_surface_profile.md`. The selected-surface reconciliation
script checks the PR #41 selected surfaces with read-only aggregate queries only.
It is a review pack for comparing counts and totals against AutoCount UI/report
outputs before any raw extraction profile is approved.

This does not approve final extraction mapping. It does not export customer,
supplier, invoice, payment, PO, item, or GL transaction rows. It does not
schedule extraction, create dashboards, build AI/RAG flows, create warehouse
models, or write back to AutoCount.

Every area remains `Needs reconciliation`, and `final_production_selected`
remains `false`.

## Required Prerequisites

- A successful selected-surface profile from the broader discovery workflow.
- The dedicated AC2 read-only SQL login validated by
  [AC2 read-only SQL login validation runbook](readonly_sql_login_runbook.md).
- SQL permissions limited to:
  - `SELECT` on the selected surfaces needed for aggregate checks.
  - `VIEW DEFINITION` or equivalent metadata visibility for selected surface
    column discovery.

Do not run this with a write/admin login. Do not paste connection strings,
screenshots, local config files, or raw SQL output into Git, docs, chat, or PRs.

## Local Config And Output

Copy the secret-free template to the ignored local path:

```powershell
Copy-Item config\autocount_selected_surface_reconcile.example.json config\autocount_selected_surface_reconcile.local.json
```

Set the read-only connection string outside the repo:

```powershell
$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = '<stored outside Git>'
```

Default local output root:

```text
C:\XB\autocount_outputs\probe\selected_surface_reconcile
```

Generated manifests and reports stay local. Do not commit generated outputs or
`.local` config files.

## Run Command

From the repository root:

```powershell
python scripts\autocount_selected_surface_reconcile.py --config config\autocount_selected_surface_reconcile.local.json
```

The run writes:

- `selected_surface_reconcile_manifest.json`
- `selected_surface_reconcile_report.md`

## What The Script Checks

The script reads selected surface metadata, then runs explicit aggregate queries
only. It gracefully skips missing optional columns and records why a metric was
skipped.

Typical aggregate checks include:

- Customer/debtor master: total count, plus active/inactive counts only if
  `IsActive` exists.
- Supplier/creditor master: total count, plus active/inactive counts only if
  `IsActive` exists.
- Branch/location: total count only unless safe active/inactive metadata exists.
- Payment method: total count, plus active/inactive counts only if `IsActive`
  exists. Payment method names and bank account details are not output.
- AR opening/outstanding: `ARInvoice` header count, cancelled/non-cancelled
  counts if `Cancelled` exists, and safe financial totals only for configured
  aggregate columns such as `Outstanding`, `LocalNetTotal`, `NetTotal`, and
  `PaymentAmt`.
- AP opening/outstanding: `APInvoice` header count, cancelled/non-cancelled
  counts if `Cancelled` exists, and safe financial totals only for configured
  aggregate columns.
- PO/outstanding: `PO` header count and safe totals; `PODTL` quantity totals
  and computed outstanding quantity only when both `Qty` and `TransferedQty`
  exist.
- GL transaction: `GLDTL` row count and min/max `TransDate` only when the
  column exists. GL debit/credit totals are intentionally not included in this
  pack.
- CoA/account master: remains unresolved until a stronger account-master
  metadata surface is confirmed.

The script does not output document numbers, debtor/creditor codes, item codes,
names, addresses, emails, descriptions, remarks, notes, supplier invoice
numbers, payment method names, or raw business rows.

## Safe To Paste Back

After reviewing for accidental secrets, these local generated files are intended
to be safe planning artifacts:

- `selected_surface_reconcile_manifest.json`
- `selected_surface_reconcile_report.md`

Do not paste raw ERP rows, screenshots, connection strings, local config files,
customer/supplier lists, payment details, invoice lines, PO lines, GL entries,
database backups, ad hoc SQL output, or any generated CSVs.

## Review Against AutoCount

Compare each aggregate with the matching AutoCount UI/report path:

- Debtor and creditor setup counts against master maintenance or listing
  reports.
- Branch/location and payment setup counts against setup screens.
- AR/AP invoice counts and totals against the same filters in AR/AP outstanding
  or aging reports.
- PO header/detail totals against outstanding PO reports with matching cancelled
  and date assumptions.
- GL transaction count/date range only as a scope sanity check.

If AutoCount report filters differ from SQL aggregate assumptions, document the
difference and rerun only after the config/runbook is updated. Do not treat a
matching aggregate as final extraction approval; it is evidence for the next
safe extraction planning step.

## Why CoA Remains Unresolved

`GLDTL` is transaction/detail GL metadata, not Chart of Accounts master
metadata. CoA/account master remains unresolved until a stronger account-master
surface is confirmed against metadata and AutoCount UI/report paths. This
aggregate pack must not promote `GLDTL`, `Accountant`, or any other non-master
surface into a final CoA mapping.

## Non-Approval Boundary

This aggregate pack supports reconciliation planning only. It does not approve
raw extraction, does not select final production SQL surfaces, and does not
replace review with Ingenious/Mike and finance or operations stakeholders.
