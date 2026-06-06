# Phase 1 Extraction Mapping

This mapping turns the SQL probe decision pack into stock extractor
configuration guidance. It is intentionally read-only and does not define any
SQL write path.

Current PR limitation: no safe `probe_report.md` or `candidates.csv` summary was
supplied in this branch context. Candidate source objects below therefore use
placeholder local view names. Replace them only after
[extraction_surface_decision.md](extraction_surface_decision.md) selects real
read-only surfaces and [reconciliation_checklist.md](reconciliation_checklist.md)
passes.

## Phase 1 Gates

Before any candidate can replace a placeholder config entry:

- `permission_risks.csv` has no direct write, schema, security, or broad execute
  risk for the probe/extractor login.
- `role_risks.csv` does not show `db_owner`, `db_datawriter`, `db_ddladmin`,
  `db_securityadmin`, `db_accessadmin`, or `db_backupoperator`.
- Candidate object columns cover the required output contract or a safe local
  read-only wrapper view supplies the missing columns.
- AutoCount UI/report reconciliation passes for a known business date.
- No raw probe output, raw extract, local config, or credential is committed.

## Stock Master

| Field | Mapping |
| --- | --- |
| Extractor dataset | `stock_master` |
| Candidate source object | Placeholder: `dbo.vw_XB_AC2_StockMaster_Phase1` |
| Decision | `Unknown` |
| Can replace current placeholder config? | No - safe probe candidate and reconciliation not supplied |
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

- Unknown until safe candidate column summary is supplied.

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
| Decision | `Unknown` |
| Can replace current placeholder config? | No - safe probe candidate and reconciliation not supplied |
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

- Unknown until safe candidate column summary is supplied.

Risk notes:

- Direct tables may not represent the same calculated stock status as AutoCount
  reports.
- Cost fields may depend on costing method, location, UOM conversion, date, and
  posting state.
- Select this only after quantity and value totals reconcile by location and
  item.

## Stock Movement/Card

| Field | Mapping |
| --- | --- |
| Extractor dataset | `stock_movement` |
| Candidate source object | Placeholder: `dbo.vw_XB_AC2_StockMovement_Phase1` |
| Decision | `Unknown` |
| Can replace current placeholder config? | No - safe probe candidate and reconciliation not supplied |
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

- Unknown until safe candidate column summary is supplied.

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
| Decision | `Unknown` |
| Can replace current placeholder config? | No - safe probe candidate and reconciliation not supplied |
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

- Unknown until safe candidate column summary is supplied.

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

Then replace only the placeholder view names that have been selected and
reconciled. Keep the connection string in
`AUTOCOUNT_READONLY_SQL_CONNECTION_STRING`; do not put credentials in JSON.
