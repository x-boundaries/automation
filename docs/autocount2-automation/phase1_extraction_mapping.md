# Phase 1 Extraction Mapping

Use the [Phase 1 reconciliation runbook](phase1_reconciliation_runbook.md) to validate these candidate surfaces against AutoCount UI/report outputs before replacing any placeholder with a wrapper view.

This mapping turns the SQL probe decision pack into stock extractor
configuration guidance. It is intentionally read-only and does not define any
SQL write path.

The confirmed AC2 target is `localhost\A2006 / AED_XBOUNDARIES`, matching the
AutoCount 2.2 login screen for `(local)\A2006`, database `AED_XBOUNDARIES`, app
DB version `2.2.94`. The safe probe handoff confirms object visibility and row
counts for several candidates, but column-level key details were not available
in this branch context. Keep the configuration placeholder/wrapper-view based
until [extraction_surface_decision.md](extraction_surface_decision.md) and
[reconciliation_checklist.md](reconciliation_checklist.md) pass.

## Phase 1 Gates

Before any candidate can replace a placeholder config entry:

- `permission_risks.csv` has no direct write, schema, security, or broad execute
  risk for the final extractor login.
- `role_risks.csv` does not show `db_owner`, `db_datawriter`, `db_ddladmin`,
  `db_securityadmin`, `db_accessadmin`, or `db_backupoperator` for the final
  extractor login.
- Candidate object columns cover the required output contract or a safe local
  read-only wrapper view supplies the missing columns.
- AutoCount UI/report reconciliation passes for a known business date.
- No raw probe output, raw extract, local config, or credential is committed.

## Confirmed AC2 target and candidate shortlist

All shortlisted objects below remain `Needs reconciliation`. None can be used
directly by scheduled extraction yet. The safest final shape is local read-only
wrapper views that normalize column names, joins, filters, signs, UOM handling,
and cancellation/posting rules after UI/report reconciliation.

### Stock Master Candidates

Required Phase 1 columns: `ItemCode`, `Description`, `UOM`, `Barcode`,
`ItemGroup`, `ItemBrand`, `ItemCategory`, `ItemClass`, `IsActive`,
`StockControl`, `LastModified`.

| Candidate source object | Available key columns from the probe | Missing/unknown columns | Reconciliation required | Risk notes | Direct or wrapper view? |
| --- | --- | --- | --- | --- | --- |
| `dbo.Item` | Column-level safe summary not supplied; object visible with 21,831 rows. | Verify all required master fields, especially active flag, stock-control flag, category/group/brand/class, and last-modified fields. | Compare count and known samples against AutoCount stock item listing. | Raw table may not include UOM/barcode/display fields exactly as reports show them. | Wrap in `dbo.vw_XB_AC2_StockMaster_Phase1` after reconciliation. |
| `dbo.ItemUOM` | Column-level safe summary not supplied; object visible with 21,831 rows. | Verify `ItemCode`, `UOM`, barcode/default UOM fields, conversion fields, and update timestamp availability. | Compare UOM/barcode coverage against item listing and sample item setup. | Multi-UOM rules may require joining to `Item` or a view. | Use only through the stock master wrapper view. |
| `dbo.vItem` | Column-level safe summary not supplied; object visible with 21,831 rows. | Verify whether view contains description, category/group/brand/class, active, stock-control, and modified fields. | Compare count and samples against AutoCount item listing. | View semantics may already apply UI filters; do not assume without reconciliation. | Preferred candidate for wrapper source if it matches UI/report fields. |
| `dbo.vItemUOM` | Column-level safe summary not supplied; object identified as a master/UOM candidate. | Verify `ItemCode`, `UOM`, barcode/default UOM fields, conversion fields, and update timestamp availability. | Compare UOM rows and known sample items against AutoCount item setup. | View may duplicate or filter item/UOM combinations. | Use only through the stock master wrapper view. |

### Stock Balance/Status Candidates

Required Phase 1 columns: `AsAtDate`, `ItemCode`, `Location`, `UOM`,
`BalanceQty`, `SmallestBalQty`, `UnitCost`, `CostValue`.

