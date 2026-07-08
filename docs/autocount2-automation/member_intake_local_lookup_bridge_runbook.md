# Member Intake Local Lookup Bridge Runbook

Status: design and dry-run worker skeleton only. Gate 3 local lookup preflight pass recorded. This does not activate production automation and does not authorize AutoCount member writes.

## Purpose

This runbook defines the long-term AC2 member lookup bridge pattern for member-intake duplicate-check review. Google Forms / Google Sheets are a temporary UAT intake surface only; the bridge contract should also survive a future custom web form or hosted intake API.

The orchestration brain may be cloud n8n, VPS n8n, or another non-AC2 n8n runtime. That orchestrator must not be expected to load AutoCount assemblies or run local AC2 PowerShell directly. A tiny locked-down Windows bridge on the AutoCount host, or on an approved Windows host with the installed AutoCount Accounting 2.x runtime, is the only component allowed to load AutoCount assemblies.

This replaces the earlier local-only proof direction where n8n was assumed to run on the AutoCount Windows host. Hosting n8n on the AutoCount host is not the final architecture for this member lookup path.

## Business Boundary

- AC2 / AutoCount 2.0 remains the source of truth for member existence.
- Google Form mobile/member number maps to AutoCount `MemberNo`.
- AutoCount `MobilePhone` remains intentionally unused for this duplicate-check path.
- Birthday Month maps to future AC2 `DOB` as `2000-MM-01`, but DOB is outside this lookup-only bridge step.
- `PDPA Acknowledged = Yes` is valid new-form consent.
- `PDPA Acknowledged = Imported` is a legacy/import marker only and remains blocked.
- `READY_FOR_CREATE_REVIEW` is a review state only. It is not approval to create a member.

## Preferred Architecture

```text
cloud/VPS/non-AC2 n8n
  -> pending lookup queue
  <- sanitized result queue/status

locked-down Windows AC2 lookup bridge
  -> outbound poll pending jobs
  -> call scripts/ac2_member_lookup_review.ps1 in lookup mode only
  -> post sanitized result metadata back
```

Preferred pattern: outbound polling from the Windows bridge. The AC2 host does not expose a public inbound webhook. The bridge periodically asks an approved queue for `PENDING_LOOKUP` jobs, marks a job as in progress using an idempotency key or lease, runs the read-only lookup script, and posts back only sanitized status metadata.

Future development and integration may use a narrow protected queue/API surface over HTTPS. Cloudflare Tunnel / reverse proxy may front that queue/API surface, including a queue API running on the operator local dev PC behind `cloudflared`, as long as the protected surface exposes only sanitized queue/result operations and never exposes AC2, AutoCount, PowerShell, SQL, RDP, or member write paths.

Any future tunneled queue/API surface must require Cloudflare Access/service-token or equivalent machine authentication, rate limits, audit logging, a least-privilege request/response schema, and a documented rollback/disable procedure. Do not put real tunnel config, URLs, account IDs, Access client IDs/secrets, service tokens, credentials, or secrets in this repo or PR evidence.

The future queue/API polling model is:

```text
n8n writes sanitized PENDING_LOOKUP jobs to the queue API over HTTPS
AC2 bridge polls the queue API outbound over HTTPS
AC2 bridge claims one job at a time with state/lease fields
AC2 bridge runs read-only AutoCount lookup locally
AC2 bridge posts sanitized result back to the queue API over HTTPS
n8n reads/routes the sanitized result
```

For UAT, polling may be manual or run by Windows Task Scheduler every 1 minute. For production, prefer a long-running Windows service/worker polling every 15-60 seconds with idle backoff. Hourly polling is too slow for the intake duplicate-check flow and should not be the default.

The queue can be implemented later with an approved cloud queue, protected queue API, n8n data table, or similar service. This PR does not choose or configure a live queue provider.

## Protected Tunnel Boundary

Cloudflare Tunnel / reverse proxy is allowed as future/dev architecture only for the protected queue/API surface. It must terminate on the queue/API layer, not on the AC2 runtime, AutoCount process, PowerShell runner, SQL Server, RDP, file shares, or any member create/update/delete/write path.

The protected queue/API must have Cloudflare Access/service-token or equivalent machine authentication, rate limits, audit logging, a least-privilege request/response schema, and a rollback/disable procedure before any tunneled development or integration run is approved.

