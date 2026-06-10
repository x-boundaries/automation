# Phase 1 Extraction Mapping

Use the [Phase 1 reconciliation runbook](phase1_reconciliation_runbook.md) to validate these candidate surfaces against AutoCount UI/report outputs before replacing any placeholder with a wrapper view.

This mapping turns the SQL probe decision pack into stock extractor
configuration guidance. It is intentionally read-only and does not define any
SQL write path.

The confirmed AC2 target is `localhost\A2006 / AED_XBOUNDARIES`, matching the
AutoCount 2.2 login screen for `(local)\A2006`, database `AED_XBOUNDARIES`, app
DB version `2.2.94`. The safe probe handoff and first non-dry-run reconciliation confirm object
visibility, aggregate counts, and metadata-only column coverage for several
candidates. Keep the configuration placeholder/wrapper-view based until
[extraction_surface_decision.md](extraction_surface_decision.md) and
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


## Actual column findings from first reconciliation

All decisions below remain `Needs reconciliation`; these findings only refine
the candidate wrapper design. They do not select a SQL surface for scheduled
Phase 1 extraction.

- `dbo.Item` plus `dbo.ItemUOM` are better raw stock-master sources than
  `dbo.vItem` plus `dbo.vItemUOM` alone because `Item` carries master flags such
  as active/stock-control and modified metadata while `ItemUOM` carries UOM and
  barcode data.
- `dbo.Item` exposes `ItemCode`, `Description`, `ItemGroup`, `ItemType`,
  `StockControl`, `LastModified`, `IsActive`, `ItemBrand`, `ItemClass`,
  `ItemCategory`, `SalesUOM`, `PurchaseUOM`, `ReportUOM`, and `BaseUOM`, but it
  does not carry `Barcode` directly.
- `dbo.ItemUOM` exposes `ItemCode`, `UOM`, `BarCode`, `Cost`, `Price`, and
  `Rate`.
- `dbo.vItem` exposes item/group/brand/class/category descriptions,
  `ItemDescription`, and `ItemBaseUOM`, but not all raw `Item` flags such as
  `IsActive`, `StockControl`, and `LastModified`.
- `dbo.vItemUOM` exposes `UOMItemCode`, `UOM`, `BarCode`, `UOMRate`, `UOMCost`,
  and `UOMBalQty`; it does not expose the item key under the exact column name
  `ItemCode`.
- `dbo.vItemBalQty` has `ItemCode`, `UOM`, `Location`, and `BalQty`, but the
  first aggregate run found only 1 row.
- `dbo.vItemUOMBalQty` has `ItemCode`, `UOM`, `Rate`, and `BalQty`; it had
  21,831 rows in the first aggregate run but does not expose `Location`.
- `dbo.ItemBatchBalQty` has `ItemCode`, `UOM`, `Location`, `BatchNo`, `BalQty`,
  and `MostRecentlyCost`; the first aggregate run found only 1 row.
- `dbo.vItemBatchBalQty` has `ItemCode`, `BatchNo`, and `BalQty`.
- `dbo.StockDTL` is the best stock movement candidate so far. It exposes
  `StockDTLKey`, `ItemCode`, `UOM`, `Location`, `BatchNo`, `DocDate`, `DocType`,
  `DocKey`, `DtlKey`, `Qty`, `Cost`, `TotalCost`, `LastModified`, and `DocInfo`,
  but it does not expose `DocNo`.
- Stock document header views expose `DocKey`, `DocNo`, `DocDate`, `Cancelled`,
  and `LastModified`.
- Stock document detail views generally expose `DtlKey`, `DocKey`, `ItemCode`,
  `Location`, `UOM`, `Qty`, `UnitCost`, and `SubTotal`; use
  `UnitCost`/`SubTotal`, not `Cost`/`TotalCost`, when drafting document evidence
  wrappers.
- `dbo.vStockTransferDetail` does not expose `Location`; transfer location
  semantics must come from the header `FromLocation` and `ToLocation`.
