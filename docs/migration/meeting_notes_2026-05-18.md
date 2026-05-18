# Meeting notes on 18.05.2026

## Mike will check with SiteGiant

Since Shopify has different input fields from SiteGiant, confirm how data can be mapped from SiteGiant into Shopify reliably.

## Integration sequence

`AutoCount 2.0 > SiteGiant > Shopify / marketplace`

## Excel templates

### One-time use

- Import stock open balance.
  - Primary key: `ITEM CODE`.
- Import AR invoice.
- Import AP invoice.

### Multiple-use templates

- Import stock.
- Import creditor.
- Import debtor.

### Import methods

- Excel template copy-paste.
- AutoCount 2.0 UI upload.

## SKU change

### To change SKU in AutoCount 2.0

- `Tools > Change Code > Change Item Code`.

### To view SKU history in AutoCount 2.0

- `Tools > Audit Trail > Input SKU Item Code & Change Date Range`.

### SiteGiant

- Update and remap inside SiteGiant after updating SKU in AutoCount 2.0.

## Accounting

### Stock in transit

- Paid to supplier but stock not received yet:
  - Filed under purchase order.
- Customer paid us but stock not delivered yet:
  - If invoice has already been issued, this may need another location.
- Treated as prepayment.

### Chart of accounts

- In AutoCount 2.0: `GL > Account Maintenance`.
- Account number can be changed in AutoCount 2.0.

## Parallel run

- AutoCount 1.0 and AutoCount 2.0 can be installed on the same server.
- Transactions must be recorded separately in both systems during parallel run.
  - Challenge: Need to key into both systems.
  - It is not synced.
- Suggestion: Parallel run for 5 days from Monday to Friday.

### Accounting parallel

- Need to figure out how to do accounting parallel from 1 Jul.

## What X-Boundaries needs to provide

### Hardware requirement

- Check whether the current printer can support Windows 11 for shop receipt printing.
  - Confirm model, specs, driver support, etc.

## Deadline: 1 June

### Cleaned-up customer and supplier master

1. `AR > Debtor File Maintenance`.
2. `AP > Creditor File Maintenance`.

### Cleaned-up stock master

1. Full stock master list.

## Deadline: 30 June

### Update all stock balance report balances / master quantity

1. Decide what stays and what goes.
2. Import stock open balance Excel template.

### Transaction data

1. `Stock > Stock Balance Report` opening balance.
2. Brendan x accountant:
   - Import AR invoice Excel template: `AR > Debtor Aging Report`.
   - Import AP invoice Excel template: `AP > Creditor Aging Report`.
