# AutoCount 2.0 Inventory Intelligence Scope

## Current Objective

The current objective is to build a read-only AutoCount 2.0 inventory, purchase
order, and stock movement extraction foundation for dashboards, purchasing
trend analytics, and future purchase requisition recommendation workflows.

This is not a full accounting migration. The current AC2 data remains classified
as `immature_pre_go_live`, business reconciliation remains `not_reconciled`, and
`final_production_selected` remains `false`.

The near-term foundation should help answer stock movement, stock balance,
purchase timing, outstanding PO, and supplier-context questions without writing
back to AutoCount or replacing AutoCount as the accounting system.

## In Scope Now

- Item master / SKU identity.
- Item UOM, barcode, category, and brand attributes where available.
- Stock balance by location.
- Stock movement ledger.
- Purchase order headers and lines.
- Outstanding PO / stock-in-transit signal.
- GRN / goods receiving surfaces.
- Stock transfer / location movement surfaces.
- Supplier context for purchasing analytics.
- Dashboard-ready staging and warehouse design.
- Safe local snapshot/export runbooks.

## Out Of Current Scope / Parked

- CoA/account master.
- GL opening balances.
- Bank opening balances.
- Full accounting cutover.
- Finance migration ownership.
- Direct AC2 write-back.
- Scheduler/automation runs.
- Replacing AC2 as the accounting system.

CoA, GL, and bank accounting context are intentionally parked because they are
not required for the stock movement analytics MVP.

## Coverage Matrix

| Analytics need | Business question answered | Current extraction status | Known/current surface | Gap / next action | Priority |
| --- | --- | --- | --- | --- | --- |
| Item master | Which SKUs exist, how are they named, and which products are active enough for dashboards and purchase planning? | Stock smoke extraction already exists for stock master style data; final production mapping still needs reconciliation. | Stock smoke workflow; item/product candidates from probe and phase 1 mapping. | Confirm item master, item UOM, barcode, category, brand, active/inactive, non-stock/service, and variant semantics against AutoCount UI/report paths. | High |
| Stock balance by location | What is on hand by item and location, and where are low-stock or negative-stock risks? | Stock smoke extraction already exists for stock balance style data; location meaning still needs reconciliation. | Stock smoke workflow; balance/status candidates from phase 1 mapping. | Reconcile stock balance by item/location and confirm where stock locations live. `Branch`/`vBranch` had 0 rows, so location master remains unclear and may live elsewhere. | High |
| Stock movement history | Why did stock move, what document caused it, and which items are fast, slow, or irregular movers? | Stock smoke extraction already exists for movement style data; document type semantics are not final. | Stock smoke workflow; movement/stock card candidates from phase 1 mapping. | Confirm signs, posting/cancelled behavior, document type codes, date-window rules, and drill-through consistency against AutoCount reports. | High |
| PO / outstanding PO | What has been ordered, what is still outstanding, and what stock-in-transit signal can support purchasing decisions? | Selected-surface snapshot extraction already exists for PO headers and lines. | `dbo.PO`, `dbo.PODTL`, `dbo.vPurchaseOrder`. | Reconcile outstanding quantity/status semantics and connect PO lines to item, supplier, and receiving status. | High |
| GRN / receiving | What was received, when, from whom, and against which PO? | Needs discovery/confirmation. | No confirmed production extraction surface yet. | Discover and reconcile GRN / goods receiving header and detail surfaces; confirm relationship to PO and stock movement rows. | High |
| Stock transfer / inter-location movement | Which transfers moved stock between locations and what remains in transit? | Needs discovery/confirmation. | Stock document/transfer candidates only; no confirmed final surface. | Discover and reconcile stock transfer header/detail or stock document surfaces, including from-location/to-location semantics. | High |
| Supplier context | Which suppliers support each item, how concentrated is supply, and what context supports reorder decisions? | Selected-surface snapshot extraction already exists for supplier master context. | `dbo.Creditor`, `dbo.vCreditor`; PO supplier fields. | Connect supplier master to PO lines and item/vendor relationships; look for supplier lead-time signal if available. | Medium |
| Debtor/customer context | Which customer context may explain demand patterns without expanding into full sales migration? | Selected-surface snapshot extraction already exists for debtor master context. | `dbo.Debtor`, `dbo.vDebtor`. | Keep as supporting context only unless sales-demand analytics later need confirmed sales/AR surfaces. | Low |
| AR/AP invoice context | Are invoice headers useful as optional context for open-item or purchasing cash-flow awareness? | Selected-surface snapshot extraction already exists for AR/AP invoice headers, currently with 0 rows in the latest safe summary; detail extraction remains disabled by default. | `dbo.ARInvoice`, `dbo.APInvoice`; `dbo.ARInvoiceDTL` and `dbo.APInvoiceDTL` remain disabled. | Keep detail surfaces disabled unless a later reviewed scope needs them; do not treat AR/AP as required for inventory analytics MVP. | Low |
| CoA/GL/bank accounting context | Is accounting cutover or finance migration ready? | Parked. Not needed for stock movement analytics MVP. | `coa_account_master` unresolved; `GLDTL` disabled by default for raw extraction; bank surfaces not selected. | Do not add CoA extraction, GL opening, bank opening, or accounting cutover work to this MVP. Revisit only under a separate finance-owned scope. | Parked |
| Payment method context | Which payment setup context exists for later purchasing or AP interpretation? | Selected-surface snapshot extraction already exists. | `dbo.PaymentMethod`. | Keep as reference context; not a blocker for inventory dashboards. | Low |

