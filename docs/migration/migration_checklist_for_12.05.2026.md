# AutoCount 2.0 Migration Preparation Checklist

**Target:** Monday Mike Call (12.05.2026)

## 1. Confirm migration scope

Are we migrating from AutoCount 1.0 to AutoCount 2.0 using the 6 Excel templates provided by Ingenious?

- Mike reply captured:
  - Mike confirmed X-Boundaries prepares all Excel data.
  - Mike confirmed sample data can be provided first for test import.
  - Mike confirmed several migration template purposes:
    - Debtor/Creditor: existing trade customers/suppliers.
    - Stock Item Opening: opening stock quantity/cost by item/location.
    - AR/AP Invoice: outstanding customer/supplier opening balances.
  - Mike confirmed accounting setup needs:
    - CoA decision.
    - Bank opening balances.
    - GL opening balances.

Follow-up still open:
- Confirm whether the provided 6 templates are latest/correct.
- Confirm exactly what Ingenious imports/configures.
- Confirm exactly what X-Boundaries prepares.
- Confirm exactly what accountant/finance signs off.
- Confirm whether stock + accounting modules go live together.

## 2. Confirm the 6 templates

| Template | Confirmation needed |
|---|---|
| Import Stock Item | Understand that this is for importing stock items. I understand which columns are needed for this import. Will this be used for every instance of stock importing? |
| Import Stock Open Bal | Is this for opening stock by location? Need qty + unit cost? |
| Import Debtor | Is this customer master? Which customers need to be included? |
| Import Creditor | Is this supplier master? Which suppliers need to be included? |
| Import AR Invoice | Is this for customers who still owe us money at go-live? |
| Import AP Invoice | Is this for suppliers we still owe at go-live? |

- Mike reply captured:
  - Import Debtor:
    - Confirmed: imports existing trade customers.
    - Data can be migrated from old AutoCount version.
  - Import Creditor:
    - Confirmed: imports existing trade suppliers.
    - Data can be migrated from old AutoCount version.
  - Import Stock Open Bal / Stock Item Opening:
    - Mike referred to `Stock Item Opening Excel template`.
    - Confirmed: used for stock opening balance.
    - Requires opening stock quantity and cost for each item according to each location.
  - Import AR Invoice:
    - Confirmed: used for customer opening balances.
    - Tracks outstanding customer invoices not yet cleared.
  - Import AP Invoice:
    - Confirmed: used for supplier opening balances.
    - Tracks outstanding supplier invoices not yet cleared.
  - Test import:
    - Confirmed: sample data can be provided first before full migration.

Follow-up still open:
- Confirm whether `Stock Item Opening` = `Import Stock Open Bal`.
- Confirm what `Import Stock Item` is for:
  - Item master only?
  - Used after go-live?
  - Used for all future stock importing?
- Confirm compulsory columns.
- Confirm optional columns.
- Request sample completed rows.
- Confirm wrong import correction/reversal process.

## 3. Confirm import sequence

What is the correct order to import / set up everything?

- Mike reply captured:
  - Mike has not confirmed exact import sequence yet.
  - His replies imply these areas are required:
    - Customers/suppliers via Debtor/Creditor.
    - Opening stock qty/cost via Stock Item Opening.
    - Outstanding AR/AP via AR/AP Invoice.
    - CoA setup.
    - Bank opening balances.
    - GL opening balances.

Follow-up still open:
- Confirm real import order with Ingenious.
- Do not assume our proposed import order is correct.
- Confirm whether CoA, locations, stock item master, debtor master, and creditor master must exist before opening balances.
- Confirm when bank/GL opening balances are keyed/imported.

## 4. Confirm master data needed

| Data | Confirmation needed |
|---|---|
| Product master | What fields needed? Item code, barcode, brand, category, price, cost, UOM? |
| Opening stock | Need by location? What date? Need unit cost? |
| Customers | Which customers need to be migrated? Retail? Wholesale? Metro? Marketplace? |
| Suppliers | Which suppliers need to be migrated? Local / overseas? |
| Locations | Confirm HQ, XB01, XB02, XB03, XB04. |
| Payment methods | Cash, NETS, credit card, PayNow, GrabPay, AliPay, etc. |
| Price lists | Do we need channel pricing tiers before go-live? |