| Candidate source object | Available key columns from the probe | Missing/unknown columns | Reconciliation required | Risk notes | Direct or wrapper view? |
| --- | --- | --- | --- | --- | --- |
| `dbo.vItemBalQty` | Column-level safe summary not supplied; object visible with 1 row. | Verify item, location, UOM, balance quantity, smallest/base quantity, cost, value, and as-at date semantics. | Compare row count and totals against AutoCount stock balance/status report. | The 1-row count is suspicious compared with 21,831 items; may expose only non-zero/current balances. | Wrap or reject after reconciliation; do not query directly. |
| `dbo.vItemUOMBalQty` | Column-level safe summary not supplied; object visible with 21,831 rows. | Verify item, location, UOM, balance quantity, smallest/base quantity, cost, value, and date semantics. | Compare quantity and value totals by item/location against stock balance/status report. | Row count may include zero balances or one row per item/UOM rather than item/location. | Preferred candidate for balance wrapper if it reconciles. |
| `dbo.ItemBatchBalQty` | Column-level safe summary not supplied; object identified as balance/status candidate. | Verify batch number, item, location, UOM, balance quantity, cost/value, and date semantics. | Compare batch-tracked stock balances if batch tracking is enabled. | Could be irrelevant if the company does not use batch tracking. | Wrap only if batch balances are required and reconcile. |
| `dbo.vItemBatchBalQty` | Column-level safe summary not supplied; object identified as balance/status candidate. | Verify batch number, item, location, UOM, balance quantity, cost/value, and date semantics. | Compare batch-tracked stock balances if batch tracking is enabled. | View may filter empty batches or aggregate differently from reports. | Wrap only if batch balances are required and reconcile. |

### Stock Movement/Card Candidates

Required Phase 1 columns: `DocType`, `DocNo`, `DocDate`, `ItemCode`,
`Location`, `BatchNo`, `UOM`, `QtyIn`, `QtyOut`, `UnitCost`, `SourceTable`,
`LastModified`.

| Candidate source object | Available key columns from the probe | Missing/unknown columns | Reconciliation required | Risk notes | Direct or wrapper view? |
| --- | --- | --- | --- | --- | --- |
| `dbo.StockDTL` | Column-level safe summary not supplied; object visible with 2 rows dated 2026-06-04. | Verify document type/number/date, item, location, batch, UOM, signed quantity, cost, source table, and modified timestamp. | Check both rows dated 2026-06-04 against the AC2 UI/report and stock card. | Low transaction count is plausible for a not-live AC2 target but must be confirmed. Sign handling and backdated postings are high risk. | Wrap in `dbo.vw_XB_AC2_StockMovement_Phase1` after reconciliation. |

### Stock Document Candidates

Required Phase 1 columns: `DocType`, `DocNo`, `DocDate`, `Cancelled`,
`FromLocation`, `ToLocation`, `ItemCode`, `Location`, `UOM`, `Qty`, `UnitCost`,
`RefDocNo`, `LastModified`.