## Proposed Analytics Architecture

### Stage 1: Read-Only Extraction And Local Snapshot

Continue using manual, read-only extraction and metadata discovery. Generated
outputs stay under the approved local output root, outside GitHub. Raw CSVs,
local `.local` configs, screenshots, credentials, and raw ERP rows are not
committed.

### Stage 2: Dashboard-Ready Warehouse/Staging Tables

Design a separate reporting/staging layer for curated inventory facts and
dimensions, such as item, item UOM, location, stock balance, movement, PO,
receiving, transfer, and supplier context. The staging/warehouse layer is for
dashboards and analytics only; it must not write to AutoCount production tables.

### Stage 3: Purchasing Recommendation Logic

Build recommendation logic from reconciled stock movement, stock balance,
outstanding PO, receiving, transfer, and supplier context. Recommendations stay
advisory until operations approves the rules and exception handling.

### Stage 4: PR Workspace/Frontend With Export/Import Handoff

If purchasing workflows need a better workspace than the AC2 UI, build an
internal PR workspace that prepares human-reviewable recommendations and
export/import handoff artifacts. It should not directly post to AC2.

### Stage 5: Controlled AC2 Write/API Integration Only If Later Approved

Direct AC2 writes, official API writes, plug-in execution, or import automation
are parked until separately approved after the read-only foundation is stable,
reconciled, and audited. Direct SQL writes to AutoCount production tables remain
out of scope.

## Next Extractor Target Shortlist

- GRN / goods receiving.
- Stock transfer.
- Stock location / location master.
- Richer item/product attributes.
- Movement document type semantics.
- Supplier lead-time signal if available.

Use the
[inventory operation surface discovery runbook](inventory_operation_surface_discovery_runbook.md)
to produce the next metadata-only candidate shortlist before expanding any
extraction profile.

## Operating Guardrails

- No SQL write-back.
- No scheduler/automation runs until separately approved.
- No new production extraction run from this documentation change.
- No generated outputs or `.local` config committed.
- No credentials, connection strings, screenshots, raw CSVs, or raw ERP rows
  committed.
- Do not enable `GLDTL` by default.
- Do not enable AR/AP detail by default.
- Do not add CoA extraction.
- Do not add direct AC2 writes or API writes.
- Keep current data classified as `immature_pre_go_live`.
- Keep business reconciliation status as `not_reconciled`.
- Keep `final_production_selected = false`.
