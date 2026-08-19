# AutoCount migration preflight and manifest runbook

Repository-only preflight for the AutoCount 2.0 migration cutover and import data
contract, implemented against final design lock `DL-XB-140-001-A4`.

## What this is, and what it is not

This tooling **prepares and validates migration evidence**. It does not import
anything.

| It does | It never does |
| --- | --- |
| Bind a frozen source dataset to a manifest by cryptographic hash | Contact AutoCount, in any form |
| Validate an explicit bounded paste range | Open or modify the six vendor `.xls` artifacts |
| Validate qualified source-to-destination field mappings | Read live AutoCount settings or a live AutoCount session |
| Validate externally supplied routing and execution-context evidence | Write to production SQL |
| Report a deterministic, public-safe preflight outcome | Perform, trigger, approve or attest an import |

Production import remains a human/vendor-controlled AutoCount action. The
strongest outcome this tooling can emit is `REPOSITORY_PREFLIGHT_SATISFIED`,
which means only that every repository-checkable gate holds on the supplied
evidence. It is **not** production import approval, and there is deliberately no
`IMPORTED`, `PRODUCTION_READY` or `CUTOVER_COMPLETE` state.

## Files

| Path | Purpose |
| --- | --- |
| `scripts/autocount_migration_preflight_contract.py` | Pure contract library: identities, gates, deterministic result assembly. No I/O. |
| `scripts/autocount_migration_preflight.py` | CLI: loads JSON inputs, runs the gates, writes the result and report. |
| `schemas/autocount_migration_preflight_manifest.schema.json` | Language-neutral manifest contract. |
| `schemas/autocount_migration_routing_evidence.schema.json` | External effective import-time field-routing evidence contract. |
| `schemas/autocount_migration_execution_context_evidence.schema.json` | External actual import execution-context attestation contract. |
| `tests/test_autocount_migration_preflight.py` | Deterministic tests. Synthetic fixtures only. |

## Fail-closed model

Three states are kept distinct on purpose:

- `PASS` - positively evidenced.
- `UNKNOWN` - authority is absent, partial, stale or unverifiable.
- `MISMATCH` - evidence exists and positively contradicts the declaration.

`UNKNOWN` and `MISMATCH` both block. Absence of evidence is never converted into
a negative fact: a missing routing observation is `UNKNOWN`, never `disabled`.

## Gates

| Gate | What must hold |
| --- | --- |
| `source_authority` | Dataset template id and identity, recomputed content hash, row count and control totals all match the manifest. |
| `paste_range` | An explicit bounded range is declared and spans exactly the validated rows; no row falls outside it. Whole-sheet or Select All authority is forbidden. |
| `columns` | Every input column is bound to exactly one declared mapping. Anything else is blocking `UNKNOWN`, never silently dropped. |
| `field_identity` | Every mapping targets the declared surface, using contract-qualified identities on both sides. |
| `routing` | External routing evidence is current and either positively not applicable or completely and correctly mapped. |
| `execution_context` | Both the account-book/company and the import surface/entity are positively attested, current, and match the declaration. |
| `duplicate_item_code_action` | An official action is explicitly selected and evidenced on an ItemCode-keyed surface. |
| `location` | Where `Location` is mapped, an explicitly configured reference set exists and every row falls inside it. |
| `sku_alias` | `PrimarySKU` uniqueness holds and every alias resolves to exactly one current SKU. |
| `quantity` | Unresolved Stock Item fields are unused; zero and negative opening rows are surfaced for disposition. |
| `item_opening_key` | The declared logical key equals the documented grain, and Excel-path `Seq` authority is evidenced. |
| `arap_ordering` | Document and detail continuation ordering matches the declared input ordering contract. |
| `conditional_authority` | Every load-bearing vendor/accountant/owner authority for the surface is explicitly resolved. |

## Qualified field identity

Every identity is written `owner.Field`. There is no unqualified form, so a bare
name such as `AccNo`, `Qty`, `UOM`, `Location`, `ExpiryDate` or `Seq` is refused
structurally rather than by blacklisting known examples.

- `ar_invoice.AccNo`, `ap_invoice.AccNo`, `debtor.AccNo` and `creditor.AccNo` are
  four distinct identities that happen to share a spelling.
- `stock_item.Qty` and `stock_item_opening.Qty` are distinct; neither is ever
  reinterpreted as the other.
- Correspondence between a source column and an AutoCount destination exists
  only through an explicit declared mapping. Nothing here couples to the gated
  member-write subsystem because a field name matches.

## The two external evidence contracts

Both are **external input contracts**. The repository defines and validates them;
it never acquires them, because acquiring them would require touching a live
AutoCount installation.

### Effective import-time field routing

State is exactly one of `enabled`, `disabled`, `unavailable_not_applicable` or
`UNKNOWN`.

| Situation | Outcome |
| --- | --- |
| Evidence absent | `UNKNOWN` / not import ready |
| State `UNKNOWN` | `UNKNOWN` / not import ready |
| Enabled, mapping incomplete | `UNKNOWN` / not import ready |
| Enabled, destination differs from contract | `MISMATCH` / not import ready |
| Positively `disabled` or `unavailable_not_applicable` | Gate passes, subject to every other gate |
| Enabled, complete and matching | Gate passes, subject to every other gate |

