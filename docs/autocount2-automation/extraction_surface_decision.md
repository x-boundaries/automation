# AutoCount Extraction Surface Decision

See also the [Phase 1 reconciliation runbook](phase1_reconciliation_runbook.md) for the safe aggregate validation workflow before wrapper views are finalized.

## Status

Decision pack date: 2026-06-09

This document records the decision process for choosing read-only AutoCount 2.2
extraction surfaces after running the SQL Server local probe on the Windows VM.
Only safe metadata from the confirmed probe handoff is recorded here. Generated
probe outputs, raw CSVs, credentials, connection strings, local configs,
screenshots, and raw business rows must stay out of Git.

No SQL object is `Selected for Phase 1` yet. The confirmed AC2 target is known,
but every stock-related surface remains `Needs reconciliation` until AutoCount
UI/report checks pass and a final read-only SQL login is validated.

## Decision Labels

Use exactly these labels for each candidate:

- `Selected for Phase 1`: safe summary, permissions, columns, and
  reconciliation evidence are strong enough to try in the read-only extractor.
- `Needs reconciliation`: candidate looks plausible but totals or business
  rules must still be checked against AutoCount UI/report output.
- `Rejected`: candidate is unsafe, lacks required fields, duplicates business
  meaning, or fails reconciliation.
- `Unknown`: safe probe summary is insufficient or not yet reviewed.

## Confirmed AC2 Target

| Field | Safe value |
| --- | --- |
| AutoCount target decision | Use `localhost\A2006 / AED_XBOUNDARIES` for AC2 discovery and reconciliation |
| Server shown in AutoCount 2.2 login | `(local)\A2006` |
| Database shown in AutoCount 2.2 login | `AED_XBOUNDARIES` |
| App DB version shown in AutoCount 2.2 | `2.2.94` |
| Probe output folder | `C:\XB\autocount_probe_outputs\probe_20260608_164301_21ea2942` (legacy/manual first-run folder; use `C:\XB\autocount_outputs\probe` going forward) |
| Probe output handling | Keep local only; do not commit generated probe files |

## Probe Metadata Summary

| Field | Safe value |
| --- | --- |
| SQL Server version/edition | Microsoft SQL Server 2019 Express |
| Current database | `AED_XBOUNDARIES` |
| Schemas reviewed | `dbo` only |
| Tables/views visible | 683 |
| Columns visible | 22,156 |
| Sample rows used | None committed; use metadata and safe counts only |

Known safe row-count observations from manual comparison:

| Object | Safe count / observation |
| --- | --- |
| `dbo.Item` | 21,831 rows |
| `dbo.ItemUOM` | 21,831 rows |
| `dbo.Location` | 7 rows |
| `dbo.Debtor` | 68 rows |
| `dbo.Creditor` | 70 rows |
| `dbo.GLMast` | 191 rows |
| `dbo.PaymentMethod` | 2 rows |
| `dbo.CS`, `dbo.IV`, `dbo.DO`, `dbo.GR`, `dbo.PO` | 0 rows |
| `dbo.StockDTL` | 2 rows, both dated 2026-06-04 |
| `dbo.vItem` | 21,831 rows |
| `dbo.vItemBalQty` | 1 row |
| `dbo.vItemUOMBalQty` | 21,831 rows |

## Database Comparison Decision

| Database candidate | Decision | Reason |
| --- | --- | --- |
| `localhost\SQLEXPRESS / AED_XBoundaries` | `Rejected` | Contains historical legacy/live transactions from 2018 to 2026 and is not the confirmed AC2 target. Do not use for AC2 extraction. |
| `localhost\SQLEXPRESS / A893478` | `Rejected` | Contains only 1 object and is not the main company data database. |
| `localhost\A2006 / AED_XBOUNDARIES` | `Needs reconciliation` | Confirmed by the AutoCount 2.2 app login as the AC2 target. Use this database for AC2 manual discovery and report reconciliation only. |

## Security Decision

| Check | Result | Decision impact |
| --- | --- | --- |
| Direct risky permissions from `permission_risks.csv` | 0 direct permission risks | Direct permission summary did not show write/schema/security grants. |
| Current discovery login database user | `dbo` | Discovery-only posture; not approved for scheduled extraction. |
| Risky database roles from `role_risks.csv` | Current login has risky role memberships | Blocks scheduled automation until replaced. |
| `db_owner` membership | Present | Blocks final read-only use. |
| `db_datawriter` membership | Present | Blocks final read-only use. |
| `db_ddladmin` membership | Present | Blocks final read-only use. |
| `db_securityadmin` membership | Present | Blocks final read-only use. |
| `db_accessadmin` membership | Present | Blocks final read-only use. |
| `db_backupoperator` membership | Present | Blocks final read-only use. |

Current conclusion: the current discovery login is allowed for manual metadata
discovery and reconciliation only. It is not approved for scheduled automation.
Final scheduling requires a SQL login or Windows account with read-only access,
no write/schema/security/admin roles, and a clean follow-up probe. See
[read_only_sql_login_plan.md](read_only_sql_login_plan.md).

## Candidate Groups

The SQL probe produces heuristic candidate matches. Treat object and column
matches as hints only. Do not treat candidates as verified AutoCount business
surfaces until they reconcile to AutoCount UI/report outputs.

