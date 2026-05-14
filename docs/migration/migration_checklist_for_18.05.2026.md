# AutoCount 2.0 Migration Preparation - Meeting Checklist

**Prepared by:** X-Boundaries  
**For:** Ingenious / Mike  
**Purpose:** Pre-meeting checklist and live call script for Monday's migration preparation discussion.

> Note: This document is intended to be used both as a pre-read and as the live call structure. The aim is to align on required preparation, responsibilities, deadlines, and go-live blockers before migration day.

---

## Meeting goal

The goal of this meeting is to confirm what X-Boundaries must prepare before migration day.

This meeting is not intended to be full AutoCount training yet; the focus is migration preparation and go-live readiness.

The required meeting output is a simple checklist:

- Data item.
- Template or setup area.
- Mandatory fields.
- Owner.
- Deadline.
- What must be final before go-live.
- What can still be corrected after go-live.

---

# Opening note

For this meeting, the objective is to confirm what X-Boundaries must prepare before migration day.

This meeting is not intended to be full AutoCount training yet; the focus is migration preparation and go-live readiness.

### Required meeting output:

- What data is needed.
- Which template to use.
- Which fields are compulsory.
- Who prepares it.
- What Ingenious handles.
- What accountant / finance must sign off.
- Deadline.
- What must be final before go-live.
- What can still be corrected after go-live.

### Main concern:

- We used AutoCount 1.0 mostly for stock.
- Now AutoCount 2.0 will include stock + accounting.

So X-Boundaries needs to understand what accounting data must be prepared properly before migration.

## Mike replies already captured

These are current answer notes from Mike / Ingenious. Treat them as useful confirmations, not as full closure of the checklist.

- [x] Debtor and Creditor Excel templates are used to import existing trade customers and suppliers.
- [x] Debtor / Creditor data can also be migrated from the old AutoCount version.
- [x] Stock Item Opening Excel template is used for stock opening balance.
  - Opening stock quantity and cost are needed for each item according to each location.
- [x] AR Invoice and AP Invoice Excel templates are used for customer and supplier opening balances.
  - Purpose: Track outstanding customer and supplier invoices that have not yet been cleared.
- [x] X-Boundaries prepares all Excel data.
- [x] Sample data can be provided first for test import before full migration.
- [x] Chart of Accounts can follow system default or be provided by accountant.
  - Account codes and information need to be keyed in manually by user.
- [x] Full accounts require bank opening balances and General Ledger (GL) account opening balances.
- [x] Stock in transit can be tracked using Outstanding PO Listing.
  - Path: `PO > PO Listing > Outstanding PO Listing`.
  - Mike said this is available in the old version too.
- [x] Programming references shared by Mike:
  - https://wiki.autocountsoft.com/wiki/Programmer
  - https://wiki.autocountsoft.com/wiki/Integration_Methods

### Still not closed

- [ ] Mandatory columns for each Excel template.
- [ ] Exact import sequence.
- [ ] Correction / reversal rules for wrong imports.
- [ ] Whether `Stock Item Opening` is the same as `Import Stock Open Bal`.
- [ ] Whether `Import Stock Item` is item master only and reusable after go-live.
- [ ] Post-go-live incoming stock workflow.
- [ ] Stock in transit accounting treatment.
- [ ] Test import sandbox / rollback process.
- [ ] API / export / SQL availability and licence requirements.

---

# 1. Confirm migration scope

### Confirmation needed:

- Are we migrating from AutoCount 1.0 to AutoCount 2.0 using the 6 Excel templates provided by Ingenious?
- Are these templates the latest / correct ones?
- Are we using stock + accounting module from go-live?
- What exactly will Ingenious handle?
- What exactly must X-Boundaries prepare?
- What must accountant / finance prepare?

### Info X-Boundaries needs to get:

| Thing | Answer needed |
|---|---|
| Migration method | Template import / manual setup / other |
| Ingenious responsibility | What they import / configure |
| X-Boundaries responsibility | What we must prepare |
| Accountant responsibility | Accounting numbers / sign-off |

### Mike confirmed / clarified

