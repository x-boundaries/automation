# Post-Migration Automation Roadmap - X-Boundaries

## What this is

This is the automation work to do AFTER AutoCount migration is stable.

## Migration goal:
- Get AutoCount 2.0 live.
- Make stock + accounting usable.
- Complete training / parallel run / cutover.
- Stop old AutoCount safely.

## Real job after that:
- Build automation, reporting, data flows, dashboards, and AI readiness around the new system.

## Working principle

### Do not automate chaos.

### After go-live, start with read-only automation:
- Export data.
- Clean data.
- Compare data.
- Alert humans.
- Build dashboards.

### Only later automate write-back into AutoCount / SiteGiant.

### Preferred stack:
- AutoCount exports / SQL / API where allowed.
- SiteGiant exports / API where allowed.
- Marketplace exports.
- Bank / payment processor exports.
- Staging database or structured Google Sheets.
- n8n for workflows.
- Scripts / vibecoding for custom logic.
- Dashboard tool later.
- Human approval before posting financial entries.

---

# Priority summary

## Phase 0 - Stabilise first

### Do this immediately after go-live.

### Goal:
- Keep the new system alive.
- Find data issues.
- Avoid users breaking the setup.

### Automation:
- Daily issue log.
- Daily export archive.
- Backup success alert.
- Failed sync tracker.
- Master data change tracker.
- POS closing checklist.
- Daily sales report archive.

Do not build fancy dashboards yet.

---

## Phase 1 - Build data foundation

### This is the first "real job" phase.

### Goal:
- Create clean data pipes from AutoCount, SiteGiant, marketplaces, payment rails, and internal trackers.

### Automation:
- Scheduled AutoCount export collection.
- Scheduled SiteGiant export collection.
- Marketplace report collection.
- Bank / payment report collection.
- SKU mapping master table.
- Product master audit.
- Location stock table.
- Data dictionary.
- Basic data quality checks.

### Output:
- One clean dataset for product / stock / sales / channel / finance reporting.

### Why first:
- Every dashboard, AI agent, reconciliation, and alert depends on this.

---

# Phase 2 - Must-do automations

These are the highest value. Build these before cool stuff.

## 1. SKU mapping / product identity automation

Problem:
- Marketplace SKUs can change.
- Same product can have multiple external SKUs.
- Reports break if SKU identity is messy.

Build:
- Internal product ID master.
- Mapping table:
  - Internal product ID.
  - AutoCount item code.
  - Barcode.
  - RFID if any.
  - Supplier SKU.
  - Shopify SKU.
  - Shopee SKU.
  - Lazada SKU.
  - TikTok SKU.
  - KrisShop SKU if any.
  - Brand.
  - Category.
  - Size.
  - Colour.
  - Active / inactive.
- Duplicate SKU checker.
- Missing mapping alert.
- New SKU approval flow.
- Historical SKU mapping archive.

Trigger:
- New product created.
- Marketplace SKU export changes.
- AutoCount product export changes.

Output:
- Alert when product/SKU mapping is incomplete or inconsistent.

Priority:
- Very high.
- This is the base for reporting and AI later.

---

## 1A. Stock upload staging / AutoCount import template generator

Problem:
- AutoCount fields are limited.
- AutoCount 2 uses Excel copy-paste/import style, which is good for controlled uploads.
- New stock upload guy should not manually fill every AutoCount import column.
- He should only key the minimum fields, then the mapping legend fills the rest.

Build:
- Simple stock upload staging sheet.
- Input fields only:
  - SKU / barcode / supplier SKU.
  - Qty.
  - Location.
  - PO / shipment reference.
  - Remarks if needed.
- Lookup against master mapping legend.
- Auto-fill AutoCount import template columns.
- Validate before upload.
- Generate clean copy-paste/import output for AutoCount.
- Create exception sheet for unresolved SKUs.

Validation:
- SKU exists in master mapping.
- Internal primary product ID exists.
- AutoCount ItemCode exists.
- Qty is valid.
- Location is valid.
- Product is active.
- UOM exists.
- Barcode is not duplicated.
- Required AutoCount template columns are filled.

Output:
- AutoCount-ready import/copy-paste table.
- Exception list for missing/bad mappings.
- Upload log.

Priority:
- Very high.
- This should be built together with SKU mapping/product master cleanup, because it directly reduces manual upload errors.


## 2. Product master / category cleanup automation

