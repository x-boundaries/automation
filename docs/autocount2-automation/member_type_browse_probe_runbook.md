# Member Type Browse Probe Runbook

This is the first read-only member API probe after auth was proven. It confirms available member type values from the actual AutoCount account book before any member creation automation is considered.

It only calls `MemberTypeCommand.LoadBrowseTable` after the explicit probe flag, runtime-only credentials, and a successful session/authentication gate.

It does not read member/customer records. It does not create/update/delete member types. It does not create/update/delete members. It does not run SQL.

## Output Location

Write generated output under:

`C:\XB\autocount_outputs\review\member_intake_discovery\`

Generated outputs stay local and are not committed. Share only a sanitized summary after review.

## Runtime-Only Credentials

Script path: `scripts/ac2_member_type_browse_probe.ps1`

Credentials must be supplied at runtime only. The password must use an environment variable and must not be passed as a command argument.

The script defaults to `PasswordEnvVar` value `AC2_PROBE_PASSWORD`.

## Example Command

Use placeholders only. Replace them locally on the AC2 machine and do not paste the values into Git, docs, chat, screenshots, or PR comments.

```powershell
$env:AC2_PROBE_PASSWORD = "<runtime-password>"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ac2_member_type_browse_probe.ps1 `
  -EnableMemberTypeBrowseProbe `
  -AllowRootLogin `
  -ServerName "<server-name>" `
  -DatabaseName "<database-name>" `
  -UserId "<user-id>" `
  -PasswordEnvVar "AC2_PROBE_PASSWORD" `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\member_type_browse_probe.json"
Remove-Item Env:\AC2_PROBE_PASSWORD
```

The `-EnableMemberTypeBrowseProbe` switch is mandatory. Without it, the script refuses to run before loading AutoCount assemblies or attempting authentication.

## Safe Summary

Use this safe summary command before sharing any result:

```powershell
$json = Get-Content "C:\XB\autocount_outputs\review\member_intake_discovery\member_type_browse_probe.json" -Raw | ConvertFrom-Json
$json | Select-Object `
  authentication_success,
  user_session_available,
  member_type_command_found,
  member_type_command_create_found,
  load_browse_table_found,
  load_browse_table_success,
  row_count,
  column_names,
  default_member_type_seen,
  error
$json.rows
```

## Sanitized Output

The probe may output only sanitized status fields:

- mode
- probe enabled
- auth/session booleans
- member type command booleans
- row count
- sanitized column names
- sanitized rows limited by `MaxRows`
- default member type seen yes/no
- sanitized error type/message if failed

If the returned table uses unexpected column names, treat the output as schema discovery. Share only the sanitized summary fields and reviewed row values.

The output must not include server name, database name, user ID, password, connection string, machine username, account book name, member/customer personal data, or member records.

## Boundary

Read-only member type browse only.

No member/customer records are read.

No member type create/update/delete.

No member create/update/delete.

No direct SQL and no DBSetting data methods.

No generated output should be committed.
