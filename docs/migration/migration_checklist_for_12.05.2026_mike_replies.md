# AutoCount 2.0 Migration Checklist - Mike Replies Captured

**Base checklist:** `migration_checklist_for_12.05.2026.md`  
**Reply source:** Mike / Ingenious WhatsApp replies received on 12/05 and 13/05.  
**Purpose:** Add Mike's replies as nested answer pointers to the existing migration checklist without replacing the original checklist questions.

---

## How to use this file

- Keep the original checklist questions unchanged.
- Treat the replies below as current answers from Mike.
- Keep remaining follow-up questions open.
- Do not treat partially answered items as fully closed unless Mike confirms missing details.
- Do not mark tracker tasks as `Done` automatically from this file.
- Use this as a working add-on for Monday's Mike call and the Brendan / finance call.

---

## Overall replies received

### Confirmed by Mike

- Debtor and Creditor Excel templates are used to import existing trade customers and suppliers.
- Debtor / Creditor data can also be migrated from the old AutoCount version.
- Stock Item Opening Excel template is used for opening stock balance.
  - X-Boundaries must fill opening stock quantity and cost for each item according to each location.
- AR Invoice and AP Invoice Excel templates are used for customer and supplier opening balances.
  - Purpose: keep track of outstanding customer and supplier invoices not yet cleared.
- X-Boundaries prepares all Excel data.
- Sample data can be provided first for test import before full migration.
- Chart of Accounts can follow system default or be provided by accountant.
  - Account codes and information need to be keyed in manually by the user.
- Full accounts require bank opening balances and General Ledger (GL) account opening balances.
- Stock in transit can be tracked using Outstanding PO Listing.
  - Path: `PO > PO Listing > Outstanding PO Listing`.
  - Mike said this is available in the old version too.
- Programming references shared by Mike:
  - https://wiki.autocountsoft.com/wiki/Programmer
  - https://wiki.autocountsoft.com/wiki/Integration_Methods

### Still needs confirmation

- Mandatory columns for each Excel template.
- Optional columns for each Excel template.
- Sample completed row for each template.
- Exact import sequence.
- Reversal / correction process after wrong import.
- Whether `Stock Item Opening` is the same as `Import Stock Open Bal`.
- Whether `Import Stock Item` is item master only.
- Whether `Import Stock Item` is reusable after go-live, or only for migration.
- Post-go-live incoming stock workflow:
  - PO.
  - GRN.
  - Stock adjustment.
  - Stock receive.
  - Other.
- Whether Outstanding PO Listing is sufficient for operational stock in transit tracking.
- Whether finance still needs separate accounting treatment for:
  - Supplier prepayments.
  - Goods in transit.
  - Paid-but-not-received stock.
- API / SQL / export method for future automation.
- Whether API Module or extra licence is needed for automation.
- Whether read-only SQL/export access is allowed after go-live.

---

## Replies by checklist section

## 1. Confirm migration scope

### Existing checklist questions - keep unchanged

- Are we migrating from AutoCount 1.0 to AutoCount 2.0 using the 6 Excel templates provided by Ingenious?
- Are these templates the latest / correct ones?
- Are we using stock + accounting module from go-live?
- What exactly will Ingenious handle?
- What exactly must X-Boundaries prepare?
- What must accountant / finance prepare?

### Mike reply notes

- Mike confirmed several Excel templates and their purpose:
  - Debtor and Creditor templates: existing trade customers and suppliers.
  - Stock Item Opening template: opening stock quantity and cost by item and location.
  - AR Invoice and AP Invoice templates: outstanding customer / supplier opening balances.
- Mike confirmed X-Boundaries must prepare all Excel data.
- Mike confirmed sample data can be provided first for testing import before full data migration.
- Mike confirmed accounting setup needs more data if X-Boundaries wants full accounts:
  - Bank opening balances.
  - GL account opening balances.
  - CoA decision: system default or accountant-provided.

### Follow-ups still open

- Confirm whether the provided 6 templates are definitely the latest / correct templates.
- Confirm what Ingenious imports versus what X-Boundaries keys manually.
- Confirm what accountant / finance must sign off before final migration.
- Confirm whether AutoCount 2.0 will be stock + accounting from go-live.

---

## 2. Confirm the 6 templates

### Existing checklist questions - keep unchanged