Direct inbound webhook, tunnel, reverse proxy, VPN callback, or public route to the Windows AC2 bridge host remains forbidden for this lookup path unless a separate future security review explicitly approves a private-network exception. A public inbound webhook on the AutoCount host is not recommended.

## n8n Runtime Boundary

- n8n can orchestrate Google Sheet intake, validation, queue creation, reviewer notifications, and review-only status updates.
- n8n may later run on a dev PC, VPS, hosted machine, or other non-AC2 environment.
- n8n does not need AC2 environment variables, AutoCount assemblies, direct SQL access, or local PowerShell execution.
- Only the Windows AC2 bridge host has AC2 environment variables and the AutoCount runtime needed for lookup.
- Cloud n8n cannot directly run local AC2 PowerShell.
- n8n Execute Command runs on the n8n host/container where n8n runs, not on the AutoCount host.
- Therefore a cloud/VPS/non-AC2 n8n Execute Command node is invalid for local AC2 lookup.
- n8n must not store raw member values, normalized member values, AutoCount credentials, local host details, or full bridge command text in workflow data.
- Hosted/cloud/VPS n8n must not use Execute Command for AC2 lookup.
- No public inbound webhook, tunnel, reverse proxy, or callback should be exposed to the AC2 host for this bridge path.
- A future protected queue/API may be exposed over HTTPS through Cloudflare Tunnel / reverse proxy, but only for sanitized queue/result operations. It must not proxy AC2, AutoCount, PowerShell, SQL, RDP, or member write paths.

## Windows Bridge Boundary

The bridge is intentionally small:

