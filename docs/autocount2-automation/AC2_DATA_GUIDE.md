# AC2 data guide: what AutoCount data exists, where, and how to use it

For people, and for their Claude Code or other AI agents. Point your agent at this file (or install it as a skill, see [Use it as a Claude Code skill](#use-it-as-a-claude-code-skill)) before you ask it about stock, sales, buying, purchasing, or what customers owe us and what we owe suppliers.

A copy of this guide lives at the top of the Drive `Automation/Report` folder as `README - for AI agents.md`. The repository version (`docs/autocount2-automation/AC2_DATA_GUIDE.md` in the automation repository) is the source; if the two differ, the repository wins. A PR that changes AC2 data (files, fields, schedules or switches) updates this guide in the same PR, and the Drive copy is updated in place after it merges (it is kept, never replaced: it may carry private detail this public file cannot).

**Status (22 Nov 2026):** hourly stock balance, daily close, buying analysis, the purchasing file and AR/AP aging are all live. The purchasing file has been on since 22 Nov 2026 (see [section 4](#4-purchasing-file-292)).

Every example value in this guide is fake (`FAKE-0201`, `LOC-A`, `C-001`, `Example Vendor`). This repository is public: never add real figures, names, codes or folder IDs to it.

## Start here

1. **The data is files in Google Drive**, written by scheduled jobs on the AutoCount 2 (AC2) server. Nothing here talks to AutoCount directly, and nothing here may write to AutoCount.
2. **All of it is under `My Drive/Automation/Report`** in the owner's Drive, in two folders:
   - `AC2 Stock Balance (daily)`: stock, sales units, buying analysis, purchasing units, and the one-off AC1 sales history;
   - `AC2 ARAP Aging (latest)`: what customers owe us (AR) and what we owe suppliers (AP).
3. **You can only read what has been shared with you.** The owner shares the folders with the staff who need them. If your agent cannot see a folder, ask the owner; do not ask anyone to make it public. See [Access](#access).
4. **Check freshness first, every time,** from the published data's own as-of time in the folder's `latest_manifest.json`. `last_run_status.json` only explains a failed or undelivered run. See [Rules for reading](#rules-for-reading).
5. **It is confidential.** Stock, sales, vendor and AR/AP data is company business data. Keep it inside company tools. See [Privacy](#privacy).

## Index of extracts

| Extract | Drive folder | Updates | Main files | Answers |
| --- | --- | --- | --- | --- |
| [Hourly stock balance](#1-hourly-stock-balance-245) (#245) | `AC2 Stock Balance (daily)` | Every hour at :00 | `stock_balance_latest.csv`, `stock_item_index.json.gz`, `stock_dashboard_feed.json.gz` | How many units, where, right now; negatives; low stock |
| [Daily close](#2-daily-close-history-245) (#245) | same, `daily/<YYYY-MM>/` | Once a day, just after midnight | `stock_close_<YYYY-MM-DD>.csv` | Stock at the end of any day since 1 Aug 2026 |
| [Buying analysis](#3-buying-analysis-files-292) (#292) | same, top level | Nightly, just after midnight | `analysis_*.json.gz`, `analysis_manifest.json` | Units sold by item, outlet, day and channel; stock-outs; placement gaps; size mix; slow sellers |
| [Purchasing file](#4-purchasing-file-292) (#292) | same, top level | Nightly, with the buying files, **only if switched on** | `analysis_purchasing.json.gz` | Open PO units, goods received, vendor lead days |
| [AC1 sales history](#5-ac1-sales-history-one-off-292) (#292) | same, top level | One-off; republished by hand | `ac1_sales_monthly.json.gz` | Units sold per item per month before AC2 go-live |
| [AR/AP aging](#6-arap-aging-321) (#321) | `AC2 ARAP Aging (latest)` | Every hour at :30 | `ar_*_latest.csv`, `ap_*_latest.csv`, `arap_dashboard_feed.json.gz` | Who owes what, how overdue, what is due to suppliers |

**Units only, except AR/AP.** The stock and buying files hold quantities only: no cost, price, sales value, margin or customer data. Money appears only in the AR/AP files.

Full technical detail lives in the runbooks; this guide summarises them:

- [Hourly stock balance runbook](stock_balance_hourly_runbook.md) (stock, daily close, feed, item index, buying analysis, purchasing);
- [AC1 sales history runbook](ac1_sales_history_runbook.md);
- [AR/AP aging runbook](arap_aging_runbook.md).

## How an agent should read the files

### Getting at the files

- **Google Drive connector** (for example Claude's Google Drive connector): search for the folder by its name (`AC2 Stock Balance (daily)` or `AC2 ARAP Aging (latest)`), then for the file by its exact name inside it. Find files by name on every read and never reuse a file ID from an earlier chat: the jobs replace the files every hour. If more than one copy turns up, take the newest by modified time and say so. Claude's connector returns a downloaded file's content as base64: decode it first (then gunzip a `.json.gz`).
- **Google Drive for desktop** on your own PC: the same folders appear as local files. The local path depends on how the folder was shared with you (a shortcut in your My Drive, or under "Shared with me"); find it by folder name.
- **`.json.gz` files are gzip-compressed JSON.** Download them and decompress (for example Python `gzip.open(path, "rt", encoding="utf-8")` then `json.load`). If your tool can only read text and cannot decompress gzip, use the CSV files instead, or the local Drive for desktop copy.
- **CSV files** are UTF-8 with a header row. The stock CSVs have no byte-order mark; the AR/AP CSVs have one (read them as `utf-8-sig`, or strip the mark from the first header name). Text cells that started with `=`, `+`, `-` or `@` were made safe with a leading quote (`'`). That includes codes: an item code or location that starts with one of those characters appears with a leading `'`, so allow for it in exact-match lookups. Numbers are plain numbers (negative quantities stay negative). The JSON files are not escaped.

### Rules for reading

1. **Freshness first: judge it from the published data, not from the status file.**
   - The data's own as-of time is in the folder's `latest_manifest.json`: `as_of_local` in the stock folder (also the first column of `stock_balance_latest.csv`), `as_at_local` in the AR/AP folder (also in the feed and in every AR/AP CSV). Note the different spelling.
   - The data is **stale** when that time is more than 70 minutes before now. This is the same rule the jobs' own `--check-latest` check uses, and the feeds carry it as `stale_after_minutes`. Say the data is stale, and give its as-of time.
   - The feed, the item index and the analysis files are built in separate steps and can lag the latest CSV if one of them fails. When you answer from one of them, quote that file's own `as_of_local` (analysis files: `built_from_close_day`).
   - Then read `last_run_status.json` to explain why. It describes the latest **attempt**: `status` (`success` or `failed`), `failure_class`, `delivery_status`, `finished_at` and `next_run_local`. Its own `as_of_local` / `as_at_local` is the time that attempt read AutoCount (blank if it failed before reading). The time is set before the later checks run, so a failed attempt can show the current hour over old data. **Never quote it as the data's as-of time.** Neither status file has `stale_after_minutes`.
   - Drive may still hold an **older set** when `status` is `failed`, or when `delivery_status` is anything other than `handed_to_drive_sync_folder` (`copy_failed` or `verify_failed`; a failed run shows `skipped_failed_run`). Two real cases:
     - **AR/AP paused.** After the first supplier payment is knocked off against an invoice, every hourly AR/AP run fails with `failure_class: unknown_knockoff_target`. The status file then shows a recent time every hour, while the data stays at its last good hour.
     - **Stock copy failed.** A stock run can be `status: success` with `delivery_status: copy_failed`. The server built a new set, but Drive still holds the previous one.
   - A failed stock or AR/AP run also emails the operations inbox (`[XB alert] AC2-STOCK` or `AC2-ARAP`), and the daily health check reports a job that stopped running. You do not need to report a stale file you notice; say it is stale in your answer.
2. **Quote the data's as-of time** (rule 1) with every answer, and say which folder it came from.
3. **Read fields by name, never by position.** The item index, the analysis files and the purchasing file are compact: rows are arrays, and each file carries its own field list (`fields`, `item_fields`, `stock_fields`, `po_fields` and so on). Look the position up from that list every time; new fields are appended in later versions. The other JSON is ordinary named keys: the manifests and status files are plain objects, and the rows of the stock feed (`negatives`, `low_stock`, `movers`) and of the AR/AP feed (`parties`, `docs`) are named-key objects.
4. **Decode indexes.** Many values are indexes into a dictionary in the same file (`items`, `locations`, `groups`, `channels`, `vendors`, `brands`). Days are often offsets from `day0` or `history_from`.
5. **Prefer the files over the dashboards.** The owner's dashboard and AR/AP tracker are private pages built on these same files. Answer from the CSVs and JSON files, not from a screenshot or a page.
6. **Prefer the CSV or JSON that answers the question directly** (the [example questions](#example-questions) say which). Recompute only when you must, and say how you computed it.
7. **Never invent.** If a field is not in the file, or the data does not cover the period asked, say so. Do not fill gaps with zero (a missing AC1 month is a gap, not zero sales).
8. **Never write.** Do not edit, rename, move or delete anything in these folders; the jobs and the dashboard depend on exact names and hashes. Work on a copy.
9. **Keep it private.** Do not paste rows into public places. Summarise; show only what the person asking needs.

## 1. Hourly stock balance (#245)

**Answers:** how many units of an item are at each outlet or store right now; where an item can be found; which items are negative or low.

**Where:** `My Drive/Automation/Report/AC2 Stock Balance (daily)`, top level.

**Updates:** every hour at :00, 24 hours a day. Each run reads AutoCount's own stored stock balance and proves it against the stock ledger before publishing. If they disagree, nothing new is published that hour.

**Freshness:** the data time is `as_of_local` in `latest_manifest.json` (and the CSV's first column); the data is stale when it is more than 70 minutes old. `last_run_status.json` only explains a failed or undelivered run: its own `as_of_local` is the latest attempt's read time, not the data's. See [Rules for reading](#rules-for-reading).

| File | What it is |
| --- | --- |
| `stock_balance_latest.csv` | One row per item, location and unit. Default columns: `as_of_local, ItemCode, ItemDescription, ItemType, Location, LocationLabel, UOM, BalQty, IsActive, StockControl, RowSource`. `BalQty` = units on hand. `RowSource` is `balance` for a real balance row, or `no_balance_row` for an active stock item that has never had stock anywhere (Location blank, label `(no balance row)`, BalQty 0). Zero rows of active items are kept. |
| `latest_manifest.json` | Facts about that snapshot: `as_of_local`, `run_id`, `row_count`, per-location totals (`per_location`: rows, non_zero, negative, sum_qty), `large_balances`, `pos_sync` (last POS sync time per location), `warnings`. No item rows except the large-balance list. |
| `stock_item_index.json.gz` | The best file for item questions. `items` (by `item_fields`: `code, description, barcode, group, brand, uom, active, reorder_level, min_qty`, plus, when buying analysis is on, `category, size, size_raw, size_order, size_class, length, gender, colour, style_code, style_name, excluded`); `stock` (by `stock_fields`: `item, location, qty, last_movement`); `history` (close changes over the last 90 days); `locations` (codes only). |
| `stock_dashboard_feed.json.gz` | A small summary: `locations` (code and **label**), `latest` per location (units, SKUs in stock, zero, negative, low stock), `history` per location per day, `movers` (biggest risers and fallers over 1, 7 and 30 days), `negatives` (with `negative_since`), `low_stock`, `anomalies` (counts only). |
| `last_run_status.json` | The latest **attempt**, not the published data; see [Rules for reading](#rules-for-reading). `status`, `failure_class`, `delivery_status`, `finished_at`, `next_run_local`, and also `large_balance_count`, `feed_status`, `item_index_status`, `daily_close_status`, `analysis_status`. |

**Traps:**

- **Location labels.** The item index lists location **codes** only; take the human label from the feed's `locations` (or `LocationLabel` in the CSV). An unmapped code shows as `code - description`.
- **Large-balance flag.** A row with `|BalQty|` above 100,000 is listed in `latest_manifest.json` under `large_balances` and counted in `large_balance_count`. It is usually a keying error in AutoCount (for example an item code typed into a quantity field), exported as AutoCount holds it. Do not add such rows into totals without saying so.
- **Negative stock is real data.** AutoCount allows it (sold before received, or a transfer keyed late). The feed's `negative_since` says how long.
- **Non-stock items are left out** (`StockControl = F`, for example marketplace fee codes). **Inactive items** with zero stock are dropped; inactive items that still hold stock are kept (`IsActive` = `F`).
- **`Default Item`** (AutoCount's placeholder item) is marked `excluded` in the item index when buying analysis is on. The CSV and feed still include it, so they match AutoCount; leave it out of item-level answers.
- **Low stock** in the feed counts **active** items only, with one global rule: 0 < qty <= `low_stock.threshold` (a server setting, default 2). The item index carries each item's `reorder_level` and `min_qty` (each null when 0 or not set) and the global figure as `low_stock_default_threshold`; the dashboard's rule is "reorder level, else minimum quantity, else the default", applied per outlet.
- **Consignment stock balances are not read.** Online marketplace stock figures may include consignment and so may differ from this export (an open owner question).
- **List caps.** The feed's `negatives` and `low_stock` lists stop at 200 rows (`truncated: true`); their `count` is always complete. Use the item index for complete lists.
- **One unit per item.** The job fails rather than mix units of measure, so `BalQty` is always in the item's base unit.

## 2. Daily close history (#245)

**Answers:** how much stock was at each location at the end of a given day.

**Where:** the same folder, `daily/<YYYY-MM>/stock_close_<YYYY-MM-DD>.csv`, with a `.manifest.json` beside each. Drive keeps every day from **1 Aug 2026** (the first day after the opening stock of 31 Jul 2026).

**Updates:** the first successful run after midnight writes the close of the day before. If the server was off, the next run catches up.

**Columns:** `close_date, ItemCode, ItemDescription, ItemType, Location, LocationLabel, UOM, CloseQty, IsActive, StockControl`. **Non-zero closes only:** an item and location missing from a day's file closed at 0 that day.

**Traps:**

- **Close = end of day** (everything dated before the next midnight), summed from the very first ledger row.
- **Recent days can still change.** A close is provisional for 7 days. If someone back-dates a stock document, the next close run rewrites **every** day from that document's date to yesterday that is still inside the 7-day window, not only the document's own day (`write_reason: revised`, `revision` + 1 in each day's manifest). From `final_from` on, a day's file is final in nightly runs. `write_reason` is `new` for a day's first file and `rebuild` when the whole history was rebuilt (a backfill or lost state); a rebuild can rewrite days past `final_from`.
- **Late changes.** A change older than 7 days is **not** written back to the old file. The feed and item index show the corrected figure, so an old daily file can differ slightly from today's rebuilt truth. The late change is flagged only in the run that finds it (`late_change_day_count` in that run's status file and feed); the old day's manifest is not marked.
- Descriptions are the item master's **current** text, not the text on that day.

## 3. Buying analysis files (#292)

**Answers:** units sold by item, outlet, day and sales channel; how long items were out of stock; outlets with zero stock while another location holds it; size mix of sales against stock; items that sold out early; slow sellers.

**Where:** the same folder, top level, beside the stock files.

**Updates:** nightly, built by the first run after midnight, from data **up to yesterday** (never today). If the build fails, the previous set stays and the next hourly run retries. `analysis_manifest.json` says which close day the set was built from (`built_from_close_day`) and is written last; `last_run_status.json` shows `analysis_status` (`published`, `not_due`, `failed`, `skipped_failed_run` when the hourly run itself failed, or `disabled`).

**Check it is switched on.** Analysis is off by default in the code; it is switched on in the live export (22 Nov 2026). Use these files only when `analysis_status` is not `disabled` and `analysis_manifest.json` exists; otherwise say buying analysis is not available.

**Units only.** No price, cost, sales value, customer, or document numbers.

| File | What it is |
| --- | --- |
| `analysis_sales_daily.json.gz` | Column arrays (`columns`) over `fields` = `item, location, day, channel, sold, returned, group`. `item`, `location`, `channel`, `group` are indexes into `items`, `locations`, `channels`, `groups`; `day` is an offset from `day0`. `sold` = units out **net of returns** (can be negative); `returned` = units back. |
| `analysis_stock_state.json.gz` | Per item and stock **pool** (a configured group of locations, for example the retail network): `first_stock_day`, `zero_intervals` (day ranges with zero or negative stock after it first held stock), `qty_now` (yesterday's close), `opening_at_golive`, `received` per month. |
| `analysis_placement.json.gz` | One row per configured pair, zero location and item, last 90 days: days the zero location (an outlet) had none while the pair's holder locations together held at least 2 (`zero_days_30`, `zero_days_90`), whether it is still the case (`open_now`), and units sold at the zero location (`sold_at_zero_loc_90`). A holder can be any location named in the pair, for example a wholesale store. The complete list. |
| `analysis_highlights.json.gz` | Ready-made rule results (`lines`): `sold_out_early`, `open_placement`, `size_drift`, `slow_seller`. Each line has `rule`, `section`, `group`, `brand`, `item` and `inputs` (the figures behind it). See the highlights trap below for scope and caps. |
| `analysis_manifest.json` | The set's index: `built_from_close_day`, `files` (with sha256 and counts), `windows_hint` (go-live, stock take, sales and placement windows), `sold_units_by_channel`, `size_coverage`, `groups`. Read this first. |

**Sales channels** (`channels`): `outlet`, `marketplace`, `shopify`, `wholesale`, `other`. They are assigned by rules in the server's settings, in this order:

1. a customer-account rule;
2. a location rule;
3. any other customer document whose customer account matched no rule goes to the default channel (`debtor_default_channel`), which is **`wholesale`** unless the setting was changed;
4. `other` is only what is left: rows with no customer account and no location rule (or every unmatched row, if the default channel is switched off). It is never folded into a retail channel.

So an unknown customer's sale counts as `wholesale`, not `other`.

**Item details for analysis** (brand, size, gender, colour, category, style) come from the item index's `items` (section 1). Match by item code.

**Traps:**

- **History starts in July 2026.** AC2 sales rows start on 3 Jul 2026 and are **outlet-only for July** (1-2 Jul missing); opening stock is 31 Jul 2026. For July 2026 and earlier, use the [AC1 sales history](#5-ac1-sales-history-one-off-292); never add AC2 July to it.
- **Net sold can be negative** when returns exceed sales in the window. Do not clip it to zero; say it.
- **Sections and consignment.** `highlights` splits lines into `main` (the main item group, default `XB`) and `other` (each other group on its own, for example a consignment group `CSGN`). A consignment group with no stock take can show negative stock.
- **Excluded items** (`Default Item`) are left out of every analysis file and counted in the manifest.
- **Size data depends on the item master.** Items with an unrecognised size get class `other` and count as not sized. `analysis_manifest.json` counts them under `attributes`: `unrecognised_size_items`, and `unrecognised_size_values`, which lists each unrecognised size **value** with its item count (not the items). Kids categories are always gender `K`.
- **Pools and placement.** Locations outside every pool (for example transit or defective stock) never count in stock-state. Placement does not use pools: only the locations named in its configured pairs count.
- **Highlights are a short list, not the full picture.** They count only the highlights pool and channels (by default the retail network, and `outlet`, `marketplace` and `shopify`). Each rule keeps the top 20 lines per brand and group section; `counts[<rule>].lines_before_cap` holds the full count. `sold_out_early` fires on a first zero within 28 days of first stock, or on a sell-through of 80% or more. For a complete placement list use `analysis_placement.json.gz`.

## 4. Purchasing file (#292)

**Answers:** open purchase-order units by item and vendor; goods received by month; how long vendors take to deliver; ordered versus received by size.

**Where:** `analysis_purchasing.json.gz` in the same folder, built with the buying files.

**Status: only if switched on.** The code is off by default; it was **switched on in the live export on 22 Nov 2026**, with the same file name and fields as described here (the first nightly confirmation is due on 23 Nov 2026). It can be switched off again, so still **check `analysis_manifest.json`:** if its `files` list has no `analysis_purchasing.json.gz` (or it has no `purchasing` block with `status: published`), purchasing data is not available; do not read a stray old copy.

**Contents:** `vendors` (names), `items`, `brands` (item brand), `groups`; rows with their own field lists:

- `po_rows` (`item, vendor, po_day, ordered, transferred, open, lines, open_lines, over_lines, age_days, stale, group`): `open` = ordered minus received against the line, never below 0; `stale` = still open more than 60 days after the PO date;
- `gr_rows` (`item, vendor, month, received, linked, lines, group`);
- `lead_rows` (`group, vendor, brand, lines, positive_lines, le0_lines, p25, median, p75, max, hist`): lead days (goods-received date minus the linked PO date). There is **one row per group, vendor and brand**, so a vendor can have several rows. `hist` is `[[lead_days, lines], ...]` over every lead, including 0 or less;
- `lead_all`: one object over every line (`lines, positive_lines, le0_lines, p25, median, p75, max`; no `hist`);
- `size_rows` (ordered and received by size);
- `vendor_rows` (`vendor, brands, items`): the vendor name, a list of its brands, and a **count** of its items (not a list).

**Traps:**

- **Units and vendor names only.** No PO or GR numbers, prices, amounts or supplier codes, by owner decision.
- **No supplier ETA exists in AutoCount**, and no PO status: "open" is computed from line quantities; `stale` is the warning.
- **Live figures move.** A goods receipt keyed today changes `transferred` and `open` for older PO months in tomorrow's file.
- Lead days count only GR lines copied from a PO (`linked`); lines with 0 or fewer days are counted apart (`le0_lines`) and left out of the spread.

## 5. AC1 sales history (one-off, #292)

**Answers:** units sold per item per month **before** AC2 go-live, for size mix and seasonality.

**Where:** `ac1_sales_monthly.json.gz` and `ac1_sales_monthly_manifest.json` at the top level of `AC2 Stock Balance (daily)`.

**Updates:** none on a schedule. It was built from the old AutoCount 1 monthly reports and copied in by hand (first on 9 Nov 2026); each republish needs the owner's OK. The manifest's `data_file.sha256` and `bytes` must match the data file.

**Contents:** `months`, `gaps`, `partial_months`, `items`, `item_mapped` (whether each AC1 code is also an AC2 code), and column arrays over `fields` = `item, month, sold` (item and month are indexes); `month_units` per month.

**Traps:**

- **Label it "AC1 history (channel mix unconfirmed)".** It has no location, day or channel, and it is not confirmed that it covered marketplace and wholesale as well as the outlet. Show it as its own series; **never add it into AC2 channel totals.**
- **One source per month:** AC1 up to and including Jul 2026, AC2 from Aug 2026.
- **Gaps are gaps, not zero.** Read `gaps` (at least one month is missing) and `partial_months` (a short export, flagged).
- `sold` is net of returns; negatives kept.
- **Use it for size mix and seasonality only.** Stock-based views (stock-outs, sell-through, placement) are AC2-only from 31 Jul 2026.
- Items with `item_mapped: false` are AC1 codes that AC2 does not know; they have no size, brand or category in the item index.

## 6. AR/AP aging (#321)

**Answers:** how much each customer owes us and how overdue; unapplied customer receipts and credit notes; how much we owe each supplier and when it falls due.

**Where:** `My Drive/Automation/Report/AC2 ARAP Aging (latest)`.

**Updates:** every hour at :30. **Latest picture only:** no history, no daily files. "What did this customer owe last month?" cannot be answered from these files.

**Freshness:** the data time is `as_at_local` in `latest_manifest.json` (also in the feed and every CSV); the data is stale when it is more than 70 minutes old. A failed run publishes nothing and leaves the previous set. `last_run_status.json` (`status`, `failure_class`, `delivery_status`, `finished_at`, `next_run_local`) only explains a failed or undelivered run: its own `as_at_local` is the latest attempt's read time, not the data's. See [Rules for reading](#rules-for-reading).

**Proven against AutoCount:** `reconciled_on` in the feed is the date the export last matched AutoCount's own Debtor and Creditor Aging reports to the cent (18 Nov 2026). `null` means not yet proven.

| File | One row per | Key fields |
| --- | --- | --- |
| `ar_open_items_latest.csv`, `ap_open_items_latest.csv` | open document | `party_code, party_name, doc_type, doc_no, doc_date, due_date, terms, currency, currency_rate, original_doc, original_local, knocked_off_doc, knocked_off_local, outstanding_doc, outstanding_local, last_knockoff_date, unapplied, post_dated, cheque_date, days_past_due, days_since_doc, bucket_due, bucket_doc, agent, description, ref` |
| `ar_parties_latest.csv`, `ap_parties_latest.csv` | every customer / supplier (active, inactive, zero balance) | `code, name, type, area, agent, terms, currency, credit_limit, aging_on, active, online_shop, outstanding, overdue, unapplied, due_current ... due_over_120, doc_current ... doc_over_120, docs_open, oldest_open_doc_date, oldest_overdue_days, last_payment_date, last_payment_amount` |
| `arap_dashboard_feed.json.gz` | the run | `as_at_local`, `reconciled_on`, `ap_payments_recorded`, `aging` (`bases`, `boundaries`, `bucket_labels`), `ar` and `ap` each with `totals`, `parties[]`, `docs[]`; `ap.schedule` (`overdue`, `this_week`, `next_week`, `later`, weeks Monday to Sunday); `health` |
| `latest_manifest.json`, `last_run_status.json` | the run | Counts, checks and status only; no names or amounts |

**Document types:** AR `IV` invoice, `CN` credit note, `PM` payment (receipt); AP `PI` supplier invoice, `SC` credit note, `PV` payment. Debit notes and deposits are not exported today (see traps).

**Amounts:** `_local` fields are in SGD; `_doc` fields are in the document's own currency (AP holds foreign currencies as well as SGD). Credits are **negative**.

**Buckets:** 30-day steps on two bases. `bucket_due` / `due_*` = days past the due date; `bucket_doc` / `doc_*` = days since the document date. Labels: `current` (not yet due, or 0 days), `1-30`, `31-60`, `61-90`, `91-120`, `over_120` (CSV columns `due_1_30` and so on). Unapplied credits have bucket `unapplied`. On both bases: sum of buckets + unapplied = outstanding.

**Traps:**

- **AP reads as unpaid.** `ap_payments_recorded: false` means AutoCount holds supplier invoices but no supplier payments yet, so **every AP invoice shows as unpaid**, including invoices already paid by bank. Say this whenever you quote an AP figure. It turns `true` once supplier payments are keyed. If the first supplier payment is knocked off against an invoice, the whole AR/AP export (AR too) pauses and goes stale until a short, owner-approved step on the server maps it. While it is paused, the status file still shows a recent time every hour (`failure_class: unknown_knockoff_target`); only `latest_manifest.json` shows how old the data is (rule 1).
- **Our buckets are not AutoCount's menu buckets.** AutoCount's Debtor and Creditor Aging reports age by **document date in calendar months** (current month, then 1, 2, 3, 4 months, then 5 and over) with credits **netted into** the buckets. These files use 30-day steps with credits kept apart. Totals per party match; bucket columns will not. The files carry only the 30-day columns.
- **AutoCount-style ageing ("By month (as AutoCount)").** When someone asks for ageing as AutoCount shows it, compute it from the per-document rows (the open-items CSV or the feed's `docs[]`), the same way the owner's AR/AP tracker page does:
  - months = (year x 12 + month of `as_at_local`) minus (year x 12 + month of `doc_date`), clamped to 0..5: 0 = current (the as-at month), then 1, 2, 3, 4 months, 5 = 5 months and over. A missing or future `doc_date` counts as 0.
  - Add **every** open row, credits included (they net into the month they are dated): `outstanding_local` (feed: `outstanding`) for an SGD view, `outstanding_doc` grouped by `currency` for the per-currency view AutoCount prints.
  - Check: the month buckets add up to the side's total outstanding in SGD, and each currency's buckets to that currency's sum of `outstanding_doc`.
  - AutoCount's "Total Over Due" = invoices whose due date is before the as-at date (the same as `overdue`).
  - This is the rule the 18 Nov 2026 reconciliation proved to the cent. Whether the export itself should carry month buckets is an open owner decision.
- **"Today" is `as_at_local`**, never your own clock, for overdue days, week bands and month buckets.
- **Unapplied credits** (customer receipts and credit notes not yet matched to an invoice) are their own negative rows (`unapplied = true`). They are never netted against an invoice. A customer's `outstanding` already includes them; `overdue` does not.
- **Online-shop customer.** The online-shop debtor account(s) carry `online_shop = true` (feed: `flags.online_shop`). It is a flag, not a filter: totals include it. Say whether you included it.
- **Post-dated.** `post_dated = true` marks a payment dated after the run date. None exist today; it is a flag, not a filter.
- **Foreign currency.** SGD figures for foreign-currency documents use the rate stored on the document (`currency_rate`), not today's rate. For a foreign-currency supplier, quote `outstanding_doc` with its currency. On payment rows `currency_rate` is blank (that rate source is switched off as a guess source); their SGD figure comes from AutoCount's own SGD amount.
- **Debit notes and deposits** are not exported until proven against AutoCount (they are switched off as "guess sources"). If any open ones exist, the manifest counts them under `guess_rows_skipped` and warns. None existed at the 18 Nov 2026 check.
- `days_past_due` counts from the due date, or the document date when there is no due date.

## Example questions

Always check freshness first ([rule 1](#rules-for-reading)) and quote the data's as-of time. Folder **S** = `AC2 Stock Balance (daily)`; folder **A** = `AC2 ARAP Aging (latest)`.

| # | Question | File (folder) | How |
| --- | --- | --- | --- |
| 1 | How many `FAKE-0201` do we have at `LOC-A` right now? | `stock_balance_latest.csv` (S) | `BalQty` where `ItemCode` and `Location` match (a code starting with `-`, `=`, `+` or `@` has a leading `'` in the CSV). |
| 2 | Which outlets have `FAKE-0201` in stock? | `stock_item_index.json.gz` (S) | `stock` rows for the item with `qty` > 0; labels from the feed's `locations`. |
| 3 | What was the stock of `FAKE-0201` at `LOC-B` at the end of 30 Sep 2026? | `daily/2026-09/stock_close_2026-09-30.csv` (S) | `CloseQty`; no row means 0. |
| 4 | Which items are negative, and since when? | `stock_dashboard_feed.json.gz` (S) | `negatives.rows` (`qty`, `negative_since`); if `truncated`, use the item index. |
| 5 | What is low at `LOC-A` by each item's own reorder level? | `stock_item_index.json.gz` (S) | Active items that are not `excluded`, with 0 < `qty` <= `reorder_level`, else `min_qty`, else `low_stock_default_threshold` (`reorder_level` and `min_qty` are null when not set). |
| 6 | How many units of brand `EXAMPLEBRAND` sold in the last 30 days, by channel? | `analysis_sales_daily.json.gz` + item index (S) | Sum `sold` by `channel` for items whose `brand` matches, over the 30 days ending on `built_from_close_day`: rows whose date (`day0` + `day`) is from `built_from_close_day` minus 29 days to `built_from_close_day`, both included. |
| 7 | Which items sold out soon after arriving? | `analysis_highlights.json.gz` (S) | `lines` with `rule` = `sold_out_early`; `inputs` hold the figures behind each line. |
| 8 | How many days was `FAKE-0201` out of stock in the retail network this month? | `analysis_stock_state.json.gz` (S) | Count the days of `zero_intervals` for that item and pool that fall in the month. |
| 9 | Which outlets are out of an item that another location holds? | `analysis_placement.json.gz` or highlights `open_placement` (S) | Rows with `open_now` true; `sold_at_zero_loc_90` shows demand. |
| 10 | Is our size mix of men's pants in line with what sells? | `analysis_highlights.json.gz` (S) | `size_drift` lines with that `group` and `brand`; `category`, `gender`, `size` and `flags` sit inside each line's `inputs` (check `inputs.flags` for net returns). |
| 11 | How many units are still open on POs to `Example Vendor`, and which are stale? | `analysis_purchasing.json.gz` (S), if published | `po_rows` for that vendor: sum `open`; `stale` true = open over 60 days. |
| 12 | How long does `Example Vendor` usually take to deliver? | `analysis_purchasing.json.gz` (S), if published | `lead_rows` for that vendor: one row per group and brand. Per brand, quote that row's `median` (and `p25` / `p75`). For the vendor overall, add up the rows' `hist` counts, keep lead days above 0, and take the median of the combined lines; never average the rows' medians. |
| 13 | What did `FAKE-0201` sell per month in 2025? | `ac1_sales_monthly.json.gz` (S) | `sold` per `month`; report `gaps` and `partial_months`; label "AC1 history (channel mix unconfirmed)". |
| 14 | How much does customer `C-001` owe, and how much is overdue? | `ar_parties_latest.csv` (A) | `outstanding`, `overdue`, `unapplied`, `oldest_overdue_days`. |
| 15 | Which customer invoices are more than 60 days past due? | `ar_open_items_latest.csv` (A) | `days_past_due` > 60 and `unapplied` false; list `party_name`, `doc_no`, `outstanding_local`. |
| 16 | What supplier invoices fall due this week? | `arap_dashboard_feed.json.gz` (A) | `ap.schedule.this_week` (`total`, `parties`). State the `ap_payments_recorded` caveat. |
| 17 | Why does our 31-60 bucket not match AutoCount's Debtor Aging report? | `ar_open_items_latest.csv` (A) | Different bucket rules: recompute by calendar month of `doc_date` with credits netted (see AR/AP traps). |

## Traps at a glance

- [ ] Judged freshness from the data's own as-of time in `latest_manifest.json` (stale after 70 minutes), not from the status file; checked `status` / `delivery_status` for a failed or undelivered run; quoted the data's `as_of_local` / `as_at_local`.
- [ ] Read JSON fields by name from the file's own field list; decoded indexes and day offsets.
- [ ] Large-balance rows (`large_balances`) called out, not silently summed.
- [ ] Daily closes within 7 days may still change; older files are not rewritten.
- [ ] Analysis files cover up to **yesterday**; AC2 sales start July 2026 (July outlet-only).
- [ ] Net `sold` can be negative.
- [ ] AC1 history kept as its own series; gaps are not zero.
- [ ] Buying analysis used only if `analysis_status` is not `disabled` and `analysis_manifest.json` exists; purchasing only if that manifest lists it.
- [ ] An unmatched customer's sales count as `wholesale` (the default channel), not `other`.
- [ ] AP figures: `ap_payments_recorded` false = every supplier invoice reads unpaid.
- [ ] AR/AP buckets are 30-day steps, not AutoCount's calendar months; credits kept apart. For AutoCount-style ageing, use the by-month rule (credits netted, per currency) with `as_at_local` as today.
- [ ] Online-shop customer flag and foreign-currency amounts stated.
- [ ] No cost, price, margin or sales-value answers from these files (they hold none).
- [ ] Nothing written, moved or deleted; nothing pasted publicly.

## Access

- Both folders are in the owner's **My Drive**. The owner shares them with the staff who need them; a staff member's agent can read only what is shared with that staff member's own Google account (the folders appear under "Shared with me", or as a shortcut the staff member adds to their own My Drive).
- If your agent cannot see a folder, ask the owner. Who gets access, and to which folder, is the **owner's decision**. The AR/AP folder holds customer and supplier names and amounts and is more sensitive than the stock folder. Ask the owner; do not change sharing yourself, and never use "anyone with the link".

## Privacy

- All of this is confidential company data: stock, sales units, vendor names, and above all AR/AP (customer and supplier names, codes and amounts).
- Keep it inside company tools and accounts. Do not paste it into public chats, public repositories (including this one), social media, or personal AI accounts outside the company.
- Share answers only with colleagues who need them; prefer totals to row dumps.
- The jobs' own logs, manifests and status files carry counts only; keep it that way in anything you write.

## Use it as a Claude Code skill

A small wrapper, [`ac2-data-skill/SKILL.md`](ac2-data-skill/SKILL.md), lets a staff member's Claude Code load this guide when asked about stock, buying, purchasing or AR/AP:

1. Create the folder `~/.claude/skills/ac2-data/` on the staff member's PC (on Windows, `%USERPROFILE%\.claude\skills\ac2-data\`).
2. Copy `ac2-data-skill/SKILL.md` and this file (`AC2_DATA_GUIDE.md`) into it, side by side.
3. Restart Claude Code. Ask "how many FAKE-0201 do we have?" and check it reads `latest_manifest.json` first and quotes the data's `as_of_local`.
4. The staff member still needs Drive access (see [Access](#access)) and a Google Drive connector or Drive for desktop.

When this guide changes in the repository, copy the new version over the old one.

## Out of scope

- Member sync, member core (member benefits) and member tags (#155, #268): they write to AutoCount and Shopify, not data extracts for reading.
- The daily health check (#325): it reads the two `last_run_status.json` files above and emails the owner only when something needs a look; it holds no business data.
- The error alert emails (#288): codes and counts only, no business data.
- The Shopify kit re-tagger (#269), the read-only Drive mirror of this repository, EnergyGrid bills and SIMBA invoices: not AutoCount data.
- The older Phase 1 extract and discovery scripts (for example `autocount_stock_extract.py`, `autocount_selected_surface_extract.py`, and the `autocount_inventory_*` and `*_discovery.py` scripts): they write local files on the server only, and nothing from them is in these Drive folders.
- Money figures for stock and sales (cost, value, margin): not exported; a separate daily money package (#246) is planned.