| Template | Confirmation needed |
|---|---|
| Import Stock Item | Understand that this is for importing stock items. I understand which columns are needed for this import. Will this be used for every instance of stock importing? |
| Import Stock Open Bal | Is this for opening stock by location? Need qty + unit cost? |
| Import Debtor | Is this customer master? Which customers need to be included? |
| Import Creditor | Is this supplier master? Which suppliers need to be included? |
| Import AR Invoice | Is this for customers who still owe us money at go-live? |
| Import AP Invoice | Is this for suppliers we still owe at go-live? |

### Mike reply notes

- Import Debtor:
  - Mike confirmed Debtor Excel template is used to import existing trade customers.
  - Mike said this data can be migrated from the old version as well.
- Import Creditor:
  - Mike confirmed Creditor Excel template is used to import existing trade suppliers.
  - Mike said this data can be migrated from the old version as well.
- Import Stock Open Bal / Stock Item Opening:
  - Mike referred to `Stock Item Opening Excel template`.
  - Mike confirmed it is used for stock opening balance.
  - Mike confirmed X-Boundaries needs opening stock quantity and cost for each item according to each location.
- Import AR Invoice:
  - Mike confirmed AR Invoice template is used for customer opening balances.
  - Purpose is to track outstanding customer invoices not yet cleared.
- Import AP Invoice:
  - Mike confirmed AP Invoice template is used for supplier opening balances.
  - Purpose is to track outstanding supplier invoices not yet cleared.
- Test import:
  - Mike confirmed X-Boundaries may provide sample data first for test import before full migration.

### Follow-ups still open

- Confirm whether `Stock Item Opening Excel template` = `Import Stock Open Bal`.
- Confirm purpose of `Import Stock Item` separately:
  - Is it item master only?
  - Does it create item codes / descriptions / UOM / barcode / category / price?
  - Is it reusable after go-live?
- Confirm compulsory columns for all 6 templates.
- Confirm optional columns for all 6 templates.
- Request sample completed row for each template.
- Confirm reversal / correction method for wrong imports.

---

## 3. Confirm import sequence

### Existing checklist question - keep unchanged

- What is the correct order to import / set up everything?

### Mike reply notes

- Mike has not confirmed the exact import sequence yet.
- Mike's replies imply these areas are required before or during migration:
  - Customers / suppliers via Debtor and Creditor templates.
  - Opening stock quantity and cost by item/location via Stock Item Opening template.
  - Outstanding AR/AP invoices via AR/AP Invoice templates.
  - CoA either system default or accountant-provided.
  - Bank opening balances and GL account opening balances for full accounts.

### Follow-ups still open

- Confirm the real import sequence with Ingenious.
- Do not assume the checklist's possible order is correct.
- Ask whether CoA, locations, item master, debtor master, and creditor master must exist before opening balances are imported.
- Ask where bank opening balances and GL opening balances sit in the sequence.

---

## 4. Confirm master data needed

### Existing checklist questions - keep unchanged

| Data | Confirmation needed |
|---|---|
| Product master | What fields needed? Item code, barcode, brand, category, price, cost, UOM? |
| Opening stock | Need by location? What date? Need unit cost? |
| Customers | Which customers need to be migrated? Retail? Wholesale? Metro? Marketplace? |
| Suppliers | Which suppliers need to be migrated? Local / overseas? |
| Locations | Confirm HQ, XB01, XB02, XB03, XB04. |
| Payment methods | Cash, NETS, credit card, PayNow, GrabPay, AliPay, etc. |
| Price lists | Do we need channel pricing tiers before go-live? |

### Mike reply notes

- Customers:
  - Mike confirmed Debtor template imports existing trade customers.
  - Data can also be migrated from old AutoCount version.
- Suppliers:
  - Mike confirmed Creditor template imports existing trade suppliers.
  - Data can also be migrated from old AutoCount version.
- Opening stock:
  - Mike confirmed opening stock requires quantity and cost for each item according to each location.
- Product master:
  - Mike has not yet confirmed item master fields.
- Locations:
  - Mike has not yet confirmed location codes.
- Payment methods and price lists:
  - Mike has not yet confirmed.

### Follow-ups still open

- Confirm whether retail cash customers are migrated as individual debtors or not.
- Confirm whether marketplace customers are migrated as debtors or treated as channel settlement only.
- Confirm exact fields for product master.
- Confirm exact fields for opening stock.
- Confirm AutoCount location code setup.
- Confirm channel pricing / price list setup before go-live.

---

## 5. Confirm accounting data needed

### Existing checklist questions - keep unchanged