Problem:
- Reports by vendor, brand, and category only work if product master is clean.

Build:
- Product master validation script.
- Missing brand/category alert.
- Bad category naming alert.
- Inactive item checker.
- Product attribute completeness score.
- Category mapping table.

Check fields:
- Brand.
- Vendor.
- Category.
- Product group.
- Gender / segment if needed.
- Size.
- Colour.
- UOM.
- Barcode.
- Cost.
- Selling price.
- Channel SKU mapping.

Output:
- Weekly product master cleanup list.

Priority:
- Very high.

---

## 3. Price calculator + price change alert

Problem:
- Selling price may depend on supplier cost, FX, landed cost, GST, marketplace fees, and margin.
- Staff need to know when prices change.

Build:
- External price calculator first.
- Formula by brand/category/channel.
- Compare current price vs calculated price.
- Price change approval queue.
- Alert after approved change.

Inputs:
- Supplier cost.
- FX.
- Freight.
- Duty.
- Import GST.
- Landed cost.
- Markup.
- Channel fee.
- GST.
- Rounding rule.
- Minimum margin.

Outputs:
- Retail price.
- Shopify price.
- Shopee price.
- Lazada price.
- Wholesale price.
- Promo floor price.

Alert recipients:
- Retail.
- E-commerce.
- Wholesale.
- Finance if needed.

Priority:
- High.

Do not do:
- Do not auto-update prices into AutoCount/marketplaces without approval at first.

---

## 4. Goods-in-transit / supplier prepayment tracker

Problem:
- PO released / supplier paid / goods not received yet is financially and operationally messy.

Build:
- Goods-in-transit tracker.
- PO tracker.
- Supplier prepayment tracker.
- Shipment ETA tracker.
- Packing list received flag.
- GRN completed flag.
- Landed cost completed flag.
- Variance / delay alert.

Track:
- Supplier.
- PO number.
- Supplier invoice.
- Currency.
- Amount paid.
- Items.
- Quantity.
- ETA.
- Shipment status.
- Invoice received?
- Stock received?
- GRN done?
- Landed cost allocated?
- Closed?

Alerts:
- Paid but no ETA.
- ETA passed but no GRN.
- GRN done but AP not matched.
- PO open too long.
- Partial shipment unresolved.

Priority:
- High.

---

## 5. GRN discrepancy tracker

Problem:
- Warehouse receives goods, scans against PO, and discrepancies need follow-up.

Build:
- Expected vs received checker.
- Discrepancy queue.
- Rectify now / rectify later status.
- Owner assignment.
- Resolution log.

Track:
- PO.
- Item.
- Expected qty.
- Received qty.
- Variance.
- Reason.
- Owner.
- Status.
- Date resolved.

Priority:
- High.

---

## 6. Stock transfer / channel acknowledgement tracker

Problem:
- HQ disburses to MG, JBM, wholesale, and marketplace stock pools.
- Need confirmation that receiving side acknowledged stock.

Build:
- Transfer request form.
- Transfer out log.
- Receiving acknowledgement form.
- Variance alert.
- Open transfer dashboard.

Locations:
- HQ.
- XB01 - MG / Shopify.
- XB02 - JBM.
- XB03 - Wholesale / Metro.
- XB04 - Marketplaces.

Alerts:
- Transfer sent but not acknowledged.
- Received quantity differs from sent quantity.
- Stock stuck in transit between locations.

Priority:
- High.

---

## 7. Evergreen replenishment alert

Problem:
- Evergreen items should trigger replenishment when quantity drops below threshold.

Build:
- Evergreen SKU list.
- Min/max level table.
- Daily stock export.
- Reorder alert.
- Suggested transfer/replenishment quantity.

Inputs:
- Stock by location.
- Sales velocity.
- Reorder level.
- Reorder quantity.
- Incoming stock / PO.
- Reserved stock if available.

Output:
- Replenishment alert by item/location.

Priority:
- High after stock data stabilises.

---

## 8. Daily sales reconciliation

Problem:
- Need compare POS sales to bank/card/payment receipts.

Build:
- Daily reconciliation workflow.
- POS sales export collector.
- Payment terminal report collector.
- Bank statement/import collector.
- Matching logic by date, amount, payment method, outlet.
- Exception queue.

Payment rails:
- Cash.
- NETS.
- Credit card.
- PayNow.
- GrabPay.
- AliPay.
- Shopify gateway.
- Shopee payout.
- Lazada payout.