- [x] X-Boundaries prepares all Excel data.
- [x] Sample data can be provided first for test import.
- [x] Debtor / Creditor templates are for existing trade customers and suppliers.
- [x] Stock Item Opening is for opening stock quantity and cost by item/location.
- [x] AR/AP Invoice templates are for outstanding customer/supplier opening balances.
- [x] Full accounts need CoA decision, bank opening balances, and GL opening balances.

### Still pending / ask Mike

- [ ] Are the 6 Excel templates provided by Ingenious definitely the latest/correct ones?
- [ ] Confirm exactly what Ingenious imports/configures.
- [ ] Confirm exactly what X-Boundaries prepares.
- [ ] Confirm exactly what accountant/finance signs off.
- [ ] Confirm whether stock + accounting modules go live together.
- [ ] Confirm whether any data will be migrated automatically from AutoCount 1.0 instead of Excel.

---

# 2. Confirm the 6 templates

### Templates currently provided by Ingenious:

- Import Stock Item.
- Import Stock Open Bal.
- Import Debtor.
- Import Creditor.
- Import AR Invoice.
- Import AP Invoice.

### Please confirm the purpose of each template.

| Template | Confirmation needed |
|---|---|
| Import Stock Item | Understand that this is for importing stock items. I understand which columns are needed for this import. Will this be used for every instance of stock importing? |
| Import Stock Open Bal | Is this for opening stock by location? Need qty + unit cost? |
| Import Debtor | Is this customer master? Which customers need to be included? |
| Import Creditor | Is this supplier master? Which suppliers need to be included? |
| Import AR Invoice | Is this for customers who still owe us money at go-live? |
| Import AP Invoice | Is this for suppliers we still owe at go-live? |

For each template, please confirm:

- Purpose.
- Compulsory columns.
- Optional columns.
- Sample completed row.
- One-time migration only or reusable after go-live.
- Who prepares it.
- Deadline.
- Whether we can test import 5-10 rows first.
- If imported wrongly, whether it can be reversed or corrected.

### Mike confirmed / clarified

- [x] Debtor template imports existing trade customers.
- [x] Creditor template imports existing trade suppliers.
- [x] Debtor/Creditor data can also be migrated from old AutoCount version.
- [x] Stock Item Opening template is used for opening stock balance.
  - Opening stock quantity and cost are needed for each item according to each location.
- [x] AR Invoice template is for customer opening balances / outstanding invoices.
- [x] AP Invoice template is for supplier opening balances / outstanding invoices.
- [x] Sample data can be provided first for test import.

### Still pending / ask Mike
- [ ] What exactly is `Import Stock Item` for?
  - Item master only, or can it affect stock quantity?
  - Used for creation only?
  - Can it update existing items after go-live? If update is allowed, what key does AutoCount use?
  - Will this be used for future stock importing?
- [ ] When new stock is coming in, how do we check whether the SKU has already been created in AutoCount 2.0?
- [ ] If the incoming stock SKU is new and does not exist in AutoCount 2.0 yet, will `Import Stock Item` auto-create it, or must X-Boundaries create the item master first?
- [ ] If a stock opening / stock quantity import row uses a SKU that does not exist yet, will AutoCount reject the row, auto-create the item, or create an error report?
- [ ] What is the safest workflow for a new SKU: check existing AutoCount item first, create item master, then import opening/incoming stock?
- [ ] What are the compulsory columns for each template?
- [ ] What are the optional columns for each template?
- [ ] What are the mandatory columns for `Import Stock Item`?
- [ ] What are the mandatory columns for `Import Stock Open Bal` / `Stock Item Opening`?
- [ ] Can Mike provide one sample completed row for each template?
- [ ] If imported wrongly, can each template import be reversed, deleted, or corrected?
- [ ] Is `Stock Item Opening` the same thing as `Import Stock Open Bal`?

---

# 3. Confirm import sequence

### Confirmation needed:

- What is the correct order to import / set up everything?

### Need Ingenious to confirm the real order.

#### Possible order to check with Ingenious:

1. Company / database setup.
2. Chart of accounts.
3. Tax codes / currency / payment methods.
4. Locations.
5. Stock item master.
6. Debtor / customer master.
7. Creditor / supplier master.
8. Opening stock balance.
9. Opening AR invoices.
10. Opening AP invoices.
11. GL opening balance.
12. Check / reconcile.

