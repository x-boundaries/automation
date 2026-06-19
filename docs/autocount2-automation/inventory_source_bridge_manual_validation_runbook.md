# Inventory Source Bridge Manual Validation Runbook

Use this after the inventory source bridge evidence review has produced a
metadata-only packet. This runbook guides the operator through manual validation
before any future staging mapping patch.

This document does not select final mappings. It does not change extraction,
staging builds, scheduler behavior, database loads, dashboards, KPIs, purchase
recommendations, analytics joins, write-back behavior, or reconciliation state.

## Inputs

Start from the latest bridge evidence review run:

```text
C:\XB\autocount_outputs\review\inventory_source_bridge_evidence_review\<run-folder>
```

Required files:

- `inventory_source_bridge_evidence_review_manifest.json`
- `inventory_source_bridge_evidence_review_report.md`

The evidence review must still report:

```text
decision = Needs reconciliation
business_reconciliation_status = not_reconciled
data_maturity = immature_pre_go_live
final_production_selected = false
recommendation = no_dashboard_until_schema_gap_resolved
```

Stop if the evidence review says a final production mapping was selected, a
dashboard is ready, or reconciliation is complete.

## Safety Rules

- Record only metadata findings, screenshots with values redacted, and
  operator decisions.
- Do not commit or paste raw ERP/business row values, document numbers, item
  codes, supplier names, prices, costs, quantities, credentials, or connection
  strings.
- Do not run write-back, import, scheduler, dashboard, KPI, analytics join, DB
  load, or staging build commands as part of this validation.
- Do not patch staging mappings until every selected bridge path and quantity
  source has been manually confirmed.

## Bridge Validation Checklist

Validate each candidate independently. Use AC2 metadata, read-only catalog
inspection, application documentation, or operator knowledge. If a raw row must
be viewed locally during validation, keep it local and do not copy values into
GitHub, reports, docs, commits, or chat.

For each candidate, confirm:

- Detail surface exists.
- Header surface exists.
- `detail.DocKey` exists on the detail surface.
- `header.DocKey` exists on the header surface.
- `header.DocNo` exists on the header surface.
- `detail.DocKey -> header.DocKey` is the intended relationship for this
  transaction family.
- The resulting `header.DocNo` is the correct document number for the dependent
  staging field.
- The detail surface grain matches the intended staging line grain.
- The bridge does not require a final mapping decision beyond the documented
  candidate path.

Record one of these outcomes:

- `confirmed_for_future_patch`
- `rejected_by_manual_validation`
- `needs_more_source_documentation`
- `needs_read_only_operator_review`

Do not use any outcome as production approval by itself.

## Candidate Checklist

### GRN: vGoodsReceivedNote Detail

Candidate path:

```text
dbo.vGoodsReceivedNoteDetail.DocKey -> dbo.vGoodsReceivedNote.DocKey -> dbo.vGoodsReceivedNote.DocNo
```

Dependent staging field:

```text
grn_doc_no
```

Manual checks:

- Confirm `dbo.vGoodsReceivedNoteDetail` is the intended GRN line/detail source.
- Confirm `dbo.vGoodsReceivedNote` is the matching GRN header source.
- Confirm `dbo.vGoodsReceivedNote.DocNo` is the document number expected in
  `grn_doc_no`.
- Confirm detail rows using this bridge do not duplicate or omit the intended
  staging line grain.
- Record whether this candidate should be preferred, rejected, or held pending
  more documentation.

### GR / GRDTL

Candidate path:

```text
dbo.GRDTL.DocKey -> dbo.GR.DocKey -> dbo.GR.DocNo
```

Dependent staging field:

```text
grn_doc_no
```

Manual checks:

- Confirm `dbo.GRDTL` is a valid GRN detail source for the same business event
  as the staging GRN line.
- Confirm `dbo.GR` is the matching header source.
- Confirm `dbo.GR.DocNo` is the document number expected in `grn_doc_no`.
- Confirm this path is not a legacy, alternate, duplicate, or lower-quality
  surface compared with `dbo.vGoodsReceivedNoteDetail` and
  `dbo.vGoodsReceivedNote`.
- Record whether this candidate should be preferred, rejected, or held pending
  more documentation.

### Stock Receive

Candidate path:

```text
dbo.vStockReceiveDetail.DocKey -> dbo.vStockReceive.DocKey -> dbo.vStockReceive.DocNo
```

Dependent staging field:

```text
receive_doc_no
```

Manual checks:

- Confirm `dbo.vStockReceiveDetail` is the intended stock receive detail source.
- Confirm `dbo.vStockReceive` is the matching stock receive header source.
- Confirm `dbo.vStockReceive.DocNo` is the document number expected in
  `receive_doc_no`.
- Confirm the bridge preserves the intended stock receive line grain.
- Record whether this candidate is confirmed, rejected, or held pending more
  documentation.

### Stock Transfer

Candidate path:

```text
dbo.vStockTransferDetail.DocKey -> dbo.vStockTransfer.DocKey -> dbo.vStockTransfer.DocNo
```

Dependent staging field:

```text
transfer_doc_no
```

Manual checks:

- Confirm `dbo.vStockTransferDetail` is the intended stock transfer detail
  source.
- Confirm `dbo.vStockTransfer` is the matching stock transfer header source.
- Confirm `dbo.vStockTransfer.DocNo` is the document number expected in
  `transfer_doc_no`.
