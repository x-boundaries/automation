# AC2 Read-Only SQL Login Validation Runbook

## Purpose

Use this runbook to validate a dedicated AutoCount 2 / AC2 SQL login or Windows
account before scheduled extraction or broader extraction scope is considered.
The validation kit checks whether the account can run metadata-only reads
against the known stock smoke surfaces and whether advisory permission probes
show obvious write-like permissions or dangerous fixed-role memberships.

This does not approve scheduled extraction. It does not select final production
SQL surfaces. It does not create production users automatically.

## Account Creation Is Manual

A DBA/admin must create and review the dedicated read-only account outside this
repository. Prefer a Windows/domain/service account if practical. If a SQL login
is required, store its password only outside Git, such as in Windows Credential
Manager, a secured VM environment variable, or another approved secret store.

Do not use accounts with elevated database roles or server roles, including:

- `sysadmin`
- `db_owner`
- `db_datawriter`
- `db_ddladmin`
- `db_securityadmin`
- `db_accessadmin`
- `db_backupoperator`
- schema/security/admin roles that can change objects or permissions

Grant only the read permissions needed for approved extraction surfaces. The
initial validation surfaces are:

- `dbo.Item`
- `dbo.ItemUOM`
- `dbo.vItemBalQty`
- `dbo.StockDTL`

Any SQL setup commands must be reviewed by the DBA/admin before running. Use
placeholder-only notes in Git; never commit real usernames, passwords, server
names, or production connection strings.

## Local Config

Copy the secret-free config template to the ignored local config path:

```powershell
Copy-Item config\autocount_readonly_login_validate.example.json config\autocount_readonly_login_validate.local.json
```

Set the connection string outside the repo for the dedicated read-only account:

```powershell
$env:AUTOCOUNT_READONLY_SQL_CONNECTION_STRING = '<stored outside Git>'
```

The template writes local manifests under:

```text
C:\XB\autocount_outputs\probe\readonly_login
```

The config loader accepts UTF-8 with BOM for PowerShell-edited JSON.

## Run Validation

Run the validator manually on the VM:

```powershell
python scripts\autocount_readonly_login_validate.py --config config\autocount_readonly_login_validate.local.json
```

The validator:

- Connects using only `AUTOCOUNT_READONLY_SQL_CONNECTION_STRING`.
- Reads current database/login/user metadata.
- Checks table/view presence for the configured smoke surfaces.
- Runs `SELECT TOP (0)` metadata checks, which return no ERP rows.
- Uses `HAS_PERMS_BY_NAME`, `IS_ROLEMEMBER`, and `IS_SRVROLEMEMBER` advisory
  checks for write-like permissions and dangerous fixed roles.
- Writes one local `readonly_login_validation_manifest.json`.

The validator does not attempt writes. It does not run `INSERT`, `UPDATE`,
`DELETE`, `MERGE`, `CREATE`, `ALTER`, `DROP`, or `TRUNCATE`. It does not export
raw ERP/business rows.

## Interpret Results

The manifest is safe to paste back after operator review because it contains
only status, metadata labels, booleans, aggregate counts, surface names, and
redacted exception text.

Treat validation as failed if:

- The account cannot connect.
- Any configured smoke surface is missing or cannot run the metadata-only
  check.
- Advisory permission checks detect write-like permissions such as `INSERT`,
  `UPDATE`, `DELETE`, `ALTER`, `CONTROL`, or `TAKE OWNERSHIP`.
- Advisory role checks detect dangerous fixed database or server roles such as
  `db_owner`, `db_datawriter`, `db_ddladmin`, `db_securityadmin`,
  `db_accessadmin`, `db_backupoperator`, `sysadmin`, or `securityadmin`.

The permission checks are advisory. A clean manifest does not replace
DBA/admin/operator sign-off.

## Required Follow-Up Before Scheduling

After the dedicated login validation passes, rerun the
[Phase 1 AC2 stock extraction smoke runbook](phase1_stock_extract_smoke_runbook.md)
using that dedicated login.

Do not schedule extraction yet. Scheduling still requires:

- Dedicated read-only login validation.
- AC2 stock smoke extraction using the dedicated login.
- Reconciliation/sign-off against AutoCount UI/report outputs.
- Approved wrapper views or explicit approval for the direct smoke profile.
- Operator approval.

Do not widen extraction scope until read-only login validation and smoke
extraction both pass.
