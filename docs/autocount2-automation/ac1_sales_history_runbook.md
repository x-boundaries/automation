# AC1 sales history build runbook (#292)

One-off, offline build of a units-only AutoCount 1 (AC1) sales history for the dashboard's Buying tab, so size mix and seasonality can reach back before AC2 go-live (31 Jul 2026).

Script: `scripts/ac1_sales_history_build.py` (stdlib only). Tests: `tests/test_ac1_sales_history_build.py` (synthetic workbooks only).

## Owner decisions (9 Nov 2026)

- AC1 history is used for **size mix and seasonality only**. Stock-based views (stock-outs, zero days, corrected share, sell-through, slow sellers, placement) stay AC2-only from 31 Jul 2026.
- Use as much AC1 data as exists. **Each month comes from one source:** AC1 up to and including **Jul 2026**, AC2 from **Aug 2026**. AC2 has outlet sales from 3 Jul 2026 that are the same sales as AC1's July; AC2's July is outlet-only and misses 1-2 Jul, so AC1's complete July is kept and AC2's July is not added to this series.
- Label: **"AC1 history (channel mix unconfirmed)"**. The AC1 exports have no location, day or channel column, and it is not confirmed whether the AC1 report covered marketplace and wholesale sales as well as the outlet. Show it as its own series; never add it into AC2 per-channel totals.
- A missing month is a **gap** (no data), never zero. A short export is kept and **flagged partial** (default `2025-01`).
- Sold Qty is treated as units **net of returns**, negatives kept as they are (the same rule as AC2 `sold`).
- AC1 item codes that are not AC2 item codes are kept and flagged `item_mapped: false`, so totals stay complete.

## Source files

The AC1 monthly profit-margin report exports, one workbook per month named `YYYY-MM.xlsx`, one sheet, header in row 1 with exactly these columns: Item Code, Description, Desc 2, Item Group, Item Type, UOM, Sold Qty, Subtotal, Total Cost, Profit, Margin(%), Markup(%), Average Subtotal, Average Total Cost. One row per item; the last row is the report's grand total (no item code).

The folder also holds an item list workbook; it does not match `YYYY-MM.xlsx`, so it is ignored and listed by name in the manifest. Its balance column is one undated all-location total and is not used. **Its name must not start with a date:** any file whose name starts `YYYY-M` (for example `2026-11-08 item list.xlsx` or `2026-01.csv`) looks like a month file and stops the build. Keep only month workbooks and the item list workbook in the folder.

**Confidential:** the AC1 folder holds cost, price and margin data. It stays on the VM. Never copy the workbooks into the repo, a ticket, Drive or a chat.

## What is read and what is written

- Only **column A (Item Code)** and **column G (Sold Qty)** of each data row are read. Description, group, type, money and margin cells are skipped while parsing; the shared-strings table is read in a second pass that resolves only the strings column A and the header row use.
- The whole header must match the layout above, or the build stops (a different layout could put something other than Sold Qty in column G).
- The build stops, writing nothing, on: a non-text item code, a blank or repeated code in a month, a non-numeric, infinite or fractional Sold Qty, a code-less row that is not the last row, item rows that do not add up to the grand-total row's Sold Qty, a file name that looks like a month file but is not `YYYY-MM.xlsx` (for example `2026-05 (1).xlsx`, `2026-05.xls`, `2026-13.xlsx`), two files for the same month, an unreadable workbook (including a row or cell with no reference), or an unreadable item index. Error messages give the file, row and column, never a cell value. Any other unexpected input also exits 2 with a `FAILED:` line.
- The last row is the grand total when column A is blank or reads `Total` / `Grand Total`; its Sold Qty must equal the sum of the item rows. Month file names match without regard to case (`2026-05.XLSX` is May 2026). Gaps are listed up to `--last-month`, so a missing final month is a marked gap, never a hole before AC2 starts.
- Codes are trimmed and upper-cased. Mapping flags come from the AC2 exporter's local `stock_item_index.json.gz` (read only, exact code match; AC2 item barcodes are empty, so there is no barcode fallback).