| Accounting item | Question |
|---|---|
| Chart of accounts | Does Ingenious provide a template, or must accountant provide? |
| Opening balance | What format? What date? Who signs off? |
| Opening stock value | Comes from Stock Open Bal or separate GL entry? |
| Outstanding AR | Use AR Invoice template? Only unpaid invoices? |
| Outstanding AP | Use AP Invoice template? Only unpaid supplier invoices? |
| Supplier prepayments | How to load paid-but-not-received stock? |
| Goods in transit | Account, location, or separate workflow? |
| Bank balances | How to set up opening bank / cash balances? |
| GST / FX | What setup needed for GST, import GST, JPY / USD / KRW? |

### Mike reply notes

- Chart of Accounts:
  - Mike confirmed CoA can either follow AutoCount system default or be provided by accountant.
  - Mike said account codes and information need to be keyed in manually by the user.
- Outstanding AR:
  - Mike confirmed AR Invoice template is for customer opening balances.
  - It tracks customer invoices still outstanding and not cleared.
- Outstanding AP:
  - Mike confirmed AP Invoice template is for supplier opening balances.
  - It tracks supplier invoices still outstanding and not cleared.
- Bank balances:
  - Mike confirmed full accounts require bank opening balances.
- GL opening balances:
  - Mike confirmed full accounts require General Ledger account opening balances.
- Opening stock value:
  - Mike confirmed Stock Item Opening requires stock quantity and cost by item/location.
  - Mike has not confirmed whether GL inventory opening value comes automatically from this or still needs a GL entry.
- Supplier prepayments / goods in transit:
  - Mike's operational answer is to use Outstanding PO Listing to track stock in transit.
  - Finance/accounting treatment is still not confirmed.

### Follow-ups still open

- Confirm exact format for bank opening balances.
- Confirm exact format for GL opening balances.
- Confirm who keys CoA manually:
  - X-Boundaries.
  - Accountant.
  - Ingenious.
- Confirm whether GL opening balance import template exists or if it is manual entry.
- Confirm whether opening stock value posts to GL automatically from Stock Item Opening.
- Confirm GST / FX setup for JPY, USD, KRW and import GST.
- Confirm supplier prepayment and goods-in-transit accounting treatment.

---

## 6. Confirm 5 locations

### Existing checklist assumptions - keep unchanged

- These are the correct AutoCount stock locations:
  - HQ.
  - XB01 - MG / Shopify
  - XB02 - JBM
  - XB03 - Wholesale
  - XB04 - Marketplaces

### Existing checklist questions - keep unchanged

- Are these location codes valid in AutoCount?
- Does opening stock need one row per item per location?
- Can Shopify share XB01  stock?

### Mike reply notes

- Mike confirmed opening stock quantity and cost are needed for each item according to each location.
- This supports the assumption that opening stock must be location-level.

### Follow-ups still open

- Confirm exact location codes:
  - `HQ`.
  - `XB01`.
  - `XB02`.
  - `XB03`.
  - `XB04`.
- Confirm whether each item/location combination needs one row.
- Confirm whether Shopify can share XB01 stock.
- Confirm how SiteGiant marketplace stock should map to XB04.

---

## 6B. Confirm SKU identity / primary SKU / SKU change handling

### Existing checklist key question - keep unchanged

- What is the safest product identity structure for us before migration, assuming SKUs may change later?

### Mike reply notes

- Mike has not replied on SKU identity / ItemCode change handling yet.

### Follow-ups still open

- Confirm whether AutoCount ItemCode can be changed after item creation.
- Confirm what happens to historical transactions if ItemCode changes.
- Confirm whether AutoCount ItemCode should be treated as the stable bridge key.
- Confirm whether internal product ID should be stored in:
  - UDF.
  - Alternative item code.
  - Barcode.
  - Description field.
  - External mapping table.
- Confirm duplicate SKU / barcode prevention.
- Confirm whether reports can be run using internal product ID.

---

## 6A. Confirm stock in transit / goods in transit handling

### Existing checklist context - keep unchanged

Sometimes we may have already placed PO / paid supplier / received supplier invoice / received packing list, but the goods have not physically arrived at warehouse yet.

Need to know how AutoCount should handle this during migration and after go-live.

### Existing checklist key question - keep unchanged

- For migration day, what exact list of supplier prepayments / stock in transit / paid-but-not-received goods must X-Boundaries prepare, and who should sign it off?

### Mike reply notes

