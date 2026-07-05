# Member Intake n8n Direct Lookup Runbook

Status: local self-hosted n8n lookup contract only. This is not final write automation.

## Purpose

Future runtime direction is self-hosted local n8n calling `scripts/ac2_member_lookup_review.ps1` directly on the Windows AutoCount host, or on a locked-down local host that can load the approved AutoCount Accounting 2.x assemblies.

AC2 / AutoCount 2.0 is the source of truth. Google Form mobile/member number maps to AutoCount `MemberNo`. AutoCount `MobilePhone` is intentionally unused. Birthday Month maps to future `DOB` as `2000-MM-01`, but DOB is outside this lookup contract. Old POS and side sheet data are reference-only.

This runbook does not create a production n8n workflow, does not create/update/delete AutoCount members, and does not authorize final write automation.

## Runtime Boundary

- Use only local self-hosted n8n that runs where it can execute the PowerShell lookup script safely.
- Cloud n8n cannot call local AC2 PowerShell unless the call is routed through a separately approved local bridge, private tunnel, VPN, or equivalent reviewed local adapter.
- The local n8n runtime must treat form-submitted member values as untrusted input.
- n8n should pass `MemberNoBase64Utf8`, not raw `MemberNo`, for Google Form values.
- `AC2_PROBE_PASSWORD` must be configured as a local environment secret. Do not pass it in command arguments, node parameters, workflow text, logs, or JSON payloads.
- The PowerShell output is sanitized JSON only. n8n must not expect names, email addresses, raw phone numbers, raw `MemberNo`, normalized `MemberNo`, DOB, address, AutoKey, Guid, server, database, user, or password values.

## Command Contract

For form-submitted values, encode the untrusted member/mobile value as UTF-8 base64 before invoking PowerShell, then pass it with `-MemberNoBase64Utf8`.

```powershell
$env:AC2_PROBE_PASSWORD = "<configured local secret>"

.\scripts\ac2_member_lookup_review.ps1 `
  -EnableMemberLookupReview `
  -MemberNoBase64Utf8 "<utf8-base64-member-number>" `
  -ServerName "<server>" `
  -DatabaseName "<database>" `
  -UserId "<user>"
```

Manual local tests may still use `-MemberNo`, but n8n form intake calls should use `-MemberNoBase64Utf8` to avoid interpolating raw Google Form values into a shell command.

Exactly one of `-MemberNo` or `-MemberNoBase64Utf8` must be supplied. Supplying both, supplying neither, or supplying invalid UTF-8 base64 returns sanitized JSON with `status = error`.

The explicit `-EnableMemberLookupReview` switch remains required. Without it, the script refuses to run before loading AutoCount assemblies.

## PowerShell Output

The script returns one sanitized JSON object with this schema:

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

The script calls `MemberCommand.GetMember(normalizedMemberNo)` only. It does not call `SaveMember`, `NewMember`, `DeleteMember`, `GetNextMemberNo`, member browse output, direct SQL, or DBSetting data methods.

## n8n Routing Contract

n8n should parse the sanitized JSON and route by status fields only:

- `status != ok`: lookup error review.
- `manual_review_required = true`: manual review.
- `member_exists = true`: existing member review.
- `member_exists = false` and `warning_count = 0`: ready for create review, but still no write automation.

Any missing field, invalid JSON, process failure, timeout, or unexpected stderr/stdout shape should route to lookup error review.

## Security Rationale

Base64 does not make the value secret. It reduces command-line quoting and shell interpolation risk by keeping raw Google Form values out of the command text. The PowerShell script still decodes as UTF-8, normalizes internally, validates shape and length, and emits only sanitized JSON.

The local runtime must still avoid logging the raw form value, the encoded value, command arguments, or unsanitized PowerShell errors.

## Out Of Scope

- Production n8n workflow creation.
- AutoCount member create/update/delete automation.
- Direct SQL.
- Cloud n8n direct access to local AC2 PowerShell.
- Any approval for final write automation.