- `dbo.vGoodsReceivedNoteDetail` uses `UnitPrice`, `SubTotal`, `UserUOM`/`UOM`,
  `Location`, and `ItemCode`; GRN evidence may overlap purchase/receiving flows
  and must not be double-counted.

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
| `dbo.Item` | `ItemCode`, `Description`, `ItemGroup`, `ItemType`, `StockControl`, `LastModified`, `IsActive`, `ItemBrand`, `ItemClass`, `ItemCategory`, `SalesUOM`, `PurchaseUOM`, `ReportUOM`, `BaseUOM`; 21,831 rows. | `Barcode` is not directly on `Item`; join to `ItemUOM` for UOM/barcode. | Compare count and known samples against AutoCount stock item listing. | Raw table may not include UOM/barcode/display fields exactly as reports show them. | Wrap in `dbo.vw_XB_AC2_StockMaster_Phase1` after reconciliation. |
| `dbo.ItemUOM` | `ItemCode`, `UOM`, `BarCode`, `Cost`, `Price`, `Rate`; 21,831 rows. | Master flags such as active/stock-control remain on `Item`; verify update timestamp availability. | Compare UOM/barcode coverage against item listing and sample item setup. | Multi-UOM rules may require joining to `Item` or a view. | Use only through the stock master wrapper view. |
| `dbo.vItem` | Item key plus group/brand/class/category descriptions, `ItemDescription`, `ItemBaseUOM`; 21,831 rows. | Does not expose all raw `Item` flags such as `IsActive`, `StockControl`, and `LastModified`. | Compare count and samples against AutoCount item listing. | View semantics may already apply UI filters; do not assume without reconciliation. | Preferred candidate for wrapper source if it matches UI/report fields. |
| `dbo.vItemUOM` | `UOMItemCode`, `UOM`, `BarCode`, `UOMRate`, `UOMCost`, `UOMBalQty`; 21,831 rows. | Does not expose `ItemCode` under the exact name `ItemCode`; verify join/key mapping before use. | Compare UOM rows and known sample items against AutoCount item setup. | View may duplicate or filter item/UOM combinations. | Use only through the stock master wrapper view. |

### Stock Balance/Status Candidates

Required Phase 1 columns: `AsAtDate`, `ItemCode`, `Location`, `UOM`,
`BalanceQty`, `SmallestBalQty`, `UnitCost`, `CostValue`.

| Candidate source object | Available key columns from the probe | Missing/unknown columns | Reconciliation required | Risk notes | Direct or wrapper view? |
| --- | --- | --- | --- | --- | --- |
| `dbo.vItemBalQty` | `ItemCode`, `UOM`, `Location`, `BalQty`; 1 row in first aggregate run. | Verify as-at date semantics, cost/value fields, and why row count is much lower than master count. | Compare row count and totals against AutoCount stock balance/status report. | The 1-row count is suspicious compared with 21,831 items; may expose only non-zero/current balances. | Wrap or reject after reconciliation; do not query directly. |
| `dbo.vItemUOMBalQty` | `ItemCode`, `UOM`, `Rate`, `BalQty`; 21,831 rows in first aggregate run. | Does not expose `Location`; verify whether it is all-location/all-UOM balance before use. | Compare quantity and value totals by item/location against stock balance/status report. | Row count may include zero balances or one row per item/UOM rather than item/location. | Useful all-item/UOM evidence, but not a by-location wrapper source unless location semantics are supplied elsewhere. |
| `dbo.ItemBatchBalQty` | `ItemCode`, `UOM`, `Location`, `BatchNo`, `BalQty`, `MostRecentlyCost`; 1 row in first aggregate run. | Verify date semantics and whether batch balance is relevant for Phase 1. | Compare batch-tracked stock balances if batch tracking is enabled. | Could be irrelevant if the company does not use batch tracking. | Wrap only if batch balances are required and reconcile. |
| `dbo.vItemBatchBalQty` | `ItemCode`, `BatchNo`, `BalQty`; 21,831 rows in first aggregate run. | Missing location/UOM fields for by-location balance; verify batch semantics before use. | Compare batch-tracked stock balances if batch tracking is enabled. | View may filter empty batches or aggregate differently from reports. | Wrap only if batch balances are required and reconcile. |

### Stock Movement/Card Candidates

Required Phase 1 columns: `DocType`, `DocNo`, `DocDate`, `ItemCode`,
`Location`, `BatchNo`, `UOM`, `QtyIn`, `QtyOut`, `UnitCost`, `SourceTable`,
`LastModified`.

