# X-Boundaries Automation Dashboard

*Last reviewed: 2026-06-06 18:43:18 SGT*

Source scope: `todo.md` and `XB new system 2026.xlsx` only.

# Two-Document Intake

This dashboard is intentionally scoped to two working documents:

- `todo.md`: confirmed work the user already knows is needed.
- `XB new system 2026.xlsx`: wishlist/intake workbook that needs clarification before execution.

The original files live outside this repository. This note is a distilled, non-operational summary so the GitHub dashboard can build without committing the workbook or desktop files.

## Confirmed From todo.md

- Track past and future category trends to support forecasting and planning.
- Track SKU speed sold, not only total volume sold.
- Create a chatbot and inventory tracking hub as the main source for incoming orders.
- Reduce overbuying risk caused by incoming-order information being split across places.
- Add inventory low alerts.
- Decide whether budget should be tracked per item, category, vendor, or a combination.
- After the order sheet is ready, track what is still pending delivery.
- Migration sidenote: current XB03 stocks will be transferred into new XB05.

## What The Excel Workbook Is Asking For

### Backend

- Upload shipment details into AutoCount to create purchase orders before shipment arrival.
- Use those purchase orders to create new products or update existing stock.
- Alert when product price changes are detected.
- Receive warehouse goods by scanning barcodes against the purchase order.
- Alert on stock quantity discrepancies during receiving.
- Support a receive discrepancy choice: rectify now or rectify later.
- Upload goods-received data and disburse goods to MG, Online, Warehouse, and JBM.
- Let each channel owner acknowledge goods received in the system.
- Send replenishment alerts when evergreen item quantity falls below a threshold.
- Clarify ETA tracking for pre-order stock. The workbook names this item but gives no details.

### Retail

- Receive goods from HQ by scanning items with the AutoCount app on a laptop and acknowledging on POS.
- Receive supplier goods by creating a GRN from the supplier DO and scanning received items.
- Support membership POS behavior: member database, automatic membership discount, points accumulation, and birthday vouchers.
- Capture membership fields: mobile number, email, birthday month, and postal code.
- Clarify POS hardware. The workbook explicitly asks: "What POS hardware?"

### Wholesale

- Receive wholesale goods by scanning items with the AutoCount app on a laptop.
- For outright buyers, create invoices in AutoCount.
- For consignees, create delivery orders that can push to AR invoice creation in AutoCount.
- Support different consignee stores.

### Reports

- Build reporting by vendor.
- Build reporting by brand.
- Build reporting by category.
- Build sales breakdown reporting for average pieces per transaction and average sales per transaction.
- The workbook includes a current AutoCount category list for report grouping; keep the full category values in the workbook/source system rather than copying them into the repo.

## Open Questions To Clarify

- What is the first operational priority: incoming orders hub, stock receiving, replenishment alerts, or reporting?
- Which system is the source of truth for incoming orders before the order sheet is ready?
- What exactly counts as SKU speed sold: units per day, sell-through rate, days of cover, or another metric?
- What date window should forecasting use for category trends?
- What stock threshold logic should drive low-stock and evergreen alerts?
- Should budget be tracked by item, category, vendor, channel, or all four?
- Which AutoCount export/import/API path is available for purchase orders, GRN, stock updates, invoices, delivery orders, and AR?
- What barcode scanners, laptops, and POS hardware are actually in use?
- Who owns discrepancy decisions when warehouse receiving finds mismatches?
- Who owns goods-received acknowledgement for MG, Online, Warehouse, and JBM?
- What statuses should pending delivery tracking use after the order sheet is ready?
- Does the XB03 to XB05 stock transfer change item codes, locations, or opening balance requirements?
- Should membership work include consent/PDPA fields in addition to the workbook's mandatory fields?
- Which reports are needed first: vendor, brand, category, or sales breakdown?

## Working Backlog

1. Confirm the source of truth and statuses for incoming orders and pending delivery.
2. Define SKU speed sold and category trend metrics.
3. Confirm AutoCount capabilities for shipment upload, PO creation, GRN, stock update, invoice, DO, and AR flows.
4. Confirm hardware and owner responsibilities for retail, warehouse, and wholesale scanning.
5. Design the lowest-risk first dashboard around incoming orders, pending delivery, low-stock alerts, and SKU/category trend views.
6. Keep write-back out of scope until export/import behavior is confirmed and manually validated.