| Candidate source object | Available key columns from the probe | Missing/unknown columns | Reconciliation required | Risk notes | Direct or wrapper view? |
| --- | --- | --- | --- | --- | --- |
| `dbo.vStockAdjustment` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify header keys, dates, cancellation/posting status, references, and modified timestamp. | Compare adjustment listing against UI/report for the selected date window. | Header rows alone may not provide item/location quantities. | Use through stock document wrapper only if paired with detail rows. |
| `dbo.vStockAdjustmentDetail` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify item, location, UOM, quantity, unit cost, and line reference fields. | Reconcile detail totals to stock card and adjustment documents. | Quantity signs may differ from movement report signs. | Use through stock document wrapper after sign rules are confirmed. |
| `dbo.vStockReceive` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify header keys, dates, cancellation/posting status, references, and modified timestamp. | Compare stock receive listing against UI/report. | Header rows alone may not provide item/location quantities. | Use through stock document wrapper only if paired with detail rows. |
| `dbo.vStockReceiveDetail` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify item, location, UOM, quantity, unit cost, and line reference fields. | Reconcile receiving detail totals to stock card. | Receiving may overlap with GRN semantics; avoid double counting. | Use through stock document wrapper after reconciliation. |
| `dbo.vStockTransfer` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify transfer number/date, from/to locations, cancellation/posting status, references, and modified timestamp. | Compare stock transfer listing against UI/report. | Transfers need paired in/out treatment; header rows can double count if flattened incorrectly. | Use through stock document wrapper only. |
| `dbo.vStockTransferDetail` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify item, UOM, quantity, from/to location linkage, unit cost, and line references. | Reconcile transfer detail to stock card for both locations. | Must preserve both source and destination movement meaning. | Use through stock document wrapper after sign/location rules are confirmed. |
| `dbo.vStockIssue` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify header keys, dates, cancellation/posting status, references, and modified timestamp. | Compare issue listing against UI/report. | Header rows alone may not provide item/location quantities. | Use through stock document wrapper only if paired with detail rows. |
| `dbo.vStockIssueDetail` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify item, location, UOM, quantity, unit cost, and line reference fields. | Reconcile issue detail totals to stock card. | Quantity sign and cancellation handling must be explicit. | Use through stock document wrapper after reconciliation. |
| `dbo.vStockWriteOff` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify header keys, dates, cancellation/posting status, references, and modified timestamp. | Compare write-off listing against UI/report. | Header rows alone may not provide item/location quantities. | Use through stock document wrapper only if paired with detail rows. |
| `dbo.vStockWriteOffDetail` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify item, location, UOM, quantity, unit cost, and line reference fields. | Reconcile write-off detail totals to stock card. | Quantity sign and valuation handling must be explicit. | Use through stock document wrapper after reconciliation. |
| `dbo.vGoodsReceivedNote` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify header keys, dates, cancellation/posting status, supplier references, and modified timestamp. | Compare GRN listing against UI/report if GRN is in Phase 1 evidence. | Could be later-phase purchasing evidence rather than stock extraction source. | Use through wrapper only if needed for reconciliation evidence. |
| `dbo.vGoodsReceivedNoteDetail` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify item, location, UOM, quantity, unit cost, and line reference fields. | Reconcile GRN detail totals to stock card/receiving reports if in scope. | May overlap stock receive or purchasing surfaces; avoid double counting. | Use through wrapper only if needed for reconciliation evidence. |

### Supporting Setup Candidate

| Candidate source object | Available key columns from the probe | Missing/unknown columns | Reconciliation required | Risk notes | Direct or wrapper view? |
| --- | --- | --- | --- | --- | --- |
| `dbo.Location` | Column-level safe summary not supplied; object visible with 7 rows. | Verify location code/name/active fields and any warehouse grouping fields. | Compare location count against AutoCount location setup. | Location filters affect every stock balance and movement total. | Reference from wrapper views after location setup reconciles. |

## Stock Master

| Field | Mapping |
| --- | --- |
| Extractor dataset | `stock_master` |
| Candidate source object | Placeholder: `dbo.vw_XB_AC2_StockMaster_Phase1` |
| Decision | `Needs reconciliation` |
| Can replace current placeholder config? | No - confirmed target exists, but candidate columns and UI/report reconciliation are not complete |
| Business meaning | One row per stock-controlled item/product/SKU surface used for reporting and downstream stock checks |
| Reconciliation report needed | AutoCount item/stock item listing with active/inactive status counts |

Required columns:

- `ItemCode`
- `Description`
- `UOM`
- `Barcode`
- `ItemGroup`
- `ItemBrand`
- `ItemCategory`
- `ItemClass`
- `IsActive`
- `StockControl`
- `LastModified`

Missing columns:

- Column-level safe summary was not included in this handoff.
- Wrapper view design must verify every required column against `Item`, `ItemUOM`,
  `vItem`, and `vItemUOM`.

Risk notes:

- Do not assume a heuristic `item` or `product` match is the stock master.
- Confirm whether inactive, non-stock, service, package, matrix, or variant
  items are included/excluded as intended.
- If AutoCount exposes multiple item/UOM/barcode tables, prefer a read-only
  wrapper view that reconciles to the UI/report.

## Stock Balance/Status

| Field | Mapping |
| --- | --- |
| Extractor dataset | `stock_balance` |
| Candidate source object | Placeholder: `dbo.vw_XB_AC2_StockBalance_Phase1` |
| Decision | `Needs reconciliation` |
| Can replace current placeholder config? | No - confirmed target exists, but candidate columns and UI/report reconciliation are not complete |
| Business meaning | End-of-day item/location/UOM quantity and cost snapshot for the requested business date |
| Reconciliation report needed | AutoCount stock balance/status report by location and by item for the same as-at date |

