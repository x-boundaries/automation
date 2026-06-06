# AutoCount SQL Server Local Probe Runbook

## Purpose

Use `scripts/autocount_sql_probe.py` on the Windows VM where AutoCount 2.0 and
SQL Server 2019 are installed. The probe gathers SQL Server metadata with a
read-only login so we can identify candidate read surfaces before replacing the
placeholder stock extractor views.

The probe does not verify AutoCount business meaning by itself. Candidate
reports are heuristic metadata matches and must be confirmed with official
AutoCount documentation, local sandbox tests, read-only permission checks, and
AutoCount UI/report reconciliation.

## Files

- `scripts/autocount_sql_probe.py`: read-only SQL metadata probe CLI.
- `config/autocount_sql_probe.example.json`: secret-free example config.
- `tests/test_autocount_sql_probe.py`: unit tests for matching, manifests,
  output safety, permission risk flags, and redaction.

## Windows VM Setup

Install Python and the SQL Server ODBC pieces on the AutoCount VM:

- Minimum Python: 3.9.
- Recommended Python: 3.11 or newer.
- Python package: `pyodbc`.
- Microsoft ODBC Driver for SQL Server.

```powershell
py -3.11 -m pip install pyodbc
New-Item -ItemType Directory -Force D:\AutoCountSqlProbe
```

Copy the example config to a local-only path:

```powershell
New-Item -ItemType Directory -Force D:\AutoCountSqlProbeConfig
Copy-Item config\autocount_sql_probe.example.json D:\AutoCountSqlProbeConfig\autocount_sql_probe.local.json
```

Do not commit the local config or generated probe outputs.

## Connection String

The probe reads the SQL Server connection string only from an environment
variable. By default it reads:

```text
AUTOCOUNT_READONLY_SQL_CONNECTION_STRING
```

Set it on the VM for the Windows/task account that will run the probe:

```powershell
[Environment]::SetEnvironmentVariable(
  "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
  "Driver={ODBC Driver 18 for SQL Server};Server=YOUR-SERVER;Database=YOUR-AUTOCOUNT-DB;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly;",
  "User"
)
```

If SQL authentication is unavoidable, keep the password only in the VM
environment or an approved Windows secret store. Never put credentials in this
repo or in the JSON config.

## Dry Run

Dry-run mode validates the config shape, output path, and planned options
without connecting to SQL Server or creating a probe output folder.

```powershell
py -3.11 scripts\autocount_sql_probe.py `
  --config D:\AutoCountSqlProbeConfig\autocount_sql_probe.local.json `
  --dry-run
```

## Metadata-Only Probe

The default probe writes SQL metadata only. It does not write raw business rows.

```powershell
py -3.11 scripts\autocount_sql_probe.py `
  --config D:\AutoCountSqlProbeConfig\autocount_sql_probe.local.json `
  --output-root D:\AutoCountSqlProbe `
  --schemas dbo
```

Useful options:

- `--database-name`: optional database context if the connection string does
  not already select the AutoCount database.
- `--schemas`: comma-separated schema filter, for example `dbo,report`.
- `--include-row-counts`: include best-effort table row counts from SQL Server
  metadata.

## Optional Limited Samples

Samples are off by default: `sample_limit` is `0`.

If samples are needed, treat them as business data. Keep them in the local
output root outside GitHub and do not paste raw rows into issues, PRs, docs, or
ChatGPT/Codex.

To enable samples:

1. Edit only the local config on the VM.
2. Set `sample_limit` to a small number.
3. Add explicit `sample_objects`, for example:

   ```json
   {
     "sample_limit": 5,
     "sample_objects": [
       {
         "schema_name": "dbo",
         "object_name": "REPLACE_WITH_CANDIDATE_OBJECT"
       }
     ]
   }
   ```

The script samples only explicitly listed objects that also appeared in the
heuristic candidate list. It uses `SELECT TOP (N)` and does not run destructive
test statements.

## Outputs

Each real probe creates one timestamped folder under the output root:

```text
D:\AutoCountSqlProbe\
  probe_20260606_093000_1a2b3c4d\
    probe_manifest.json
    probe_report.md
    schemas.csv
    objects.csv
    columns.csv
    indexes.csv
    permissions.csv
    permission_risks.csv
    role_memberships.csv
    role_risks.csv
    candidates.csv
    row_counts.csv
    samples\
      ...
```

`row_counts.csv` exists only when `--include-row-counts` is used. The `samples`
folder exists only when `sample_limit` is greater than `0` and explicit
candidate sample objects are configured.

`probe_manifest.json` stores run metadata, counts, output file paths, candidate
group counts, warning/error text with obvious secrets redacted, and best-effort
direct-permission and role-membership risk flags. It must not contain raw
sampled rows or credentials.

## Candidate Report

`candidates.csv` and `probe_report.md` group tables/views by keyword matches in
object names and column names. Groups include:

- item/product/stock master,
- stock balance/status,
- stock movement/stock card,
- stock document/transfer/adjustment,
- sales invoice/cash sale,
- purchase order/GRN,
- debtor/customer,
- creditor/supplier,
- AR/AP,
- GL/account/journal,
- payment/bank/cashbook.

These are heuristic hints, not verified AutoCount truth. A candidate object is
only safe to use after local validation proves the read-only login can query it
and its totals reconcile with AutoCount UI/report outputs.

## Read-Only Permission And Role Checks

The probe reads visible SQL permissions and database-role memberships. It also
runs fixed-role membership checks with `IS_ROLEMEMBER(...)` for common SQL
Server database roles. These checks are read-only and do not run destructive
test statements.

For direct permissions, it flags obvious risky capabilities:

- `INSERT`,
- `UPDATE`,
- `DELETE`,
- `ALTER`,
- `CONTROL`,
- `CREATE TABLE`,
- broad-schema or database-level `EXECUTE`.

For role memberships, dangerous roles such as these should block use as the
read-only probe/extractor login until removed or replaced with a safer account:

- `db_owner`,
- `db_datawriter`,
- `db_ddladmin`,
- `db_securityadmin`,
- `db_accessadmin`,
- `db_backupoperator`.

`db_datareader` alone is not flagged as risky by the probe, but it is still only
one part of the read-only review. Confirm no other direct permissions or role
memberships grant write, schema, security, or broad execute capability.

This is a best-effort guardrail, not a formal security audit. A clean
`permission_risks.csv` and `role_risks.csv` do not prove the login is safe.
Confirm separately that the probe/extractor account cannot insert, update,
delete, execute posting procedures, alter schema, change security, or write to
AutoCount production tables.

## Next Manual Step

After running the metadata-only probe, paste or summarise `probe_report.md` and
`candidates.csv` back into ChatGPT/Codex. Do not paste raw sample rows or
credentials. Use the candidate report to choose real read-only views or queries
for the stock extractor config.
