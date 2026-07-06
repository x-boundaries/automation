# Member Intake Local Direct Lookup Runbook

Status: local proof and bridge implementation detail only. This is not the long-term n8n hosting architecture and is not final write automation.

## Purpose

This runbook documents the proven local command shape for `scripts/ac2_member_lookup_review.ps1`. It may be used for manual local review and by the Windows AC2 lookup bridge. It should not be read as a recommendation to host n8n on the AutoCount host long-term.

See [member_intake_local_lookup_bridge_runbook.md](member_intake_local_lookup_bridge_runbook.md) for the preferred outbound polling bridge architecture, [member_intake_n8n_dry_run_workflow.md](member_intake_n8n_dry_run_workflow.md) for the n8n dry-run workflow outline, and [member_intake_n8n_node_contract.md](member_intake_n8n_node_contract.md) for the queue/routing contract.

AC2 / AutoCount 2.0 is the source of truth. Google Form mobile/member number maps to AutoCount `MemberNo`. AutoCount `MobilePhone` is intentionally unused. Birthday Month maps to future `DOB` as `2000-MM-01`, but DOB is outside this lookup contract. Old POS and side sheet data are reference-only.

This runbook does not create a production n8n workflow, does not create/update/delete AutoCount members, and does not authorize final write automation.

## Runtime Boundary

- The preferred architecture is cloud/VPS/non-AC2 n8n queueing lookup jobs for an outbound-polling Windows AC2 bridge.
- n8n should not be hosted on the AutoCount host as the final member lookup architecture.
- Cloud n8n cannot call local AC2 PowerShell directly.
- n8n Execute Command runs on the n8n host/container where n8n runs, not on the AutoCount host.
- A cloud/VPS/non-AC2 Execute Command node is invalid for local AC2 lookup.
- The Windows bridge may call this script locally after explicit bridge opt-in and local configuration.
- Any local proof or bridge runtime must treat form-submitted member values as untrusted input.
- The bridge should pass `MemberNoBase64Utf8`, not raw `MemberNo`, for Google Form values.
- The orchestrator and bridge must validate the encoded value against a strict base64-safe allowlist before shell execution.
- `AC2_PROBE_PASSWORD` must be configured as a local environment secret for the bridge runtime or Windows service account. Do not pass it in command arguments, node parameters, workflow text, logs, or JSON payloads.
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

Manual local tests may still use `-MemberNo`, but bridge form intake calls should use `-MemberNoBase64Utf8` to avoid interpolating raw Google Form values into a shell command.

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

The script calls `MemberCommand.GetMember(normalizedMemberNo)` only. It does not call member save, member create, member delete, member-number generation, member browse output, direct SQL, or DBSetting data methods.

## Review Routing Contract

n8n or the bridge result consumer should parse the sanitized JSON and route by status fields only:

- process failure, invalid JSON, missing fields, `status != ok`, or unexpected shape: `LOOKUP_ERROR_REVIEW`.
- `manual_review_required = true`: `MANUAL_REVIEW_REQUIRED`.
- `member_exists = true`: `EXISTING_MEMBER_REVIEW`.
- `member_exists = false` and `warning_count = 0`: `READY_FOR_CREATE_REVIEW`, but still no write automation.

When `status != ok`, route to lookup error review (`LOOKUP_ERROR_REVIEW`).
When `manual_review_required = true`, route to manual review (`MANUAL_REVIEW_REQUIRED`).
When `member_exists = true`, route to existing member review (`EXISTING_MEMBER_REVIEW`).
When `member_exists = false` and `warning_count = 0`, route to ready for create review (`READY_FOR_CREATE_REVIEW`).

Any missing field, invalid JSON, process failure, timeout, or unexpected stderr/stdout shape should route to lookup error review.

`READY_FOR_CREATE_REVIEW` is not approval to create. It is still only a review decision.

## Security Rationale

Base64 is not encryption and does not make the value secret. It reduces command-line quoting and shell interpolation risk by keeping raw Google Form values out of the command text. The PowerShell script still decodes as UTF-8, normalizes internally, validates shape and length, and emits only sanitized JSON.

The local runtime must still avoid logging the raw form value, the encoded value, full command arguments, credentials, connection details, or unsanitized PowerShell stderr.

## Out Of Scope

- Production n8n workflow creation.
- AutoCount member create/update/delete automation.
- Direct SQL.
- Cloud/VPS/non-AC2 n8n direct Execute Command access to local AC2 PowerShell.
- Any approval for final write automation.
