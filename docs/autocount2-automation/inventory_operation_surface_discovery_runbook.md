# Inventory Operation Surface Discovery Runbook

## Purpose

Use this metadata-only discovery step to shortlist AutoCount 2.0 SQL tables and
views that may support the current inventory intelligence objective:

- GRN / goods receiving.
- Stock transfer / inter-location movement.
- Stock location / location master.
- Richer item/product attributes.
- Stock movement document type semantics.
- Supplier lead-time or purchasing context.
- Outstanding PO / stock-in-transit support beyond the already confirmed
  `PO`, `PODTL`, and `vPurchaseOrder` surfaces.

The script reads SQL catalog metadata only: object names, schema names, object
types, column names, data types, and metadata row counts from SQL Server
partition stats. It does not query raw ERP/business rows, export CSV data,
schedule extraction, write back to AutoCount, or revive CoA/GL/bank/accounting
migration scope.

Every candidate remains `Needs reconciliation`, current data remains
`immature_pre_go_live`, business reconciliation remains `not_reconciled`, and
`final_production_selected` remains `false`.

## Confirmed And Unresolved Surfaces

Already confirmed selected-surface snapshot coverage:

- `PO`
- `PODTL`
- `vPurchaseOrder`
- `Debtor`
- `vDebtor`
- `Creditor`
- `vCreditor`
- `PaymentMethod`
- `ARInvoice`
- `APInvoice`

Stock smoke extraction already covers stock master, stock balance, and stock
movement candidate surfaces, but those still need reconciliation before final
dashboard use.

Unresolved inventory-operation areas:

- GRN / goods receiving.
- Stock transfer / inter-location movement.
- Stock location / location master. `Branch` and `vBranch` returned 0 rows in
  prior review, so stock locations may live elsewhere.
- Richer item/product attributes such as barcode, UOM, brand, category, class,
  group, and supplier attributes.
- Movement document type semantics such as document type, source type, module,
  reference, in/out quantity, and balance quantity.
- Supplier lead-time / ETA / delivery signal, if available.
- Outstanding PO and stock-in-transit support beyond the current PO surfaces.

CoA, GL opening balances, bank opening balances, full accounting cutover,
finance migration ownership, scheduler, direct AC2 write-back, and replacing
AC2 accounting are parked.

## Local Config

Copy the secret-free example to the ignored local path:

```powershell
Copy-Item config\autocount_inventory_operation_discovery.example.json config\autocount_inventory_operation_discovery.local.json
```

The example config expects the confirmed AC2 target:

- Server: `localhost\A2006`
- Database: `AED_XBOUNDARIES`
- SQL login/user: `xb_ac2_readonly`

It rejects the legacy/wrong targets `localhost\SQLEXPRESS / AED_XBoundaries`
and `localhost\SQLEXPRESS / A893478`.

Generated outputs stay under:

```text
C:\XB\autocount_outputs\probe\inventory_operations
```

Do not commit generated outputs or `.local` config files.

## Local PowerShell Command

Run this on the Windows AC2 VM. It sets the connection string only in the
current PowerShell process and does not print the password.

```powershell
$password = Read-Host 'Password for xb_ac2_readonly on localhost\A2006 / AED_XBOUNDARIES' -AsSecureString
$plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
)
$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = "Driver={ODBC Driver 17 for SQL Server};Server=localhost\A2006;Database=AED_XBOUNDARIES;UID=xb_ac2_readonly;PWD=$plainPassword;Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly;"
try {
  python scripts\autocount_inventory_operation_discovery.py --config config\autocount_inventory_operation_discovery.local.json
} finally {
  Remove-Item Env:\AUTOCOUNT_READONLY_SQL_CONNECTION_STRING -ErrorAction SilentlyContinue
  Remove-Variable plainPassword -ErrorAction SilentlyContinue
  Remove-Variable password -ErrorAction SilentlyContinue
}
```

