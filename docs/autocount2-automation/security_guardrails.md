# AutoCount 2.0 Automation Security Guardrails

These guardrails apply to all X-Boundaries AutoCount 2.0 automation work.

## Non-negotiable Rules

- Start read-only.
- Do not write directly to AutoCount production SQL tables.
- Do not design direct SQL writes into AutoCount production tables.
- Use a separate SQL read account for AutoCount source reads.
- Use a separate reporting database for extracted and transformed data.
- Keep secrets outside the repository.
- Log every job run and dataset load.
- Keep raw exports in secure storage outside GitHub.
- Require human approval before any future write-back.
- AI must consume only curated reporting views or generated summary packs, not raw unrestricted ERP data.

## Access Model

### Source read account

Create a dedicated SQL login or Windows identity for source reads.

Minimum permissions:

- `SELECT` on approved AutoCount views or vendor-approved read surfaces.
- No `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `ALTER`, `CREATE`, `DROP`, or `EXECUTE` on posting routines.
- No ownership chaining that allows writes indirectly.

If using the AutoCount API, create a dedicated AutoCount/API user where possible and grant only the access required for read-only reporting.

### Reporting loader account

Create a separate identity for writing to the reporting database.

Allowed:

- Write to `raw`, `stg`, and `audit` tables in `XB_AutoCount_Reporting`.
- Execute approved reporting database stored procedures.

Denied:

- Any write permission on the AutoCount production database.
- Any permission to modify AutoCount schema.

### Dashboard and AI accounts

Dashboard and AI jobs should read from `mart` views only.

Denied:

- Direct access to AutoCount production tables.
- Direct access to `raw` unless explicitly approved for diagnostics.
- Broad access to customer, supplier, invoice, bank, or payment detail that is not needed for the summary.

## Secrets Handling

Never commit:

- SQL connection strings,
- AutoCount usernames/passwords,
- AOTG API keys,
- Cloud Accounting `API-Key` or `Key-ID`,
- hosted AI provider keys,
- `.env` files,
- raw exports or production backups.

Approved storage options:

- Windows Credential Manager,
- user or machine environment variables on the VM,
- a local encrypted config file outside the repo,
- a secret manager if one already exists.

Repository files may include only placeholders, for example:

```text
AUTOCOUNT_READONLY_SQL_CONNECTION_STRING=<set on VM>
AI_PROVIDER_API_KEY=<set outside repo>
```

## Job Logs And Audit Tables

Every scheduled run must write:

- run ID,
- start/end timestamp,
- source system/build if available,
- dataset name,
- source date window,
- row count,
- file path outside repo,
- file hash,
- status,
- warning/error details.

Recommended audit tables:

- `audit.extract_run`
- `audit.extract_dataset`
- `audit.extract_file`
- `audit.data_quality_issue`
- `audit.ai_summary_run`
- `audit.approval_event` for future write-back approval

## Raw Export Retention

Raw exports are sensitive operational data.

Rules:

- Store outside GitHub.
- Restrict NTFS permissions to the automation service account and approved admins.
- Retain daily extracts for a defined period, initially 90 days.
- Keep month-end snapshots only if finance approves the retention need.
- Record hashes in audit tables so files can be verified without exposing contents.
- Do not email raw exports.

## AI Data Boundary

AI may consume:

- curated `mart` views,
- generated summary packs,
- aggregated exception lists,
- row counts and quality metrics,
- dashboard links.

AI must not consume by default:

- unrestricted invoice line exports,
- raw customer/supplier master data,
- bank/payment exports,
- credentials,
- AutoCount backups,
- production SQL dumps.

Before using a hosted AI API, approve:

- exact fields sent,
- destination/provider,
- retention policy,
- prompt/output storage location,
- redaction rules.

## Future Write-back Approval Gate

No write-back is allowed until read-only extraction, reporting, dashboards, and reconciliation are stable.

Future write-back must use:

- a queue outside AutoCount production,
- documented proposed changes,
- human approval with approver identity and timestamp,
- execution through official AutoCount API, plug-in, or approved import route,
- execution logs and failure handling,
- a rollback or reversal process approved by finance/vendor.

Future write-back must not use:

- direct SQL updates to AutoCount production tables,
- automated posting without approval,
- AI-generated actions executed without human review.

## Review Checklist

Before enabling any new dataset or job:

- Confirm the source permission is read-only.
- Confirm output path is outside the repo.
- Confirm no credentials are in config committed to Git.
- Confirm raw data is not sent to AI.
- Confirm dashboard query reads `mart` views, not production tables.
- Confirm audit tables receive run records.
- Confirm the job can fail safely without partial production changes.
