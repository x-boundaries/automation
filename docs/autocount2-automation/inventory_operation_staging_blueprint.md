# Inventory Operation Staging Blueprint

## Purpose

This blueprint proposes future staging tables for the selected AutoCount 2.0
inventory operation surfaces. It does not implement loading, warehouse tables,
dashboards, scheduler behavior, write-back, or import/API behavior.

All future staging work remains `Needs reconciliation`,
`business_reconciliation_status=not_reconciled`,
`data_maturity=immature_pre_go_live`, and
`final_production_selected=false` until business reconciliation proves
otherwise.

## Proposed Staging Tables

### `stg_ac2_supplier`

Source candidates:

- `dbo.vCreditor`
- `dbo.Creditor`

Purpose: supplier reference staging. `dbo.Creditor` uses `AccNo`; `dbo.vCreditor`
exposes `CreditorCode`.

### `stg_ac2_purchase_order_header`

Source candidates:

- `dbo.vPurchaseOrder`
- `dbo.PO`

Purpose: purchase order header staging for future outstanding PO and purchasing
context review.

### `stg_ac2_purchase_order_line`

Source candidates:

- `dbo.PODTL`

Purpose: purchase order line staging for item, quantity, and transfer evidence.

### `stg_ac2_grn_header`

Source candidates:

- `dbo.vGoodsReceivedNote`
- `dbo.GR`

Purpose: GRN header staging. Current local data is readable but empty.

### `stg_ac2_grn_line`

Source candidates:

- `dbo.vGoodsReceivedNoteDetail`
- `dbo.vGoodsReceivedNoteSubDetail`
- `dbo.GRDTL`

Purpose: GRN line staging. Current local data is readable but empty.

### `stg_ac2_stock_receive_header`

Source candidates:

- `dbo.vStockReceive`

Purpose: stock receive header staging. Current local data is readable but empty.

### `stg_ac2_stock_receive_line`

Source candidates:

- `dbo.vStockReceiveDetail`

Purpose: stock receive line staging. Current local data is readable but empty.

### `stg_ac2_transfer_header`

Source candidates:

- `dbo.vStockTransfer`
- `dbo.XFER`

Purpose: transfer header staging. Current local data is readable but empty.
Transfer/GIT fields are column evidence on `dbo.vStockTransfer`, not a
standalone object.

### `stg_ac2_transfer_line`

Source candidates:

- `dbo.vStockTransferDetail`

Purpose: transfer line staging. Current local data is readable but empty.

### `stg_ac2_stock_movement_reference`

Source candidates:

- `dbo.StockDTL`

Purpose: stock movement reference staging for future movement semantics review.

### `stg_ac2_item_balance_reference`

Source candidates:

- `dbo.vItemBalQty`

Purpose: item balance reference staging for future stock balance snapshot review.

## Future Dashboard Model

Future dimensions, not implemented yet:

- item dimension
- supplier dimension
- location dimension, still unresolved

Future facts, not implemented yet:

- purchase order fact
- goods received fact
- stock transfer fact
- stock movement fact
- stock balance snapshot fact

Future analytics layer, not implemented yet:

- purchase recommendation analytics future layer

## Location Dimension Caveat

Location master remains unresolved. Current evidence includes `FromLocation`,
`ToLocation`, `PurchaseLocation`, `SalesLocation`, branch context, and stock
balance references, but this is not enough to declare a clean location
dimension. Future staging design must keep location profile review separate
until the authoritative location master is confirmed.

## Current Readiness Interpretation

Use explicit readiness statuses:

- `ready_empty_surface`
- `ready_non_empty_surface`
- `needs_profile_review`
- `blocked`
- `not_applicable`

Based on the local PR #50 run:

- Ready but empty: GRN, stock receive, and stock transfer surfaces.
- Ready with limited data: PO, supplier, `StockDTL`, and `vItemBalQty`.
- Not final: all surfaces remain not reconciled and not production approved.