If the VM does not have `python` on `PATH`, replace `python` with the full
Python executable path used for the other AutoCount scripts.

If metadata row counts are blocked by the SQL login, rerun with:

```powershell
python scripts\autocount_inventory_operation_discovery.py --config config\autocount_inventory_operation_discovery.local.json --no-row-counts
```

If object and column metadata succeeds but row counts fail because
`xb_ac2_readonly` lacks `VIEW DATABASE STATE`, the run status is
`success_with_warnings` and the manifest includes the warning
`row_counts_unavailable_permission_denied`. This means the candidate metadata
was still generated safely. Use `--no-row-counts` for a clean metadata-only run
that skips partition-stat row counts.

## Output Files

Each run writes a timestamped local folder containing:

- `inventory_operation_discovery_manifest.json`
- `inventory_operation_discovery_report.md`

The manifest and report include only safe metadata summaries:

- object name,
- schema name,
- object type,
- metadata row count when available,
- matched keyword family,
- matched column names,
- deterministic score,
- reason codes.

They do not include raw ERP rows, raw CSV contents, screenshots, connection
strings, local config values, or credentials.

## Safe Manifest Summary Command

Use this after a local run to summarize only manifest metadata. It does not
print CSV contents or raw ERP rows.

```powershell
$runPath = 'C:\XB\autocount_outputs\probe\inventory_operations\inventory_operation_discovery_YYYYMMDD_HHMMSS_RUNID'
$manifest = Get-Content -Raw -Path (Join-Path $runPath 'inventory_operation_discovery_manifest.json') | ConvertFrom-Json

[pscustomobject]@{
  run_path = $manifest.storage.run_path
  status = $manifest.status
  exception_count = @($manifest.exceptions).Count
  data_maturity = $manifest.data_maturity
  business_reconciliation_status = $manifest.business_reconciliation_status
  row_count_records = $manifest.counts.row_count_records
  total_candidates = $manifest.candidate_summary.total_candidates
} | Format-List

'Candidate counts by family:'
$manifest.candidate_summary.counts_by_family.PSObject.Properties |
  Sort-Object Name |
  Select-Object @{Name='family';Expression={$_.Name}}, @{Name='candidate_count';Expression={$_.Value}} |
  Format-Table -AutoSize

'Top candidates by family:'
$manifest.candidates_by_family.PSObject.Properties | ForEach-Object {
  $family = $_.Name
  $_.Value |
    Select-Object @{Name='family';Expression={$family}}, rank, object_id, score, row_count,
      @{Name='matched_columns';Expression={($_.matched_column_names -join ', ')}},
      @{Name='reason_codes';Expression={($_.reason_codes -join ', ')}} |
    Format-Table -AutoSize
}

'Warnings:'
$manifest.warnings
```

## Safe To Paste Back

After reviewing for accidental secrets, these are generally safe to paste into a
PR or chat:

- candidate object names,
- schema names,
- object types,
- metadata row counts,
- matched keyword families,
- matched column names,
- scores and reason codes,
- exception summaries with secrets redacted.

Do not paste raw ERP/business rows, raw CSVs, screenshots, connection strings,
passwords, local `.local` configs, or database backups.

## How To Use The Result

Use the shortlist to choose the next extraction-profile review targets:

1. Review high-scoring GRN and transfer candidates with AutoCount UI/report
   paths and Ingenious/Mike.
2. Confirm whether stock locations are represented by `Location`, `Branch`,
   `vBranch`, warehouse tables, or another stock-specific location table.
3. Confirm item/UOM/barcode/brand/category/class/group fields before expanding
   dashboard dimensions.
4. Confirm stock movement document type semantics before dashboard trend logic
   depends on document classification.
5. Confirm supplier lead-time, delivery, ETA, or purchasing context before PR
   recommendation logic uses it.
6. Update the next safe extraction profile only after reconciliation evidence is
   documented.

This discovery output is evidence for review, not extraction approval.
