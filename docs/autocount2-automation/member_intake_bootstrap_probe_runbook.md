# Member Intake Bootstrap Probe Runbook

Use this runbook on the X-Boundaries AC2 machine to collect sanitized metadata about the installed AutoCount bootstrap/session surface. This is the next discovery step after the member API surface probe.

## Output Location

Write generated output under:

`C:\XB\autocount_outputs\review\member_intake_discovery\`

Do not commit generated JSON, console captures, screenshots, account book names, credentials, member rows, or machine-specific runtime dumps.

## Metadata-Only Bootstrap Probe

Script path: `scripts/ac2_bootstrap_api_probe.ps1`

From the repo root on the AC2 machine:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\ac2_bootstrap_api_probe.ps1 `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\bootstrap_api_metadata.json"
```

If AutoCount is installed somewhere else, pass `-AcRoot`:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\ac2_bootstrap_api_probe.ps1 `
  -AcRoot "C:\Program Files\AutoCount\Accounting 2.2" `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\bootstrap_api_metadata.json"
```

The probe reports:

- Presence of expected AC2 DLLs.
- Metadata for exact known types `AutoCount.Authentication.UserSession` and `AutoCount.Data.DBSetting`.
- Candidate bootstrap/auth/data/session types whose full names include terms such as `UserSession`, `DBSetting`, `Login`, `Authentication`, `Auth`, `AccountBook`, `Company`, `Database`, `DB`, or `Session`.
- Public and non-public constructors.
- Public static methods.
- Public instance methods.
- Public properties.
- Method parameter types and return types.

The probe is reflection-only. It does not instantiate types, does not invoke methods, performs no command factory invocation, does not connect to a database, does not read/list member rows, and performs no member create/update/delete.

## Why This Probe Exists

PR #63 discovered that the installed member commands require `AutoCount.Authentication.UserSession` and `AutoCount.Data.DBSetting`.

Known local findings:

- `MemberCommand` has an internal constructor requiring `UserSession` plus `DBSetting`.
- `MemberCommand` has a public static `Create(UserSession, DBSetting)` factory.
- `MemberTypeCommand` has an internal constructor requiring `UserSession` plus `DBSetting`.
- `MemberTypeCommand` has a public static `Create(UserSession, DBSetting)` factory.

Do not reflect-call internal constructors. Do not call the public factories yet. The current unknown is the official, safe way for an integration to obtain `UserSession` and `DBSetting`.

## Sanitized Capture

Before sharing any output back into the repo or a PR comment:

- Remove local user names if they appear in paths.
- Remove account book names if any exception includes them.
- Do not include screenshots.
- Do not include credentials, connection strings, API keys, or token material.
- Do not include real member numbers, names, phone numbers, emails, dates of birth, addresses, notes, or consent records.
- Prefer sharing only type names, constructor signatures, method signatures, and whether exact target types were found.

## Writeback Boundary

No live read/list/create until bootstrap path is understood and separately approved.

No command factory invocation belongs in this probe.

No direct SQL writes are allowed for member intake.
