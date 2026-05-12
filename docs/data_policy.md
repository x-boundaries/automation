# Data Policy

**WARNING: DO NOT COMMIT REAL OPERATIONAL DATA**

This repository is for tracking work, storing documentation, schemas, templates, dummy sample data, and automation code related to the AutoCount migration and operational automation.

## Allowed in this repository
- Tracker tasks
- Documentation (Markdown files)
- Schemas and template definitions
- Dummy sample data (clearly marked as sample/dummy)
- Automation code and scripts

## Strictly Forbidden
You MUST NOT store any of the following in this repository:
- Real stock master or product master lists
- Real customer data
- Real supplier data
- Real AP/AR records or invoices
- Bank or payment exports
- AutoCount database backups
- API keys, passwords, or credentials (e.g., `.env` files)
- Production exports of any kind

Real data should live in approved, secure storage outside of GitHub (e.g., shared drives, secure vaults).

If local testing requires actual files, ensure they are placed in ignored folders such as `data/`, `exports/`, `outputs/`, or `exceptions/`.
