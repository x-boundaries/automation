# X-Boundaries AutoCount 2 Automation

This repository is for AutoCount 2 automation work only: local extraction scripts, SQL/API investigation notes, configuration templates, and runbooks for safe read-only automation around the AutoCount 2 environment.

Task tracking, pending-work dashboards, completed-task logs, and personal planning notes are intentionally kept outside this repo.

## Current Automation Surfaces

- `scripts/autocount_sql_probe.py`: read-only SQL metadata probe for discovering AutoCount database objects, columns, role memberships, and risky permissions.
- `scripts/autocount_stock_extract.py`: stock extraction/archive workflow for AutoCount stock master, stock balance, and stock movement datasets.
- `scripts/install_autocount_stock_extract_task.ps1`: Windows Task Scheduler installer for the stock extraction job.

## Configuration Templates

- `config/autocount_sql_probe.example.json`
- `config/autocount_stock_extract.example.json`

Copy example files to local ignored config paths before use. Do not commit credentials, connection strings, production exports, or real operational data.

## Runbooks And Design Notes

- [SQL Server local probe runbook](docs/autocount_sql_probe.md)
- [AutoCount stock extraction runbook](docs/autocount_stock_extraction.md)
- [AutoCount 2 automation architecture](docs/autocount2-automation/architecture.md)
- [AutoCount 2 API research](docs/autocount2-automation/api_research.md)
- [AutoCount 2 MVP plan](docs/autocount2-automation/mvp_plan.md)
- [Security guardrails](docs/autocount2-automation/security_guardrails.md)
- [Data policy](docs/data_policy.md)

## AutoCount Template References

The `migration/` folder contains vendor AutoCount Excel import templates. These are kept as AutoCount reference artifacts, not as task tracking.

## Data Policy Warning

Do not commit real company operational data to this repository.

This repository must not store:

- Real stock master or product master files.
- Real customer or supplier lists.
- AP/AR records or invoices.
- Bank or payment exports.
- AutoCount database backups.
- Passwords, API keys, or `.env` files.

Real data should stay in approved secure storage outside GitHub. Local test outputs should go into ignored folders such as `data/`, `exports/`, `outputs/`, or `exceptions/`.