Output:
- Matched.
- Unmatched.
- Timing difference.
- Short/over.
- Fees.
- Refunds.

Priority:
- Very high once POS is stable.

---

## 9. Marketplace payout reconciliation

Problem:
- Shopee/Lazada payouts are net of fees, refunds, freight, vouchers, and commissions.

Build:
- Marketplace order export collection.
- Payout export collection.
- Fee breakdown parser.
- Order-to-payout matching.
- Exception report.
- Monthly summary by channel.

Track:
- Gross sale.
- Platform voucher.
- Seller voucher.
- Shipping fee.
- Commission.
- Transaction fee.
- Service fee.
- Refund.
- Return.
- Net payout.
- Payout date.

Priority:
- High, but after daily POS recon.

---

## 10. AR chasing queue

Problem:
- Customer invoices need a central follow-up queue.

Build:
- AutoCount AR ageing export.
- AR chasing tracker.
- Follow-up reminder.
- Owner assignment.
- Weekly overdue summary.

Track:
- Customer.
- Invoice number.
- Invoice date.
- Due date.
- Amount.
- Outstanding amount.
- Owner.
- Last chased date.
- Next chase date.
- Notes.
- Dispute status.

Customer groups:
- Metro.
- Resellers.
- Corporate / group sales.
- Gebiz.
- Consignees.

Priority:
- High.

---

## 11. AP invoice inbox / OCR

Problem:
- Supplier invoices and expenses are manual.

Build later:
- One supplier invoice email inbox.
- Attachment archive.
- OCR/parser.
- Draft AP table.
- Duplicate invoice check.
- Human approval.
- Import/post to AutoCount only after approval.
- Link to PO / GRN where possible.

Start simple:
- Central inbox + spreadsheet tracker first.
- OCR later.
- Auto-posting last.

Priority:
- Medium-high, but do not build before finance process stabilises.

---

## 12. Returns / RMA tracker

Problem:
- Returns happen across Shopify, Shopee, Lazada, retail, wholesale, and supplier defective returns.

Build:
- RMA tracker.
- Channel return collector.
- Restock/damaged decision.
- Refund status.
- Supplier claim status.
- SLA alert for marketplace returns.

Track:
- RMA number.
- Channel.
- Customer.
- SKU.
- Reason.
- Condition.
- Refund approved?
- Restock?
- Damaged?
- Supplier claim?
- Credit note?
- Resolution date.

Priority:
- Medium-high.

---

# Phase 3 - Reporting / dashboard layer

Build after the data foundation is stable.

## Management dashboard

Core reports:
- Sales by SKU.
- Sales by channel.
- Sales by brand.
- Sales by category.
- Sales by vendor.
- Average pieces per transaction.
- Average sales per transaction.
- Gross margin.
- Channel margin.
- Marketplace fees.
- Stock on hand.
- Stock ageing.
- Sell-through %.
- Days on hand.
- Dead stock.
- Fast-moving SKUs.
- Goods in transit.
- AR ageing.
- AP ageing.
- Daily cash/bank position.

Recommended order:
1. Sales by channel / outlet.
2. Sales by SKU / brand / category.
3. Stock on hand by location.
4. Replenishment / dead stock.
5. AR/AP ageing.
6. Margin and marketplace fee analysis.
7. Cashflow / CCC.
8. Demand forecast.

Do not start with AI dashboard.
Start with boring KPI accuracy.

---

# Phase 4 - E-commerce automation

## 1. SiteGiant sync monitor

Build:
- Sync status monitor.
- Failed order alert.
- Failed product sync alert.
- Failed inventory sync alert.
- Retry queue.
- Daily channel sync summary.

Priority:
- High after SiteGiant integration starts.

## 2. Marketplace listing / SKU monitor

Build:
- Listing export collector.
- Missing SKU mapping alert.
- Inactive listing alert.
- Price mismatch alert.
- Stock mismatch alert.

Priority:
- Medium-high.

## 3. KrisShop workaround

Build only after compatibility decision:
- If SiteGiant supports KrisShop: monitor sync.
- If not: create manual import/export workflow.
- Track KrisShop orders separately until integrated.

Priority:
- Medium.

---

# Phase 5 - Retail / membership automation

Build after POS is stable.

## 1. Membership data cleanup

