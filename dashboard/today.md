# Daily Action Plan

**Date:** 2026-05-23 SGT

> ⚠️ **Reminder:** The agent does not auto-mark tasks as Done. Update the tracker manually when work is confirmed and evidence is provided.

- [X-Boundaries Automation Repo](https://github.com/x-boundaries/automation)
- [Main Dashboard](https://github.com/x-boundaries/automation/blob/main/dashboard/README.md)

## 🏆 Today's Top 10 Tasks
1. **[T068] Investigate AutoCount export/API/SQL access** (Category: Automation & Integrations, Priority: High, Effort: 3)
   - **Next Action:** Investigate AutoCount export/API/SQL access options directly.
   - **Goal:** Confirm allowed ways to extract data from AutoCount 2.0: export, SQL, API, or scheduled reports.

2. **[T012] Confirm POS edge case handling** (Category: AutoCount ERP & Migration, Priority: High, Effort: 2)
   - **Goal:** Ask Ingenious how refunds, exchanges, mall vouchers, store credits, voids, partial refunds, and split payments are handled.

3. **[T075] Confirm SiteGiant to Shopify mapping reliability** (Category: Automation & Integrations, Priority: High, Effort: 2)
   - **Next Action:** Ask Vendor Ingenious/Mike for SiteGiant-Shopify field mapping approach, mismatch handling, and whether a sample sync/export can be tested.
   - **Goal:** Get Vendor Ingenious/Mike/SiteGiant to confirm how SiteGiant data maps reliably into Shopify despite different input fields, including mandatory fields, fallback fields, and exception handling.

4. **[T024] Shipment upload to PO/product update workflow** (Category: Automation & Integrations, Priority: High, Effort: 3)
   - **Goal:** Take shipment details and prepare process for PO creation, new product creation, existing stock update, and price-change flag.

5. **[T067] Define read-only automation rule** (Category: Automation & Integrations, Priority: High, Effort: 3)
   - **Next Action:** Document phase 1 automations as export/compare/alert only; no write-back until process stable.
   - **Goal:** Document that phase 1 automations collect/export/compare/alert only; no direct write-back until process is stable.

6. **[T074] Define 5-day parallel run plan** (Category: AutoCount ERP & Migration, Priority: High, Effort: 3)
   - **Next Action:** Draft 5-day parallel checklist covering POS, stock receiving, stock transfer, AR/AP, payments, refunds, and daily comparison reports.
   - **Goal:** Turn Vendor Ingenious/Mike's suggestion into an exact Monday-Friday parallel run plan: what gets keyed into both systems, what reports are compared, who checks variances, and what blocks go-live.

7. **[T023] Create product master cleanup checker** (Category: Automation & Integrations, Priority: High, Effort: 5)
   - **Goal:** Check missing brand, category, barcode, UOM, cost, price, duplicate SKU, and inactive items.

8. **[T027] Create warehouse GRN scan/discrepancy tracker** (Category: Operations & SOPs, Priority: High, Effort: 5)
   - **Goal:** Compare scanned goods received at warehouse against PO and support rectify now / rectify later flow.

9. **[T028] Create goods disbursement workflow by channel** (Category: Operations & SOPs, Priority: High, Effort: 5)
   - **Goal:** Use goods received data to disburse stock to MG, Online, Warehouse, JBM, or other locations.

10. **[T039] Create daily POS/payment reconciliation MVP** (Category: Automation & Integrations, Priority: High, Effort: 5)
   - **Goal:** Compare POS sales by payment method against bank/card/payment reports and flag exceptions.

## ⚡ Quick Wins (Effort 1)
*No quick wins identified.*

## 🛑 Blocked & Waiting Tasks
- **[T007] Prepare stock master list** (Category: AutoCount ERP & Migration, Waiting) - Blocked by: MD Kar Han
- **[T008] Prepare customer/debtor master list** (Category: AutoCount ERP & Migration, Waiting) - Blocked by: Brendan
- **[T009] Prepare supplier/creditor master list** (Category: AutoCount ERP & Migration, Waiting) - Blocked by: Brendan
- **[T010] Prepare Chart of Accounts draft** (Category: AutoCount ERP & Migration, Waiting) - Blocked by: Brendan/accountant
- **[T013] Confirm opening accounting balances** (Category: AutoCount ERP & Migration, Waiting) - Blocked by: Brendan/accountant + Vendor Ingenious/Mike accounting treatment confirmation
- **[T020] Build stock upload exception report** (Category: Automation & Integrations, Waiting) - Blocked by: Vendor Ingenious/Mike
- **[T021] Create data dictionary** (Category: Automation & Integrations, Waiting) - Blocked by: Vendor Ingenious/Mike
- **[T022] Create export archive structure** (Category: Automation & Integrations, Waiting) - Blocked by: T068
- **[T025] Create goods-in-transit tracker** (Category: Operations & SOPs, Waiting) - Blocked by: T013
- **[T051] Create basic sales/channel dashboard** (Category: Analytics & Dashboards, Waiting) - Blocked by: Data exports reliability
- **[T062] Create backup success / restore check alert** (Category: AutoCount ERP & Migration, Waiting) - Blocked by: Go-live
- **[T076] Create bank reconciliation dashboard task** (Category: Analytics & Dashboards, Waiting) - Blocked by: T068
- **[T071] Build bank reconciliation download comparator** (Category: Automation & Integrations, Waiting) - Blocked by: T068
- **[T072] Create 30 Jun cutover balance gate** (Category: AutoCount ERP & Migration, Waiting) - Blocked by: T013

## 📝 Recent Daily Log Entries
- **2026-05-22 [T073]** - Verified receipt printer Windows 11 support. Current printer model is supported.
- **2026-05-22 [T007]** - Updated next action: Pending MD Kar Han to clean master list data.
- **2026-05-22 [T008]** - Prepared customer/debtor master list draft and passed to Brendan for checking.
- **2026-05-22 [T009]** - Prepared supplier/creditor master list draft and passed to Brendan for checking.
- **2026-05-22 [T010]** - Updated ReadyStatus to Waiting, blocked by Brendan/accountant.