### Important:

Do not assume this order is correct. Ingenious confirmation is required.

### Mike confirmed / clarified

- [x] Mike has not confirmed exact sequence yet, but his replies imply these areas are required:
  - Debtor / customer master.
  - Creditor / supplier master.
  - Stock opening quantity and cost by item/location.
  - Outstanding AR/AP invoices.
  - CoA setup.
  - Bank opening balances.
  - GL opening balances.

### Still pending / ask Mike

- [ ] Confirm the real import/setup order.
- [ ] Confirm whether CoA, locations, item master, debtor master, and creditor master must exist before opening balances.
- [ ] Confirm where bank opening balances and GL opening balances sit in the sequence.
- [ ] Confirm what must be done in sandbox/test database before final import.
- [ ] Confirm what can still be corrected after import.

---

# 4. Confirm master data needed

### Please confirm what lists X-Boundaries must prepare.

| Data | Confirmation needed |
|---|---|
| Product master | What fields needed? Item code, barcode, brand, category, price, cost, UOM? |
| Opening stock | Need by location? What date? Need unit cost? |
| Customers | Which customers need to be migrated? Retail? Wholesale? Metro? Marketplace? |
| Suppliers | Which suppliers need to be migrated? Local / overseas? |
| Locations | Confirm HQ, XB01, XB02, XB03, XB04. |
| Payment methods | Cash, NETS, credit card, PayNow, GrabPay, AliPay, etc. |
| Price lists | Do we need channel pricing tiers before go-live? |

### Internal Source Sheets (for stock import):

X-Boundaries has internal Google Sheets prepared for the stock import workflow. The working sheets should remain private, but the important source/master fields are:
- `InternalProductID`
- `PrimarySKU`
- `ProductDescription`
- `Vendor`
- `Brand`
- `Category`
- `Style Name`
- `Style Number`
- `Size`
- `Colour`
- `Gender`
- `Price`
- `Cost`
- `UOM`
- `Rate`
- `LookupKey`
- `IsActive`
- `ActiveLookupKey`

- `PrimarySKU` is intended to be the AutoCount `ItemCode`.
- `InternalProductID` is a permanent internal ID and probably should not enter AutoCount unless Mike confirms a safe field.
- Old/changed SKUs should be tracked in `SKU_History`, not overwritten.

Current stock input fields include:
| Field | Purpose |
|---|---|
| `LineNo` | Input row number / traceability. |
| `VendorSKU` | User-entered SKU or supplier SKU to match against master data. |
| `IncomingQty` | Incoming quantity. |
| `Location` | Target stock location. |
| `PORef` | PO or shipment reference. |
| `Remarks` | Free-text notes. |
| `LookupKey` | Generated lookup key. |
| `MatchCount` | Number of master matches found. |
| `MatchStatus` | Match result, such as `OK` or `NOT FOUND`. |

Current AutoCount stock output fields include:
| AutoCount output field | Current source / default |
|---|---|
| `InternalProductID` | From master. |
| `ItemCode (30 chars)` | `PrimarySKU`. |
| `Description (100 chars)` | `ProductDescription`. |
| `Desc2 (100 chars)` | `Style Name`. |
| `ItemGroup (8 chars)` | Pending default / Mike confirmation. |
| `ItemType (12 chars)` | Pending default / Mike confirmation. |
| `ItemBrand (20 chars)` | `Brand`. |
| `ItemCategory (20 chars)` | `Vendor`. |
| `ItemClass (20 chars)` | `Category`. |
| `LeadTime (40 chars)` | Pending. Do not leave `???` in production import. |
| `StockControl` | `T`. |
| `UOM (8 chars)` | `UOM`, commonly `PCS` for sample rows. |
| `Rate (Decimal)` | `Rate`. |
| `Price (Decimal)` | `Price`. |
| `Cost (Decimal)` | `Cost`. |
| Min/max sale/purchase price fields | Current default `0`, pending confirmation. |
| Min/Max/Normal/Reorder quantity fields | Current default `0`, pending confirmation. |
| `BarCode (30 chars)` | Pending barcode policy. |

### Key question:

- Which of these must be clean before migration, and which can be cleaned after go-live?

