# Daily Action Plan

**Date:** 2026-05-18 SGT

> ⚠️ **Reminder:** The agent does not auto-mark tasks as Done. Update the tracker manually when work is confirmed and evidence is provided.

- [X-Boundaries Automation Repo](https://github.com/x-boundaries/automation)
- [Main Dashboard](https://github.com/x-boundaries/automation/blob/main/dashboard/README.md)

## 🏆 Today's Top 5 Tasks
1. **[T003] Confirm 6 migration templates** (Priority: High, Effort: 2)
   - **Next Action:** Ask Mike for compulsory columns, optional columns, sample completed rows, import sequence, and rollback/correction process.
   - **Goal:** Confirm purpose, mandatory fields, import order, and reversibility for stock, stock open balance, debtor, creditor, AR, and AP templates.

2. **[T002] Confirm AutoCount 2.0 module scope** (Priority: High, Effort: 2)
   - **Next Action:** Confirm exact modules live at go-live and what Ingenious configures vs X-Boundaries/accountant prepares.
   - **Goal:** Confirm stock, accounting, POS, bank recon, AP/AR, price master, multi-location, and import capabilities.

3. **[T006] Prepare Brendan finance data-source call** (Priority: High, Effort: 3)
   - **Next Action:** Build Brendan/accountant source-owner table and confirm sign-off owner for each opening balance.
   - **Goal:** Map Mike's required accounting data to Brendan/finance/accountant sources and owners.

4. **[T068] Investigate AutoCount export/API/SQL access** (Priority: High, Effort: 3)
   - **Next Action:** Ask Mike what integration/export method is allowed for X-Boundaries and whether API Module/licence is needed.
   - **Goal:** Confirm allowed ways to extract data from AutoCount 2.0: export, SQL, API, or scheduled reports.

5. **[T012] Confirm POS edge case handling** (Priority: High, Effort: 2)
   - **Goal:** Ask Ingenious how refunds, exchanges, mall vouchers, store credits, voids, partial refunds, and split payments are handled.

## ⚡ Quick Wins (Effort 1)
*No quick wins identified.*

## 🛑 Blocked & Waiting Tasks
- **[T007] Prepare stock master list** (Waiting) - Blocked by: T003
- **[T010] Prepare Chart of Accounts draft** (Waiting) - Blocked by: Brendan/accountant CoA decision
- **[T013] Confirm opening accounting balances** (Waiting) - Blocked by: Brendan/accountant + Mike accounting treatment confirmation
- **[T020] Build stock upload exception report** (Waiting) - Blocked by: T019
- **[T022] Create export archive structure** (Waiting) - Blocked by: T068
- **[T025] Create goods-in-transit tracker** (Waiting) - Blocked by: T013
- **[T071] Create bank reconciliation dashboard task** (Waiting) - Blocked by: T068
- **[T071] Build bank reconciliation download comparator** (Waiting) - Blocked by: T068
- **[T072] Create 30 Jun cutover balance gate** (Waiting) - Blocked by: T003,T013
- **[T074] Define 5-day parallel run plan** (Waiting) - Blocked by: T002
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