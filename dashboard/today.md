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

3. **[T073] Verify receipt printer Windows 11 support** (Priority: High, Effort: 2)
   - **Next Action:** Get current printer model from store/server area, check Windows 11 driver availability, and record go/no-go result.
   - **Goal:** Check current shop receipt printer model, specs, Windows 11 driver support, and whether replacement is needed before AutoCount 2.0/POS rollout.

4. **[T075] Confirm SiteGiant to Shopify mapping reliability** (Priority: High, Effort: 2)
   - **Next Action:** Ask Mike for SiteGiant-Shopify field mapping approach, mismatch handling, and whether a sample sync/export can be tested.
   - **Goal:** Get Mike/SiteGiant to confirm how SiteGiant data maps reliably into Shopify despite different input fields, including mandatory fields, fallback fields, and exception handling.

5. **[T007] Prepare stock master list** (Priority: High, Effort: 3)
   - **Next Action:** Prepare real cleaned stock master list using the completed master legend structure after Mike confirms required migration fields.
   - **Goal:** Prepare item/product master data for migration and vendor review.

## ⚡ Quick Wins (Effort 1)
*No quick wins identified.*

## 🛑 Blocked & Waiting Tasks
- **[T013] Confirm opening accounting balances** (Waiting) - Blocked by: Brendan/accountant + Mike accounting treatment confirmation
- **[T022] Create export archive structure** (Waiting) - Blocked by: T068
- **[T025] Create goods-in-transit tracker** (Waiting) - Blocked by: T013
- **[T071] Create bank reconciliation dashboard task** (Waiting) - Blocked by: T068
- **[T071] Build bank reconciliation download comparator** (Waiting) - Blocked by: T068
- **[T072] Create 30 Jun cutover balance gate** (Waiting) - Blocked by: T013
## 🔎 Tasks Needing Status Review
- **[T014] Create master legend concept** - Suggested: *2026-05-18*
- **[T015] Create SKU history approach** - Suggested: *2026-05-18*
- **[T016] Create primary product ID concept** - Suggested: *2026-05-18*
- **[T017] Create new stock input template** - Suggested: *2026-05-18*
- **[T018] Create AutoCount stock item copy-paste template** - Suggested: *2026-05-18*
- **[T019] Build stock upload generator** - Suggested: *2026-05-18*

## 📝 Recent Daily Log Entries
- **2026-05-18 [T014]** - Completed live stock master legend workbook with README, Product_Master, SKU_History, and Lists tabs.
- **2026-05-18 [T017]** - Completed live stock input workbook for staff incoming-stock entry.
- **2026-05-18 [T019]** - Completed stock upload preparation MVP: input rows match against the master legend, show OK / NOT FOUND / DUPLICATE, and prepare AC2-ready output columns.
- **2026-05-18 [T070]** - Updated dashboard generator to use SGT timestamps and added duplicate-check output to the generated dashboard.
- **2026-05-14 [T019]** - Documented internal stock import automation flow, field mapping, validation rules, LeadTime uncertainty, repeat-use questions, and production guardrails.