- Mike first asked what was meant by stock in transit.
- X-Boundaries clarified:
  - Stock purchased as a purchase order.
  - Not all delivered yet.
  - Delivered in batches.
  - Enters HQ location batch by batch.
- Mike replied that Outstanding PO Listing can be used for tracking.
  - Path: `PO > PO Listing > Outstanding PO Listing`.
  - Mike said this is available in the old version too.

### Follow-ups still open

- Confirm whether Outstanding PO Listing is only operational tracking or also sufficient for accounting.
- Confirm how supplier prepayment is recorded if supplier is paid before goods arrive.
- Confirm whether goods in transit should be recorded as:
  - Supplier prepayment.
  - Goods-in-transit asset.
  - Open PO only.
  - AP invoice.
  - Manual tracker.
- Confirm whether PO can be open at cutover and migrated / recreated in AutoCount 2.0.
- Confirm what exact open PO / stock in transit list is needed at cutover.
- Confirm who signs off stock in transit and supplier prepayment position.

---

## 7. Confirm cutover date logic

### Existing checklist questions - keep unchanged

- What date should all opening numbers be based on?
- Is it closing 30 Jun or opening 1 Jul?
- When do we stop using AutoCount 1.0?
- When do we do final stocktake?
- When do we freeze stock movement?
- When does Ingenious import final data?
- What can still change after import?
- What must not change after import?

### Mike reply notes

- Mike has not replied on cutover timing yet.
- Mike confirmed opening stock quantity/cost, AR/AP opening invoices, bank opening balances, and GL opening balances are required inputs for full accounts.

### Follow-ups still open

- Confirm whether balances are based on closing 30 Jun or opening 1 Jul.
- Confirm when old AutoCount stops.
- Confirm stock freeze timing.
- Confirm final import timing.
- Confirm what can be corrected after import.
- Confirm go-live blockers.

---

## 7A. Confirm parallel run / migration rehearsal

### Existing checklist key question - keep unchanged

- What exactly does X-Boundaries need to do during parallel run, and what does Ingenious check before confirming we are ready to go live?

### Mike reply notes

- Mike has not replied on parallel run yet.
- Mike did confirm sample data can be provided first for testing import before full migration.

### Follow-ups still open

- Confirm whether there will be a parallel run.
- Confirm whether transactions are keyed into both AutoCount 1.0 and 2.0 during parallel run.
- Confirm what reports to compare.
- Confirm who checks differences.
- Confirm acceptable variance.
- Confirm go/no-go owner.

---

## 8. Confirm test import

### Existing checklist question - keep unchanged

Can we test import 5-10 sample rows for each template before preparing the full file?

### Mike reply notes

- Mike confirmed sample data can be provided first for testing import purposes before proceeding with full data migration.

### Follow-ups still open

- Confirm which database will be used for test import.
- Confirm whether a sandbox / test database is available.
- Confirm how to check if import succeeded.
- Confirm where import errors show.
- Confirm whether failed rows can be exported.
- Confirm whether wrong test imports can be deleted / reversed.

---

## 9. Confirm what NOT to do

### Existing checklist questions - keep unchanged

- What should X-Boundaries avoid touching before migration?
- Should we avoid creating products manually?
- Should we avoid importing into the real database?
- Should we avoid changing template headers?
- Should we avoid changing item codes after setup?
- Should we avoid changing CoA after opening balance?

### Mike reply notes

- Mike has not replied on what to avoid yet.

### Follow-ups still open

- Ask Mike for explicit `do not touch` rules before migration.
- Especially confirm:
  - Do not change template headers.
  - Do not manually create items if template import is expected.
  - Do not import into production before test import passes.
  - Do not change CoA after opening balances without accountant sign-off.
  - Do not change ItemCode after transaction history starts unless Mike confirms safe method.

---

## 10. Required output table summary

### Existing checklist output table - keep unchanged