- runs only on the AutoCount host or an approved Windows host that can load the installed AutoCount assemblies,
- polls outbound over HTTPS for pending lookup jobs when a future queue/API is approved,
- accepts only the allowed request fields defined in [member_intake_n8n_node_contract.md](member_intake_n8n_node_contract.md),
- calls `scripts/ac2_member_lookup_review.ps1` only with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8`,
- relies on runtime-only local configuration for AC2 target settings and password,
- returns only allowed response fields,
- never writes to AutoCount,
- never emits raw stdout/stderr if it could contain local data.

The existing PowerShell lookup script is the only AC2-facing lookup implementation in scope. It calls the proven read-only member lookup path and returns sanitized JSON.

## Request Contract Summary

Allowed request fields:

| Field | Purpose | Logging |
| --- | --- | --- |
| `job_id` | Queue job identity and idempotency key. | Safe if non-PII. |
| `intake_source` | Source label for the intake surface. | Safe only if non-PII. |
| `source_reference` | Source-agnostic row/submission reference. | Safe only if non-PII. |
| `source_row_ref` | Optional row/reference label. | Safe only if non-PII. |
| `row_number` | Spreadsheet row metadata for Google Sheets UAT only. | Safe review metadata. |
| `intake_id` | Optional internal idempotency key. | Safe only if non-PII. |
| `state` | Must be `PENDING_LOOKUP` for new work. | Safe. |
| `submitted_member_no_base64_utf8` | Encoded submitted member value. | Sensitive operational data; never log or echo. |
| `consent_status` | Optional separate consent/marketing category; not a PDPA override. | Safe category only. |
| `pdpa_status` | Mandatory valid new-form PDPA acknowledgement gate. Current form `PDPA Acknowledged = Yes` normalizes to `yes`. | Safe category only. |
| `attempt` | Retry attempt counter. | Safe. |
| `created_at` | Queue metadata. | Safe if non-PII. |

Forbidden request fields include raw member numbers, normalized member numbers, names, emails, raw phone numbers, DOB, addresses, AutoCount internal identifiers, local server/database/user details, connection strings, passwords, tokens, stderr, and arbitrary payload dumps.

## Response Contract Summary

Allowed response fields:

| Field | Purpose |
| --- | --- |
| `job_id` |
| `intake_source` |
| `source_reference` |
| `source_row_ref` |
| `row_number` |
| `state` |
| `status` |
| `authentication_success` |
| `user_session_available` |
| `member_command_found` |
| `get_member_found` |
| `submitted_member_no_status` |
| `normalized_member_no_length` |
| `member_exists` |
| `member_found_by` |
| `manual_review_required` |
| `warning_count` |
| `error_code` |
| `consent_status` |
| `pdpa_status` |
| `attempt` |
| `dry_run_only` |
| `final_write_automation` |

Forbidden response fields are the same as the forbidden request fields. The bridge must not return the encoded member value either.

## Job States

| State | Meaning |
| --- | --- |
| `PENDING_LOOKUP` | n8n queued a review-only lookup request. |
| `LOOKUP_IN_PROGRESS` | Bridge claimed exactly one job with lease metadata before local lookup. |
| `LOOKUP_ERROR_REVIEW` | Process failure, timeout, invalid JSON, schema mismatch, sanitized lookup error, or retry exhaustion. |
| `MANUAL_REVIEW_REQUIRED` | Lookup or normalization requires human review before any create review state. |
| `EXISTING_MEMBER_REVIEW` | AC2 lookup found an existing `MemberNo`; route to duplicate review. |
| `READY_FOR_CREATE_REVIEW` | Lookup did not find the member and emitted no warning; still review-only. |

No state authorizes member creation.

## Retry And Idempotency

- `job_id` or `intake_id` must be stable and non-PII.
- The queue should atomically move exactly one job from `PENDING_LOOKUP` to `LOOKUP_IN_PROGRESS` before lookup so two bridge instances do not process it at the same time.
- The same job payload should produce the same sanitized result.
- A job with the same idempotency key and different payload hash must be rejected or held for review.
- Timeouts and transient local failures may retry up to a small configured limit.
- If the `LOOKUP_IN_PROGRESS` lease expires before a result is posted, a timeout/retry sweep may return the job to `PENDING_LOOKUP` with incremented attempt metadata until the retry cap is reached.
- Retry exhaustion routes to `LOOKUP_ERROR_REVIEW`, not to a create-review state.
- Result posting should be idempotent; duplicate posts for the same payload hash should return the prior result.

## Logging Rules

Bridge logs may contain:

- job id,
- row number,
- intake id if non-PII,
- state transition,
- attempt count,
- sanitized status code,
- sanitized error code,
- timestamp,
- duration.

Bridge logs must not contain:

- raw member/mobile values,
- encoded member values,
- normalized member values,
- names,
- emails,
- DOB values,
- raw phone numbers,
- addresses,
- AutoCount internal identifiers,
- AutoCount host/database/user details,
- credentials,
- connection strings,
- command arguments,
- unsanitized stderr/stdout.

## Dry-Run Worker Skeleton

`scripts/ac2_member_lookup_bridge_worker.py` is a disabled-by-default skeleton for this design.

Safe review modes:

- default invocation refuses to run,
- fixture queue mode processes local synthetic JSON jobs only after `--enable-local-lookup-bridge-review`,
- mock lookup mode returns sanitized fixture results for tests and design review,
- PowerShell lookup mode additionally requires `--enable-powershell-lookup` and calls only `scripts/ac2_member_lookup_review.ps1` in lookup mode.

The skeleton contains no real endpoint, credential, queue provider, tunnel, webhook, or write path. The first UAT may model a Google Sheets queue, but the fixture fields include source-agnostic references so a later custom web form or hosted intake API can reuse the bridge contract. This runbook does not implement that future form/API or synchronous duplicate rejection.

`PDPA Acknowledged = Imported` must remain blocked even if `consent_status` looks acknowledged. `consent_status` is optional sanitized metadata for non-PDPA consent/marketing categories and must not rescue or override invalid, missing, or imported `pdpa_status`.

## Local Fixture UAT Pass

Use this pass after the worker code is reviewed and merged, while the bridge remains fixture-only, dry-run, review-only, and inactive. It does not call Google APIs, does not read a real Sheet, does not call n8n, does not run PowerShell lookup mode, and does not write to AutoCount.

Create only synthetic local fixture files under:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
```

Create `member_lookup_bridge_fixture_jobs.jsonl` locally. Generate the encoded submitted value on the AC2 bridge host and do not commit or paste it. The placeholder below is replaced in memory before writing the local ignored fixture file:

```powershell
$safeFixturePlaintext = 'SYNTHETIC'
$localGeneratedSafeFixtureValue = [Convert]::ToBase64String(
  [System.Text.Encoding]::UTF8.GetBytes($safeFixturePlaintext)
)
if ($localGeneratedSafeFixtureValue -notmatch '^[A-Za-z0-9+/]+={0,2}$') {
  throw 'Generated fixture value failed the base64 shape check.'
}

$fixtureJobsTemplate = @"
{"job_id":"job-uat-ready","intake_source":"google_sheets_uat","source_reference":"uat-queue-ready","source_row_ref":"row-ready","row_number":2,"intake_id":"intake-uat-ready","state":"PENDING_LOOKUP","submitted_member_no_base64_utf8":"<local-generated-safe-fixture-value>","consent_status":"acknowledged","pdpa_status":"yes","payload_hash":"hash-uat-ready","attempt":0,"max_attempts":3,"created_at":"fixture-created-at","updated_at":"fixture-updated-at","lease_owner":"fixture-bridge","lease_expires_at":"fixture-lease-expires-at","timeout_at":"fixture-timeout-at","last_error_code":null}
{"job_id":"job-uat-existing","intake_source":"google_sheets_uat","source_reference":"uat-queue-existing","source_row_ref":"row-existing","row_number":3,"intake_id":"intake-uat-existing","state":"PENDING_LOOKUP","submitted_member_no_base64_utf8":"<local-generated-safe-fixture-value>","consent_status":"acknowledged","pdpa_status":"yes","payload_hash":"hash-uat-existing","attempt":0,"max_attempts":3,"created_at":"fixture-created-at","updated_at":"fixture-updated-at","lease_owner":"fixture-bridge","lease_expires_at":"fixture-lease-expires-at","timeout_at":"fixture-timeout-at","last_error_code":null}
{"job_id":"job-uat-manual","intake_source":"google_sheets_uat","source_reference":"uat-queue-manual","source_row_ref":"row-manual","row_number":4,"intake_id":"intake-uat-manual","state":"PENDING_LOOKUP","submitted_member_no_base64_utf8":"<local-generated-safe-fixture-value>","consent_status":"acknowledged","pdpa_status":"yes","payload_hash":"hash-uat-manual","attempt":0,"max_attempts":3,"created_at":"fixture-created-at","updated_at":"fixture-updated-at","lease_owner":"fixture-bridge","lease_expires_at":"fixture-lease-expires-at","timeout_at":"fixture-timeout-at","last_error_code":null}
{"job_id":"job-uat-error","intake_source":"google_sheets_uat","source_reference":"uat-queue-error","source_row_ref":"row-error","row_number":5,"intake_id":"intake-uat-error","state":"PENDING_LOOKUP","submitted_member_no_base64_utf8":"<local-generated-safe-fixture-value>","consent_status":"acknowledged","pdpa_status":"yes","payload_hash":"hash-uat-error","attempt":0,"max_attempts":3,"created_at":"fixture-created-at","updated_at":"fixture-updated-at","lease_owner":"fixture-bridge","lease_expires_at":"fixture-lease-expires-at","timeout_at":"fixture-timeout-at","last_error_code":null}
{"job_id":"job-uat-imported-pdpa","intake_source":"google_sheets_uat","source_reference":"uat-queue-imported-pdpa","source_row_ref":"row-imported-pdpa","row_number":6,"intake_id":"intake-uat-imported-pdpa","state":"PENDING_LOOKUP","submitted_member_no_base64_utf8":"<local-generated-safe-fixture-value>","consent_status":"acknowledged","pdpa_status":"imported","payload_hash":"hash-uat-imported-pdpa","attempt":0,"max_attempts":3,"created_at":"fixture-created-at","updated_at":"fixture-updated-at","lease_owner":"fixture-bridge","lease_expires_at":"fixture-lease-expires-at","timeout_at":"fixture-timeout-at","last_error_code":null}
"@
$fixtureJobs = $fixtureJobsTemplate.Replace(
  '<local-generated-safe-fixture-value>',
  $localGeneratedSafeFixtureValue
)
[System.IO.File]::WriteAllText(
  (Join-Path $root 'member_lookup_bridge_fixture_jobs.jsonl'),
  $fixtureJobs,
  $utf8NoBom
)
```
Create `member_lookup_bridge_mock_results.jsonl`:

```powershell
$mockResults = @'
{"job_id":"job-uat-existing","status":"ok","authentication_success":true,"user_session_available":true,"member_command_found":true,"get_member_found":true,"submitted_member_no_status":"canonical_65_mobile","normalized_member_no_length":10,"member_exists":true,"member_found_by":null,"manual_review_required":false,"warning_count":0}
{"job_id":"job-uat-manual","status":"ok","authentication_success":true,"user_session_available":true,"member_command_found":true,"get_member_found":true,"submitted_member_no_status":"manual_review","normalized_member_no_length":0,"member_exists":false,"member_found_by":null,"manual_review_required":true,"warning_count":1}
{"job_id":"job-uat-error","status":"error","authentication_success":true,"user_session_available":true,"member_command_found":true,"get_member_found":true,"submitted_member_no_status":"canonical_65_mobile","normalized_member_no_length":10,"member_exists":false,"member_found_by":null,"manual_review_required":false,"warning_count":0,"error_code":"mock_lookup_error"}
'@
[System.IO.File]::WriteAllText(
  (Join-Path $root 'member_lookup_bridge_mock_results.jsonl'),
  $mockResults,
  $utf8NoBom
)
```

Run fixture/mock mode from the repository root:

```powershell
python scripts\ac2_member_lookup_bridge_worker.py `
  --enable-local-lookup-bridge-review `
  --queue-mode fixture `
  --fixture-jobs "$root\member_lookup_bridge_fixture_jobs.jsonl" `
  --fixture-mock-results "$root\member_lookup_bridge_mock_results.jsonl" `
  --results-jsonl "$root\member_lookup_bridge_results.jsonl"
```

Expected local result states:

| Synthetic job | Expected state | Meaning |
| --- | --- | --- |
| `job-uat-ready` | `READY_FOR_CREATE_REVIEW` | Review-only candidate; not approval to create. |
| `job-uat-existing` | `EXISTING_MEMBER_REVIEW` | Duplicate review path. |
| `job-uat-manual` | `MANUAL_REVIEW_REQUIRED` | Manual review path. |
| `job-uat-error` | `LOOKUP_ERROR_REVIEW` | Sanitized lookup error path. |
| `job-uat-imported-pdpa` | `LOOKUP_ERROR_REVIEW` | Imported PDPA remains blocked even when `consent_status` is `acknowledged`. |

Paste back only this sanitized summary evidence after reviewing the local output file for accidental sensitive fields:

```json
{
  "status": "ok",
  "queue_mode": "fixture",
  "lookup_mode": "mock",
  "processed_count": 5,
  "result_state_counts": {
    "READY_FOR_CREATE_REVIEW": 1,
    "EXISTING_MEMBER_REVIEW": 1,
    "MANUAL_REVIEW_REQUIRED": 1,
    "LOOKUP_ERROR_REVIEW": 2
  },
  "dry_run_only": true,
  "final_write_automation": false,
  "sanitized_note": "Local result output was reviewed; raw member values, encoded submitted values, normalized member values, credentials, Sheet IDs, Sheet URLs, and row-level result details were not present in the pasted evidence."
}
```

Do not paste result rows, raw fixture input, encoded submitted values, raw member values, normalized member values, names, emails, raw phone numbers, credentials, real Sheet IDs or URLs, local AC2 target values, command transcripts, or full command output if it contains row-level detail. Keep the fixture and result JSONL files local under `C:\XB\autocount_outputs\review\member_lookup_bridge`.

## Gate 3 Local PowerShell Lookup Preflight

Use this pass after the fixture/mock bridge pass and the dummy n8n wiring rehearsal evidence are recorded. Gate 3 is still pre-production, manual, read-only, and local-only. It prepares the next AC2-facing readiness proof; it is not a production queue run and does not authorize Gate 4.

Operator boundaries:

- Run only on the local Windows AC2 lookup environment or approved AC2-capable Windows bridge host.
- Use the bridge worker in fixture queue mode with PowerShell lookup mode explicitly enabled.
- Use only safe synthetic input or one manually approved dummy-only lookup input generated and stored locally.
- Call only `scripts/ac2_member_lookup_review.ps1` with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8`.
- Keep local runtime settings and `AC2_PROBE_PASSWORD` out of repo files and pasted evidence.
- Review local output files only to reduce them to aggregate booleans and counts.
- Do not paste result rows, raw fixture rows, raw member values, encoded member values, normalized member values, names, emails, raw phone numbers, local target values, command transcripts, stderr/stdout, credentials, connection strings, Sheet IDs, Sheet URLs, node payloads, or PII.

Gate 3 verifies only these readiness points:

- local Windows preflight environment is available,
- AutoCount session/auth bootstrap path is available,
- `MemberCommand` is available,
- `MemberCommand.GetMember` lookup path is available,
- lookup remains read-only,
- no member create/update/delete path is invoked,
- no AutoCount write or direct SQL write is attempted,
- n8n is not involved and did not call the bridge,
- output evidence is sanitized and aggregate-only.

Required Gate 3 paste-back shape:

```text
status = ok
gate = gate3_local_powershell_lookup_preflight
runtime_location = local_windows_ac2_lookup_environment
execution_mode = manual_read_only_preflight
autocount_session_bootstrap_available = <true/false>
member_command_found = <true/false>
get_member_found = <true/false>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_manual_review_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
n8n_involved = false
bridge_called_by_n8n = false
final_write_automation = false
sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, or PII are pasted.
```

Do not paste the bridge worker stdout directly as Gate 3 evidence. The worker may write local fixture/result files for operator review, but Gate 3 evidence must be the aggregate shape above. A result state such as `READY_FOR_CREATE_REVIEW` remains review-only and is not approval to create.

Recorded Gate 3 sanitized evidence:

```text
status = ok
gate = gate3_local_powershell_lookup_preflight
runtime_location = local_windows_ac2_lookup_environment
execution_mode = manual_read_only_preflight
autocount_session_bootstrap_available = true
member_command_found = true
get_member_found = true
lookup_attempt_count = 1
lookup_success_count = 1
lookup_manual_review_count = 1
lookup_error_count = 0
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
n8n_involved = false
bridge_called_by_n8n = false
final_write_automation = false
sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, or PII are pasted.
```

The pasted operator output contained only local PowerShell prompt wrapper noise around the sanitized body and no sensitive values. The wrapper noise is intentionally not recorded.

This pass proves only that the local Windows AC2 lookup environment was available, AutoCount session/auth bootstrap was available, `MemberCommand` was found, `MemberCommand.GetMember` was found, one lookup attempt succeeded, the result was manual-review rather than an error, and no create/update/write/direct SQL/n8n/final automation path was invoked.

This pass does not prove production automation, does not authorize member create/update, does not authorize AutoCount writes, and does not by itself prove hosted/VPS n8n runtime readiness.

Gate 4 remains not approved to run until the reviewed Gate 4 plan PR is merged and the operator gives explicit run approval. Passing Gate 3 is readiness evidence for the plan review, not approval to run a real queue UAT.

## Gate 3B AC2 Local Bridge Readiness

Status: AC2-side local bridge readiness only. This is not Gate 4A, not n8n evidence, not Google Sheets evidence, and not production activation.

Gate 3B proves the local bridge path from one ignored queue row through the read-only PowerShell lookup and back to an aggregate-only evidence summary:

```text
local one-row ignored queue JSONL
-> scripts/ac2_member_lookup_bridge_worker.py
-> read-only PowerShell lookup
-> local sanitized result JSONL
-> aggregate-only evidence summary
```

Gate 3B does not require or use n8n, Google Sheets, hosted n8n, a queue API, Cloudflare Tunnel, `cloudflared`, a hosted service, a scheduler, a webhook, result mapping, a public inbound path, or final write automation. It also does not create, update, delete, or otherwise write AutoCount members, and it does not perform direct SQL writes.

Later integration may link non-AC2 n8n to the bridge through a shared queue/result surface exposed by a narrow protected queue/API over HTTPS:

```text
n8n writes sanitized PENDING_LOOKUP jobs to the queue API
AC2 bridge polls/reads the queue API outbound over HTTPS
AC2 bridge claims one job at a time using state/lease fields
AC2 bridge writes sanitized results back to the queue API
n8n reads/routes the sanitized result
```

The later n8n runtime may be a dev PC, VPS, hosted machine, or other non-AC2 environment. It does not need AC2 environment variables, AutoCount assemblies, direct SQL access, or local PowerShell execution. Only the Windows AC2 bridge host has AC2 environment variables and the AutoCount runtime. Hosted/cloud/VPS n8n must not use Execute Command for AC2 lookup, and no public inbound webhook, tunnel, or reverse proxy may expose the AC2 host.

Cloudflare Tunnel / reverse proxy may be used for development and likely integration only for the protected queue/API surface. For development, the queue API may run on the operator local dev PC behind `cloudflared`. The tunnel must not expose AC2, AutoCount, PowerShell, SQL, RDP, or any member write path.