Workbook shape, a `New`/`Update` preview, row counts, amount totals and a
previous successful import are not routing proof and have no representation in
the schema.

### Actual import execution context

Two independent dimensions are bound, and neither substitutes for the other:

1. **Account-book/company.** The manifest declares an intended target as a
   one-way non-secret fingerprint (`acctbk_<64 hex>`); the attestation supplies
   the actual fingerprint. The raw company or account-book identity never enters
   the repository.
2. **Import operation/surface/entity.** The manifest declares the intended
   qualified surface; the attestation supplies the actual selected surface.

For each dimension: no evidence or unverifiable evidence is `UNKNOWN`; actual
differing from declared is `MISMATCH`; only a positive current match passes. The
actual selection is never inferred from workbook type, worksheet name, recognised
headers, effective column mapping, a preview, or control totals.

### Freshness

Both evidence documents carry `import_operation_id` and must match the manifest's
value. That is what makes an attestation current to the relevant import
operation: an unchanged source workbook, generated workbook, template or mapping,
or a prior successful import, never turns a previous attestation into standing
authority.

This gate defines and validates the evidence contract only. The future
live evidence-acquisition mechanism is out of scope here.

## Conditional authority register

Load-bearing authorities that remain vendor, accountant or owner owned are
declared explicitly. `RESOLVED` requires both a resolver and a non-secret
evidence reference; anything else, including omission, is blocking `UNKNOWN`. No
guessed default ever converts `UNKNOWN` to `PASS`, and the tooling never promotes
an authority on its own.

Every surface requires: `mandatory_columns`, `update_reimport_semantics`,
`udf_availability_licensing`, `import_api_licensing_applicability`,
`sandbox_test_company_evidence`, `production_import_order`,
`installed_runtime_applicability`.

Additionally by surface:

| Surface | Extra required authorities |
| --- | --- |
| `stock_item` | `stock_item_qty_semantics`, `base_uom_rate_one`, `costing`, `duplicate_item_code_runtime_behaviour` |
| `stock_item_opening` | `item_opening_excel_seq`, `opening_doc_date_mechanism`, `excel_zero_negative_behaviour`, `costing`, `duplicate_item_code_runtime_behaviour` |
| `ar_invoice` | `ar_projno` |
| `ap_invoice` | `anomalous_ap_partial_rows` |

## Locations

Historically observed location codes are not permanent authority and are not
hardcoded anywhere in this tooling. When a dataset maps `Location`, an explicitly
configured reference set, freshly reconciled to the relevant AutoCount setup by
an operator, must be supplied; otherwise the gate fails closed. A row whose
location falls outside the supplied set is rejected. The tooling never contacts
AutoCount to refresh the set.

## Stock Item Opening

The documented logical identity is `ItemCode x UOM x Location x BatchNo x Seq`.
`SerialNo` is not part of that key and is refused if promoted into it. `Seq` is
structurally part of the key, but the Stock Open Balance template exposes no
`Seq`, so Excel-path representation remains a vendor gate and stays fail-closed
until `item_opening_excel_seq` is resolved.

Zero and negative opening quantities are surfaced as blocking findings requiring
explicit disposition. The Excel importer is never assumed to drop zero rows, and
the opening document-date mechanism is never inferred.

## AR/AP

Document and detail continuation ordering is load-bearing. Interleaved document
groups, a detail row preceding its header, a broken input row order, or a
document group with no detail row all block, and the tooling never silently
reorders data into something that looks valid. Debtor and Creditor master data
stay distinct from AR/AP opening documents, and no AR/AP opening is converted to
a GL opening balance. Per-invoice versus lump-sum, Past versus YTD, partly-paid
treatment and account mapping remain external accountant decisions.

## Determinism and public safety

`result["canonical"]` and `result["canonical_hash"]` are equality-critical and
carry no timestamp, run identifier or host path; volatile run metadata is written
under `result["volatile"]` only. Repeating a run over identical input and
identical evidence reproduces byte-identical canonical content.

Findings reference rows by `sheet_row` and records by index, and never echo cell
values, so the result stays public-safe. Manifests and evidence documents are
refused outright if they carry a credential-shaped key.

## Usage

Compute the source content hash for a frozen dataset:

```bash
python scripts/autocount_migration_preflight.py --hash-dataset path/to/dataset.json
```

Run the preflight:

```bash
python scripts/autocount_migration_preflight.py --manifest path/to/manifest.json --dataset path/to/dataset.json --routing-evidence path/to/routing.json --execution-context-evidence path/to/execution_context.json --output-dir C:\XB\autocount_outputs\review\migration_preflight\run-001
```

Exit codes: `0` when every gate passes, `1` when anything blocks, `2` for a usage
or input-contract error. Add `--print-only` to emit the result JSON without
writing files.

Inputs and outputs are ordinary JSON. Keep real datasets outside the repository
under the approved local output convention; only synthetic fixtures belong in
version control.

## Validation

```bash
python -m unittest tests.test_autocount_migration_preflight
```

The schema-validation tests require `jsonschema`; they skip when it is not
installed, matching the existing create-UAT schema tests. No test contacts
AutoCount, production SQL, a network provider, credentials or private business
data.