| Data item | Template / setup | Mandatory fields | Owner | Deadline | Must be final before go-live? |
|---|---|---|---|---|---|
| Product master | Import Stock Item | Ingenious to confirm | X-Boundaries | TBC | Yes |
| Opening stock | Import Stock Open Bal | Ingenious to confirm | Ops + finance | TBC | Yes |
| Customer master | Import Debtor | Ingenious to confirm | X-Boundaries | TBC | Mostly yes |
| Supplier master | Import Creditor | Ingenious to confirm | X-Boundaries | TBC | Mostly yes |
| AR opening | Import AR Invoice | Ingenious to confirm | Finance / accountant | TBC | Yes |
| AP opening | Import AP Invoice | Ingenious to confirm | Finance / accountant | TBC | Yes |
| CoA | TBC | Ingenious / accountant to confirm | Finance / accountant | TBC | Yes |
| GL opening balance | TBC | Ingenious / accountant to confirm | Finance / accountant | TBC | Yes |
| Locations | AutoCount setup | Ingenious to confirm | X-Boundaries + Mike | TBC | Yes |
| Payment methods | AutoCount setup | Ingenious to confirm | Retail + finance | TBC | Yes |
| SKU identity / internal primary product ID | TBC | Ingenious to confirm | X-Boundaries + Mike | TBC | Yes |
| Stock in transit / supplier prepayment | TBC | Ingenious to confirm | Finance + ops | TBC | Yes |
| Parallel run / migration rehearsal | TBC | Ingenious to confirm | Ingenious + X-Boundaries | TBC | Yes |

### Mike reply notes to add below the table

| Data item | Mike reply captured | Still open |
|---|---|---|
| Customer master | Debtor template imports existing trade customers. Data can also be migrated from old version. | Which customers count as trade customers? Mandatory fields? |
| Supplier master | Creditor template imports existing trade suppliers. Data can also be migrated from old version. | Mandatory fields? Local/overseas supplier fields? |
| Opening stock | Stock Item Opening template requires opening quantity and cost by item/location. | Confirm relationship to Import Stock Open Bal. Confirm mandatory columns. |
| AR opening | AR Invoice template tracks outstanding customer invoices not yet cleared. | Confirm only unpaid invoices and required fields. |
| AP opening | AP Invoice template tracks outstanding supplier invoices not yet cleared. | Confirm only unpaid invoices and required fields. |
| CoA | Use system default or accountant-provided CoA. Account codes/info keyed manually by user. | Who keys it? When? Can it be changed after opening balance? |
| Bank balances | Full accounts require bank opening balances. | Format, sign-off owner, cutover date. |
| GL opening balance | Full accounts require GL account opening balances. | Format, entry method, sign-off owner. |
| Stock in transit / supplier prepayment | Outstanding PO Listing can track stock in transit: `PO > PO Listing > Outstanding PO Listing`. | Confirm accounting treatment and cutover list. |
| Test import | Sample data can be provided first for test import. | Confirm sandbox database and rollback process. |
| API / automation | Mike shared AutoCount Programmer and Integration Methods wiki links. | Confirm approved integration method, API licence, SQL/export access. |

---

## Internal interpretation for Brendan / finance call

Mike's replies mean Brendan / finance / accountant likely need to prepare or confirm:

- Chart of Accounts decision:
  - AutoCount system default.
  - Accountant-provided CoA.
- Bank opening balances.
- GL account opening balances.
- AR opening outstanding invoices.
- AP opening outstanding invoices.
- Opening stock quantity and cost by item/location.
- Open PO / stock in transit / supplier prepayment position as of cutover.
- Whether paid-but-not-received goods sit in:
  - Outstanding PO only.
  - Supplier prepayment.
  - Goods-in-transit asset.
  - AP invoice.
  - Manual tracker.
- Cutover date basis:
  - Closing 30 Jun.
  - Opening 1 Jul.

Practical note: do not let this become ERP theory. The call output should be a table of data owner, source file, deadline, and sign-off owner.

---

## Immediate internal action for Brendan / finance call

Bring this list and fill owners / source files:

| Item | Needed from finance/accountant | Suggested owner | Status |
|---|---|---|---|
| CoA decision | Use default or accountant CoA | Brendan / accountant | Open |
| Bank opening balances | Bank/cash balances as of cutover | Brendan / accountant | Open |
| GL opening balances | Trial balance / GL opening by account | Brendan / accountant | Open |
| AR opening | Unpaid customer invoices at cutover | Brendan / finance | Open |
| AP opening | Unpaid supplier invoices at cutover | Brendan / finance | Open |
| Opening stock value | Qty and cost by item/location | Ops + finance | Open |
| Stock in transit | Open PO / paid-not-received / partial deliveries | Ops + finance | Open |
| Supplier prepayments | Deposits/prepayments not cleared by GRN/invoice | Brendan / finance | Open |
| GST / FX | GST, import GST, JPY/USD/KRW setup | Brendan / accountant | Open |

---

## Immediate Monday Mike call focus

