# X-Boundaries tenure work tracker backup

This folder stores a GitHub-friendly backup/export of `xb_tenure_work_tracker_v2_full_coverage.xlsx`.

## Why this exists

The original tracker is an Excel workbook. GitHub can store Excel files, but it cannot diff them nicely. These CSV exports make the tracker easier to review, track, and later use for automation/dashboard generation.

## Original workbook sheets exported

| CSV file | Original sheet | What it is for |
|---|---|---|
| `Tracker.csv` | `Tracker` | Main task tracker. One row = one work item. |
| `Dashboard.csv` | `Dashboard` | Excel dashboard formulas/summary view from the workbook. |
| `Source_Coverage.csv` | `Source_Coverage` | Maps source requests/wishlist items to tracker coverage. |
| `Lists.csv` | `Lists` | Dropdown/reference values used by the workbook. |
| `README.csv` | `README` | Original workbook README sheet exported as CSV. |

## Important rule

Do not put real operational company data here. This repo should hold code, schemas, templates, dummy data, docs, and tracker/management metadata only.

## Current status

This is a backup/export, not yet the final automation format.

Later, we can convert `Tracker.csv` into the repo's main automation tracker and use a Python script to regenerate the dashboard in `README.md` automatically.
