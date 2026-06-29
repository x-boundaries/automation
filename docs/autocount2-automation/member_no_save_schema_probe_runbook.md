# AC2 Member No-Save Schema Probe Runbook

Status: explicit opt-in local probe only. This is the next discovery step after auth/session bootstrap and read-only MemberType browse were proven.

## Purpose

`scripts/ac2_member_no_save_schema_probe.ps1` confirms the in-memory `MemberEntity` schema exposed by the installed AutoCount account book/API before any member creation automation is designed.

The probe may:

- authenticate with runtime-only credentials,
- create `MemberCommand` through the public factory,
- call `GetNextMemberNo()` as a no-write diagnostic,
- call `NewMember(false)` to create an in-memory `MemberEntity`,
- inspect `MemberTable` or `Row.Table` column metadata.

The probe must not output the actual number returned by `GetNextMemberNo()`. It emits only whether the call succeeded, whether a value was nonempty, and the value length.

## Safety Boundary

This is a no-save schema probe. It creates `MemberCommand` and an in-memory MemberEntity only.

It does not read existing member/customer records. It does not browse members. It does not create/update/delete members. It does not run SQL.

It must not call `SaveMember`, `MemberEntity.Save`, `DeleteMember`, `GetMember`, `MemberCommand.LoadBrowseTable`, any MemberType write method, direct SQL, or DBSetting data methods.

Use runtime credentials only. The password is read from the environment variable named by `-PasswordEnvVar`; do not place passwords, connection strings, server names, database names, or user IDs in committed files.

Generated outputs stay local and are not committed. Only sanitized summary should be shared.

## Placeholder Command

```powershell
$env:AC2_PROBE_PASSWORD = "<runtime-password>"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ac2_member_no_save_schema_probe.ps1 `
  -EnableMemberNoSaveSchemaProbe `
  -AllowRootLogin `
  -ServerName "<server-name>" `
  -DatabaseName "<database-name>" `
  -UserId "<user-id>" `
  -PasswordEnvVar "AC2_PROBE_PASSWORD" `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\member_no_save_schema_probe.json"
Remove-Item Env:\AC2_PROBE_PASSWORD
```

## Safe Summary Command

```powershell
$json = Get-Content "C:\XB\autocount_outputs\review\member_intake_discovery\member_no_save_schema_probe.json" -Raw | ConvertFrom-Json

$json | Select-Object `
  authentication_success,
  user_session_available,
  member_command_found,
  member_command_create_found,
  get_next_member_no_found,
  get_next_member_no_success,
  next_member_no_nonempty,
  next_member_no_length,
  new_member_found,
  new_member_success,
  member_entity_available,
  member_table_available,
  member_row_available,
  column_count,
  intake_relevant_columns_seen,
  error

$json.columns
```

## Expected Sanitized Output Shape

The JSON output includes:

- `mode`,
- `probe_enabled`,
- auth/session booleans,
- `member_command_found`,
- `member_command_create_found`,
- `get_next_member_no_found`,
- `get_next_member_no_success`,
- `next_member_no_length`,
- `next_member_no_nonempty`,
- `new_member_found`,
- `new_member_success`,
- `member_entity_available`,
- `member_table_available`,
- `member_row_available`,
- `default_member_type_confirmed`,
- `column_count`,
- `columns`,
- `intake_relevant_columns_seen`,
- sanitized `error.type` and `error.message`.

Column schema entries include only:

- `column_name`,
- `data_type`,
- `allow_db_null`,
- `max_length`,
- `read_only`,
- `default_value_present`.

`default_value_present` is a boolean only. Actual defaults, generated member numbers, member/customer values, credentials, account book identifiers, and connection details must not be shared.
