# AutoCount 2.0 Stock Import Automation Notes

**Date:** 2026-05-14  
**Area:** Migration preparation / stock import automation  
**Status:** Prepared internally; not ready for production import until Mike / Ingenious confirms open rules.

---

## 1. Purpose

X-Boundaries has prepared an internal stock import Excel automation flow to reduce manual keying and create AutoCount-ready stock item rows from the product master / stock input layer.

This document records the intended mapping, validation rules, and remaining questions before the file is used for migration or post-go-live operations.

Do not commit real product, customer, supplier, cost, or operational data to this repository. Keep production data in the internal Google Sheets / operational workbooks only.

---

## 2. Internal source sheets

Two internal Google Sheets have been prepared for the stock import workflow.

The links are intentionally not committed here to avoid exposing operational working files inside repo history. Store private links in the internal tracker / working notes if needed.

Visible source/master fields include:

| Field | Intended role |
|---|---|
| `InternalProductID` | Permanent internal product identity. |
| `PrimarySKU` | Current company SKU / proposed AutoCount item code, pending Mike confirmation. |
| `ProductDescription` | Main item description. |
| `Vendor` | Supplier / vendor grouping. |
| `Brand` | Brand field. |
| `Category` | Product category / class field. |
| `Style Name` | Secondary description / style field. |
| `Style Number` | Supplier/style reference. |
| `Size` | Product size. |
| `Colour` | Product colour. |
| `Gender` | Product gender/category attribute. |
| `Price` | Selling price. |
| `Cost` | Cost. |
| `UOM` | Unit of measure. |
| `Rate` | UOM rate. |
| `LookupKey` | Matching key for stock input lookup. |
| `IsActive` | Active product flag. |
| `ActiveLookupKey` | Active-only lookup key. |

---

## 3. Stock input / output fields

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

---

## 4. Validation rules before any real import

Before sending any production import file to Mike / Ingenious, the export should pass these checks:

- `MatchStatus` must be `OK` for every import row.
- No `NOT FOUND` rows.
- No duplicate active lookup key rows unless deliberately reviewed.
- `IncomingQty` must be numeric and greater than zero where quantity is used.
- `Location` must match confirmed AutoCount location codes.
- Required AutoCount fields must not be blank once Mike confirms mandatory columns.
- Character length limits must be checked before export:
  - `ItemCode`: 30 chars.
  - `Description`: 100 chars.
  - `Desc2`: 100 chars.
  - `ItemGroup`: 8 chars.
  - `ItemType`: 12 chars.
  - `ItemBrand`: 20 chars.
  - `ItemCategory`: 20 chars.
  - `ItemClass`: 20 chars.
  - `LeadTime`: 40 chars.
  - `UOM`: 8 chars.
  - `BarCode`: 30 chars.
- Do not import rows with `LeadTime = ???`.
- Do not assume `0`, blank, or text is safe for LeadTime until Mike confirms.
- Do not mix item master import and opening stock quantity import unless Mike confirms the exact workflow.

---

## 5. LeadTime policy

Current uncertainty:

- `LeadTime` appears to mean supplier delivery lead time after PO is placed.
- It may be optional or mandatory depending on AutoCount import behaviour.
- It may expect numeric days, blank, zero, or text.

Temporary test policy before confirmation:

| Test row | Value |
|---|---|
| Sample A | blank |
| Sample B | `0` |
| Sample C | `30` |

Use whichever format Mike / AutoCount confirms imports cleanly.

Ask Mike:

```text
For Stock Item import, what should we key into LeadTime? Is it mandatory? Is it number of calendar days from PO to supplier delivery? If we do not have reliable supplier lead-time data yet, should we leave blank or key 0?
```

---

## 6. One-time migration vs repeat-use imports

This is not confirmed yet.

Practical working assumption until Mike confirms:

| Template | Working assumption | Risk |
|---|---|---|
| `Import Stock Item` | Likely creates stock item master records and may possibly update item master fields. | Need confirm create/update behaviour and safe key. |
| `Import Stock Open Bal` / `Stock Item Opening` | Likely one-time cutover opening stock quantity/cost. | Should not be used casually after go-live unless Mike confirms. |
| `Import Debtor` | Likely customer master import. | Need confirm reusable after go-live. |
| `Import Creditor` | Likely supplier master import. | Need confirm reusable after go-live. |
| `Import AR Invoice` | Likely one-time outstanding AR at cutover. | Do not treat as normal invoice import without confirmation. |
| `Import AP Invoice` | Likely one-time outstanding AP at cutover. | Do not treat as normal supplier invoice import without confirmation. |

Ask Mike:

```text
For each template, please confirm whether it is one-time migration only or safe for repeat import after go-live; create-only or update existing records; if update is allowed, what key AutoCount uses; and if wrong import can be reversed/deleted/corrected.
```

---

## 7. Specific open questions for Mike / Ingenious

### Stock item import

- Is `Import Stock Item` the correct template for item master creation?
- Is `Import Stock Item` item master only, or does it affect stock quantity?
- Is `Stock Item Opening` the same as `Import Stock Open Bal`?
- What are the mandatory columns for `Import Stock Item`?
- What are the mandatory columns for `Import Stock Open Bal` / `Stock Item Opening`?
- Can Mike provide one sample completed row for each stock-related template?
- Can `Import Stock Item` be reused after go-live?
- Is it create-only or can it update existing items?
- If update is allowed, what key does AutoCount use?
- Can a wrong stock item import be reversed, deleted, or corrected?

### LeadTime

- Is `LeadTime` mandatory?
- Does it expect numeric days only?
- Calendar days or working days?
- Is blank accepted?
- Is `0` accepted?
- Is this field used in PO expected delivery or reorder planning?

### Defaults and mapping

- What should X-Boundaries use for `ItemGroup`?
- What should X-Boundaries use for `ItemType`?
- Should `ItemCategory = Vendor` and `ItemClass = Category`, or should these be swapped?
- What is the barcode policy if barcode is unavailable?
- Are min/max/reorder quantity defaults of `0` acceptable?
- Are min/max sale/purchase price defaults of `0` acceptable?

### Post-go-live workflow

- After go-live, should incoming stock be handled through PO > GRN / Receive instead of stock opening import?
- For new products after go-live, should users create item master in AutoCount manually, or can they still use the import template?
- For existing products after go-live, should quantity changes always be handled by GRN / stock adjustment / transfer rather than import?

---

## 8. Repo tracker impact

Recommended tracker interpretation:

| Task | Suggested update |
|---|---|
| `T003 Confirm 6 migration templates` | Still `In Progress`; Mike confirmed purposes but not mandatory columns, sequence, rollback, or repeat-use rules. |
| `T018 Create AutoCount stock item copy-paste template` | Done, but notes should say stock import automation is prepared and awaiting Mike confirmation. |
| `T019 Build stock upload generator` | Move to `In Progress` if recognising the internal Google Sheets work; keep `ReadyStatus = Waiting`. |
| `T020 Build stock upload exception report` | Partial / in progress if `MatchStatus`, `NOT FOUND`, or duplicate checks exist; still depends on final import rules. |

---

## 9. Production guardrails

- Do not import production rows until Mike confirms the mandatory fields and import sequence.
- Do not use stock opening import after go-live unless Mike confirms it is safe.
- Do not overwrite SKU history.
- Do not treat marketplace SKU as permanent product identity.
- Keep `InternalProductID` permanent.
- Treat `PrimarySKU` / proposed `ItemCode` as current operational SKU, pending AutoCount identity confirmation.
- Keep old SKU / alias history as growing rows, not overwritten cells.
- Keep real operational data out of GitHub.

---

## 10. Immediate next action

Ask Mike the stock import questions above and test 5-10 dummy/sample rows before full migration import.

Preferred test set:

- One normal active product.
- One product with long description near character limit.
- One product with blank LeadTime.
- One product with `LeadTime = 0`.
- One product with `LeadTime = 30`.
- One intentionally unmatched SKU to confirm exception handling catches it before export.
