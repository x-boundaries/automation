# X-Boundaries Automation

Automation repo for X-Boundaries digital transformation work.

## Purpose

This repo stores automation logic, schemas, documentation, and template definitions for the AutoCount 2.0 / SiteGiant / marketplace data layer.

## Rule

Do **not** commit real company data here.

Use this repo for:

- Documentation.
- Data schemas.
- Empty template definitions.
- Scripts.
- Validation rules.
- n8n workflow exports.
- Dummy sample data.

Do **not** commit:

- Real stock exports.
- Real product master files.
- Real customer or supplier lists.
- AP / AR / bank data.
- AutoCount backups.
- Passwords, API keys, or `.env` files.

## Current MVP

The first MVP is a simple product/SKU master legend + new stock input flow:

```text
Master Legend
+ New Stock Input
-> Generated AutoCount copy-paste rows
-> Paste into AutoCount 2
```

## Key design decision

AutoCount fields are limited, so X-Boundaries will maintain its own external mapping legend.

The master legend must support SKU changes over time. Old SKUs must be kept as history instead of directly overwritten.

## Folder layout

```text
docs/       Project notes, architecture, work tracker.
schemas/    Column definitions and data rules.
templates/  Empty CSV template definitions and workbook notes.
scripts/    Future Python scripts / validators / generators.
samples/    Dummy sample files only. No real company data.
n8n/        Future n8n workflow exports.
```

## Current local workbook templates

The working Excel files should stay outside GitHub unless they are clean empty templates:

- `xb_master_legend_TEMPLATE_v0_1.xlsx`
- `xb_new_stock_input_TEMPLATE_v0_1.xlsx`
- `ac2_stock_item_copypaste_TEMPLATE_v0_1.xlsx`

GitHub tracks the schema and logic. Real working data should stay in a secure shared drive.
