# AC2 Member Lookup Review Runbook

Status: local review probe only. This is a read-only lookup only for future n8n/local bridge duplicate checking.

## Purpose

Use `scripts/ac2_member_lookup_review.ps1` on the AutoCount host to check whether a submitted Google Form mobile/member number already exists as an AutoCount 2.0 `MemberNo`.

AC2 / AutoCount 2.0 is the source of truth for member records. The old POS member list and any side sheet are reference-only. For this lookup boundary, the Google Form mobile/member number maps to AutoCount `MemberNo`; AutoCount `MobilePhone` is intentionally unused. Birthday Month mapping to future `DOB` remains `2000-MM-01` and is outside this lookup probe.

This probe does not create/update/delete members, does not modify AutoCount, and must not be used as final write automation.

## Command

Set the password only in the current PowerShell process:

```powershell
$env:AC2_PROBE_PASSWORD = "<runtime password>"
```

Run a manual local lookup with explicit opt-in:

```powershell
.\scripts\ac2_member_lookup_review.ps1 `
  -EnableMemberLookupReview `
  -MemberNo "+65 9123 4567" `
  -ServerName "<server>" `
  -DatabaseName "<database>" `
  -UserId "<user>"
```

For self-hosted local n8n form-intake calls, pass UTF-8 base64 instead of the raw form value:

```powershell
.\scripts\ac2_member_lookup_review.ps1 `
  -EnableMemberLookupReview `
  -MemberNoBase64Utf8 "<utf8-base64-member-number>" `
  -ServerName "<server>" `
  -DatabaseName "<database>" `
  -UserId "<user>"
```

Optional parameters:

- `-DllRoot`: AutoCount installation DLL folder. Defaults to `C:\Program Files\AutoCount\Accounting 2.2`.
- `-ServerName`: AutoCount server name. Defaults to `AC2_PROBE_SERVER_NAME`.
- `-DatabaseName`: AutoCount database name. Defaults to `AC2_PROBE_DATABASE_NAME`.
- `-UserId`: AutoCount user ID. Defaults to `AC2_PROBE_USER_ID`.
- `-MemberNoBase64Utf8`: UTF-8 base64 encoded submitted member/mobile value for local n8n calls.
- `-AllowRootLogin`: sets `UserSession.AllowRootLogin = true` only when explicitly requested.

Exactly one of `-MemberNo` or `-MemberNoBase64Utf8` must be supplied. Supplying both, supplying neither, or supplying invalid UTF-8 base64 returns sanitized JSON with `status = error`.

The password must come from `AC2_PROBE_PASSWORD`. Do not pass passwords as command-line arguments.

## Lookup Path

The script uses the proven local API context:

- load `AutoCount.dll` and `AutoCount.Invoicing.dll` from the installed AutoCount folder,
- create `DBSetting` with `DBSetting.CreateAutoCountDefaultDBSetting(...)`,
- authenticate with `UserSession.Authenticate(...)`,
- create a `UserSession(dbSetting)`,
- optionally set `session.AllowRootLogin = true`,
- call `session.Login(...)`,
- call `session.SetAsCurrent()`,
- call `session.CheckHasLogined()`,
- create `MemberCommand` using `MemberCommand.Create(UserSession, DBSetting)`,
- call `MemberCommand.GetMember(normalizedMemberNo)` exactly once.

It does not use member browse output, direct SQL, write SQL, or member save/delete/generation APIs.

## Member Number Normalization

The submitted value is normalized before lookup:

- remove spaces, plus signs, dashes, brackets, dots, underscores, and symbols while keeping letters and digits,
- if exactly 8 digits starting with 8 or 9, canonicalize to `65XXXXXXXX`,
- if already 10 digits starting with 65, keep as-is,
- keep other digit or alphanumeric cleaned shapes but mark `manual_review`,
- reject cleaned values over 20 characters before lookup with `submitted_member_no_status = invalid_too_long`.

manual-review shapes may be looked up only if they are 20 characters or fewer, and the script sets `manual_review_required = true`. Values over 20 characters are rejected before lookup; the script does not call `MemberCommand.GetMember` for them.

## Sanitized Output

Output is sanitized and PII-free JSON only. It must not include names, email addresses, phone numbers, DOB, addresses, AutoKey, Guid, raw `MemberNo`, normalized `MemberNo`, server names, database names, user IDs, or passwords.

Expected fields:

- `status`
- `authentication_success`
- `user_session_available`
- `member_command_found`
- `get_member_found`
- `submitted_member_no_status`
- `normalized_member_no_length`
- `member_exists`
- `member_found_by`
- `manual_review_required`
- `warning_count`
- `error`

If a member exists, the script reports only `member_exists=true`, `member_found_by`, and `normalized_member_no_length`. If not found, it reports `member_exists=false`.

## Guardrails

- Read-only lookup only.
- AC2 is source of truth.
- Old POS and side sheet data are reference-only.
- AutoCount `MobilePhone` is intentionally unused for this duplicate-check path.
- This is intended for future n8n/local bridge duplicate checking.
- Self-hosted local n8n should call this lookup directly with `MemberNoBase64Utf8`; cloud n8n cannot call local AC2 PowerShell unless routed through a separately approved local bridge.
- It does not create/update/delete members.
- It must not be used as final write automation.
- Do not use this probe for production member creation.
- Do not paste raw runtime errors if they include local secrets or member data; rerun with sanitized output only.

See `docs/autocount2-automation/member_intake_n8n_direct_lookup_runbook.md` for the n8n routing contract.
