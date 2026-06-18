# X-Boundaries AutoCount 2 Automation

This repository is for AutoCount 2 automation work only: local extraction scripts, SQL/API investigation notes, configuration templates, and runbooks for safe read-only automation around the AutoCount 2 environment.

Task tracking, pending-work dashboards, completed-task logs, and personal planning notes are intentionally kept outside this repo.

## Current Automation Surfaces

- `scripts/autocount_sql_probe.py`: read-only SQL metadata probe for discovering AutoCount database objects, columns, role memberships, and risky permissions.
- `scripts/autocount_phase1_reconcile.py`: safe aggregate Phase 1 stock reconciliation summaries for validating candidate SQL surfaces against AutoCount UI/report outputs before scheduling extraction.
- `scripts/autocount_broader_surface_discovery.py`: metadata-only broader AC2 surface discovery for customer, supplier, GL/accounting, AR/AP, locations, payment, PO, and stock-in-transit planning.
- `scripts/autocount_inventory_operation_discovery.py`: metadata-only inventory-operation surface discovery for GRN, stock transfer, location, richer item attributes, movement semantics, supplier context, and outstanding PO support.
- `scripts/autocount_stock_extract.py`: stock extraction/archive workflow for AutoCount stock master, stock balance, and stock movement datasets.
- `scripts/install_autocount_stock_extract_task.ps1`: Windows Task Scheduler installer for the stock extraction job.

## Configuration Templates

- `config/autocount_sql_probe.example.json`
- `config/autocount_readonly_login_validate.example.json`
- `config/autocount_phase1_reconcile.example.json`
- `config/autocount_broader_surface_discovery.example.json`
- `config/autocount_inventory_operation_discovery.example.json`
- `config/autocount_stock_extract.ac2_smoke.example.json`
- `config/autocount_stock_extract.example.json`
- `config/autocount_stock_extract.from_probe.example.json`

Copy example files to local ignored config paths before use. Do not commit credentials, connection strings, production exports, or real operational data.

## Local Output Convention

Keep all generated AutoCount outputs under `C:\XB\autocount_outputs`:

- SQL probe: `C:\XB\autocount_outputs\probe`
- Broader surface discovery: `C:\XB\autocount_outputs\probe\broader_surfaces`
- Inventory operation discovery: `C:\XB\autocount_outputs\probe\inventory_operations`
- Phase 1 reconciliation: `C:\XB\autocount_outputs\reconcile`
- Stock extraction: `C:\XB\autocount_outputs\extract\stock`

Raw CSVs stay local and must not be committed. Paste back only reviewed manifests and safe summaries.

## Runbooks And Design Notes

- [SQL Server local probe runbook](docs/autocount_sql_probe.md)
- [AutoCount stock extraction runbook](docs/autocount_stock_extraction.md)
- [AutoCount 2 automation architecture](docs/autocount2-automation/architecture.md)
- [AutoCount 2 API research](docs/autocount2-automation/api_research.md)
- [Inventory intelligence scope](docs/autocount2-automation/inventory_intelligence_scope.md)
- [Inventory operation surface discovery runbook](docs/autocount2-automation/inventory_operation_surface_discovery_runbook.md)
- [AutoCount 2 MVP plan](docs/autocount2-automation/mvp_plan.md)
- [Extraction surface decision pack](docs/autocount2-automation/extraction_surface_decision.md)
- [AC2 read-only SQL login validation runbook](docs/autocount2-automation/readonly_sql_login_runbook.md)
- [Broader AC2 surface discovery runbook](docs/autocount2-automation/broader_surface_discovery_runbook.md)
- [Phase 1 extraction mapping](docs/autocount2-automation/phase1_extraction_mapping.md)
- [Phase 1 AC2 stock extraction smoke runbook](docs/autocount2-automation/phase1_stock_extract_smoke_runbook.md)
- [Phase 1 reconciliation runbook](docs/autocount2-automation/phase1_reconciliation_runbook.md)
- [Stock reconciliation checklist](docs/autocount2-automation/reconciliation_checklist.md)
- [Security guardrails](docs/autocount2-automation/security_guardrails.md)
- [Data policy](docs/data_policy.md)

## AutoCount Template References

The `migration/` folder contains vendor AutoCount Excel import templates. These are kept as AutoCount reference artifacts, not as migration planning notes or task tracking.

## Data Policy Warning

Do not commit real company operational data to this repository.

This repository must not store:

- Real stock master or product master files.
- Real customer or supplier lists.
- AP/AR records or invoices.
- Bank or payment exports.
- AutoCount database backups.
- Passwords, API keys, or `.env` files.

Real data should stay in approved secure storage outside GitHub. AutoCount workflow outputs should go under `C:\XB\autocount_outputs` on the VM; generic local test outputs should go into ignored folders such as `data/`, `exports/`, `outputs/`, or `exceptions/`.
