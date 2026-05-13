# AutoCount 2.0 Migration Preparation Checklist

**Target:** Monday Mike Call (12.05.2026)

## Issue: Migration scope

- [ ] What is needed?
  - Confirm whether the provided 6 templates are latest/correct.
  - Confirm exactly what Ingenious imports/configures.
  - Confirm exactly what X-Boundaries prepares.
  - Confirm exactly what accountant/finance signs off.
  - Confirm whether stock + accounting modules go live together.

- [x] Confirmed / clarified by Mike:
  - X-Boundaries prepares all Excel data.
  - Sample data can be provided first for test import.
  - Templates purpose: Debtor/Creditor for customers/suppliers, Stock Item Opening for stock qty/cost, AR/AP Invoice for outstanding balances.
  - Accounting setup needs: CoA decision, bank opening balances, GL opening balances.

- [ ] Pending confirmation:
  - (No specific blockers mentioned, focus on templates below)

## Issue: Migration templates

- [ ] What is needed?
  - Confirm what each template is for.
  - Confirm mandatory and optional columns.
  - Request sample completed rows.
  - Confirm wrong import correction/reversal process.

- [x] Confirmed / clarified by Mike:
  - Debtor/Creditor templates are for existing trade customers/suppliers (data can be migrated from old version).
  - Stock Item Opening is for opening stock quantity and cost by item/location.
  - AR/AP Invoice templates are for outstanding customer/supplier opening balances.
  - Sample data can be provided first for test import.

- [ ] Pending confirmation:
  - Is `Stock Item Opening` the same as `Import Stock Open Bal`?
  - What is `Import Stock Item` for (Item master only? Used after go-live?)?
  - What are the compulsory and optional columns?
  - How do we reverse wrong imports?

## Issue: Import sequence

- [ ] What is needed?
  - Confirm real import order with Ingenious.
  - Confirm whether CoA, locations, item master, debtor/creditor must exist before opening balances.
  - Confirm when bank/GL opening balances are keyed.

- [x] Confirmed / clarified by Mike:
  - Required data implies: Customers/suppliers, stock qty/cost, outstanding AR/AP, CoA setup, bank/GL balances.

- [ ] Pending confirmation:
  - Exact import sequence not confirmed.

## Issue: Master data needed

- [ ] What is needed?
  - Which customers count as trade customers? (Retail? Marketplace?)
  - Exact product master fields.
  - Exact location code setup.
  - Whether Shopify can share XB01 stock.
  - Price tier/channel pricing setup.

- [x] Confirmed / clarified by Mike:
  - Debtor/Creditor templates handle trade customers/suppliers.
  - Opening stock requires qty/cost by item/location.

- [ ] Pending confirmation:
  - Product master fields not yet clarified.
  - Location codes, payment methods, price lists not yet clarified.

## Issue: Accounting data needed

- [ ] What is needed?
  - Format for bank opening balances.
  - Format for GL opening balances.
  - Who keys CoA.
  - Whether opening stock value posts to GL automatically.
  - GST/FX setup.
  - Supplier prepayment and goods-in-transit accounting treatment.

- [x] Confirmed / clarified by Mike:
  - CoA can follow system default or accountant-provided; keyed manually by user.
  - AR/AP Invoice templates track outstanding invoices.
  - Full accounts require bank and GL opening balances.
  - Use Outstanding PO Listing for tracking goods in transit (operational).

- [ ] Pending confirmation:
  - Accounting treatment for supplier prepayments / goods in transit still pending.
  - Bank/GL format, GST/FX setup.

## Issue: 5 Locations

- [ ] What is needed?
  - Confirm exact AutoCount location codes (HQ, XB01, XB02, XB03, XB04).
  - Confirm whether each item/location combination needs one row.
  - Confirm Shopify shared stock with XB01.
  - Confirm SiteGiant marketplace stock maps to XB04.

- [x] Confirmed / clarified by Mike:
  - Opening stock qty/cost is needed by item and location.

- [ ] Pending confirmation:
  - Exact location codes not explicitly confirmed.

## Issue: SKU identity / SKU change handling

- [ ] What is needed?
  - Can AutoCount ItemCode be changed after creation?
  - What happens to historical transactions?
  - Where should internal product ID live?
  - Can duplicate SKU/barcode be prevented?

