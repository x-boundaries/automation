# Daily Action Plan

**Date:** 2026-05-18

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

4. **[T016] Create primary product ID concept** (Priority: High, Effort: 5)
   - **Next Action:** Ask Mike whether AutoCount 2.0 ItemCode can be changed safely after item creation; if not, rely on SKU_History for SKU changes.
   - **Goal:** Define InternalProductID / PrimarySKU / AutoCountItemCode roles so SKU changes do not break reports.

5. **[T012] Confirm POS edge case handling** (Priority: High, Effort: 2)
   - **Goal:** Ask Ingenious how refunds, exchanges, mall vouchers, store credits, voids, partial refunds, and split payments are handled.

## ⚡ Quick Wins (Effort 1)
*No quick wins identified.*

## 🛑 Blocked & Waiting Tasks
- **[T007] Prepare stock master list** (Waiting) - Blocked by: T003
- **[T010] Prepare Chart of Accounts draft** (Waiting) - Blocked by: Brendan/accountant CoA decision
- **[T013] Confirm opening accounting balances** (Waiting) - Blocked by: Brendan/accountant + Mike accounting treatment confirmation
- **[T019] Build stock upload generator** (Waiting) - Blocked by: T003,T018,T016
- **[T020] Build stock upload exception report** (Waiting) - Blocked by: T019
- **[T022] Create export archive structure** (Waiting) - Blocked by: T068
- **[T025] Create goods-in-transit tracker** (Waiting) - Blocked by: T013
## 🔎 Tasks Needing Status Review
- **[T016] Create primary product ID concept** - Suggested: *Ready for Mike confirmation*

## 📝 Recent Daily Log Entries
- **2026-05-14 [T019]** - Documented internal stock import automation flow, field mapping, validation rules, LeadTime uncertainty, repeat-use questions, and production guardrails.
- **2026-05-14 [T016]** - Confirmed internal SKU identity direction: PrimarySKU will be shared across platforms and imported into AutoCount; InternalProductID remains internal/outside AutoCount; SKU_History handles future SKU changes.
- **2026-05-13 [T003]** - Captured Mike replies on Debtor/Creditor, Stock Item Opening, AR/AP Invoice templates, CoA, bank/GL balances, sample test import, and Outstanding PO Listing.