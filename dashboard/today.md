# Daily Action Plan

**Date:** 2026-06-06 SGT

> ⚠️ **Reminder:** The agent does not auto-mark tasks as Done. Update the tracker manually when work is confirmed and evidence is provided.

- [X-Boundaries Automation Repo](https://github.com/x-boundaries/automation)
- [Main Dashboard](https://github.com/x-boundaries/automation/blob/main/dashboard/README.md)

## 🏆 Today's Top 10 Tasks

### Automation & Integrations
- **[T075] Confirm SiteGiant to Shopify mapping reliability** (Priority: High, Effort: 2)
  - **Next Action:** Ask Vendor Ingenious/Mike for SiteGiant-Shopify field mapping approach, mismatch handling, and whether a sample sync/export can be tested.
  - **Goal:** Get Vendor Ingenious/Mike/SiteGiant to confirm how SiteGiant data maps reliably into Shopify despite different input fields, including mandatory fields, fallback fields, and exception handling.

- **[T024] Shipment upload to PO/product update workflow** (Priority: High, Effort: 3)
  - **Goal:** Take shipment details and prepare process for PO creation, new product creation, existing stock update, and price-change flag.

- **[T067] Define read-only automation rule** (Priority: High, Effort: 3)
  - **Next Action:** Document phase 1 automations as export/compare/alert only; no write-back until process stable.
  - **Goal:** Document that phase 1 automations collect/export/compare/alert only; no direct write-back until process is stable.

- **[T023] Create product master cleanup checker** (Priority: High, Effort: 5)
  - **Goal:** Check missing brand, category, barcode, UOM, cost, price, duplicate SKU, and inactive items.

- **[T039] Create daily POS/payment reconciliation MVP** (Priority: High, Effort: 5)
  - **Goal:** Compare POS sales by payment method against bank/card/payment reports and flag exceptions.

- **[T070] Generate GitHub dashboard from tracker** (Priority: Medium, Effort: 3)
  - **Goal:** Use tracker/work_tracker.csv and source_coverage.csv to build dashboard/README.md automatically.


### Operations & SOPs
- **[T027] Create warehouse GRN scan/discrepancy tracker** (Priority: High, Effort: 5)
  - **Goal:** Compare scanned goods received at warehouse against PO and support rectify now / rectify later flow.

- **[T028] Create goods disbursement workflow by channel** (Priority: High, Effort: 5)
  - **Goal:** Use goods received data to disburse stock to MG, Online, Warehouse, JBM, or other locations.

- **[T045] Confirm KrisShop workflow** (Priority: Medium, Effort: 2)
  - **Goal:** Confirm SiteGiant compatibility or create manual import/export workaround.

- **[T059] Create weekly achievement update habit** (Priority: Medium, Effort: 5)
  - **Goal:** Update this tracker weekly with status, outputs, and proof of work.

## ⚡ Quick Wins (Effort 1)
*No quick wins identified.*

## 🛑 Blocked & Waiting Tasks
- **[T020] Build stock upload exception report** (Category: Automation & Integrations, Waiting) - Blocked by: Vendor Ingenious/Mike
- **[T021] Create data dictionary** (Category: Automation & Integrations, Waiting) - Blocked by: Vendor Ingenious/Mike
- **[T022] Create export archive structure** (Category: Automation & Integrations, Waiting) - Blocked by: T077
- **[T025] Create goods-in-transit tracker** (Category: Operations & SOPs, Waiting) - Blocked by: T077
- **[T051] Create basic sales/channel dashboard** (Category: Analytics & Dashboards, Waiting) - Blocked by: Data exports reliability
- **[T077] AutoCount ERP migration readiness and cutover control** (Category: AutoCount ERP & Migration, Waiting) - Blocked by: Vendor Ingenious/Mike; Brendan/accountant; MD Kar Han; AutoCount export/API/SQL/licence confirmation
- **[T071] Build bank reconciliation download comparator** (Category: Automation & Integrations, Waiting) - Blocked by: T077

## 📝 Recent Daily Log Entries
- **2026-05-22 [T073]** - Verified receipt printer Windows 11 support. Current printer model is supported.
- **2026-05-22 [T007]** - Updated next action: Pending MD Kar Han to clean master list data.
- **2026-05-22 [T008]** - Prepared customer/debtor master list draft and passed to Brendan for checking.
- **2026-05-22 [T009]** - Prepared supplier/creditor master list draft and passed to Brendan for checking.
- **2026-05-22 [T010]** - Updated ReadyStatus to Waiting, blocked by Brendan/accountant.