Build:
- Member export.
- Mandatory field completeness checker.
- Duplicate phone/email checker.
- Consent/PDPA flag checker.

Fields:
- Name.
- Mobile.
- Email.
- Birthday month.
- Postal code.
- Consent status.
- Points if available.

Priority:
- Medium.

## 2. Birthday voucher automation

Build:
- Monthly birthday member list.
- Voucher issue workflow.
- Email/SMS/WhatsApp send flow if consent allows.
- Redemption tracker.

Priority:
- Medium-low.

## 3. Points and membership reporting

Build:
- Points balance report.
- Member sales report.
- Repeat purchase report.
- Member vs non-member sales.

Priority:
- Medium-low.

---

# Phase 6 - RFID / advanced warehouse automation

Do not start here.

Build only after:
- Barcode flow works.
- GRN flow works.
- Stock transfer flow works.
- Stocktake process works.
- Hardware is confirmed.
- AutoCount/SiteGiant support is clear.

Potential automation:
- RFID stock count.
- RFID GRN scan.
- RFID transfer scan.
- RFID discrepancy detection.
- RFID high-value product audit.

Priority:
- Later.

---

# Phase 7 - AI / analytics

Only after clean data exists.

Potential AI agents:
- Demand forecast.
- Reorder suggestion.
- Dead stock recommendation.
- Channel margin monitor.
- Rolling cashflow planner.
- SKU anomaly detector.
- Price anomaly detector.
- Return/refund leakage monitor.
- Product mix recommendation.

Prerequisite:
- Clean product master.
- Clean SKU mapping.
- Stable stock data.
- Stable sales data.
- Stable payment/reconciliation data.

Priority:
- Later, but important for DLP.

---

# What not to automate first

Do not start with:
- RFID.
- AI demand forecasting.
- OCR AP auto-posting.
- Direct write-back to AutoCount.
- Direct write-back to marketplaces.
- Fully automated bank reconciliation.
- Fully automated pricing update.
- Fancy dashboard before data cleanup.

These are tempting, but they will break if base data is messy.

---

# Recommended build order

## Month 1 after go-live

- Issue log.
- Export archive.
- Data dictionary.
- Product/SKU mapping table.
- Product master audit.
- Daily POS report archive.
- Backup/restore check alert.
- Basic sales by channel report.

## Month 2

- Daily sales reconciliation MVP.
- AR chasing queue.
- Goods-in-transit tracker.
- Stock transfer acknowledgement tracker.
- Price change alert.
- Basic inventory dashboard.

## Month 3

- Marketplace payout reconciliation.
- Evergreen replenishment alert.
- GRN discrepancy tracker.
- Returns/RMA tracker.
- SiteGiant sync error monitor.

## Month 4+

- AP invoice inbox/OCR.
- Membership birthday voucher flow.
- Advanced dashboards.
- Margin analysis.
- Demand forecast.
- RFID pilot.

---

# Minimal first automation stack

Use this simple architecture first:

1. Scheduled exports:
   - AutoCount.
   - SiteGiant.
   - Shopee/Lazada/Shopify.
   - Bank/card/payment reports.

2. Central storage:
   - Shared drive folder.
   - Structured Google Sheets.
   - Or lightweight database.

3. Transform:
   - Python/scripts.
   - n8n.
   - Manual review sheet if needed.

4. Output:
   - Email/Telegram/Slack alerts.
   - Dashboard.
   - Exception list.
   - Human approval queue.

5. Write-back:
   - Only after workflow is stable.
   - Start with import files, not direct API writes.
   - Keep audit trail.

---

# My recommended top 10 backlog

1. SKU mapping / internal product ID.
2. Product master cleanup checker.
3. Daily export archive + data dictionary.
4. Daily POS/bank reconciliation MVP.
5. Goods-in-transit tracker.
6. AR chasing queue.
7. Price calculator + price change alert.
8. Stock transfer acknowledgement tracker.
9. Marketplace payout reconciliation.
10. KPI dashboard.

---

# Simple definition of done

Migration is "done" when:
- AutoCount is live.
- POS works.
- Accounting opening balances are signed off.
- Stock by location is correct enough.
- AP/AR opening is loaded.
- Training/hypercare issues are manageable.

Your real job starts when:
- You can reliably export data.
- You know which system owns each data type.
- You can build checks/alerts without breaking operations.
- You can show management useful reports from clean data.