| Candidate source object | Available key columns from the probe | Missing/unknown columns | Reconciliation required | Risk notes | Direct or wrapper view? |
| --- | --- | --- | --- | --- | --- |
| `dbo.StockDTL` | `StockDTLKey`, `ItemCode`, `UOM`, `Location`, `BatchNo`, `DocDate`, `DocType`, `DocKey`, `DtlKey`, `Qty`, `Cost`, `TotalCost`, `LastModified`, `DocInfo`; 2 rows dated 2026-06-04. | Does not expose `DocNo`; verify document-key joins, signed quantity, cost, source table, and modified timestamp. | Check both rows dated 2026-06-04 against the AC2 UI/report and stock card. | Low transaction count is plausible for a not-live AC2 target but must be confirmed. Sign handling and backdated postings are high risk. | Wrap in `dbo.vw_XB_AC2_StockMovement_Phase1` after reconciliation. |

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
| `dbo.vStockTransferDetail` | Detail fields include item/UOM/quantity/cost evidence but not detail `Location`. | Use header `FromLocation`/`ToLocation`; verify item, UOM, quantity, unit cost, and line references. | Reconcile transfer detail to stock card for both locations. | Must preserve both source and destination movement meaning and avoid inventing detail location. | Use through stock document wrapper after sign/location rules are confirmed. |
| `dbo.vStockIssue` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify header keys, dates, cancellation/posting status, references, and modified timestamp. | Compare issue listing against UI/report. | Header rows alone may not provide item/location quantities. | Use through stock document wrapper only if paired with detail rows. |
| `dbo.vStockIssueDetail` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify item, location, UOM, quantity, unit cost, and line reference fields. | Reconcile issue detail totals to stock card. | Quantity sign and cancellation handling must be explicit. | Use through stock document wrapper after reconciliation. |
| `dbo.vStockWriteOff` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify header keys, dates, cancellation/posting status, references, and modified timestamp. | Compare write-off listing against UI/report. | Header rows alone may not provide item/location quantities. | Use through stock document wrapper only if paired with detail rows. |
| `dbo.vStockWriteOffDetail` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify item, location, UOM, quantity, unit cost, and line reference fields. | Reconcile write-off detail totals to stock card. | Quantity sign and valuation handling must be explicit. | Use through stock document wrapper after reconciliation. |
| `dbo.vGoodsReceivedNote` | Column-level safe summary not supplied; object identified as stock document candidate. | Verify header keys, dates, cancellation/posting status, supplier references, and modified timestamp. | Compare GRN listing against UI/report if GRN is in Phase 1 evidence. | Could be later-phase purchasing evidence rather than stock extraction source. | Use through wrapper only if needed for reconciliation evidence. |
| `dbo.vGoodsReceivedNoteDetail` | `UnitPrice`, `SubTotal`, `UserUOM`/`UOM`, `Location`, and `ItemCode`. | Verify quantity, line references, and whether `UnitPrice` is the intended cost evidence. | Reconcile GRN detail totals to stock card/receiving reports if in scope. | May overlap stock receive or purchasing surfaces; avoid double counting. | Use through wrapper only if needed for reconciliation evidence. |

### Supporting Setup Candidate

| Candidate source object | Available key columns from the probe | Missing/unknown columns | Reconciliation required | Risk notes | Direct or wrapper view? |
| --- | --- | --- | --- | --- | --- |
| `dbo.Location` | Location setup visible with 7 rows: `GIT`, `HQ`, `XB01`, `XB02`, `XB03`, `XB04`, `XB05`. | Verify location code/name/active fields and any warehouse grouping fields. | Compare location count against AutoCount location setup. | Location filters affect every stock balance and movement total. | Reference from wrapper views after location setup reconciles. |

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

- `Item` does not carry barcode directly; `ItemUOM` carries `UOM` and `BarCode`.
- Wrapper view design should start from `Item` joined to `ItemUOM`, while using
  `vItem`/`vItemUOM` only as reconciliation evidence where helpful.

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

- `vItemBalQty` includes `Location` but currently has only 1 row.
- `vItemUOMBalQty` has 21,831 rows but no `Location`.
- `ItemBatchBalQty` includes location and batch fields but currently has only 1
  row.
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

- `StockDTL` has document keys (`DocType`, `DocKey`, `DtlKey`) and `DocInfo` but
  does not expose `DocNo`.
- Wrapper view design must verify signs, dates, location, UOM, cost, and modified
  timestamp against `StockDTL`.

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

- Header views carry `DocNo`, `DocDate`, `Cancelled`, and `LastModified`; detail
  views generally use `UnitCost` and `SubTotal` instead of `Cost` and
  `TotalCost`.
- Transfer detail lacks `Location`; preserve header `FromLocation` and
  `ToLocation` and reconcile sign/location semantics manually.
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