### Mike confirmed / clarified

- [x] Debtor template imports existing trade customers.
- [x] Creditor template imports existing trade suppliers.
- [x] Customer/supplier data can also be migrated from old AutoCount version.
- [x] Opening stock requires quantity and cost for each item by location.

### Still pending / ask Mike

- [ ] Which customers count as trade customers?
  - Wholesale?
  - Metro?
  - Corporate?
  - Gebiz?
  - Retail cash customers?
  - Marketplace customers?
- [ ] Exact product master fields.
- [ ] Exact location codes.
- [ ] Whether Shopify can share XB01 stock.
- [ ] SiteGiant marketplace stock mapping to XB04.
- [ ] Payment methods setup.
- [ ] Price lists / channel pricing tiers needed before go-live.

---

# 5. Confirm accounting data needed

This is the most important section because X-Boundaries was not using the accounting module properly before.

### Confirmation needed:

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

### Key question:

- Since we did not use accounting module before, what accounting numbers must be final before go-live?

Also confirm whether stock in transit / paid-but-not-received goods is part of opening accounting balances, opening stock, open PO, AP, or a separate tracker.

### Mike confirmed / clarified

- [x] CoA can follow AutoCount system default or be provided by accountant.
- [x] Account codes and information need to be keyed in manually by user.
- [x] AR Invoice template is for outstanding customer invoices not yet cleared.
- [x] AP Invoice template is for outstanding supplier invoices not yet cleared.
- [x] Full accounts require bank opening balances.
- [x] Full accounts require GL account opening balances.
- [x] Stock Item Opening requires quantity and cost by item/location.
- [x] Operational stock in transit can be tracked using Outstanding PO Listing.

### Still pending / ask Mike

- [ ] Who keys the CoA manually?
  - X-Boundaries?
  - Accountant?
  - Ingenious?
- [ ] Is there a GL opening balance import template, or is this manual entry?
- [ ] Does Stock Item Opening automatically post inventory value to GL, or is a separate GL entry needed?
- [ ] What exact format is needed for bank opening balances?
- [ ] What exact format is needed for GL opening balances?
- [ ] GST / import GST / FX setup for JPY, USD, KRW.
- [ ] Supplier prepayment accounting treatment.
- [ ] Goods-in-transit accounting treatment.
- [ ] Who signs off final opening balances before go-live?

---

# 6. Confirm 5 locations

### Assumptions
- These are the correct AutoCount stock locations:
  - HQ.
  - XB01 - MG / Shopify
  - XB02 - JBM
  - XB03 - Wholesale
  - XB04 - Marketplaces

- Marketplace stock sits in XB04, SiteGiant will sync movement with AutoCount, where AutoCount is the source of truth

### Confirmation needed:

- Are these location codes valid in AutoCount?
- Does opening stock need one row per item per location?
- Can Shopify share XB01  stock?

### Mike confirmed / clarified

- [x] Opening stock quantity and cost are needed by item and by location.

### Still pending / ask Mike

- [ ] Confirm exact AutoCount location codes:
  - `HQ`.
  - `XB01`.
  - `XB02`.
  - `XB03`.
  - `XB04`.
- [ ] Confirm whether each item/location combination needs one opening stock row.
- [ ] Confirm whether Shopify can share XB01 stock.
- [ ] Confirm SiteGiant marketplace stock mapping to XB04.

---

# 6B. Confirm SKU identity / primary SKU / SKU change handling

### Context:

X-Boundaries has a SKU problem.

Marketplace SKU / supplier SKU / old AutoCount SKU can change even when the actual product is the same.

We need our own stable primary product key so product history, stock, sales reports, channel mapping, and future automation do not break when external SKUs change.

### Confirmation needed:

- In AutoCount 2.0, can ItemCode / stock code be changed after the item is created?
- If ItemCode can be changed, what happens to historical transactions?
- If ItemCode should not be changed, should we treat AutoCount ItemCode as the permanent internal product ID?
- If AutoCount ItemCode is not suitable as our permanent key, where should our internal primary SKU / product ID be stored?
  - UDF?
  - Alternative item code?
  - Barcode?
  - Item description field?
  - External mapping table?
