---
name: ac2-data
description: Use when asked about X-Boundaries stock levels, where an item is, negative or low stock, stock on a past day, units sold by item, outlet or channel, size mix, stock-outs, slow sellers, purchase orders, goods received, vendor lead times, AC1 sales history, or what customers owe us and what we owe suppliers (AR/AP aging). Reads the AutoCount 2 (AC2) data files in Google Drive under My Drive/Automation/Report. Read-only.
---

# AC2 data (stock, buying, purchasing, AR/AP)

Before answering, read `AC2_DATA_GUIDE.md` in this same folder and follow it. It lists every AutoCount data file, where it is, how fresh it is, what each field means, and the traps.

Always:

1. Find the files in Google Drive by folder name (`AC2 Stock Balance (daily)` or `AC2 ARAP Aging (latest)`, under `My Drive/Automation/Report`), through the Google Drive connector or the local Drive for desktop folder. If you cannot see them, say the folder has not been shared with this account and stop; do not ask anyone to make it public.
2. Read `last_run_status.json` first. Quote the data's "as of" time (`as_of_local` for stock, `as_at_local` for AR/AP), and say so if the last run failed or the data is more than about 70 minutes old.
3. Read JSON fields by name from each file's own field list; decode indexes and day offsets.
4. Never invent a field, figure or period the files do not hold. Say what is missing.
5. Never edit, move, rename or delete anything in those folders.
6. Treat everything as confidential company data: keep it inside company tools, and share only what the person asking needs.

If this folder has no `AC2_DATA_GUIDE.md`, copy it from the automation repository (`docs/autocount2-automation/AC2_DATA_GUIDE.md`) or ask the owner for it.
