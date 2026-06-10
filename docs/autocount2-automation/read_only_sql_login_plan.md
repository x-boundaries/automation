# Read-Only SQL Login Plan

Use the [Phase 1 reconciliation runbook](phase1_reconciliation_runbook.md) before granting the final read-only login access to approved wrapper views.

This plan defines the security posture required before any scheduled AutoCount
2.2 extraction runs against the confirmed AC2 target.

## Why The Current Login Is Discovery-Only

The confirmed AC2 probe was run successfully against the real target database,
but the current discovery login maps to `dbo` and has risky database role
memberships:

- `db_owner`
- `db_datawriter`
- `db_ddladmin`
- `db_securityadmin`
- `db_accessadmin`
- `db_backupoperator`

Even though the safe probe summary reported 0 direct permission risks, those
role memberships allow far more than read-only extraction. The current login is
therefore approved only for manual metadata discovery and reconciliation. It is
not approved for final scheduled automation.

## Required Final Posture

Create or assign a SQL login or Windows account dedicated to read-only
extraction. The account must have only the minimum database permissions needed
to read approved wrapper views or reconciled source views.

The final extractor account must not have:

- `db_owner`
- `db_datawriter`
- `db_ddladmin`
- `db_securityadmin`
- `db_accessadmin`
- `db_backupoperator`
- direct `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `ALTER`, `CONTROL`, `TAKE
  OWNERSHIP`, broad `EXECUTE`, or other write/admin permissions

Preferred access pattern:

- grant read access to local wrapper views such as
  `dbo.vw_XB_AC2_StockMaster_Phase1`,
  `dbo.vw_XB_AC2_StockBalance_Phase1`,
  `dbo.vw_XB_AC2_StockMovement_Phase1`, and
  `dbo.vw_XB_AC2_StockDocuments_Phase1` after reconciliation passes;
- avoid granting broad table access if wrapper views can provide the required
  read-only contract;
- keep the connection string in `AUTOCOUNT_READONLY_SQL_CONNECTION_STRING`;
- do not store passwords, tokens, or production connection strings in Git.

## CSV And Metadata Handling

SQL probe CSVs are spreadsheet-formula-neutralised, including optional sample
exports when sample mode is explicitly enabled. This protection is only for
spreadsheet formula injection; sample exports can still contain raw ERP values
and must remain outside Git.

Treat exact server/database names, local output paths, row counts, and role
posture as environment metadata. Prefer generic labels in version-controlled
docs unless the repository is restricted to trusted admins.

## Recommended Validation

Before scheduling:

1. Re-run the SQL probe against the confirmed AC2 target using the final
   read-only login. Keep exact server/database details in ignored local config
   where practical.
2. Confirm the probe can see only the approved source objects or wrapper views
   needed for Phase 1.
3. Confirm `permission_risks.csv` is empty.
4. Confirm `role_risks.csv` is empty, or contains only safe read-only roles such
   as `db_datareader`.
5. Confirm no `db_owner`, `db_datawriter`, `db_ddladmin`, `db_securityadmin`,
   `db_accessadmin`, or `db_backupoperator` membership remains.
6. Record only safe metadata summaries in the decision docs.
7. Keep generated probe outputs, raw rows, local configs, screenshots, and
   credentials out of the repository.

If the read-only probe exposes missing columns or blocked wrapper views, update
the wrapper grants or view definitions and rerun the probe before scheduling.

## Scheduling Gate

Scheduled extraction can begin only after:

- the reconciliation checklist passes for the confirmed AC2 target,
- the final read-only login probe has no blocking permission or role risks,
- wrapper views are selected and documented,
- the extractor config remains secret-free and references only approved
  read-only surfaces.
