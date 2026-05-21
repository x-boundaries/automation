# Daily Action Plan

**Date:** 2026-05-22 SGT

> ⚠️ **Reminder:** The agent does not auto-mark tasks as Done. Update the tracker manually when work is confirmed and evidence is provided.

- [X-Boundaries Automation Repo](https://github.com/x-boundaries/automation)
- [Main Dashboard](https://github.com/x-boundaries/automation/blob/main/dashboard/README.md)

## 🏆 Today's Top 5 Tasks
1. **[T068] Investigate AutoCount export/API/SQL access** (Priority: High, Effort: 3)
   - **Next Action:** Investigate AutoCount export/API/SQL access options directly.
   - **Goal:** Confirm allowed ways to extract data from AutoCount 2.0: export, SQL, API, or scheduled reports.

2. **[T012] Confirm POS edge case handling** (Priority: High, Effort: 2)
   - **Goal:** Ask Ingenious how refunds, exchanges, mall vouchers, store credits, voids, partial refunds, and split payments are handled.

3. **[T075] Confirm SiteGiant to Shopify mapping reliability** (Priority: High, Effort: 2)
   - **Next Action:** Ask Vendor Ingenious/Mike for SiteGiant-Shopify field mapping approach, mismatch handling, and whether a sample sync/export can be tested.
   - **Goal:** Get Vendor Ingenious/Mike/SiteGiant to confirm how SiteGiant data maps reliably into Shopify despite different input fields, including mandatory fields, fallback fields, and exception handling.

4. **[T007] Prepare stock master list** (Priority: High, Effort: 3)
   - **Next Action:** Pending MD Kar Han to clean master list data
   - **Goal:** Prepare item/product master data for migration and vendor review.

5. **[T008] Prepare customer/debtor master list** (Priority: High, Effort: 3)
   - **Next Action:** Identify trade customers to migrate and prepare 5-10 sample Debtor rows for test import.
   - **Goal:** Prepare customer/debtor data for migration, especially wholesale, Metro, corporate, and Gebiz customers.

## ⚡ Quick Wins (Effort 1)
*No quick wins identified.*

## 🛑 Blocked & Waiting Tasks
- **[T013] Confirm opening accounting balances** (Waiting) - Blocked by: Brendan/accountant + Vendor Ingenious/Mike accounting treatment confirmation
- **[T022] Create export archive structure** (Waiting) - Blocked by: T068
- **[T025] Create goods-in-transit tracker** (Waiting) - Blocked by: T013
- **[T071] Create bank reconciliation dashboard task** (Waiting) - Blocked by: T068
- **[T071] Build bank reconciliation download comparator** (Waiting) - Blocked by: T068
- **[T072] Create 30 Jun cutover balance gate** (Waiting) - Blocked by: T013

## 📝 Recent Daily Log Entries
- **2026-05-22 [T073]** - Verified receipt printer Windows 11 support. Current printer model is supported.
- **2026-05-22 [T007]** - Updated next action: Pending MD Kar Han to clean master list data.
- **2026-05-18 [T014]** - Completed live stock master legend workbook with README, Product_Master, SKU_History, and Lists tabs.
- **2026-05-18 [T017]** - Completed live stock input workbook for staff incoming-stock entry.
- **2026-05-18 [T019]** - Completed stock upload preparation MVP: input rows match against the master legend, show OK / NOT FOUND / DUPLICATE, and prepare AC2-ready output columns.