Outputs, in a folder of their own (default `autocount_outputs\ac1_history` in the clone, gitignored). The script refuses a folder inside the AC1 folder and a folder holding the exporter's `last_run_status.json`, so it can never write into the AC2 archive.

| File | Schema | Content |
| --- | --- | --- |
| `ac1_sales_monthly.json.gz` | `xb.ac1.sales_monthly` v1 | `label`, `scope`, `sold_rule`, `use`, `first_month`, `last_month`, `ac2_from_month`, `months`, `gaps`, `partial_months`, `items` (codes), `item_mapped` (per item), `fields` = `item, month, sold` with `columns` (item and month are indexes), `month_units`, `month_items`, `counts`. Gzip with a fixed header time and sorted keys, so the same inputs give the same bytes. |
| `ac1_sales_monthly_manifest.json` | `xb.ac1.sales_monthly_manifest` v1 | `built_at`, settings, months, gaps, `source_files` (name, month, sha256, bytes, item rows, total rows skipped), files ignored or after the last month, `item_index` (as_of, item count, sha256), `counts`, `columns_read`, `data_file` (sha256, bytes). Written last. |

No description, group, brand, cost, price, amount, margin, vendor, customer or document field appears in either file.

## Separation from the AC2 exporter

The build is not part of `scripts/autocount_stock_balance_hourly.py`. It opens no database, does not change the exporter, its settings or the hourly task, and never writes to the AC2 archive, the daily closes or the `analysis_*` files. The exporter's ledger check and the AC3 sales reconcile therefore still see only AC2 data and stay exact.

## Commands

Run off-peak and never between :55 and :05 past the hour (the hourly export runs on the hour), from the clone:

```powershell
python scripts\ac1_sales_history_build.py --ac1-dir "<AC1 export folder>" --item-index autocount_outputs\stock_balance_hourly\stock_item_index.json.gz
```

Options: `--out-dir` (default above), `--last-month YYYY-MM` (default `2026-07`), `--partial-months` (comma list, default `2025-01`; `""` for none). Exit code 0 = written, 2 = stopped with a `FAILED:` line and nothing new written. The console shows months, gaps, partial months, item and row counts and the data file hash.

Rebuild after a re-export (for example a missing or partial month): drop the new workbook into the AC1 folder and run the command again. The data file is written first and the manifest last, each replaced atomically and checked after writing.

## Publish to the Drive folder (manual, owner OK each time)

The script refuses to write into the Drive folder by design, so every publish is a manual copy. First publish: 9 Nov 2026, with the owner's OK. Each later publish needs its own owner OK.

1. Check the time: never between :55 and :05 past the hour (the hourly export runs on the hour).
2. Run the build (above) and keep its console output. It must end with `OK:` and print the data sha256.
3. Copy `ac1_sales_monthly.json.gz` **first** into the top level of the Drive folder `Report/AC2 Stock Balance (daily)`, replacing the old copy.
4. Copy `ac1_sales_monthly_manifest.json` **last** into the same place. The page checks the manifest's `data_file.sha256` and `bytes` against the data file, so the manifest must never arrive before its data.
5. Check: the SHA-256 of the copied gz (`Get-FileHash -Algorithm SHA256 <Drive copy>`) equals the manifest's `data_file.sha256`, and the byte size equals `data_file.bytes`.
6. Touch nothing else in the folder: no other file is added, renamed or removed, and no `.ac1_*.tmp` file is left behind. The exporter's files keep their normal hourly or nightly modified times.
7. Record the time, sha256 and bytes in the private mailbox (never in this repository).

## Not in this step (each needs its own owner OK)

- The dashboard reads the file in Buying > History (dashboard Version 9 and later; the page source is a claude.ai artifact, with copies in the private mailbox).
- Re-exporting AC1 months (2024-12 is missing; 2025-01 looks partial), or an AC1 export by location that would settle the channel question.