- Mike reply captured:
  - Customers:
    - Debtor template imports existing trade customers.
    - Can be migrated from old AutoCount version.
  - Suppliers:
    - Creditor template imports existing trade suppliers.
    - Can be migrated from old AutoCount version.
  - Opening stock:
    - Requires quantity and cost by item/location.
  - Product master:
    - Not yet clarified.
  - Locations:
    - Not yet clarified.
  - Payment methods / price lists:
    - Not yet clarified.

Follow-up still open:
- Which customers count as trade customers?
- Are retail cash customers migrated?
- Are marketplace customers migrated?
- Exact product master fields.
- Exact location code setup.
- Whether Shopify can share XB01 stock.
- Price tier/channel pricing setup.

## 5. Confirm accounting data needed

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

- Mike reply captured:
  - Chart of Accounts:
    - CoA can follow system default or accountant-provided CoA.
    - Account codes/info must be keyed manually by user.
  - AR opening:
    - AR Invoice template tracks outstanding customer invoices not yet cleared.
  - AP opening:
    - AP Invoice template tracks outstanding supplier invoices not yet cleared.
  - Bank balances:
    - Full accounts require bank opening balances.
  - GL opening balances:
    - Full accounts require GL account opening balances.
  - Opening stock value:
    - Stock Item Opening requires quantity and cost by item/location.
    - Still need to confirm whether this posts to GL inventory automatically or needs separate GL entry.
  - Supplier prepayments / goods in transit:
    - Mike gave operational answer: use Outstanding PO Listing.
    - Accounting treatment is still not confirmed.

Follow-up still open:
- Format for bank opening balances.
- Format for GL opening balances.
- Who keys CoA.
- Whether GL opening balance import template exists.
- Whether opening stock value posts to GL automatically.
- GST/FX setup.
- Supplier prepayment and goods-in-transit accounting treatment.

## 6. Confirm 5 locations

Are these location codes valid in AutoCount?
Does opening stock need one row per item per location?
Can Shopify share XB01 stock?

- Mike reply captured:
  - Mike confirmed opening stock qty/cost is needed by item and location.
  - This supports location-level opening stock.

Follow-up still open:
- Confirm exact AutoCount location codes:
  - HQ
  - XB01
  - XB02
  - XB03
  - XB04
- Confirm whether each item/location combination needs one row.
- Confirm Shopify shared stock with XB01.
- Confirm SiteGiant marketplace stock maps to XB04.

## 6B. Confirm SKU identity / primary SKU / SKU change handling

What is the safest product identity structure for us before migration, assuming SKUs may change later?

- Mike reply captured:
  - Mike has not replied on SKU identity / ItemCode change behaviour yet.

Follow-up still open:
- Can AutoCount ItemCode be changed after creation?
- What happens to historical transactions?
- Should AutoCount ItemCode be treated as stable bridge key?
- Where should internal product ID live?
- Can AutoCount store multiple identifiers?
- Can duplicate SKU/barcode be prevented?
- Can reports run by internal product ID?

## 6A. Confirm stock in transit / goods in transit handling

For migration day, what exact list of supplier prepayments / stock in transit / paid-but-not-received goods must X-Boundaries prepare, and who should sign it off?

- Mike reply captured:
  - Mike first asked what stock in transit means.
  - X-Boundaries clarified:
    - Stock purchased as PO.
    - Not all delivered yet.
    - Delivery in batches.
    - Enters HQ location batch by batch.
  - Mike replied:
    - Use Outstanding PO Listing for tracking.
    - Path: `PO > PO Listing > Outstanding PO Listing`.
    - Available in old version too.

Follow-up still open:
- Is Outstanding PO Listing only operational tracking, or also enough for accounting?
- If supplier paid before goods arrive, is it supplier prepayment?
- Should goods in transit be:
  - Stock location?
  - Balance sheet account?
  - Supplier prepayment?
  - Open PO only?
  - AP invoice?
  - Manual tracker?