- Can one AutoCount item store multiple identifiers?
  - Old AutoCount 1.0 code.
  - AutoCount 2.0 ItemCode.
  - Internal primary product ID.
  - Supplier SKU.
  - Barcode.
  - RFID.
  - Shopify SKU.
  - Shopee SKU.
  - Lazada SKU.
  - TikTok SKU.
  - KrisShop SKU.
- If a marketplace SKU changes, how should we keep old SKU history?
- Can reports be run by internal product ID instead of marketplace SKU?
- Can AutoCount import/update item records without changing historical product identity?
- Can duplicate SKUs / duplicate barcodes be prevented?
- Can item codes be locked so normal users cannot accidentally change them?
- Is there a recommended AutoCount structure for products with changing channel SKUs?

### Internal decision:

- X-Boundaries will use the same SKU across platforms where practical.
- `PrimarySKU` is the shared platform SKU and should be imported into AutoCount as ItemCode.
- `InternalProductID` remains the permanent internal product identity, likely outside AutoCount.
- AutoCount probably does not need `InternalProductID`.
- `SKU_History` will hold old/changed SKU aliases if SKU changes later.
- Future web app expansion is parked for later.

### Key question:

- What is the safest product identity structure for us before migration, assuming SKUs may change later?

### Follow-up for internal automation:

If AutoCount fields are too limited, we will keep our own master mapping legend outside AutoCount.

Need confirmation on:
- Can AutoCount 2.0 ItemCode / SKU be changed safely after item creation?
- If not, X-Boundaries will rely on `SKU_History` outside AutoCount for SKU changes.
- Which AutoCount field should be used as the stable bridge key.
- Which AutoCount exports we can use to refresh our mapping legend.
- Whether copy-paste Excel import can update/create items safely using this bridge key.

### Mike confirmed / clarified

- [ ] No confirmation yet on SKU identity / ItemCode behaviour.

### Still pending / ask Mike

- [ ] Can AutoCount ItemCode / stock code be changed after item creation?
- [ ] What happens to historical transactions if ItemCode changes?
- [ ] Should AutoCount ItemCode be treated as the stable bridge key?
- [ ] If not, where should X-Boundaries store the internal product ID?
- [ ] Can one AutoCount item store multiple identifiers?
- [ ] Can duplicate SKU / barcode be prevented?
- [ ] Can item codes be locked from normal users?
- [ ] Can reports run by internal product ID?
- [ ] If AutoCount fields are limited, confirm which export/field should bridge to our external master mapping legend.

---

# 6A. Confirm stock in transit / goods in transit handling

### Context:

Sometimes we may have already placed PO / paid supplier / received supplier invoice / received packing list, but the goods have not physically arrived at warehouse yet.

Need to know how AutoCount should handle this during migration and after go-live.

### Confirmation needed:

- What is the proper AutoCount workflow for stock in transit?
  - PO created.
  - Supplier paid / deposit paid.
  - Supplier invoice received.
  - Goods shipped but not received.
  - Goods physically received.
  - GRN done.
  - Stock moved into HQ / sub-location.

- During migration, do we need to prepare a list of stock in transit as of cutover date?
- If yes, what fields are needed?
  - Supplier.
  - PO number.
  - Supplier invoice number.
  - Item code.
  - Quantity.
  - Currency.
  - Unit cost.
  - Amount paid.
  - Shipment / ETA.
  - Whether invoice received.
  - Whether stock received.
  - Whether GRN done.

- Should stock in transit be handled as:
  - A stock location?
  - A balance sheet account?
  - Supplier prepayment?
  - Open PO only?
  - AP invoice not matched to GRN?
  - Manual tracker outside AutoCount?

- If we already paid the supplier but goods have not arrived:
  - Does it go under supplier prepayment?
  - Does it go under goods in transit?
  - Does it go under AP?
  - Does it affect inventory value before GRN?

- If supplier invoice is received before goods arrive:
  - Can AutoCount record the AP invoice first?
  - Can it later match to GRN?
  - How do we avoid double-counting stock value?

- When goods arrive:
  - How do we convert stock in transit into actual inventory?
  - Does GRN automatically move value from goods in transit to inventory?
  - Is any journal entry needed?
  - Who should check this?

