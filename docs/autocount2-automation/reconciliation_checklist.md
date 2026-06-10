# AutoCount Stock Reconciliation Checklist

Run the [Phase 1 reconciliation runbook](phase1_reconciliation_runbook.md) before marking any candidate object or wrapper view as reconciled.

Use this checklist before scheduling any Phase 1 stock extraction job. All
checks compare read-only extractor output against AutoCount UI/report outputs
for the same company/account book, date, location scope, posting state, and UOM
rules.

Do not commit AutoCount exports, raw CSVs, screenshots with sensitive data,
credentials, or local config files. Record only safe summaries, pass/fail
results, row counts, totals, and non-sensitive notes.

## Confirmed AC2 Target Phase 1 Checklist

Use this concrete checklist for `localhost\A2006 / AED_XBOUNDARIES`, confirmed
by the AutoCount 2.2 login screen as `(local)\A2006`, database
`AED_XBOUNDARIES`, app DB version `2.2.94`.

| Check | Compare | Pass criteria |
| --- | --- | --- |
| Confirm target database | Extractor/probe connection target vs AutoCount 2.2 login screen | Server is `localhost\A2006` or `(local)\A2006`; database is `AED_XBOUNDARIES`; no SQLEXPRESS database is used for AC2 extraction. |
| Item master count | `Item` and `vItem` counts vs AutoCount stock item listing | 21,831-row safe count reconciles after applying the same stock/non-stock and active/inactive filters. |
| Item/UOM count | `ItemUOM`, `vItemUOM`, and `vItemUOMBalQty` counts vs AutoCount item/UOM setup or listing | 21,831-row safe counts are expected, or differences are explained by UI/report filters. |
| Location setup count | `Location` count vs AutoCount location setup | 7 locations reconcile to the AC2 location setup. |
| Stock balance/status rows | `vItemBalQty`, `vItemUOMBalQty`, `ItemBatchBalQty`, and `vItemBatchBalQty` vs AutoCount stock balance/status report | Row counts and quantity totals match after the same date, location, UOM, zero-balance, and batch filters. |
| `vItemBalQty` low count | `vItemBalQty` 1-row safe count vs stock balance/status report filters | Confirm whether the 1 row is expected, filtered, non-zero only, or unsuitable for Phase 1. |
| `StockDTL` two rows | The two `StockDTL` rows dated 2026-06-04 vs AC2 stock card/movement UI/report | Both rows trace to AC2 UI/report output with matching document/date/item/location/UOM/sign meaning. |
| Mostly empty transaction tables | `CS`, `IV`, `DO`, `GR`, and `PO` counts vs AC2 UI/report state | 0-row counts are expected because AC2 is not live yet. |
| Imported master data counts | `Item`, `ItemUOM`, `Location`, `Debtor`, `Creditor`, `GLMast`, and `PaymentMethod` counts vs AC2 setup/import summaries | Counts are expected for imported master data and do not imply historical AC1 transactions. |
| SQLEXPRESS exclusion | Candidate source references and local config placeholders | No old AC1 historical transactions are pulled from `localhost\SQLEXPRESS / AED_XBoundaries`. |
| Final login posture | Follow-up probe with read-only login | Current `dbo` discovery login is replaced before scheduling; permission and role-risk outputs do not block automation. |

## Setup

| Check | Expected evidence |
| --- | --- |
| Business date selected | One known date with normal activity and one date with low/no activity |
| Location scope selected | Same locations in AutoCount report and extractor output |
| Posting state confirmed | AutoCount report filter matches extractor source, for example posted only |
| UOM basis confirmed | Base/smallest UOM and display UOM rules are understood |
| Read-only login approved | No blocking direct permission or database-role risk remains |
| Raw output handling confirmed | Files stay outside GitHub in approved VM folders |

## Item Master