- Can open POs be migrated/recreated at cutover?
- What exact open PO / stock-in-transit list is required at cutover?
- Who signs off stock in transit and supplier prepayment position?

## 7. Confirm cutover date logic

What date should all opening numbers be based on?

- Mike reply captured:
  - Mike has not replied on cutover timing yet.
  - Mike confirmed several opening numbers are required for full accounts:
    - Opening stock quantity/cost.
    - AR/AP outstanding invoices.
    - Bank opening balances.
    - GL opening balances.

Follow-up still open:
- Closing 30 Jun or opening 1 Jul?
- When old AutoCount stops.
- Final stocktake timing.
- Stock freeze timing.
- Final import timing.
- What can/cannot change after import.

## 7A. Confirm parallel run / migration rehearsal

What exactly does X-Boundaries need to do during parallel run, and what does Ingenious check before confirming we are ready to go live?

- Mike reply captured:
  - Mike has not replied on parallel run yet.
  - Mike confirmed sample data can be tested before full migration.

Follow-up still open:
- Whether there will be a parallel run.
- Whether transactions are keyed into both systems.
- What reports to compare.
- Who checks differences.
- Acceptable variance.
- Go/no-go owner.

## 8. Confirm test import

Can we test import 5-10 sample rows for each template before preparing the full file?

- Mike reply captured:
  - Mike confirmed sample data can be provided first for test import before full migration.

Follow-up still open:
- Which database is used?
- Sandbox/test database?
- How to check success?
- Where errors show?
- Can failed rows be exported?
- Can wrong test imports be reversed/deleted?

## 9. Confirm what NOT to do

What should X-Boundaries avoid touching before migration?

- Mike reply captured:
  - Mike has not replied on what to avoid yet.

Follow-up still open:
- Do not change template headers?
- Avoid manual product creation?
- Avoid production import before test import?
- Avoid ItemCode changes after setup?
- Avoid CoA changes after opening balances?

## 10. Required output table summary

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

| Data item | Mike reply captured | Still open |
|---|---|---|
| Customer master | Debtor template imports existing trade customers. Data can also be migrated from old version. | Which customers count as trade customers? Mandatory fields? |
| Supplier master | Creditor template imports existing trade suppliers. Data can also be migrated from old version. | Mandatory fields? Local/overseas supplier fields? |
| Opening stock | Stock Item Opening requires opening quantity and cost by item/location. | Confirm relationship to Import Stock Open Bal. Confirm mandatory columns. |
| AR opening | AR Invoice template tracks outstanding customer invoices not yet cleared. | Confirm only unpaid invoices and required fields. |
| AP opening | AP Invoice template tracks outstanding supplier invoices not yet cleared. | Confirm only unpaid invoices and required fields. |
| CoA | Use system default or accountant-provided CoA. Account codes/info keyed manually by user. | Who keys it? When? Can it be changed after opening balance? |
| Bank balances | Full accounts require bank opening balances. | Format, sign-off owner, cutover date. |
| GL opening balance | Full accounts require GL opening balances. | Format, entry method, sign-off owner. |
| Stock in transit / supplier prepayment | Outstanding PO Listing can track stock in transit: `PO > PO Listing > Outstanding PO Listing`. | Confirm accounting treatment and cutover list. |
| Test import | Sample data can be provided first for test import. | Confirm sandbox database and rollback process. |
| API / automation | Mike shared AutoCount Programmer and Integration Methods wiki links. | Confirm approved integration method, API licence, SQL/export access. |

## Internal action for Brendan / finance call

Mike's replies mean Brendan / finance / accountant need to prepare or confirm:

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
- GST / FX setup:
  - GST.
  - Import GST.
  - JPY / USD / KRW.

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

## Immediate Monday Mike call focus

Ask Mike only what blocks migration and go-live.

Must ask:

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

Do not over-focus yet:

- Do not design full AI agents before migration is stable.
- Do not build write-back automation before export/read-only access is confirmed.
- Do not build stock upload generator until Mike confirms the correct stock quantity workflow and import method.