- How is landed cost handled for stock in transit?
  - Freight.
  - Duty.
  - Import GST.
  - Insurance.
  - FX rate.
  - Partial shipment.

- Can AutoCount show a report of all stock in transit?
- Can it show paid-but-not-received stock?
- Can it show PO / AP / GRN matching status?
- If AutoCount cannot handle this cleanly without customisation, what is the recommended manual SOP or Excel tracker?

### Key question:

- For migration day, what exact list of supplier prepayments / stock in transit / paid-but-not-received goods must X-Boundaries prepare, and who should sign it off?

### Mike confirmed / clarified

- [x] Mike first asked what stock in transit means.
- [x] X-Boundaries clarified:
  - Stock purchased as PO.
  - Not all delivered yet.
  - Delivered in batches.
  - Enters HQ location batch by batch.
- [x] Mike replied that Outstanding PO Listing can be used for tracking.
  - Path: `PO > PO Listing > Outstanding PO Listing`.
  - Available in old version too.

### Still pending / ask Mike

- [ ] Is Outstanding PO Listing only operational tracking, or enough for accounting too?
- [ ] If supplier is paid before goods arrive, should it be supplier prepayment?
- [ ] If supplier invoice is received before goods arrive, can AP be recorded before GRN?
- [ ] Can AP later match to GRN?
- [ ] How do we avoid double-counting inventory value?
- [ ] Does GRN move value into inventory automatically?
- [ ] Is any journal entry needed?
- [ ] How are landed costs handled for stock in transit?
- [ ] Can AutoCount show paid-but-not-received stock?
- [ ] What exact stock-in-transit / supplier prepayment list is needed at cutover?
- [ ] Who signs off the cutover position?

---

# 7. Confirm cutover date logic

### Confirmation needed:

- What date should all opening numbers be based on?
- Is it closing 30 Jun or opening 1 Jul?
- When do we stop using AutoCount 1.0?
- When do we do final stocktake?
- When do we freeze stock movement?
- When does Ingenious import final data?
- What can still change after import?
- What must not change after import?

### Important:

Opening stock, AP, AR, bank, cash, and accounting balances should all be based on the same cutover timing.

### Mike confirmed / clarified

- [x] Mike confirmed full accounts require:
  - Opening stock quantity/cost.
  - AR/AP opening invoices.
  - Bank opening balances.
  - GL opening balances.

### Still pending / ask Mike

- [ ] Confirm whether numbers are based on closing 30 Jun or opening 1 Jul.
- [ ] Confirm when old AutoCount stops.
- [ ] Confirm final stocktake timing.
- [ ] Confirm stock movement freeze timing.
- [ ] Confirm final import timing.
- [ ] Confirm what can still change after import.
- [ ] Confirm what must not change after import.

---

# 7A. Confirm parallel run / migration rehearsal

### Context:

Kickoff plan mentioned a 2-week parallel run before go-live, but need Ingenious to confirm what this actually means in practice.

Main concern:

If X-Boundaries is using AutoCount 1.0 and AutoCount 2.0 in parallel, we need to know who keys what, what reports to compare, and when we can safely stop the old system.

### Confirmation needed:

- Are we definitely doing a parallel run before go-live?
- How long is the parallel run?
  - 1 week?
  - 2 weeks?
  - Other?
- What is the purpose of the parallel run?
  - Check stock quantity?
  - Check accounting postings?
  - Check POS?
  - Check AP / AR?
  - Check reports?
  - Staff training / UAT?
- During parallel run, do we enter transactions into both AutoCount 1.0 and AutoCount 2.0?
- If yes, which transactions must be entered into both systems?
  - POS sales.
  - Stock receiving / GRN.
  - Stock transfer.
  - Stock adjustment.
  - Customer invoice / AR.
  - Supplier invoice / AP.
  - Payments.
  - Refunds / credit notes.
  - Expenses.
- If no, how exactly does the parallel run work?

### Reports to compare:

- What reports must we compare between AutoCount 1.0 and AutoCount 2.0?
  - Stock balance by item.
  - Stock balance by location.
  - Sales report.
  - Purchase / GRN report.
  - AP ageing.
  - AR ageing.
  - Trial balance.
  - Bank / cash report.
  - Inventory valuation report.