| Check | Compare | Pass criteria |
| --- | --- | --- |
| Item count | Extracted `stock_master.csv` vs AutoCount item/stock item listing | Counts match after applying same stock/non-stock filters |
| Active item count | `IsActive` true/active rows vs AutoCount active item report/filter | Counts match |
| Inactive item count | `IsActive` false/inactive rows vs AutoCount inactive item report/filter | Counts match |
| Required identifiers | `ItemCode`, `Description`, `UOM` | No unexpected blanks for active stock-controlled items |
| Barcode/UOM coverage | `Barcode`, `UOM` | Known sample items match UI/report values |
| Category/group/brand fields | `ItemGroup`, `ItemBrand`, `ItemCategory`, `ItemClass` | Known sample items match UI/report values or gaps are documented |

## Stock Balance By Location

| Check | Compare | Pass criteria |
| --- | --- | --- |
| Quantity by location | Sum `BalanceQty` by `Location` vs AutoCount stock balance/status report | Totals match within documented rounding/UOM rules |
| Item count by location | Distinct `ItemCode` per `Location` vs AutoCount report | Counts match after same zero-balance filter |
| Zero/negative stock handling | Extracted zero and negative balances vs AutoCount report filters | Inclusion/exclusion is intentional and documented |
| Smallest UOM quantity | `SmallestBalQty` vs AutoCount report/base UOM display | Conversion logic matches or is documented |

## Stock Balance By Item

| Check | Compare | Pass criteria |
| --- | --- | --- |
| Quantity by item | Sum `BalanceQty` by `ItemCode` vs AutoCount stock status by item | Totals match |
| Quantity by item/location | Sum by `ItemCode` and `Location` vs report detail | Totals match for sample high-volume items |
| UOM consistency | `UOM` and `SmallestBalQty` | Known multi-UOM items reconcile |
| Inactive item balance | Inactive items with stock balance | Matches AutoCount report inclusion rules |

## Stock Value And Cost

| Check | Compare | Pass criteria |
| --- | --- | --- |
| Stock value by location | Sum `CostValue` by `Location` vs AutoCount stock value/status report | Totals match within rounding tolerance |
| Stock value by item | Sum `CostValue` by `ItemCode` vs AutoCount report | Totals match for sampled items |
| Unit cost reasonableness | `UnitCost` vs report unit cost | Matches costing method and report date |
| Negative value handling | Negative stock/value rows | Matches report behavior or is documented |
| Rounding tolerance | Currency/quantity rounding | Tolerance is agreed before dashboard use |

## Stock Movement Date Window

| Check | Compare | Pass criteria |
| --- | --- | --- |
| Movement row count | Extracted date-window rows vs AutoCount stock card/movement report | Counts match after same filters |
| Quantity in total | Sum `QtyIn` by date/window vs report | Totals match |
| Quantity out total | Sum `QtyOut` by date/window vs report | Totals match |
| Movement by item | Sample `ItemCode` movement totals vs stock card | Totals match |
| Movement by location | Movement totals by `Location` vs report | Totals match |
| Document traceability | `DocType`, `DocNo`, `SourceTable` | Sample rows can be traced to UI/report drill-through |

## Cancelled Or Voided Documents

| Check | Compare | Pass criteria |
| --- | --- | --- |
| Cancelled flag meaning | `Cancelled` vs AutoCount UI/report status | Meaning is confirmed |
| Cancelled quantity effect | Cancelled/voided documents in movement and document extracts | Inclusion/exclusion matches stock card totals |
| Replacement/correction behavior | Cancelled and replacement documents | No double counting |
| Dashboard filter rule | Treatment in mart/dashboard layer | Rule is written before dashboards consume the data |

## Backdated Movement Handling

| Check | Compare | Pass criteria |
| --- | --- | --- |
| Overlap window catches changes | Re-run same business date after a known backdated posting in sandbox/test | Changed rows are re-extracted |
| Idempotent rerun | Re-run same date twice | Batch folder is replaced without duplicate rows |
| Balance after backdate | Balance as-at date after backdated movement | Matches AutoCount stock balance/status |
| Audit trail | `run_manifest.json` | New `run_id`, row counts, byte size, and SHA-256 hashes are recorded |

## Sign-Off

Phase 1 scheduling can begin only when:

- stock master count checks pass,
- stock balance quantity and value checks pass,
- movement date-window checks pass,
- cancelled/voided and backdated handling are documented,
- permission and role-risk checks do not block read-only use,
- generated outputs remain outside the repo,
- finance/operations agree the dashboard numbers are fit for internal review.