Use one operator-approved test member/member-number value. Do not paste the raw, encoded, decoded, or normalized value into commands, docs, PR comments, screenshots, tickets, or evidence.

Prepare one local ignored queue row on the Windows AC2 bridge host:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$secureValue = Read-Host -AsSecureString 'Enter one approved test member/member-number value'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
try {
  $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  $plainValue | python scripts\member_lookup_gate3b_prepare_local_queue.py `
    --member-value-stdin `
    --queue-jsonl "$root\member_lookup_bridge_gate3b_pending_queue.jsonl"
} finally {
  if ($bstr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
  Remove-Variable plainValue -ErrorAction SilentlyContinue
}
```

The helper output must be aggregate-only:

```json
{
  "status": "ok",
  "gate": "gate3b_ac2_local_bridge_readiness_queue_prep",
  "queue_file_written": true,
  "queue_row_count": 1,
  "encoded_present_count": 1,
  "no_row_values_printed": true,
  "n8n_required": false,
  "google_sheets_required": false,
  "dry_run_only": true,
  "final_write_automation": false
}
```

Run the local AC2 bridge worker in read-only PowerShell lookup mode:

```powershell
python scripts\ac2_member_lookup_bridge_worker.py `
  --enable-local-lookup-bridge-review `
  --queue-mode fixture `
  --fixture-jobs "$root\member_lookup_bridge_gate3b_pending_queue.jsonl" `
  --results-jsonl "$root\member_lookup_bridge_gate3b_results.jsonl" `
  --lookup-mode powershell `
  --enable-powershell-lookup `
  --allow-root-login
```

Then reduce the local result JSONL to aggregate-only Gate 3B evidence:

```powershell
python scripts\member_lookup_gate3b_evidence_summary.py `
  --results-jsonl "$root\member_lookup_bridge_gate3b_results.jsonl" `
  --local-queue-row-count 1 `
  --local-queue-rows-loaded-count 1
```

Required Gate 3B paste-back shape:

```text
status = <ok/needs_fix>
gate = gate3b_ac2_local_bridge_readiness
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_one_row_read_only_lookup
local_queue_row_count = <aggregate-count-only>
local_queue_rows_loaded_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_ready_for_create_review_count = <aggregate-count-only>
lookup_existing_member_review_count = <aggregate-count-only>
lookup_manual_review_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
local_review_result_rows_written_count = <aggregate-count-only>
n8n_required = false
n8n_involved = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
public_inbound_to_ac2_host = false
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
no_row_values_printed = true
sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/decoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, or PII are pasted.
```

Do not paste the local queue row, local result row, raw member value, encoded member value, decoded member value, normalized member value, command transcript, stdout/stderr transcript, node payload, credential value, server/database/user/password value, Sheet ID/URL, or screenshot with row-level data. Keep `member_lookup_bridge_gate3b_pending_queue.jsonl` and `member_lookup_bridge_gate3b_results.jsonl` local and ignored.

Gate 3B proves only that the Windows AC2 bridge host can safely process one local queued lookup through the read-only lookup path and produce sanitized local result evidence. It does not approve Gate 4A, n8n setup, Google Sheets lookup queue use, queue API use, Cloudflare Tunnel / `cloudflared` use, hosted/VPS runtime readiness, result mapping, member create/update, AutoCount writes, direct SQL writes, scheduler activation, webhook activation, or final write automation.

## Review-Only Routing

The bridge posts results for review routing only:

- `LOOKUP_ERROR_REVIEW`: operator investigates lookup failure.
- `MANUAL_REVIEW_REQUIRED`: operator reviews unusual member number shape or lookup warning.
- `EXISTING_MEMBER_REVIEW`: operator reviews possible duplicate.
- `READY_FOR_CREATE_REVIEW`: operator may review as a new-member candidate, but this is not create approval.

Real create/update automation remains blocked pending a separate PR, explicit business approval, idempotency and consent/audit handling, production write guardrails, and an independently reviewed activation plan.

## Out Of Scope

- production queue provider selection,
- protected queue/API deployment,
- Cloudflare Tunnel / reverse proxy setup,
- public inbound webhook to the AC2 host,
- direct tunnel to AC2, AutoCount, PowerShell, SQL, RDP, or member write paths,
- production n8n workflow export,
- AutoCount member writes,
- direct database write paths,
- real endpoints,
- secrets,
- row-level output commits,
- production activation.
