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
For the current inventory and purchasing analytics focus, use the
[inventory intelligence scope](inventory_intelligence_scope.md) to decide which
candidate groups matter next; CoA, GL, and bank accounting work are parked.

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

## Selected Surface Profile

Each successful run also writes a local `selected_surface_profile.md` review
pack under the run folder. This report is a curated metadata-only profile for
the currently likely AC2 review surfaces:

- customer master: `Debtor`, `vDebtor`
- supplier master: `Creditor`, `vCreditor`
- branch/location: `Branch`, `vBranch`
- payment method setup: `PaymentMethod`
- AR opening/outstanding: `ARInvoice`, `ARInvoiceDTL`
- AP opening/outstanding: `APInvoice`, `APInvoiceDTL`
- purchase order/outstanding PO: `PO`, `PODTL`, `vPurchaseOrder`
- GL transaction detail: `GLDTL`
- CoA/account master: unresolved unless stronger account-master metadata is
  discovered

Read this profile as a human review queue. Every item remains
`Needs reconciliation`, and every item has `final_production_selected: false`.
The profile records the review area, object type, candidate role, metadata-only
key columns found, expected columns not seen in metadata, and notes for
reconciliation. It must not contain row samples, row values, raw ERP exports, or
business records.

The candidate roles distinguish the review question:

- `master_table` means setup/master metadata such as debtor, creditor, branch,
  payment method, or a possible account master table.
- `enriched_view` means a view that may combine or expose master/setup fields
  in a more review-friendly shape, but still needs reconciliation.
- `header_table` means document header metadata such as AR/AP invoices or PO.
- `detail_table` means document line/detail metadata such as invoice or PO
  details.
- `transaction_detail` means accounting transaction/detail metadata. `GLDTL`
  falls here.
- `unresolved` means no safe metadata candidate has been selected for that
  review area.

`GLDTL` should be reviewed for GL transaction detail only. It contains
transaction/detail signals such as account numbers and journal/document fields,
but that does not make it the Chart of Accounts master. Do not use `GLDTL` as a
CoA/account master mapping.

The script performs a targeted CoA/account-master metadata search for object
names such as `Account`, `GLAccount`, `GLAcc`, `ChartOfAccount`, `COA`,
`PostingAccount`, and `AccountGroup`, combined with columns such as `AccNo`,
`Description`, `Desc2`, `AccountType`, `ParentAccNo`, `SpecialAccType`, and
`IsActive`. It penalizes likely transaction/detail surfaces including `GLDTL`,
journal/detail tables, invoice/payment/detail tables, and revaluation or
gain-loss tables. `Accountant` is not treated as CoA master. If no strong
candidate remains, the profile explicitly records `coa_account_master` as
`unresolved`.

Use this review pack to plan the next safe extraction design step: reconcile
each metadata candidate against AutoCount UI/report paths and Ingenious/Mike,
then document any future extraction scope separately. The profile itself does
not approve extraction, does not schedule anything, and does not select a final
production SQL mapping.

## Candidate Scoring And Shortlists

Broad AC2 metadata discovery can return hundreds of candidate objects per
group. The manifest and report include a `top_candidates` shortlist for each
candidate group to make review more practical. The shortlist is produced from
metadata only by scoring object names, column names, known AutoCount naming
patterns, likely header/detail table pairs, and obvious false-positive
patterns.

Broad `top_candidates` can still be noisy because a single keyword group may
mix setup/master data, transaction headers, transaction detail tables, and
views. Review the intent-specific shortlist keys first when they are present,
then use the broad `top_candidates` list as fallback context.

Recommended intent-specific review paths include:

- `debtor_customer.master_candidates` before debtor-bearing AR transaction
  candidates. `Debtor`, `vDebtor`, and `DebtorType` are different review
  questions from AR invoice or payment surfaces.
- `creditor_supplier.master_candidates` before AP transaction candidates.
  `Creditor`, `vCreditor`, and `CreditorType` are different review questions
  from AP invoice, payment, credit-note, or goods-received surfaces.
- `payment_methods.master_candidates` before payment/refund/detail transaction
  tables. `PaymentMethod` is usually stronger evidence for payment-method
  setup than payment transaction detail tables.
- `purchase_order_outstanding_po.po_header_candidates` and
  `purchase_order_outstanding_po.po_detail_candidates` before POS-related
  tables. Review `PO`, `PODTL`, and `vPurchaseOrder` before treating `Pos` or
  `PosOrder` as relevant to purchase orders.
- `chart_of_accounts_gl.account_master_candidates` separately from
  `chart_of_accounts_gl.gl_transaction_candidates`. CoA/account master review
  should not be dominated by `GLDTL` or journal/detail tables, although those
  may remain relevant for GL transaction review.
- `ar_ap_opening.ar_opening_candidates` and
  `ar_ap_opening.ap_opening_candidates` before generic cashbook/imported-goods
  detail views unless those views are reconciled to direct AR/AP opening needs.
- `locations.master_candidates` before transaction location candidates.

Scoring is only a ranking aid. A high score does not approve extraction, does
not select a SQL surface for production use, and does not replace reconciliation
against AutoCount UI/report outputs. Every shortlisted candidate remains
`Needs reconciliation`, and `final_production_selected` remains `false`.

Review shortlisted debtor/customer, creditor/supplier, GL/accounting, AR/AP,
payment, PO, stock-in-transit, stock reference, and location candidates with
Ingenious/Mike and finance or operations stakeholders where relevant. Do not
treat `top_candidates` or any intent-specific shortlist as a final production
mapping.

## What The Script Does

The script:

- Connects using only `AUTOCOUNT_READONLY_SQL_CONNECTION_STRING`.
- Reads SQL catalog metadata for visible tables/views and columns.
- Optionally captures approximate object row counts from SQL Server metadata.
- Matches candidate groups by object names and column names.
- Scores matched candidates to create a bounded metadata-only shortlist for
  human review.
- Adds intent-specific shortlist keys where broad groups need separate master
  and transaction review paths.
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
- `selected_surface_profile.md`

Do not paste raw ERP rows, screenshots, connection strings, local config files,
customer/supplier lists, payment details, invoice lines, database backups, or
ad hoc SQL output.
