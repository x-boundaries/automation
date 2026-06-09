# Data Policy

**WARNING: DO NOT COMMIT REAL OPERATIONAL DATA**

This repository is for AutoCount 2 automation code, runbooks, secret-free configuration templates, vendor import-template references, schemas, and dummy sample data related to safe local automation.

Task tracking, pending-work dashboards, completed-task logs, personal planning notes, and real operational exports belong outside this repository.

## Allowed in this repository

- Automation code and scripts.
- Documentation and runbooks for approved AutoCount automation.
- Secret-free configuration templates.
- Vendor AutoCount import templates used as references.
- Schemas and template definitions.
- Dummy sample data that is clearly marked as sample/dummy.

## Strictly Forbidden

You MUST NOT store any of the following in this repository:

- Real stock master or product master lists.
- Real customer data.
- Real supplier data.
- Real AP/AR records or invoices.
- Bank or payment exports.
- AutoCount database backups.
- API keys, passwords, or credentials, including `.env` files.
- Production exports of any kind.

Real data should live in approved, secure storage outside of GitHub (e.g., shared drives, secure vaults).

If local testing requires actual files, ensure they are placed in ignored folders such as `data/`, `exports/`, `outputs/`, or `exceptions/`.