- Who is responsible for checking differences?
- How often do we check?
  - Daily?
  - End of week?
  - End of parallel run only?
- What difference is acceptable?
- What differences are considered go-live blockers?

### Cutover / stopping old system:

- When do we stop entering transactions into AutoCount 1.0?
- When does AutoCount 2.0 become the official source of truth?
- Can AutoCount 1.0 remain read-only after cutover?
- How long should we keep AutoCount 1.0 accessible after go-live?
- If a transaction is missed during cutover, where should it be entered?
- If parallel run fails, what is the fallback plan?
- Can go-live be delayed if differences are not resolved?
- Who gives final go/no-go approval?

### Key question:

- What exactly does X-Boundaries need to do during parallel run, and what does Ingenious check before confirming we are ready to go live?

### Mike confirmed / clarified

- [x] Sample data can be provided first for test import before full migration.
- [ ] No confirmation yet on parallel run details.

### Still pending / ask Mike

- [ ] Are we definitely doing a parallel run?
- [ ] How long is the parallel run?
- [ ] During parallel run, are transactions keyed into both AutoCount 1.0 and AutoCount 2.0?
- [ ] Which transactions are keyed twice?
- [ ] Which reports must be compared?
- [ ] Who checks differences?
- [ ] What variance is acceptable?
- [ ] What differences are go-live blockers?
- [ ] Who gives final go/no-go approval?
- [ ] What is the fallback plan if parallel run fails?

---

# 8. Confirm test import

### Confirm clearly:

Can we test import 5-10 sample rows for each template before preparing the full file?

### Follow-up questions:

- Which database should test import use?
- Can we use a sandbox / test database?
- How do we check if import succeeded?
- Where do import errors show?
- Can failed rows be exported?
- Can a wrong test import be deleted / reversed?

### Reason:

Do not spend days cleaning a huge Excel file before confirming the format works.

### Mike confirmed / clarified

- [x] Sample data can be provided first for test import before full migration.

### Still pending / ask Mike

- [ ] Which database should test import use?
- [ ] Is there a sandbox/test database?
- [ ] How do we check if import succeeded?
- [ ] Where do import errors show?
- [ ] Can failed rows be exported?
- [ ] Can wrong test imports be deleted/reversed?
- [ ] Can we test 5-10 sample rows for each template before preparing full files?

### Test cases to ensure:
- One normal active product.
- One product with long description near character limit.
- One product with blank `LeadTime`.
- One product with `LeadTime = 0`.
- One product with `LeadTime = 30`.
- One SKU that already exists in AutoCount 2.0, to confirm update/create behaviour.
- One new SKU that does not exist in AutoCount 2.0 yet, to confirm whether import auto-creates or rejects it.
- One intentionally unmatched SKU to confirm exception handling catches it before export.

---

# 9. Confirm what NOT to do

### Confirmation needed:

- What should X-Boundaries avoid touching before migration?
- Should we avoid creating products manually?
- Should we avoid importing into the real database?
- Should we avoid changing template headers?
- Should we avoid changing item codes after setup?
- Should we avoid changing CoA after opening balance?

### Reason:

Avoid creating migration problems by accident.

### Mike confirmed / clarified

- [ ] No confirmation yet on what to avoid before migration.

### Still pending / ask Mike

- [ ] Do not change template headers?
- [ ] Avoid manual product creation before template import?
- [ ] Avoid importing into production before test import passes?
- [ ] Avoid changing ItemCode after setup?
- [ ] Avoid changing CoA after opening balances?
- [ ] Any other `do not touch` rules?

### Guardrails:
- Do not import production rows until Mike confirms mandatory fields and import sequence.
- Do not use stock opening import after go-live unless Mike confirms it is safe.
- Do not import incoming stock for a SKU until AutoCount confirms whether that SKU already exists or can be auto-created through import.
- Do not leave `LeadTime = ???` in production import.
- Do not assume blank, `0`, or text is safe for `LeadTime` until Mike confirms.
- Do not overwrite SKU history.
- Keep real operational data out of GitHub.

---

# 10. Required output from Ingenious

Before ending the call, ask Ingenious to confirm this table.

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

### Mike reply summary to capture in this output table