- Confirm the bridge preserves the intended stock transfer line grain.
- Record whether this candidate is confirmed, rejected, or held pending more
  documentation.

## GRN Candidate Preference Criteria

Two GRN bridge families are available:

- `dbo.vGoodsReceivedNoteDetail + dbo.vGoodsReceivedNote`
- `dbo.GRDTL + dbo.GR`

Do not select automatically. Prefer a candidate only after manual validation
answers these questions:

- Which detail surface is the documented or operator-recognized source for GRN
  line staging?
- Which header surface is the documented or operator-recognized source for the
  document number?
- Which pair has the clearest `DocKey` relationship with no extra transform?
- Which pair best matches the staging grain and expected dependent field
  `grn_doc_no`?
- Which pair is less likely to be a compatibility, legacy, denormalized, or
  duplicate surface?
- Which pair has the clearest future support story if the mapping needs to be
  audited later?

If both are valid, document the reason for preferring one. If neither can be
confirmed, keep both parked and do not patch staging mappings.

## Quantity Validation Checklist

Quantity mapping must be validated separately from the `DocNo` bridge. Do not
infer quantity semantics from column names alone.

### Direct SmallestQty Candidates

For `dbo.vGoodsReceivedNoteDetail` and `dbo.GRDTL`:

- Confirm `SmallestQty` exists on the selected detail source.
- Confirm `SmallestQty` is expressed in the intended base or smallest unit for
  the staging field.
- Confirm sign conventions, unit conversion, and transaction direction match
  the staging expectation.
- Use direct `SmallestQty` only after this manual validation is recorded.

### Possible Quantity Aliases

For `dbo.vStockReceiveDetail`:

- Do not automatically map `Qty` to `SmallestQty`.
- Do not automatically map `BatchBalQty` to `SmallestQty`.
- Validate whether `Qty` is transaction quantity, display quantity, base
  quantity, remaining quantity, or another semantic.
- Validate whether `BatchBalQty` is a balance quantity rather than transaction
  quantity.
- Patch staging only after a human confirms the intended source column.

For `dbo.vStockTransferDetail`:

- Do not automatically map `Qty` to `SmallestQty`.
- Validate whether `Qty` is transfer quantity, display quantity, base quantity,
  or another semantic.
- Patch staging only after a human confirms the intended source column.

Record one of these outcomes for each quantity source:

- `confirmed_direct_smallestqty`
- `confirmed_alias_for_future_patch`
- `rejected_alias`
- `needs_more_source_documentation`
- `needs_read_only_operator_review`

## BatchBalQty Review

Keep `dbo.vGoodsReceivedNoteSubDetail.BatchBalQty` parked unless a dependent
staging field appears.

Current expected status:

```text
staging_mapping_status = not_required_without_dependent_staging_field
```

Do not treat this expected column as a required staging mapping unless a future
schema gap review identifies an explicit dependent staging field.

## Operator Decision Record

Use this table in a local note or PR description after redacting any business
values. Leave undecided items blank rather than guessing.

| Area | Candidate | Decision | Evidence type | Notes |
| --- | --- | --- | --- | --- |
| GRN bridge | `vGoodsReceivedNoteDetail -> vGoodsReceivedNote` |  | metadata/operator/docs |  |
| GRN bridge | `GRDTL -> GR` |  | metadata/operator/docs |  |
| Stock receive bridge | `vStockReceiveDetail -> vStockReceive` |  | metadata/operator/docs |  |
| Stock transfer bridge | `vStockTransferDetail -> vStockTransfer` |  | metadata/operator/docs |  |
| GRN quantity | `vGoodsReceivedNoteDetail.SmallestQty` |  | metadata/operator/docs |  |
| GR quantity | `GRDTL.SmallestQty` |  | metadata/operator/docs |  |
| Stock receive quantity | `vStockReceiveDetail Qty/BatchBalQty` |  | metadata/operator/docs |  |
| Stock transfer quantity | `vStockTransferDetail Qty` |  | metadata/operator/docs |  |
| GRN subdetail expected column | `vGoodsReceivedNoteSubDetail.BatchBalQty` | parked | metadata | No dependent staging field |

## Go/No-Go Checklist Before Future Staging Patch

Do not patch staging mappings unless every required item is checked:

- All selected bridge paths are manually confirmed.
- GRN candidate preference is documented if one GRN bridge family is selected.
- Quantity source column semantics are manually confirmed.
- `BatchBalQty` remains parked unless a dependent staging field appears.
- No raw business values are committed, pasted, or attached.
- `recommendation` is still `no_dashboard_until_schema_gap_resolved`.
- `business_reconciliation_status` is still `not_reconciled`.
- `final_production_selected` is still `false`.
- The future patch changes only staging mapping logic needed by the validated
  manual decision.
- No dashboard, KPI, purchase recommendation, analytics join, DB load,
  scheduler, write-back, extraction behavior, or final production selection is
  introduced by the staging patch.

If any item is unchecked, the decision is no-go. Keep the bridge evidence and
manual validation notes as diagnostic material only.

## Next Step

After manual validation, a future PR may patch staging mappings for confirmed
bridge and quantity paths only. That future PR must keep dashboard readiness
blocked until business reconciliation and production selection are explicitly
completed in a separate controlled step.