Required columns:

- `AsAtDate`
- `ItemCode`
- `Location`
- `UOM`
- `BalanceQty`
- `SmallestBalQty`
- `UnitCost`
- `CostValue`

Missing columns:

- Column-level safe summary was not included in this handoff.
- Wrapper view design must verify date semantics and quantity/cost/value fields
  against `vItemBalQty`, `vItemUOMBalQty`, `ItemBatchBalQty`, and
  `vItemBatchBalQty`.

Risk notes:

- Direct tables or views may not represent the same calculated stock status as
  AutoCount reports.
- Cost fields may depend on costing method, location, UOM conversion, date, and
  posting state.
- Select this only after quantity and value totals reconcile by location and
  item.

## Stock Movement/Card

| Field | Mapping |
| --- | --- |
| Extractor dataset | `stock_movement` |
| Candidate source object | Placeholder: `dbo.vw_XB_AC2_StockMovement_Phase1` |
| Decision | `Needs reconciliation` |
| Can replace current placeholder config? | No - confirmed target exists, but `StockDTL` rows must be reconciled to AC2 UI/report output |
| Business meaning | Date-window stock movement/card rows, including enough document context to explain changes in balance |
| Reconciliation report needed | AutoCount stock card/movement report for the same movement window |

Required columns:

- `DocType`
- `DocNo`
- `DocDate`
- `ItemCode`
- `Location`
- `BatchNo`
- `UOM`
- `QtyIn`
- `QtyOut`
- `UnitCost`
- `SourceTable`
- `LastModified`

Missing columns:

- Column-level safe summary was not included in this handoff.
- Wrapper view design must verify document keys, signs, dates, location, UOM,
  cost, and modified timestamp against `StockDTL`.

Risk notes:

- Movement extraction must handle backdated postings; keep the extractor overlap
  window.
- Confirm whether cancelled, voided, draft, unposted, transferred, assembled,
  and adjusted documents appear and how signs are represented.
- If a candidate is document-line based rather than stock-card based, it needs
  extra reconciliation before Phase 1 scheduling.

## Stock Documents

| Field | Mapping |
| --- | --- |
| Extractor dataset | `stock_documents` |
| Candidate source object | Placeholder: `dbo.vw_XB_AC2_StockDocuments_Phase1` |
| Decision | `Needs reconciliation` |
| Can replace current placeholder config? | No - stock document candidates are evidence surfaces until movement totals reconcile |
| Business meaning | Supporting stock document rows for adjustment, transfer, receive, issue, write-off, stocktake, and assembly analysis |
| Reconciliation report needed | AutoCount stock document listings and stock card drill-through for the same date window |

Required columns:

- `DocType`
- `DocNo`
- `DocDate`
- `Cancelled`
- `FromLocation`
- `ToLocation`
- `ItemCode`
- `Location`
- `UOM`
- `Qty`
- `UnitCost`
- `RefDocNo`
- `LastModified`

Missing columns:

- Column-level safe summary was not included in this handoff.
- Wrapper view design must verify header/detail keys, cancellation/posting
  status, item/location/UOM fields, quantity signs, costs, references, and
  modified timestamp across each stock document view.

Risk notes:

- Cancelled/voided handling must be explicit before the data feeds dashboards.
- Transfers may need both from-location and to-location rows; avoid double
  counting.
- This dataset is useful for explanations but should not be trusted until
  movement totals reconcile.

## Config Guidance

Use [config/autocount_stock_extract.from_probe.example.json](../../config/autocount_stock_extract.from_probe.example.json)
as the starting point after the decision pack is updated. Copy it to a local VM
path such as:

```text
D:\AutoCountStockExtract\autocount_stock_extract.local.json
```

Keep the current placeholder wrapper view names until reconciliation passes:

- `dbo.vw_XB_AC2_StockMaster_Phase1`
- `dbo.vw_XB_AC2_StockBalance_Phase1`
- `dbo.vw_XB_AC2_StockMovement_Phase1`
- `dbo.vw_XB_AC2_StockDocuments_Phase1`

Only replace wrapper definitions after the candidate source objects reconcile
and the final read-only SQL login is validated. Keep the connection string in
`AUTOCOUNT_READONLY_SQL_CONNECTION_STRING`; do not put credentials in JSON.