| Data item | Mike reply captured | Still open |
|---|---|---|
| Customer master | Debtor template imports existing trade customers. Data can also be migrated from old version. | Which customers count as trade customers? Mandatory fields? |
| Supplier master | Creditor template imports existing trade suppliers. Data can also be migrated from old version. | Mandatory fields? Local/overseas supplier fields? |
| Opening stock | Stock Item Opening requires opening quantity and cost by item/location. | Confirm relationship to Import Stock Open Bal. Confirm mandatory columns. |
| AR opening | AR Invoice template tracks outstanding customer invoices not yet cleared. | Confirm only unpaid invoices and required fields. |
| AP opening | AP Invoice template tracks outstanding supplier invoices not yet cleared. | Confirm only unpaid invoices and required fields. |
| CoA | Use system default or accountant-provided CoA. Account codes/info keyed manually by user. | Who keys it? When? Can it be changed after opening balance? |
| Bank balances | Full accounts require bank opening balances. | Format, sign-off owner, cutover date. |
| GL opening balance | Full accounts require GL account opening balances. | Format, entry method, sign-off owner. |
| Stock in transit / supplier prepayment | Outstanding PO Listing can track stock in transit: `PO > PO Listing > Outstanding PO Listing`. | Confirm accounting treatment and cutover list. |
| Test import | Sample data can be provided first for test import. | Confirm sandbox database and rollback process. |
| API / automation | Mike shared AutoCount Programmer and Integration Methods wiki links. | Confirm approved integration method, API licence, SQL/export access. |

---

# 11. Internal action for Brendan / finance call

Mike's replies mean Brendan / finance / accountant need to prepare or confirm the accounting side clearly.

| Item | Needed from finance/accountant | Suggested owner | Status |
|---|---|---|---|
| CoA decision | Use AutoCount default or accountant CoA | Brendan / accountant | Open |
| Bank opening balances | Bank/cash balances as of cutover | Brendan / accountant | Open |
| GL opening balances | Trial balance / GL opening by account | Brendan / accountant | Open |
| AR opening | Unpaid customer invoices at cutover | Brendan / finance | Open |
| AP opening | Unpaid supplier invoices at cutover | Brendan / finance | Open |
| Opening stock value | Quantity and cost by item/location | Ops + finance | Open |
| Stock in transit | Open PO / paid-not-received / partial deliveries | Ops + finance | Open |
| Supplier prepayments | Deposits/prepayments not cleared by GRN/invoice | Brendan / finance | Open |
| GST / FX | GST, import GST, JPY / USD / KRW setup | Brendan / accountant | Open |

---

# 12. Immediate Monday Mike call focus

Use this if time is short. Ask only what blocks migration and go-live.

- [ ] Mandatory fields for each Excel template.
- [ ] Exact import sequence.
- [ ] Correction / reversal rules for wrong imports.
- [ ] Whether `Stock Item Opening` is the same as `Import Stock Open Bal`.
- [ ] Whether `Import Stock Item` is item master only.
- [ ] Whether `Import Stock Item` is reusable after go-live.
- [ ] Exact post-go-live incoming stock workflow:
  - PO.
  - GRN.
  - Stock Receive.
  - Stock Adjustment.
  - Other.
- [ ] Stock in transit accounting treatment:
  - Outstanding PO only?
  - Supplier prepayment?
  - Goods in transit?
  - AP invoice?
- [ ] Test import database / sandbox process.
- [ ] Import error checking and rollback process.
- [ ] API / export / SQL availability for future automation.
- [ ] Whether API Module or other licence is needed for automation.
- [ ] New SKU import behaviour: whether missing SKUs are rejected, auto-created, or must be created in item master first.

### Stock import blockers:
- [ ] Mandatory fields for `Import Stock Item`.
- [ ] Mandatory fields for `Import Stock Open Bal` / `Stock Item Opening`.
- [ ] Whether `Import Stock Item` is reusable after go-live.
- [ ] Whether `LeadTime` is mandatory and what value to use if unknown.

### Do not over-focus yet

- Do not design full AI agents before migration is stable.
- Do not build write-back automation before export/read-only access is confirmed.
- Do not build stock upload generator until Mike confirms the correct stock quantity workflow and import method.