### Item/Product/Stock Master

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.Item` | 21,831 rows | `Needs reconciliation` | Plausible item master table; verify count, stock-control filters, inactive/service/package handling, and required columns against the AutoCount stock item listing. |
| `dbo.ItemUOM` | 21,831 rows | `Needs reconciliation` | Plausible UOM companion table; verify one-to-one count, barcode/UOM rules, and whether multi-UOM items require joins. |
| `dbo.vItem` | 21,831 rows | `Needs reconciliation` | Plausible item view; may be safer than raw tables if it mirrors UI fields, but still needs report count and column reconciliation. |
| `dbo.vItemUOM` | Object identified as stock master candidate | `Needs reconciliation` | Plausible UOM view; column-level safe summary was not included in this handoff, so verify before use. |

### Stock Balance/Status

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.vItemBalQty` | 1 row | `Needs reconciliation` | Row count is unexpectedly low relative to item count; check report filters and whether this view only exposes non-zero/current balances. |
| `dbo.vItemUOMBalQty` | 21,831 rows | `Needs reconciliation` | Plausible item/UOM balance status surface; reconcile quantity, location, UOM, zero-balance behavior, and cost/value fields. |
| `dbo.ItemBatchBalQty` | Object identified as stock balance/status candidate | `Needs reconciliation` | Batch-specific balance candidate; verify whether AC2 uses batch tracking for this company before including it. |
| `dbo.vItemBatchBalQty` | Object identified as stock balance/status candidate | `Needs reconciliation` | Batch-specific balance view candidate; use only if batch balances reconcile to UI/report output. |

### Stock Movement/Stock Card

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.StockDTL` | 2 rows, both dated 2026-06-04 | `Needs reconciliation` | Plausible stock-card/detail surface. Check both rows in AC2 UI/report and confirm AC2 is not live yet before treating low volume as expected. |

### Stock Document/Transfer/Adjustment

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.vStockAdjustment` | Stock document candidate | `Needs reconciliation` | Header-style candidate; cancelled/voided/posting-state handling must be explicit. |
| `dbo.vStockAdjustmentDetail` | Stock document candidate | `Needs reconciliation` | Detail-style candidate; verify signs and stock-card totals. |
| `dbo.vStockReceive` | Stock document candidate | `Needs reconciliation` | Header-style candidate; reconcile to receiving documents and stock movement. |
| `dbo.vStockReceiveDetail` | Stock document candidate | `Needs reconciliation` | Detail-style candidate; verify item/location/UOM quantities. |
| `dbo.vStockTransfer` | Stock document candidate | `Needs reconciliation` | Header-style candidate; transfer from/to location handling must avoid double counting. |
| `dbo.vStockTransferDetail` | Stock document candidate | `Needs reconciliation` | Detail-style candidate; verify whether both source and destination movements are represented. |
| `dbo.vStockIssue` | Stock document candidate | `Needs reconciliation` | Header-style candidate; reconcile issue documents to stock card. |
| `dbo.vStockIssueDetail` | Stock document candidate | `Needs reconciliation` | Detail-style candidate; verify signs and cancellation behavior. |
| `dbo.vStockWriteOff` | Stock document candidate | `Needs reconciliation` | Header-style candidate; reconcile write-off documents to stock card. |
| `dbo.vStockWriteOffDetail` | Stock document candidate | `Needs reconciliation` | Detail-style candidate; verify quantity signs and value handling. |
| `dbo.vGoodsReceivedNote` | Stock document candidate | `Needs reconciliation` | Header-style GRN candidate; reconcile expected empty/new AC2 transaction state before later-phase use. |
| `dbo.vGoodsReceivedNoteDetail` | Stock document candidate | `Needs reconciliation` | Detail-style GRN candidate; verify item/location/UOM quantities before use. |

### Sales Invoice/Cash Sale

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.CS` | 0 rows | `Needs reconciliation` | Later phase; confirm empty state is expected because AC2 is not live yet. |
| `dbo.IV` | 0 rows | `Needs reconciliation` | Later phase; confirm no old AC1 historical invoices are being pulled from SQLEXPRESS. |

### Purchase Order/GRN

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.PO` | 0 rows | `Needs reconciliation` | Later phase; confirm empty state is expected because AC2 is not live yet. |
| `dbo.GR` | 0 rows | `Needs reconciliation` | Later phase; reconcile to AC2 UI/report only if receiving is in scope. |

### Debtor/Customer

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.Debtor` | 68 rows | `Needs reconciliation` | Later phase; master-data count appears plausible but is not part of stock extractor Phase 1. |

### Creditor/Supplier

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.Creditor` | 70 rows | `Needs reconciliation` | Later phase; master-data count appears plausible but is not part of stock extractor Phase 1. |

### AR/AP

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not selected | No AC2 AR/AP extraction surface is approved in this phase | `Unknown` | Later phase; sensitive financial data handling required. |

### GL/Account/Journal

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.GLMast` | 191 rows | `Needs reconciliation` | Later phase; extra care needed around finance reporting semantics. |

### Payment/Bank/Cashbook

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| `dbo.PaymentMethod` | 2 rows | `Needs reconciliation` | Later phase; sensitive financial data handling required. |

## Phase 1 Decision

No SQL object is selected for Phase 1 in this document yet. The next manual step
is to reconcile the confirmed AC2 target against AutoCount UI/report outputs:

- stock item listing for `Item`, `ItemUOM`, `vItem`, and `vItemUOM`,
- location setup for `Location`,
- stock balance/status report for `vItemBalQty`, `vItemUOMBalQty`,
  `ItemBatchBalQty`, and `vItemBatchBalQty`,
- stock card/movement report for `StockDTL`,
- stock document listings for the stock document views.

Keep the extractor configuration wrapper-view based until those checks pass and
a read-only login probe shows no blocking permission or role risks.