- [x] Confirmed / clarified by Mike:
  - (No replies yet)

- [ ] Pending confirmation:
  - SKU identity behaviour and ItemCode stability.

## Issue: Stock in transit / goods in transit handling

- [ ] What is needed?
  - Is Outstanding PO Listing enough for accounting, or just operational tracking?
  - If supplier paid before goods arrive, is it supplier prepayment?
  - Should goods in transit be: balance sheet account, supplier prepayment, open PO only, AP invoice?
  - Can open POs be migrated/recreated at cutover?
  - Who signs off stock in transit?

- [x] Confirmed / clarified by Mike:
  - Use Outstanding PO Listing for tracking (`PO > PO Listing > Outstanding PO Listing`).

- [ ] Pending confirmation:
  - Accounting treatment and cutover list required.

## Issue: Cutover date logic

- [ ] What is needed?
  - Closing 30 Jun or opening 1 Jul?
  - When old AutoCount stops.
  - Final stocktake and freeze timing.
  - Final import timing.
  - What can/cannot change after import.

- [x] Confirmed / clarified by Mike:
  - Full accounts require opening stock qty/cost, AR/AP invoices, bank/GL opening balances.

- [ ] Pending confirmation:
  - Cutover timing not confirmed.

## Issue: Parallel run / migration rehearsal

- [ ] What is needed?
  - Whether transactions are keyed into both systems.
  - What reports to compare.
  - Acceptable variance and go/no-go owner.

- [x] Confirmed / clarified by Mike:
  - Sample data can be provided for test import.

- [ ] Pending confirmation:
  - Parallel run process not confirmed.

## Issue: Test import

- [ ] What is needed?
  - Which database is used? Sandbox?
  - How to check success / errors?
  - Can wrong test imports be reversed/deleted?

- [x] Confirmed / clarified by Mike:
  - Test import is allowed using sample data.

- [ ] Pending confirmation:
  - Sandbox process and rollback/reversal.

## Issue: What NOT to do

- [ ] What is needed?
  - Explicit `do not touch` rules before migration (e.g., template headers, manual item creation, CoA changes).

- [x] Confirmed / clarified by Mike:
  - (No replies yet)

- [ ] Pending confirmation:
  - Rules on what to avoid.

---

## Required output table summary

| Data item | Template / setup | Mandatory fields | Owner | Deadline | Must be final before go-live? |
|---|---|---|---|---|---|
| Product master | Import Stock Item | TBC | X-Boundaries | TBC | Yes |
| Opening stock | Import Stock Open Bal | TBC | Ops + finance | TBC | Yes |
| Customer master | Import Debtor | TBC | X-Boundaries | TBC | Mostly yes |
| Supplier master | Import Creditor | TBC | X-Boundaries | TBC | Mostly yes |
| AR opening | Import AR Invoice | TBC | Finance / accountant | TBC | Yes |
| AP opening | Import AP Invoice | TBC | Finance / accountant | TBC | Yes |
| CoA | TBC | TBC | Finance / accountant | TBC | Yes |
| GL opening balance | TBC | TBC | Finance / accountant | TBC | Yes |
| Locations | AutoCount setup | TBC | X-Boundaries + Mike | TBC | Yes |
| Payment methods | AutoCount setup | TBC | Retail + finance | TBC | Yes |
| SKU identity | TBC | TBC | X-Boundaries + Mike | TBC | Yes |
| Stock in transit | TBC | TBC | Finance + ops | TBC | Yes |
| Parallel run | TBC | TBC | Ingenious + X-Boundaries | TBC | Yes |

---

## Internal action for Brendan / finance call

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

Ask Mike only what blocks migration and go-live.

Must ask:

1. Mandatory columns for each Excel template.
2. Exact import sequence.
3. Correction / reversal rules for wrong imports.
4. Whether `Stock Item Opening` is the same as `Import Stock Open Bal`.
5. Whether `Import Stock Item` is item master only and reusable after go-live.
6. Exact post-go-live incoming stock workflow (PO > GRN > Receive > Adjustment).
7. Stock in transit accounting treatment (Outstanding PO only? AP invoice?).
8. Test import sandbox / rollback process.
9. API / export / SQL availability and module licence for future automation.
