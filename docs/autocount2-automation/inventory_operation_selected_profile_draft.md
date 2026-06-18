# Inventory Operation Selected Profile Draft

## Purpose

This draft records the inventory-operation surfaces that look most useful after
the PR #47 metadata-only discovery run against the confirmed AutoCount 2.0
target.

This is not extraction approval. Every entry remains `Needs reconciliation`,
`data_maturity=immature_pre_go_live`,
`business_reconciliation_status=not_reconciled`, and
`final_production_selected=false`.

The next step is local reconciliation against AutoCount UI/report paths and
business review before any extraction profile changes.

## What Local PR #47 Discovery Confirmed

The local metadata-only run completed successfully with `--no-row-counts`.

- Server and database: `localhost\A2006` / `AED_XBOUNDARIES`
- SQL login/user: `xb_ac2_readonly` / `xb_ac2_readonly`
- Objects discovered: 683
- Columns discovered: 22156
- Row-count records: 0
- Exceptions: 0
- Candidate families: 105 candidates, 15 in each family

Row counts may be unavailable to `xb_ac2_readonly` without
`VIEW DATABASE STATE`. That does not change candidate maturity or approval
status.

## Candidate Selected Surfaces By Business Function

All candidates below are draft selections for reconciliation only:
`decision=Needs reconciliation`, `business_reconciliation_status=not_reconciled`,
and `final_production_selected=false`.

### GRN / Goods Receiving

- `dbo.vGoodsReceivedNote`
- `dbo.vGoodsReceivedNoteDetail`
- `dbo.vGoodsReceivedNoteSubDetail`
- `dbo.GR`
- `dbo.GRDTL`
- `dbo.vStockReceive`
- `dbo.vStockReceiveDetail`

### Stock Transfer / Inter-Location Movement

- `dbo.vStockTransfer`
- `dbo.vStockTransferDetail`
- `dbo.XFER`

### PO / In-Transit Support

- `dbo.vPurchaseOrder`
- `dbo.PO`
- `dbo.PODTL`

Useful PO and in-transit metadata signals:

- `TransferedQty`
- `PostToStock`
- `PurchaseLocation`

### Movement Semantics

Useful stock movement signal columns:

- `FromDocType`
- `FromDocNo`
- `DocType`
- `DocNo`
- `TransferedQty`
- `SmallestQty`
- `LocationBalQty`
- `BatchBalQty`

### Transfer / GIT Hints

- `dbo.XFERUDF_GIT`
- `XFERUDF_RcvDate`
- `XFERUDF_RcvBy`
- `XFERUDF_UseGIT`

### Location Evidence

Useful location signal columns and views:

- `FromLocation`
- `ToLocation`
- `PurchaseLocation`
- `SalesLocation`
- `dbo.vBranch`

`dbo.Branch` and `dbo.vBranch` previously showed 0 rows, so branch evidence is
only possible context. The true stock location master remains unresolved.

## Unresolved Gaps

- Confirm the GRN and receiving surfaces against AutoCount UI/report outputs.
- Confirm whether `GR`/`GRDTL` and receiving views represent the same business
  event grain or different reporting grains.
- Confirm transfer grain and whether `XFER` is the transactional anchor.
- Confirm whether the GIT UDF fields represent current stock in transit logic.
- Confirm stock location master ownership; branch address/contact/postcode
  fields are not enough to select a location master.
- Confirm item/UOM/barcode/brand/category/class/group fields separately before
  expanding dashboard dimensions.

## Next Local Validation Step

Run a metadata-safe profile reconciliation step that inspects only object names,
column names, row counts when permitted, and generated manifest summaries.

Do not run live raw extraction, do not export raw CSV rows, do not schedule the
job, and do not write back to AutoCount.

## Surfaces Intentionally Not Selected Yet

- CoA/account master extraction remains unresolved and not selected.
- `dbo.GLDTL` remains disabled by default.
- AR/AP detail remains disabled by default.
- POS-only and set-meal surfaces are not inventory-operation selections.
- AP/AR/payment/deposit/refund/contra surfaces are not inventory-operation
  selections.
- `dbo.vCreditor` is not selected as an item/product attribute surface based on
  generic creditor/contact/postcode fields alone.
- Branch address/contact/postcode-heavy views are not selected as stock location
  master surfaces.
