# AC2 Member No-Save Assignment Probe Runbook

Status: explicit opt-in local probe only. This is a fake-data no-save assignment probe after auth, member type browse, and no-save schema probe were proven.

## Purpose

`scripts/ac2_member_no_save_assignment_probe.ps1` proves that synthetic intake-shaped values can be assigned into the in-memory `MemberEntity` row returned by the installed AutoCount account book/API before any real member creation automation is designed.

The probe may:

- authenticate with runtime-only credentials,
- create `MemberCommand` through the public factory,
- call `GetNextMemberNo()` as a no-write diagnostic,
- call `NewMember(false)` to create an in-memory `MemberEntity`,
- assign synthetic fake data only into `MemberEntity.Row` or the row from `MemberEntity.MemberTable`,
- emit sanitized assignment status by field.

The probe must not output the actual number returned by `GetNextMemberNo()`. It emits only whether the call succeeded, whether a value was nonempty, and the value length.

## Safety Boundary

This is a fake-data no-save assignment probe. It creates `MemberCommand` and an in-memory MemberEntity only.

It assigns synthetic fake data only. It does not use real customer/member data.

It does not read existing member/customer records. It does not browse members. It does not create/update/delete members. It does not run SQL.

It must not call save, delete, member read, member browse, MemberType write, direct SQL, or DBSetting data methods.

Use runtime credentials only. The password is read from the environment variable named by `-PasswordEnvVar`; do not place passwords, connection strings, server names, database names, or user IDs in committed files.

Generated outputs stay local and are not committed. Only sanitized summary should be shared.

## Placeholder Command

```powershell
$env:AC2_PROBE_PASSWORD = "<runtime-password>"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ac2_member_no_save_assignment_probe.ps1 `
  -EnableMemberNoSaveAssignmentProbe `
  -AllowRootLogin `
  -ServerName "<server-name>" `
  -DatabaseName "<database-name>" `
  -UserId "<user-id>" `
  -PasswordEnvVar "AC2_PROBE_PASSWORD" `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\member_no_save_assignment_probe.json"
Remove-Item Env:\AC2_PROBE_PASSWORD
```

## Safe Summary Command

```powershell
$json = Get-Content "C:\XB\autocount_outputs\review\member_intake_discovery\member_no_save_assignment_probe.json" -Raw | ConvertFrom-Json

$json | Select-Object `
  authentication_success,
  user_session_available,
  member_command_found,
  member_command_create_found,
  get_next_member_no_success,
  next_member_no_nonempty,
  next_member_no_length,
  new_member_success,
  member_entity_available,
  member_row_available,
  all_required_assignment_success,
  all_intake_assignment_success,
  member_type_default_assignment_success,
  no_save_confirmed,
  error

$json.assignment_results
```

## Expected Sanitized Output Shape

The JSON output includes:

- `mode`,
- `probe_enabled`,
- auth/session booleans,
- member command booleans,
- `next_member_no_length`,
- `next_member_no_nonempty`,
- new member/entity booleans,
- `assignment_results`,
- `required_assignment_results`,
- `intake_assignment_results`,
- `all_required_assignment_success`,
- `all_intake_assignment_success`,
- `member_type_default_assignment_success`,
- `no_save_confirmed`,
- sanitized `error.type` and `error.message`.

Assignment results include only:

- `field`,
- `column_exists`,
- `attempted`,
- `success`,
- `data_type`,
- `max_length`,
- `read_only`,
- `error_type`,
- `sanitized_error_message`.

The actual generated member number is never output. Fake values are used only to exercise in-memory assignment and must not be treated as payload evidence for real member creation.
