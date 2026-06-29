# Member Intake Session Auth Probe Runbook

This probe is the first explicit opt-in live authentication probe for the local AutoCount bridge path. It only authenticates against AutoCount using runtime-supplied values.

It does not read/list/create/update/delete members. It does not call MemberCommand.Create. It does not call MemberTypeCommand.Create. It does not run SQL.

Static Authenticate returned false with no exception in the first local run. Metadata also shows an instance UserSession.Login path, so this probe now records both static authentication and instance login diagnostics.

Previous instance login returned false with no exception. The operator confirmed the test uses an AutoCount admin/root-style user, so the probe includes an explicit `-AllowRootLogin` diagnostic path. The script changes `AllowRootLogin` only when that switch is supplied.

## Output Location

Write generated output under:

`C:\XB\autocount_outputs\review\member_intake_discovery\`

Do not commit generated outputs, runtime JSON, screenshots, account book names, credentials, connection strings, or member data.

## Runtime-Only Credentials

Script path: `scripts/ac2_session_auth_probe.ps1`

Credentials must be supplied at runtime only. The password must use an environment variable and must not be passed as a command argument.

The script defaults to `PasswordEnvVar` value `AC2_PROBE_PASSWORD`.

## Example Command

Use placeholders only. Replace them locally on the AC2 machine and do not paste the values into Git, docs, chat, screenshots, or PR comments.

```powershell
$env:AC2_PROBE_PASSWORD = "<runtime-password>"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ac2_session_auth_probe.ps1 `
  -EnableSessionProbe `
  -ServerName "<server-name>" `
  -DatabaseName "<database-name>" `
  -UserId "<user-id>" `
  -PasswordEnvVar "AC2_PROBE_PASSWORD" `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\session_auth_probe.json"
Remove-Item Env:\AC2_PROBE_PASSWORD
```

For an admin/root-style diagnostic, opt in explicitly:

```powershell
$env:AC2_PROBE_PASSWORD = "<runtime-password>"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ac2_session_auth_probe.ps1 `
  -EnableSessionProbe `
  -AllowRootLogin `
  -ServerName "<server-name>" `
  -DatabaseName "<database-name>" `
  -UserId "<user-id>" `
  -PasswordEnvVar "AC2_PROBE_PASSWORD" `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\session_auth_probe.json"
Remove-Item Env:\AC2_PROBE_PASSWORD
```

The `-EnableSessionProbe` switch is mandatory. Without it, the script refuses to run before loading AutoCount assemblies or attempting authentication.

## Sanitized Output

The probe may output only sanitized status fields:

- mode
- AC2 root exists yes/no
- target assembly loaded yes/no
- DBSetting factory found yes/no
- UserSession authentication method found yes/no
- AllowRootLogin requested/property found/set yes/no
- static authentication success yes/no
- instance UserSession.Login method found yes/no
- instance login success yes/no
- instance IsLogin yes/no
- SetAsCurrent found/called yes/no
- current session available after SetAsCurrent yes/no
- CheckHasLogined found/success yes/no
- session probe enabled
- authentication success yes/no
- user session available yes/no
- sanitized error type/message if failed

The output must not include server name, database name, user ID, password, connection string, machine username, account book name, or member data.

## Boundary

Auth only; no member read/list/create/update/delete.

No MemberCommand.Create or MemberTypeCommand.Create.

No SQL queries, no DBSetting data methods, and no direct SQL writes.

No member factory invocation and no member read/list/write.

No generated output should be committed.
