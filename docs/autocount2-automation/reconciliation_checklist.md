# AutoCount Stock Reconciliation Checklist

Use this checklist before scheduling any Phase 1 stock extraction job. All
checks compare read-only extractor output against AutoCount UI/report outputs
for the same company/account book, date, location scope, posting state, and UOM
rules.

Do not commit AutoCount exports, raw CSVs, screenshots with sensitive data,
credentials, or local config files. Record only safe summaries, pass/fail
results, row counts, totals, and non-sensitive notes.

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