Ask Mike only what blocks migration and go-live. Keep API/developer questions short unless needed for near-term export/read-only automation.

### Must ask Mike

1. Mandatory fields for each Excel template.
2. Exact import sequence.
3. Correction / reversal rules for wrong imports.
4. Whether `Stock Item Opening` is the same as `Import Stock Open Bal`.
5. Whether `Import Stock Item` is item master only.
6. Whether `Import Stock Item` is reusable after go-live.
7. Exact post-go-live incoming stock workflow:
   - PO.
   - GRN.
   - Stock Receive.
   - Stock Adjustment.
   - Other.
8. Stock in transit accounting treatment:
   - Outstanding PO only?
   - Supplier prepayment?
   - Goods in transit?
   - AP invoice?
9. Test import database / sandbox process.
10. Import error checking and rollback process.
11. API / export / SQL availability for future automation.
12. Whether API Module or other licence is needed for automation.

### Do not over-focus yet

- Do not design full AI agents before migration is stable.
- Do not build write-back automation before export/read-only access is confirmed.
- Do not build stock upload generator until Mike confirms the correct stock quantity workflow and import method.

---

## Candidate decision log entries

These are examples to copy into `tracker/decision_log.csv` when that file exists. Do not treat these as final accountant decisions yet.

| Date | DecisionID | Area | Decision | Source | Owner | Status | FollowUp |
|---|---|---|---|---|---|---|---|
| 2026-05-12 | D001 | Migration templates | Debtor/Creditor templates are for existing trade customers/suppliers. | Mike WhatsApp | Wei Jun | Confirmed by Mike | Confirm mandatory fields. |
| 2026-05-12 | D002 | Opening stock | Stock Item Opening template requires opening qty and cost by item/location. | Mike WhatsApp | Wei Jun | Confirmed by Mike | Confirm same as Import Stock Open Bal. |
| 2026-05-12 | D003 | AR/AP opening | AR/AP Invoice templates are for outstanding customer/supplier opening balances. | Mike WhatsApp | Wei Jun | Confirmed by Mike | Confirm required fields and cutover date. |
| 2026-05-12 | D004 | Data ownership | X-Boundaries prepares all Excel data. | Mike WhatsApp | Wei Jun | Confirmed by Mike | Assign internal owners. |
| 2026-05-12 | D005 | Test import | X-Boundaries can provide sample data first for test import before full migration. | Mike WhatsApp | Wei Jun | Confirmed by Mike | Confirm sandbox and rollback. |
| 2026-05-12 | D006 | CoA | CoA can use system default or accountant-provided CoA; account codes/info keyed manually by user. | Mike WhatsApp | Brendan / accountant | Needs internal decision | Decide CoA owner and source. |
| 2026-05-12 | D007 | Full accounts | Full accounts require bank opening balances and GL account opening balances. | Mike WhatsApp | Brendan / accountant | Confirmed by Mike | Prepare balances and sign-off. |
| 2026-05-13 | D008 | Stock in transit | Outstanding PO Listing can be used to track stock in transit. | Mike WhatsApp | Ops + finance | Partially confirmed | Confirm accounting treatment. |
| 2026-05-12 | D009 | Automation reference | Mike shared AutoCount Programmer and Integration Methods wiki links. | Mike WhatsApp | Wei Jun | Reference only | Confirm approved integration method/licence. |

---

## Candidate daily log examples

These are examples for `tracker/daily_log.csv`. Do not mark tasks done automatically from these notes.

```csv
Date,TaskID,WhatIDid,Evidence,TimeSpentMinutes,Confidence,SuggestedStatus,Notes
2026-05-13,TBC,"Captured Mike replies on Debtor/Creditor, Stock Item Opening, AR/AP Invoice templates, CoA, bank/GL balances, and Outstanding PO Listing.","WhatsApp replies 12/05-13/05",30,High,"In Progress","Use these as answer notes under migration checklist; mandatory fields/import sequence still open."
2026-05-13,TBC,"Prepared finance follow-up list for CoA, bank opening, GL opening, AR/AP opening, stock value, stock in transit, and supplier prepayments.","Migration checklist add-on",20,Medium,"In Progress","Needs Brendan/accountant confirmation; do not mark migration prep done."
```

---

## Bottom line

Mike has answered enough to narrow the migration prep work, but not enough to close it.

The next useful move is not more planning. It is getting Mike to confirm mandatory fields, import sequence, test import/rollback, and the accounting treatment for stock in transit / supplier prepayments.