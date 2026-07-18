# Member Intake Local Lookup Bridge Runbook

Status: design and dry-run worker skeleton only. Gate 3 local lookup preflight and Gate 3B AC2 local bridge readiness passes recorded. Gate 3C adds a local-only runtime hardening harness for repeated AC2 host test runs, with fresh and duplicate-rerun evidence recorded. Gate 3D sanitized AC2 local bridge small-batch evidence (duplicate-seed setup, mixed-batch proof, and idempotent rerun) is recorded. Gate 3E adds a local-only service-readiness discipline wrapper (single-instance lock, operator stop switch, bounded cycles) without installing or activating any Windows service or scheduler; post-merge sanitized AC2 local bridge service-readiness evidence is recorded. Gate 3F records the bridge-side readiness checkpoint before returning to Gate 4A queue-write proof. Gate 4 adds the bridge-side real-queue lookup-only handoff that runs exactly one real read-only AutoCount lookup for the single already-approved Gate 4A queue row and stops, with aggregate-only evidence; it does not map results back to n8n. The first genuine Gate 4 attempt ended in `LOOKUP_ERROR_REVIEW` because the required `AC2_PROBE_*` runtime environment values were absent from that process; a separate reviewed failed-attempt recovery wrapper authorizes exactly one read-only retry into isolated recovery paths while preserving the original failed-attempt evidence unchanged. This does not activate production automation and does not authorize AutoCount member writes.

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

For the current single-machine deployment, where n8n runs on the main physical PC and AutoCount runs inside a Windows VM on that same PC, a private host-to-VM shared folder replaces the tunneled queue/API surface entirely for UAT. That topology, its boundaries, and its VM-side manual runner are documented in [member_intake_shared_folder_lookup_bridge_runbook.md](member_intake_shared_folder_lookup_bridge_runbook.md); it reuses the Gate 4A and Gate 5A contracts unchanged and introduces no network surface.

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

Recorded Gate 3B sanitized evidence:

```text
status = ok
gate = gate3b_ac2_local_bridge_readiness
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_one_row_read_only_lookup
local_queue_row_count = 1
local_queue_rows_loaded_count = 1
lookup_attempt_count = 1
lookup_success_count = 1
lookup_ready_for_create_review_count = 0
lookup_existing_member_review_count = 0
lookup_manual_review_count = 1
lookup_error_count = 0
local_review_result_rows_written_count = 1
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
```

This pass used one local synthetic/manual-review-shaped value. No row-level data, raw/encoded/decoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, screenshots, credentials, Sheet IDs/URLs, or PII are recorded.

Gate 3B proves only that the Windows AC2 bridge host can safely process one local queued lookup through the read-only lookup path and produce sanitized local result evidence. It does not approve Gate 4A, n8n setup, Google Sheets lookup queue use, queue API use, Cloudflare Tunnel / `cloudflared` use, hosted/VPS runtime readiness, result mapping, member create/update, AutoCount writes, direct SQL writes, scheduler activation, webhook activation, or final write automation.

## Gate 3C AC2 Local Bridge Runtime Hardening

Status: AC2-side local bridge runtime hardening only. This is not Gate 4A, not n8n evidence, not Google Sheets evidence, not a queue API, not hosted/VPS runtime readiness, not scheduler activation, and not final automation.

Gate 3C wraps the existing read-only lookup bridge worker with local filesystem paths that can be run repeatedly on the AC2 machine before n8n exists:

```text
local ignored pending queue JSONL
-> scripts/member_lookup_gate3c_local_bridge_runtime.py
-> read-only PowerShell lookup through scripts/ac2_member_lookup_review.ps1
-> local ignored sanitized results JSONL
-> local ignored processed idempotency markers
-> local ignored failed/dead-letter markers
-> aggregate-only evidence
```

The local pending queue must use only the allowed bridge request fields. The harness rejects extra fields, forbidden sensitive fields, invalid PDPA status, invalid base64 shape, retry exhaustion, and idempotency payload-hash conflicts into local failed/dead-letter handling. It appends sanitized result rows only for newly handled jobs. Re-running the same pending file should count already handled jobs as duplicates instead of running lookup again or appending duplicate result rows.

Gate 3C does not require or use n8n, Google Sheets, a queue API, Cloudflare Tunnel, `cloudflared`, a hosted service, a scheduler, a webhook, result mapping, a public inbound path, or final write automation. It also does not create, update, delete, or otherwise write AutoCount members, and it does not perform direct SQL writes.

Mock mode is allowed only for local harness validation and automated tests. Mock-mode evidence is not Gate 3C AC2 runtime pass evidence.

Operator local paths stay ignored under:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$pending = "$root\member_lookup_bridge_gate3c_pending_queue.jsonl"
$results = "$root\member_lookup_bridge_gate3c_results.jsonl"
$processed = "$root\member_lookup_bridge_gate3c_processed"
$failed = "$root\member_lookup_bridge_gate3c_failed"
```

Exact operator run command for the manual local AC2 bridge runtime test:

```powershell
python scripts\member_lookup_gate3c_local_bridge_runtime.py `
  --enable-local-bridge-runtime-review `
  --pending-jsonl "$pending" `
  --results-jsonl "$results" `
  --processed-dir "$processed" `
  --failed-dir "$failed" `
  --lookup-mode powershell `
  --enable-powershell-lookup `
  --allow-root-login
```

Required Gate 3C paste-back shape:

```text
status = <ok/needs_fix/no_work/dry_run_only/already_processed>
gate = gate3c_ac2_local_bridge_runtime_hardening
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_filesystem_runtime_hardening
lookup_mode = <mock/powershell>
powershell_lookup_enabled = <true/false>
pending_rows_loaded_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
processed_or_archived_count = <aggregate-count-only>
failed_or_dead_letter_count = <aggregate-count-only>
duplicate_or_already_processed_count = <aggregate-count-only>
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

Real Gate 3C AC2 runtime pass evidence requires all of the following:

- `status = ok`
- `lookup_mode = powershell`
- `powershell_lookup_enabled = true`
- `pending_rows_loaded_count >= 1`
- for a fresh run, `lookup_attempt_count >= 1` and `lookup_success_count >= 1`
- `lookup_error_count = 0`
- `failed_or_dead_letter_count = 0`

`status = no_work` means no pending rows were loaded. `status = no_work` is not Gate 3C pass evidence. `status = dry_run_only` means the harness ran without real PowerShell lookup evidence, such as mock mode, and is not Gate 3C pass evidence.

Duplicate-only rerun evidence requires `pending_rows_loaded_count >= 1`, `lookup_attempt_count = 0`, `lookup_success_count = 0`, `lookup_error_count = 0`, `processed_or_archived_count = 0`, `failed_or_dead_letter_count = 0`, and `duplicate_or_already_processed_count >= 1`. In that case the harness prints `status = already_processed`. `status = already_processed` is not fresh Gate 3C AC2 lookup pass evidence. Duplicate-only evidence proves local idempotency only. The bridge recognized already handled work, did not run another lookup, and did not append duplicate result rows.

Recorded Gate 3C fresh PowerShell lookup evidence:

```text
status = ok
gate = gate3c_ac2_local_bridge_runtime_hardening
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_filesystem_runtime_hardening
lookup_mode = powershell
powershell_lookup_enabled = true
pending_rows_loaded_count = 1
lookup_attempt_count = 1
lookup_success_count = 1
lookup_error_count = 0
processed_or_archived_count = 1
failed_or_dead_letter_count = 0
duplicate_or_already_processed_count = 0
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

Recorded Gate 3C duplicate/idempotency rerun evidence:

```text
status = already_processed
gate = gate3c_ac2_local_bridge_runtime_hardening
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_filesystem_runtime_hardening
lookup_mode = powershell
powershell_lookup_enabled = true
pending_rows_loaded_count = 1
lookup_attempt_count = 0
lookup_success_count = 0
lookup_error_count = 0
processed_or_archived_count = 0
failed_or_dead_letter_count = 0
duplicate_or_already_processed_count = 1
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

The fresh run is the Gate 3C AC2 local runtime pass evidence. The duplicate rerun is idempotency evidence only and is not fresh AC2 lookup pass evidence. Together they prove the local bridge can process one local pending job, write sanitized local output and a processed marker, and avoid duplicate processing on rerun.

Do not paste pending rows, result rows, processed markers, failed markers, raw member values, encoded member values, decoded member values, normalized member values, names, emails, phone numbers, birthday values, command transcripts, stderr/stdout transcripts, execution payloads, credentials, AC2 environment values, Sheet IDs/URLs, screenshots, or PII. Keep `member_lookup_bridge_gate3c_pending_queue.jsonl`, `member_lookup_bridge_gate3c_results.jsonl`, `member_lookup_bridge_gate3c_processed`, and `member_lookup_bridge_gate3c_failed` local and ignored.

Gate 3C proves only that the Windows AC2 bridge host has a repeatable local queue/runtime contract with local idempotency and failed-job handling around the already-proven read-only lookup. It does not approve Gate 4A, n8n setup, Google Sheets lookup queue use, queue API use, Cloudflare Tunnel / `cloudflared` use, hosted/VPS runtime readiness, result mapping, member create/update, AutoCount writes, direct SQL writes, scheduler activation, webhook activation, or final write automation.

## Gate 3D AC2 Local Bridge Small-Batch Proof

Status: AC2-side local bridge small-batch proof only. This is not Gate 4A, not n8n evidence, not Google Sheets evidence, not a queue API, not Cloudflare Tunnel / `cloudflared`, not hosted/VPS runtime readiness, not scheduler or webhook activation, and not final automation.

Gate 3D reuses the Gate 3C local filesystem runtime harness through `scripts/member_lookup_gate3d_local_bridge_small_batch.py`. It proves a deliberately small mixed local batch can distinguish:

- fresh successful lookup work,
- duplicate/already-processed idempotency behavior,
- malformed/dead-letter failure routing.

The proof is local filesystem only:

```text
local ignored Gate 3D pending queue JSONL
-> scripts/member_lookup_gate3d_local_bridge_small_batch.py
-> Gate 3C runtime harness
-> read-only PowerShell lookup through scripts/ac2_member_lookup_review.ps1
-> local ignored sanitized results JSONL
-> local ignored processed idempotency markers
-> local ignored failed/dead-letter markers
-> aggregate-only evidence
```

Gate 3D does not require or use n8n, Google Sheets, a queue API, Cloudflare Tunnel, `cloudflared`, a hosted service, a scheduler, a Windows service activation, a webhook, result mapping, a public inbound path, or final write automation. It also does not create, update, delete, or otherwise write AutoCount members, and it does not perform direct SQL writes.

Operator local paths stay ignored under:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$pending = "$root\member_lookup_bridge_gate3d_pending_queue.jsonl"
$results = "$root\member_lookup_bridge_gate3d_results.jsonl"
$processed = "$root\member_lookup_bridge_gate3d_processed"
$failed = "$root\member_lookup_bridge_gate3d_failed"
```

The manual local AC2 bridge small-batch proof has one setup run and one evidence run. The setup run creates a local processed marker for the duplicate seed. Do not paste the setup run as Gate 3D pass evidence. The second run is the mixed-batch evidence run.

Use one operator-approved synthetic lookup value. Do not use real customer/member data. Do not paste or commit the raw, encoded, decoded, or normalized value.

Exact operator commands:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$pending = "$root\member_lookup_bridge_gate3d_pending_queue.jsonl"
$results = "$root\member_lookup_bridge_gate3d_results.jsonl"
$processed = "$root\member_lookup_bridge_gate3d_processed"
$failed = "$root\member_lookup_bridge_gate3d_failed"
New-Item -ItemType Directory -Force -Path $root | Out-Null

$secureValue = Read-Host -AsSecureString 'Enter one approved synthetic Gate 3D lookup value'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
try {
  $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

  $plainValue | python scripts\member_lookup_gate3d_prepare_small_batch.py `
    --enable-local-bridge-small-batch-review `
    --mode duplicate-seed `
    --member-value-stdin `
    --queue-jsonl "$pending"

  python scripts\member_lookup_gate3d_local_bridge_small_batch.py `
    --enable-local-bridge-small-batch-review `
    --pending-jsonl "$pending" `
    --results-jsonl "$results" `
    --processed-dir "$processed" `
    --failed-dir "$failed" `
    --lookup-mode powershell `
    --enable-powershell-lookup `
    --allow-root-login

  $plainValue | python scripts\member_lookup_gate3d_prepare_small_batch.py `
    --enable-local-bridge-small-batch-review `
    --mode mixed-batch `
    --member-value-stdin `
    --queue-jsonl "$pending"

  python scripts\member_lookup_gate3d_local_bridge_small_batch.py `
    --enable-local-bridge-small-batch-review `
    --pending-jsonl "$pending" `
    --results-jsonl "$results" `
    --processed-dir "$processed" `
    --failed-dir "$failed" `
    --lookup-mode powershell `
    --enable-powershell-lookup `
    --allow-root-login
} finally {
  if ($bstr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
  Remove-Variable plainValue -ErrorAction SilentlyContinue
}
```

Required Gate 3D paste-back shape from the mixed-batch evidence run:

```text
status = <ok/mixed_expected/needs_fix/no_work/dry_run_only/already_processed>
gate = gate3d_ac2_local_bridge_small_batch
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_filesystem_small_batch
lookup_mode = powershell
powershell_lookup_enabled = true
pending_rows_loaded_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
processed_or_archived_count = <aggregate-count-only>
failed_or_dead_letter_count = <aggregate-count-only>
duplicate_or_already_processed_count = <aggregate-count-only>
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

`status = mixed_expected` is the expected status for the deliberate Gate 3D mixed-batch proof when the run has at least one fresh successful lookup, at least one duplicate/already-processed row, and at least one malformed row routed to failed/dead-letter handling, with `lookup_error_count = 0`. It is not an all-success pass. It proves mixed-batch separation and expected failure routing.

`status = ok` is for an all-success small batch with no malformed/dead-letter rows. `status = needs_fix` means an unexpected lookup/runtime failure, unsafe output, payload conflict, or other non-deliberate failure path needs review. `status = already_processed` proves idempotency only and is not fresh lookup pass evidence. Duplicate-only evidence proves local idempotency only. Malformed/dead-letter evidence proves failure routing only.

Gate 3D was run manually on the Windows AC2 bridge host after the Gate 3D harness PR merged. The run was local-only and bridge-first: no n8n, no Google Sheets, no queue API, no Cloudflare Tunnel / `cloudflared`, no hosted/VPS service, no scheduler, no webhook, no public inbound AC2 exposure, no AutoCount writes, no direct SQL writes, no member create/update/delete, and no final automation were involved.

Recorded Gate 3D duplicate-seed setup run evidence (setup only, not Gate 3D pass evidence):

```text
status = ok
gate = gate3d_ac2_local_bridge_small_batch
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_filesystem_small_batch
lookup_mode = powershell
powershell_lookup_enabled = true
pending_rows_loaded_count = 1
lookup_attempt_count = 1
lookup_success_count = 1
lookup_error_count = 0
processed_or_archived_count = 1
failed_or_dead_letter_count = 0
duplicate_or_already_processed_count = 0
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

Recorded Gate 3D mixed-batch evidence run:

```text
status = mixed_expected
gate = gate3d_ac2_local_bridge_small_batch
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_filesystem_small_batch
lookup_mode = powershell
powershell_lookup_enabled = true
pending_rows_loaded_count = 3
lookup_attempt_count = 1
lookup_success_count = 1
lookup_error_count = 0
processed_or_archived_count = 1
failed_or_dead_letter_count = 1
duplicate_or_already_processed_count = 1
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

Recorded Gate 3D rerun/idempotency evidence:

```text
status = already_processed
gate = gate3d_ac2_local_bridge_small_batch
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_filesystem_small_batch
lookup_mode = powershell
powershell_lookup_enabled = true
pending_rows_loaded_count = 3
lookup_attempt_count = 0
lookup_success_count = 0
lookup_error_count = 0
processed_or_archived_count = 0
failed_or_dead_letter_count = 0
duplicate_or_already_processed_count = 3
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

The duplicate-seed setup run created the initial local processed marker and is recorded for completeness only. The mixed-batch evidence run is the Gate 3D small-batch proof. Its `status = mixed_expected` is expected because the batch deliberately contains one duplicate/already-processed row, one fresh lookup row, and one malformed/dead-letter row. The malformed/dead-letter row is intentional failure-routing proof, not an unexpected runtime failure.

The rerun proves idempotency only: the bridge did not rerun lookup, did not process new rows, did not dead-letter again, and counted all 3 rows as already processed. Together these runs prove the local bridge can separate fresh lookup, duplicate/idempotent handling, and malformed/dead-letter routing safely.

No row-level data, raw/encoded/decoded/normalized member values, names, emails, phone numbers, birthday values, AC2 environment values, command transcripts, stdout/stderr transcripts, screenshots, Sheet IDs/URLs, credentials, secrets, or PII are recorded in this evidence.

Do not paste pending rows, result rows, processed markers, failed markers, raw member values, encoded member values, decoded member values, normalized member values, names, emails, phone numbers, birthday values, AC2 environment values, command transcripts, stdout/stderr transcripts, execution payloads, credentials, Sheet IDs/URLs, screenshots, secrets, or PII. Keep `member_lookup_bridge_gate3d_pending_queue.jsonl`, `member_lookup_bridge_gate3d_results.jsonl`, `member_lookup_bridge_gate3d_processed`, and `member_lookup_bridge_gate3d_failed` local and ignored.

Gate 3D proves only that the Windows AC2 bridge host can handle a small local mixed batch with sanitized output, local processed markers, duplicate suppression, and expected dead-letter routing around the already-proven read-only lookup. It does not approve Gate 4A, n8n setup, Google Sheets lookup queue use, queue API use, Cloudflare Tunnel / `cloudflared` use, hosted/VPS runtime readiness, result mapping, member create/update/delete, AutoCount writes, direct SQL writes, scheduler activation, webhook activation, Windows service activation, or final write automation.

## Gate 3E AC2 Local Bridge Service-Readiness Discipline

Status: AC2-side local service-readiness discipline only. This is service-readiness discipline, not Windows service activation, not Windows service installation, not scheduler activation, not a queue API, not n8n, not Google Sheets, not Cloudflare Tunnel / `cloudflared`, not hosted/VPS runtime readiness, not production automation, not AutoCount writes, not member writes, and not direct SQL writes.

Gate 3E purpose: before any n8n or queue API integration, prove the local bridge can be operated safely as a repeatable worker later. It wraps the Gate 3C/Gate 3D local filesystem runtime with the operational discipline a future worker/service would need, without installing or activating anything:

- explicit opt-in flag: `--enable-local-bridge-service-readiness-review`,
- single-instance lock: a local ignored lock file; a fresh lock blocks a second instance with aggregate-only evidence, and stale lock takeover is explicit and deterministic,
- stop/kill switch: a local ignored stop flag file; if it exists the run refuses or stops cleanly with aggregate-only evidence, proving a safe operator-controlled stop mechanism exists before any service/scheduler work,
- bounded run: no infinite loop, no daemonization, no Windows service install, no Task Scheduler install; only a bounded `--max-cycles` count (default 1, hard cap 10),
- aggregate-only health/readiness evidence, optionally mirrored to a local ignored health JSON file.

Gate 3E does not require or use n8n, Google Sheets, a queue API, Cloudflare Tunnel, `cloudflared`, a hosted service, a scheduler, a Windows service installation, a Windows service activation, a webhook, result mapping, a public inbound path, or final write automation. It also does not create, update, delete, or otherwise write AutoCount members, and it does not perform direct SQL writes. It does not require real customer/member data.

`scripts/member_lookup_gate3e_service_readiness.py` runs in two shapes:

- discipline-only run: no queue paths are supplied; the wrapper proves lock, stop, and bounded-cycle behavior with all lookup counts fixed at 0 and does not invoke the runtime harness,
- bounded runtime run: the ignored Gate 3C pending/results/processed/failed paths are supplied; each bounded cycle runs the already-proven Gate 3C runtime harness and the counts aggregate across cycles.

A fresh lock refuses with `status = lock_held` and leaves the other instance's lock untouched. A lock older than `--lock-stale-seconds` (default 3600), or an unreadable/malformed lock file, is deterministically treated as stale and taken over with `stale_lock_detected = true`. A stop flag present before the run exits cleanly with `status = stopped_by_operator` and `stop_requested = true`; the stop flag is operator-owned and is never deleted by the wrapper. The lock file is released at the end of every run that acquired it. No command transcripts or process details are printed or stored in the lock file.

Operator local paths stay ignored under:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$lock = "$root\member_lookup_bridge_gate3e_lock.json"
$stop = "$root\member_lookup_bridge_gate3e_stop.flag"
$health = "$root\member_lookup_bridge_gate3e_health.json"
```

Exact operator commands for the manual Gate 3E local service-readiness review:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$lock = "$root\member_lookup_bridge_gate3e_lock.json"
$stop = "$root\member_lookup_bridge_gate3e_stop.flag"
$health = "$root\member_lookup_bridge_gate3e_health.json"
New-Item -ItemType Directory -Force -Path $root | Out-Null

# 1. Discipline-only bounded run (no queue work, counts stay 0).
python scripts\member_lookup_gate3e_service_readiness.py `
  --enable-local-bridge-service-readiness-review `
  --lock-json "$lock" `
  --stop-flag "$stop" `
  --health-json "$health" `
  --max-cycles 2

# 2. Stop-switch rehearsal: create the stop flag, expect a clean stop, then remove it.
New-Item -ItemType File -Force -Path $stop | Out-Null
python scripts\member_lookup_gate3e_service_readiness.py `
  --enable-local-bridge-service-readiness-review `
  --lock-json "$lock" `
  --stop-flag "$stop" `
  --health-json "$health" `
  --max-cycles 2
Remove-Item $stop

# 3. Optional bounded runtime cycle over the ignored Gate 3C local queue paths.
$pending = "$root\member_lookup_bridge_gate3c_pending_queue.jsonl"
$results = "$root\member_lookup_bridge_gate3c_results.jsonl"
$processed = "$root\member_lookup_bridge_gate3c_processed"
$failed = "$root\member_lookup_bridge_gate3c_failed"
python scripts\member_lookup_gate3e_service_readiness.py `
  --enable-local-bridge-service-readiness-review `
  --lock-json "$lock" `
  --stop-flag "$stop" `
  --health-json "$health" `
  --max-cycles 1 `
  --pending-jsonl "$pending" `
  --results-jsonl "$results" `
  --processed-dir "$processed" `
  --failed-dir "$failed" `
  --lookup-mode powershell `
  --enable-powershell-lookup `
  --allow-root-login
```

Required Gate 3E paste-back shape:

```text
status = <ok/no_work/already_processed/dry_run_only/stopped_by_operator/lock_held/refused/needs_fix>
gate = gate3e_ac2_local_bridge_service_readiness
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_service_readiness_review
worker_mode = bounded_local_filesystem_worker
lock_acquired = <true/false>
stale_lock_detected = <true/false>
stop_requested = <true/false>
cycles_requested = <aggregate-count-only>
cycles_completed = <aggregate-count-only>
pending_rows_loaded_count = <aggregate-count-only if runtime is invoked, otherwise 0>
lookup_attempt_count = <aggregate-count-only if runtime is invoked, otherwise 0>
lookup_success_count = <aggregate-count-only if runtime is invoked, otherwise 0>
lookup_error_count = <aggregate-count-only if runtime is invoked, otherwise 0>
processed_or_archived_count = <aggregate-count-only if runtime is invoked, otherwise 0>
failed_or_dead_letter_count = <aggregate-count-only if runtime is invoked, otherwise 0>
duplicate_or_already_processed_count = <aggregate-count-only if runtime is invoked, otherwise 0>
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
windows_service_installed = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

Gate 3E pass evidence for the discipline-only run requires `status = ok`, `lock_acquired = true`, `stale_lock_detected = false`, `stop_requested = false`, `cycles_completed = cycles_requested`, and all lookup counts at 0. The stop-switch rehearsal must separately show `status = stopped_by_operator` with `stop_requested = true` and `cycles_completed = 0`. A blocked second instance shows `status = lock_held` with `lock_acquired = false` and is refusal evidence, not failure evidence. `status = refused` means the explicit opt-in flag, a bounded cycle count, or a complete runtime path set was missing. `status = needs_fix` means an unexpected local runtime failure needs review and is not pass evidence.

### Post-Merge AC2 Local Service-Readiness Evidence

This sanitized aggregate evidence was recorded after PR #98 merged from a manual Gate 3E run on the Windows AC2 bridge host. It is local-only bridge service-readiness evidence. It is not n8n evidence, not Google Sheets evidence, not queue API evidence, not Cloudflare Tunnel / `cloudflared` evidence, not hosted/VPS service evidence, not scheduler activation, not Windows service installation or activation, not webhook activation, not public inbound AC2 exposure, not AutoCount write evidence, not direct SQL write evidence, not member create/update/delete evidence, and not final automation.

Discipline-only bounded run:

```text
status = ok
gate = gate3e_ac2_local_bridge_service_readiness
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_service_readiness_review
worker_mode = bounded_local_filesystem_worker
lock_acquired = true
stale_lock_detected = false
stop_requested = false
cycles_requested = 2
cycles_completed = 2
pending_rows_loaded_count = 0
lookup_attempt_count = 0
lookup_success_count = 0
lookup_error_count = 0
processed_or_archived_count = 0
failed_or_dead_letter_count = 0
duplicate_or_already_processed_count = 0
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
windows_service_installed = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

Stop-switch rehearsal:

```text
status = stopped_by_operator
gate = gate3e_ac2_local_bridge_service_readiness
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_service_readiness_review
worker_mode = bounded_local_filesystem_worker
lock_acquired = false
stale_lock_detected = false
stop_requested = true
cycles_requested = 2
cycles_completed = 0
pending_rows_loaded_count = 0
lookup_attempt_count = 0
lookup_success_count = 0
lookup_error_count = 0
processed_or_archived_count = 0
failed_or_dead_letter_count = 0
duplicate_or_already_processed_count = 0
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
windows_service_installed = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

Fresh-lock / second-instance rehearsal:

```text
status = lock_held
gate = gate3e_ac2_local_bridge_service_readiness
runtime_location = windows_ac2_bridge_host_only
execution_mode = manual_local_service_readiness_review
worker_mode = bounded_local_filesystem_worker
lock_acquired = false
stale_lock_detected = false
stop_requested = false
cycles_requested = 2
cycles_completed = 0
pending_rows_loaded_count = 0
lookup_attempt_count = 0
lookup_success_count = 0
lookup_error_count = 0
processed_or_archived_count = 0
failed_or_dead_letter_count = 0
duplicate_or_already_processed_count = 0
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
n8n_required = false
google_sheets_required = false
hosted_or_vps_service_called = false
scheduler_enabled = false
windows_service_installed = false
public_inbound_to_ac2_host = false
no_row_values_printed = true
```

Interpretation:

- The discipline-only bounded run proves explicit opt-in, bounded cycle behavior, lock acquire/release, aggregate-only health/readiness evidence, and no runtime queue work.
- The stop-switch rehearsal proves an operator-owned stop flag causes a clean stop before work.
- The fresh-lock rehearsal proves a second instance is blocked safely by an existing fresh lock.
- Together these prove Gate 3E local bridge service-readiness discipline: bounded worker behavior, single-instance protection, operator stop control, and safe aggregate-only evidence.
- This is service-readiness discipline only, not service activation.
- This does not prove or approve stale-lock takeover manually unless separately recorded.
- This does not approve n8n, Google Sheets, queue API, Cloudflare Tunnel / `cloudflared`, hosted/VPS runtime, scheduler activation, Windows service installation or activation, webhook activation, public inbound exposure, AutoCount writes, direct SQL writes, member create/update/delete, or final automation.

No lock file content, health file content, stop flag content, pending queue rows, result rows, processed marker content, failed marker content, raw member value, encoded member value, decoded member value, normalized member value, names, emails, phone numbers, birthdays, AC2 environment values, command transcripts, stdout/stderr transcripts, screenshots, Sheet IDs/URLs, credentials, secrets, or PII are recorded in this evidence.

Do not paste pending rows, result rows, processed markers, failed markers, lock file content, stop flag content, health file content beyond the aggregate fields above, raw member values, encoded member values, decoded member values, normalized member values, names, emails, phone numbers, birthday values, AC2 environment values, command transcripts, stdout/stderr transcripts, execution payloads, credentials, Sheet IDs/URLs, screenshots, secrets, or PII. Keep `member_lookup_bridge_gate3e_lock.json`, `member_lookup_bridge_gate3e_stop.flag`, and `member_lookup_bridge_gate3e_health.json` local and ignored.

Gate 3E proves only that the Windows AC2 bridge host can run the local bridge with worker-grade operational discipline: single-instance locking, an operator stop switch, and bounded non-daemonized cycles around the already-proven read-only lookup. It is service-readiness discipline only. It does not approve Gate 4A, n8n setup, Google Sheets lookup queue use, queue API use, Cloudflare Tunnel / `cloudflared` use, hosted/VPS runtime readiness, result mapping, member create/update/delete, AutoCount writes, direct SQL writes, scheduler activation, webhook activation, Windows service installation, Windows service activation, or final write automation.

## Gate 3F Bridge-Side Readiness Checkpoint

Status: bridge-side closure checkpoint only. This is a documentation checkpoint over the already recorded Gate 3B, Gate 3C, Gate 3D, and Gate 3E local evidence. It adds no runtime feature, no n8n workflow, no Google Sheets integration, no queue API, no Cloudflare Tunnel / `cloudflared`, no hosted/VPS service, no scheduler, no Windows service, no webhook, no AutoCount write path, no direct SQL write path, no member create/update/delete path, and no final automation.

Gate 3F answers whether more bridge-side local hardening is required before returning to Gate 4A queue-write proof. It does not run Gate 4A and does not approve bridge handoff from a real queue row.

| Gate | Evidence status | What it proves | What it does not prove | Next dependency |
| --- | --- | --- | --- | --- |
| Gate 3B local AC2 bridge readiness | Recorded, sanitized, aggregate-only one-row local AC2 bridge evidence. | AC2-side local one-row lookup bridge readiness through the read-only PowerShell lookup path, with one local ignored queue row reduced to sanitized aggregate evidence. | Does not prove repeatable runtime/idempotency, mixed-batch handling, service-readiness discipline, n8n, Google Sheets, queue API, tunnel, hosted/VPS runtime, scheduler, Windows service, webhook, AutoCount writes, direct SQL writes, member create/update/delete, or final automation. | Gate 3C local runtime/idempotency hardening. |
| Gate 3C local runtime/idempotency | Recorded, sanitized, aggregate-only fresh PowerShell lookup and duplicate/idempotency rerun evidence. | Repeatable local filesystem runtime processing, sanitized output, local processed markers, duplicate suppression, and `already_processed` rerun behavior around the read-only lookup path. | Does not prove mixed-batch separation with malformed/dead-letter routing, service-readiness discipline, n8n, Google Sheets, queue API, tunnel, hosted/VPS runtime, scheduler, Windows service, webhook, AutoCount writes, direct SQL writes, member create/update/delete, or final automation. | Gate 3D mixed-batch/failure-routing proof. |
| Gate 3D mixed-batch/failure-routing | Recorded, sanitized, aggregate-only duplicate-seed setup, mixed-batch proof, and rerun/idempotency evidence. | Small local mixed-batch separation: one fresh lookup path, duplicate/already-processed suppression, malformed/dead-letter routing, and safe idempotent rerun behavior. | Does not prove service-readiness discipline, stale-lock takeover evidence beyond code behavior, n8n, Google Sheets, queue API, tunnel, hosted/VPS runtime, scheduler, Windows service, webhook, AutoCount writes, direct SQL writes, member create/update/delete, or final automation. | Gate 3E service-readiness discipline. |
| Gate 3E service-readiness discipline | Recorded, sanitized, aggregate-only discipline run, stop-switch rehearsal, and fresh-lock/second-instance rehearsal evidence. | Explicit opt-in, bounded cycles, single-instance fresh-lock refusal, operator stop control, aggregate-only health/readiness evidence, and no runtime queue work during the discipline-only run. | Does not prove or approve service activation, Windows service installation, scheduler activation, stale-lock takeover by manual evidence unless separately recorded, n8n, Google Sheets, queue API, tunnel, hosted/VPS runtime, webhook, public inbound exposure, AutoCount writes, direct SQL writes, member create/update/delete, or final automation. | Gate 3F checkpoint and then Gate 4A queue-write proof. |
| Gate 4A n8n queue-write proof, not yet resumed | Not resumed in this checkpoint. Existing Gate 4A docs remain queue-write preparation only. | When separately run, it should prove exactly one real, non-dummy, sanitized `PENDING_LOOKUP` queue row can be prepared and reduced to aggregate pre-bridge counters. | This checkpoint does not run n8n, does not run Google Sheets, does not call AC2, does not run the local bridge, does not map results, does not approve bridge handoff, and does not authorize member writes or final automation. | Return to Gate 4A queue-write proof, stopping after aggregate pre-bridge evidence unless separately approved. |

Bridge-side items proven by Gates 3B through 3E:

- The Windows AC2 bridge host can execute a read-only one-row local lookup path and reduce it to sanitized aggregate evidence.
- The local bridge runtime can process a local pending queue file, write sanitized local result output, create processed markers, and suppress duplicate reruns.
- The local runtime can separate a deliberately small mixed batch into fresh lookup work, duplicate/already-processed work, and malformed/dead-letter handling.
- The service-readiness wrapper can require explicit opt-in, run bounded cycles, refuse a second instance when a fresh lock exists, honor an operator stop flag, and emit aggregate-only readiness evidence.
- The recorded evidence consistently keeps n8n, Google Sheets, queue API, Cloudflare Tunnel / `cloudflared`, hosted/VPS services, scheduler/webhook activation, Windows service installation/activation, AutoCount writes, direct SQL writes, member create/update/delete, and final automation out of scope.

Bridge-side items still unproven:

- A real Google Sheets, n8n, or external queue/API handoff into the bridge.
- Lease/claim semantics against a real shared queue provider.
- Protected queue/API authentication, authorization, rate limits, audit logging, rollback, and monitoring.
- Cloudflare Tunnel / `cloudflared`, hosted/VPS, public inbound, scheduler, webhook, or Windows service operation.
- Long-running production soak behavior, production observability, and operator runbook drills beyond the bounded local service-readiness review.
- Manual stale-lock takeover evidence, unless a separate future evidence record explicitly records it.
- Result mapping back to any source system and any member create/update/delete, AutoCount write, direct SQL write, or final automation path.

Recommendation: bridge-side local hardening is sufficient to return to Gate 4A n8n queue-write proof. No additional bridge-side local task is required before Gate 4A if Gate 4A remains limited to producing exactly one real, non-dummy, sanitized `PENDING_LOOKUP` queue row and stopping after aggregate pre-bridge counters. Gate 4A must still not call AC2, run the local bridge, map results, activate a scheduler/webhook/service, expose a tunnel or public inbound path, write AutoCount, write SQL, create/update/delete members, or become final automation.

Recommended next gate: resume Gate 4A queue-write proof as documented in [member_intake_n8n_gate4a_manual_queue_handoff_runbook.md](member_intake_n8n_gate4a_manual_queue_handoff_runbook.md). The next evidence should be queue-write precheck evidence only, with `queue_row_count = 1`, `queue_base64_decode_ok_count = 1`, `queue_base64_decode_fail_count = 0`, `queue_decoded_blank_count = 0`, and `queue_decoded_looks_dummy_count = 0`, and with no row-level values or PII pasted.

Do not record lock file content, health file content, stop flag content, pending queue rows, result rows, processed marker content, failed marker content, raw member values, encoded member values, decoded member values, normalized member values, names, emails, phone numbers, birthdays, AC2 environment values, command transcripts, stdout/stderr transcripts, screenshots, Sheet IDs/URLs, credentials, secrets, or PII in this checkpoint.

## Gate 4 Real Queue UAT AC2 Lookup-Only Handoff

Status: bridge-side real-queue **PowerShell-only** lookup-only handoff. This runs exactly one real read-only AutoCount member lookup for the single already-approved Gate 4A `PENDING_LOOKUP` queue row on the Windows AC2 lookup bridge host, and stops. It is the AC2-side executor facet of Gate 4. The separate n8n-orchestration facet is documented in [member_intake_n8n_lookup_bridge_uat_plan.md](member_intake_n8n_lookup_bridge_uat_plan.md). This handoff is manual, inactive, lookup-only, read-only, review-only, local to the approved AC2 bridge runtime, explicit opt-in, fail-closed, and aggregate-evidence-only.

**Gate 4 PASS is PowerShell-only.** Only a genuine read-only PowerShell AutoCount lookup can produce `status = ok`. There is no mock route in this wrapper: **Mock mode cannot satisfy Gate 4** and cannot produce PASS evidence. The evidence explicitly reports `lookup_mode`, `powershell_lookup_enabled`, and `ac2_lookup_invoked` so a PASS is always attributable to a real invoked lookup.

This gate does not activate n8n, does not map results back to n8n, does not create, update, or delete an AutoCount member, does not call any AutoCount save path, does not perform direct SQL, and does not add a scheduler, webhook, Windows service, Task Scheduler task, daemon, tunnel, callback, or public inbound endpoint. n8n result mapping is the next separate gate and is not performed here.

`scripts/member_lookup_gate4_real_queue_lookup.py` is a thin wrapper. It reuses the existing Gate 4A precheck (`scripts/member_lookup_gate4a_queue_precheck.py`) as the exact one-row schema, base64-shape, blank, and dummy/rehearsal rejection control, and it reuses the read-only lookup engine (`scripts/ac2_member_lookup_bridge_worker.py`), which calls `scripts/ac2_member_lookup_review.ps1` with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8` only. It never decodes or echoes the encoded or decoded member value into evidence; the encoded value passes only through the existing approved read-only lookup path.

Before invoking PowerShell, Gate 4 adds two bridge-side integrity checks and a corrected marker/result state machine (see below). It writes the sanitized result durably before committing its processed marker, so an interrupted or failed run can never later be reported as `already_processed`.

### Preconditions

- Gate 4A produced exactly one sanitized `PENDING_LOOKUP` queue row and the aggregate Gate 4A precheck passed with `queue_row_count = 1`, `queue_base64_decode_ok_count = 1`, `queue_base64_decode_fail_count = 0`, `queue_decoded_blank_count = 0`, `queue_decoded_looks_dummy_count = 0`, and `unexpected_queue_shape_count = 0`.
- The operator has explicit approval to run one real read-only Gate 4 lookup for this one row.
- Gate 3B through Gate 3F bridge-side readiness evidence is already recorded.
- This is run only on the Windows AC2 lookup bridge host or an approved Windows host with the installed AutoCount 2.x runtime.

### Bridge-Side Integrity Checks Before Lookup

Before any PowerShell lookup, Gate 4 independently enforces, in addition to the Gate 4A precheck:

- **Canonical numeric member-number contract.** The wrapper decodes `submitted_member_no_base64_utf8` only in memory and requires it to match exactly `^[0-9]{6,20}$`. Fewer than 6 digits, more than 20 digits, spaces, plus signs, hyphens, letters, other punctuation, blank values, invalid UTF-8/Base64, and dummy/rehearsal markers are all rejected. The decoded value is never printed, logged, stored separately, or included in evidence.
- **Canonical Gate 4A payload identity.** The wrapper recomputes the canonical FNV-1a `payload_hash` over the same ordered fields as `n8n-workflows/member_intake_gate4a_container_queue_write.workflow.json` and verifies `payload_hash` matches, `job_id == "gate4a_" + payload_hash`, `source_row_ref == "row_" + row_number`, and `intake_id == "gate4a_row_" + row_number`. Any mismatch is rejected before PowerShell. No rejected field value is printed.

Every rejection path reports `status = needs_fix`, `lookup_attempt_count = 0`, and `ac2_lookup_invoked = false`, and does not invoke the lookup.

### Secure Operator-Mediated Queue Placement

n8n stays on the non-AC2 operator runtime. The AC2 bridge host receives no public inbound webhook or tunnel. The operator copies the one approved Gate 4A queue JSONL onto the AC2 bridge host using an existing approved secure administrative transfer performed outside this repository. This runbook does not prescribe or automate a public share, tunnel, webhook, or new network service, and does not invent a new transport layer. Do not open, decode, print, or paste the queue file during placement.

### Exact Ignored Local Paths

All Gate 4 local artifacts stay ignored under the bridge review root:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$queue = "$root\member_lookup_bridge_gate4_pending_queue.jsonl"
$results = "$root\member_lookup_bridge_gate4_results.jsonl"
$processed = "$root\member_lookup_bridge_gate4_processed"
$failed = "$root\member_lookup_bridge_gate4_failed"
```

The operator places the approved Gate 4A queue JSONL at `member_lookup_bridge_gate4_pending_queue.jsonl`. The queue, results, processed, and failed artifacts are ignored by Git and must never be committed.

### Exact Opt-In Command

The gate refuses to do anything without the explicit Gate 4 opt-in. The real read-only lookup additionally requires the second PowerShell opt-in:

```powershell
python scripts\member_lookup_gate4_real_queue_lookup.py `
  --enable-gate4-real-queue-lookup `
  --enable-powershell-lookup `
  --queue-jsonl "$queue" `
  --results-jsonl "$results" `
  --processed-dir "$processed" `
  --failed-dir "$failed" `
  --allow-root-login
```

Both opt-ins are mandatory and are checked before the queue is read. Without `--enable-gate4-real-queue-lookup` the wrapper prints `status = refused` and does nothing. Without `--enable-powershell-lookup` it also prints `status = refused` and does nothing. There is no `--lookup-mode` option; the lookup is always the real read-only PowerShell path.

### Expected Aggregate Evidence Shape

The operator may paste back only this sanitized aggregate shape:

```text
status = <ok/needs_fix/already_processed/refused>
gate = gate4_real_queue_uat_ac2_lookup_only
runtime_location = windows_ac2_lookup_bridge_host
execution_mode = manual_read_only_review_only
lookup_mode = powershell
powershell_lookup_enabled = <true/false>
ac2_lookup_invoked = <true/false>
approved_batch_size = 1
queue_rows_read_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_existing_member_review_count = <aggregate-count-only>
lookup_manual_review_count = <aggregate-count-only>
lookup_ready_for_create_review_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
review_rows_written_count = <aggregate-count-only>
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
n8n_result_mapping_run = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
final_write_automation = false
no_row_values_printed = true
```

For one successful real lookup, expect `status = ok`, `lookup_mode = powershell`, `powershell_lookup_enabled = true`, `ac2_lookup_invoked = true`, `approved_batch_size = 1`, `queue_rows_read_count = 1`, `lookup_attempt_count = 1`, `lookup_success_count = 1`, exactly one of `lookup_existing_member_review_count`, `lookup_manual_review_count`, or `lookup_ready_for_create_review_count` equal to `1` with the other two at `0`, `lookup_error_count = 0`, and `review_rows_written_count = 1`. `status = ok` is only ever emitted for a genuine PowerShell lookup that produced exactly one durable, validated sanitized review result. All count fields are aggregate-count-only. `READY_FOR_CREATE_REVIEW` remains review-only and is not approval to create.

### Processed/Failed State Machine And Durability

The wrapper distinguishes marker/result states explicitly rather than treating every already-handled row as a duplicate:

- **Existing failed/dead-letter marker for this job** -> `status = needs_fix`, `lookup_attempt_count = 0`, exit nonzero. A failed marker never becomes `already_processed`.
- **Existing processed marker with a matching durable sanitized result** -> `status = already_processed`, `lookup_attempt_count = 0`, `review_rows_written_count = 0`, exit zero.
- **Existing processed marker without a matching durable sanitized result** -> `status = needs_fix`, exit nonzero.
- **Durable result without a processed marker** -> `status = needs_fix`, exit nonzero. The lookup is not re-run automatically; recovery requires reviewed instructions.
- **Payload-hash conflict** (a marker for this job with a different `payload_hash`) -> `status = needs_fix`, never `already_processed`.

**Both marker directories and the result file must be absent or empty before a fresh Gate 4 lookup.** The wrapper inspects the dedicated processed and failed marker directories strictly (never printing filenames, job IDs, payload hashes, or content) and does not rely on the shared Gate 3C loader. **Any marker artifact at all blocks a fresh lookup**, including a malformed, unreadable, non-object, wrong-filename, wrong- or missing-content-`job_id`, unrelated, duplicate, temporary `*.json.tmp`, failed/dead-letter, or incomplete marker file, and any unexpected file in a marker directory. Every blocked case is `status = needs_fix`, `lookup_attempt_count = 0`, `ac2_lookup_invoked = false`, and the artifact is never automatically deleted, overwritten, renamed, or repaired.

**The dedicated Gate 4 result file must be absent or empty before a fresh lookup.** The wrapper inspects the results file strictly (never printing row or field values): any malformed, partial, duplicate, stale, unrelated, or unexpected result content is `status = needs_fix`, `lookup_attempt_count = 0`, `ac2_lookup_invoked = false`. Such content blocks the automatic rerun; a malformed or partially written line left by an interrupted append is treated as blocking and is never silently ignored.

**Processed-marker validity is mandatory.** A processed marker is honoured only when it is the single artifact in the processed directory, named exactly `<job_id>.json`, valid JSON object content, with `marker_type = processed`, `job_id` equal to the queue job, `payload_hash` equal to the queue `payload_hash`, `state` one recognised successful routing state, `dry_run_only = true`, and `final_write_automation = false`. A missing, null, blank, malformed, or different marker payload hash, a filename/content mismatch, or any incomplete marker field is `status = needs_fix`, `ac2_lookup_invoked = false`.

**`already_processed` requires exactly one valid processed marker, zero failed markers, and exactly one matching valid result.** The processed directory must hold exactly one clean marker (nothing extra, temporary, unrelated, duplicate, malformed, or unexpected), the failed directory must be absent or empty, and the results file must hold exactly one fully validated durable result. The single durable result must carry the exact allowed envelope, match the queue `job_id`/`intake_source`/`source_reference`/`source_row_ref`/`row_number`/`consent_status`/`pdpa_status`/`attempt`, have `dry_run_only = true`, `final_write_automation = false`, `result_applied_at = null`, `status = ok` with successful authentication/session/command evidence and `error_code = null`, and a review routing state recomputed from `member_exists`/`manual_review_required`/`warning_count`/status that equals the stored state and the marker state. A result is never trusted merely because its stored `state` string is in the allowed set.

**Fixed queue retry-contract fields.** Before any lookup the wrapper also requires the canonical Gate 4A retry-contract values `attempt = 0`, `max_attempts = 1`, and `consent_status = marketing_consent_not_queued` (validated exactly, not part of the FNV payload hash). Any different, missing, boolean, malformed, or unexpected value is `status = needs_fix`, `lookup_attempt_count = 0`, `ac2_lookup_invoked = false`, which keeps the worker's retry-exhausted branch unreachable so it can never be mis-reported as an invoked AC2 lookup.

Durability invariant: a successful processed marker is never written unless its matching sanitized result is already durable. The wrapper writes the sanitized result to the results file and flushes it to disk first, then commits the processed marker via an atomic temp-file replace. If persistence is interrupted between the durable result and the marker, the next run detects a durable-result-without-marker state and returns `needs_fix` without re-running the lookup. `already_processed` is therefore valid only for a confirmed processed marker with a matching durable sanitized result; a failed marker, an incomplete result/marker pair, or a payload conflict is always `needs_fix`.

**Invocation accuracy.** The configured read-only lookup script must exist as a regular file before a lookup is attempted. A missing lookup script is a precondition failure: `status = needs_fix`, `lookup_attempt_count = 0`, `ac2_lookup_invoked = false`. `ac2_lookup_invoked = true` is reported only when the PowerShell lookup subprocess was actually launched.

### Stop Conditions

Stop the Gate 4 handoff immediately and do not treat the run as pass evidence if any of the following appear:

- `status = needs_fix` or `status = refused`, a `lookup_error_count` above `0`, `ac2_lookup_invoked = false` on an expected lookup run, or an unexpected result shape.
- `queue_rows_read_count` is not `1`, or the input is missing, empty, has more than one row, is malformed JSON, has extra or missing fields, has a non-`PENDING_LOOKUP` state, has an invalid `pdpa_status`, a decoded value that is not `^[0-9]{6,20}$`, a payload-identity mismatch, or a blank or dummy/rehearsal decoded value.
- Any member create/update/delete path reference, any AutoCount write attempt, any direct SQL write indication, any n8n activation or result-mapping indication, any scheduler enablement, or any webhook/tunnel exposure to the AC2 host.

### Rerun And Idempotency

The wrapper processes exactly one approved job. On a rerun over the same successfully processed queue row (processed marker plus matching durable result) it prints `status = already_processed` with `lookup_attempt_count = 0`, `lookup_success_count = 0`, `review_rows_written_count = 0`, and `ac2_lookup_invoked = false`. It does not run the lookup again and does not append duplicate result rows. A failed marker, an incomplete result/marker pair, or a payload conflict returns `needs_fix` on every rerun and is never `already_processed`. Malformed or rejected input follows the existing failed/dead-letter discipline under `member_lookup_bridge_gate4_failed` and does not trigger a lookup. The original queue file is never deleted automatically.

### Cleanup

1. Preserve the sanitized aggregate evidence only as long as required by evidence retention.
2. Do not open, decode, print, or paste the queue file, the results file, the processed markers, or the failed markers.
3. After the reviewed evidence retention requirement is satisfied, remove the sensitive local queue and result artifacts (`member_lookup_bridge_gate4_pending_queue.jsonl` and `member_lookup_bridge_gate4_results.jsonl`).
4. Do not remove the processed markers until the gate is formally closed, so idempotency protection stays in place. **Operators must not delete result or marker files merely to force a rerun.** Recovery from a partial or inconsistent state (malformed result, result-without-marker, incomplete marker, or payload conflict) requires separate reviewed recovery instructions, not artifact deletion.
5. **The real AC2 lookup must not be run during this PR amendment.** n8n result mapping is the next separate gate and is not performed here.

Do not paste the queue row, result rows, processed markers, failed markers, raw member value, encoded member value, decoded member value, normalized member value, names, emails, phone numbers, birthday values, AC2 environment values, command transcripts, stdout/stderr transcripts, execution payloads, credentials, Sheet IDs/URLs, screenshots, secrets, or PII. Keep `member_lookup_bridge_gate4_pending_queue.jsonl`, `member_lookup_bridge_gate4_results.jsonl`, `member_lookup_bridge_gate4_processed`, and `member_lookup_bridge_gate4_failed` local and ignored.

Gate 4 lookup-only handoff proves only that the Windows AC2 bridge host can take exactly one already-approved sanitized `PENDING_LOOKUP` queue row through the read-only lookup and produce one sanitized review result with aggregate-only evidence. It does not approve n8n result mapping, member create/update/delete, AutoCount writes, direct SQL writes, scheduler activation, webhook activation, Windows service activation, public inbound exposure, or final write automation.

## Gate 4 Failed-Attempt Recovery (One Approved Retry)

Status: reviewed recovery instructions for the single failed genuine Gate 4 attempt. This section authorizes exactly one explicitly approved read-only retry through `scripts/member_lookup_gate4_failed_attempt_recovery.py` and nothing else. It does not approve n8n result mapping, does not approve any AutoCount member create, update, or delete, does not approve AutoCount writes or direct SQL, and does not activate any scheduler, service, webhook, tunnel, or public inbound path. `READY_FOR_CREATE_REVIEW` remains review-only and is not approval to create.

### Why The Original Gate 4 Attempt Failed

The operator ran Gate 4 exactly once against the single approved Gate 4A queue row. The run reached the real PowerShell lookup, but the durable result recorded `LOOKUP_ERROR_REVIEW` with `authentication_success = false` and a sanitized `runtimeexception` error code. Subsequent diagnosis proved that the AutoCount assembly root and required assemblies exist, the queue row is structurally valid, and the four required `AC2_PROBE_*` runtime environment values were absent from the failed Gate 4 process. After restoring those four runtime-only process values, the existing authentication-only probe (`scripts/ac2_session_auth_probe.ps1`) passed without `-AllowRootLogin`: authentication was subsequently proven healthy (`static_auth_success`, `instance_login_success`, `instance_is_login`, `authentication_success`, and `user_session_available` all true with no error). The failure cause was therefore missing runtime configuration in that one process, not a defect in the queue row, the lookup path, or AutoCount authentication.

Recovery is authorized only for this one diagnosed incident. The wrapper pins validation to the exact original failure signature and requires the original durable result to match every field exactly:

```text
state = LOOKUP_ERROR_REVIEW
status = error
authentication_success = false
user_session_available = false
member_command_found = false
get_member_found = false
submitted_member_no_status = already_65_mobile
normalized_member_no_length = 10
member_exists = false
member_found_by = null
manual_review_required = false
warning_count = 0
error_code = runtimeexception
dry_run_only = true
final_write_automation = false
result_applied_at = null
```

Any other failed attempt — including `status = refused` or any different error code — is out of scope, reports `needs_fix` before authentication, and requires separate diagnosis and separate reviewed approval. The exact queue identity, source identity, attempt, PDPA, consent, result-schema, payload-hash, and failed-marker matching checks all still apply on top of this signature.

Do not record the `AC2_PROBE_*` values, any credential, any AC2 server/database/user value, result rows, marker data, queue data, command transcripts, or PII anywhere in the repository or in pasted evidence.

### Original Evidence Preservation Rule

The complete original failed attempt is valid historical evidence. The original queue file, the original Gate 4 results file, the original processed directory, and the original failed directory must be preserved byte-for-byte and are never deleted, renamed, moved, overwritten, truncated, appended to, or repaired by the recovery wrapper or by the operator. The recovery wrapper reads them only for strict aggregate structural validation; every rejection path writes nothing at all. Operators must not delete, edit, or move any original artifact to make recovery validation pass; if validation reports `needs_fix`, stop and investigate.

### One-Recovery-Only Policy And The Permanent Attempt Claim

Exactly one recovery attempt is authorized. The wrapper defines a single `recovery1` generation of isolated ignored paths and never derives a `recovery2` or later generation.

The at-most-one-lookup guarantee is enforced mechanically by a permanent recovery attempt claim file. After every validation gate and a successful authentication preflight, and immediately before the member lookup, the wrapper atomically and exclusively creates the claim (the equivalent of `O_CREAT | O_EXCL`) with a fixed sanitized non-PII structure, durably flushed. Only the process that wins the exclusive creation may invoke the lookup, so two concurrent processes observing the same clean state can never both run it — the loser sees the existing claim and performs zero lookups. Claim creation consumes the single approved lookup attempt, and a crash after claim creation (before, during, or after the lookup) blocks every future lookup, because any present claim is terminal:

- valid claim plus one fully valid successful result/processed-marker pair -> `already_processed`, zero lookups;
- valid claim plus a failed result/dead-letter pair -> `needs_fix`, zero lookups;
- claim plus missing, partial, malformed, inconsistent, or interrupted result/marker state -> `needs_fix`, zero lookups;
- malformed or unexpected claim content -> `needs_fix`, zero lookups, never `already_processed`;
- no claim plus any recovery result or marker artifact -> `needs_fix`, zero lookups.

Exclusive creation alone is insufficient. Claim persistence is exact: every byte of the fixed claim payload is written with short-write handling (a raw write that reports fewer bytes is continued; a zero-byte write is a failure), the file is durably flushed, and the persisted claim is then revalidated through the same strict claim validator before the lookup may run. Any partial-write or flush failure consumes and permanently blocks the single approved attempt: the partial or complete claim object stays in place, the aggregate evidence recomputes and reports its presence accurately from the filesystem, the run ends `needs_fix` with zero lookups, and no automated repair or retry occurs — every rerun performs zero authentication and zero lookups.

No code path cleans, resets, overwrites, renames, or repairs the claim or any recovery artifact, and operators must never remove, rename, edit, or reset the claim. Authentication preflight failure happens before claim creation and therefore does not consume the attempt; a later corrected invocation may still claim and run the single lookup. A further retry beyond this single reviewed recovery attempt requires a new reviewed PR, not artifact deletion.

### Isolated Recovery Paths

The retry writes only to dedicated ignored recovery paths that must be disjoint from every original path (the wrapper verifies this before touching the filesystem):

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$queue = "$root\member_lookup_bridge_gate4_pending_queue.jsonl"
$origResults = "$root\member_lookup_bridge_gate4_results.jsonl"
$origProcessed = "$root\member_lookup_bridge_gate4_processed"
$origFailed = "$root\member_lookup_bridge_gate4_failed"
$recClaim = "$root\member_lookup_bridge_gate4_recovery1_attempt_started.json"
$recResults = "$root\member_lookup_bridge_gate4_recovery1_results.jsonl"
$recProcessed = "$root\member_lookup_bridge_gate4_recovery1_processed"
$recFailed = "$root\member_lookup_bridge_gate4_recovery1_failed"
```

The original approved queue file is read as the immutable source input and is not modified. All recovery artifacts — including the attempt claim — are ignored by Git and must never be committed.

Before a fresh attempt the recovery results path must be completely absent: even an empty or zero-byte recovery results file is blocking, as is a whitespace-only file, a malformed or partial file, a directory, or any other filesystem object at that path. Every blocked case is `needs_fix` with zero authentication and zero lookups.

### Authentication Preflight

Before the recovery lookup, the wrapper requires all four `AC2_PROBE_*` values (`AC2_PROBE_SERVER_NAME`, `AC2_PROBE_DATABASE_NAME`, `AC2_PROBE_USER_ID`, `AC2_PROBE_PASSWORD`) to be present and nonblank in the wrapper's own process environment. They are runtime-only process values: set them only for the current PowerShell process, never in repo files, machine/user environment persistence, command-line arguments, or pasted evidence. The wrapper checks presence only and never prints or persists the values.

The wrapper then runs the existing authentication-only probe `scripts/ac2_session_auth_probe.ps1` in the same inherited process environment, without `-AllowRootLogin`, using `-JsonOut` to a local temporary file that is deleted after the sanitized booleans are read. The probe must report `authentication_success = true`, `user_session_available = true`, and `instance_login_success = true` with no error. Any preflight failure stops the recovery before `MemberCommand` or `GetMember` is invoked, writes no recovery artifact, and does not consume the single approved lookup attempt.

### Exact Opt-In Recovery Command

All three explicit opt-ins are mandatory; default invocation refuses before authentication or lookup. Run only on the Windows AC2 lookup bridge host, from the repository root, after setting the four `AC2_PROBE_*` process values:

```powershell
python scripts\member_lookup_gate4_failed_attempt_recovery.py `
  --enable-gate4-failed-attempt-recovery `
  --confirm-original-evidence-preserved `
  --enable-powershell-lookup `
  --queue-jsonl "$queue" `
  --original-results-jsonl "$origResults" `
  --original-processed-dir "$origProcessed" `
  --original-failed-dir "$origFailed" `
  --recovery-attempt-claim-json "$recClaim" `
  --recovery-results-jsonl "$recResults" `
  --recovery-processed-dir "$recProcessed" `
  --recovery-failed-dir "$recFailed"
```

There is no `--allow-root-login` option and no mock route; the recovery lookup is always the real read-only PowerShell path through `scripts/ac2_member_lookup_review.ps1` with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8` only.

### Expected Aggregate Evidence Shape

The operator may paste back only this sanitized aggregate shape:

```text
status = <ok/needs_fix/already_processed/refused>
gate = gate4_failed_attempt_recovery_lookup_only
runtime_location = windows_ac2_lookup_bridge_host
execution_mode = manual_read_only_single_recovery_review_only
lookup_mode = powershell
powershell_lookup_enabled = <true/false>
recovery_generation = 1
original_failure_validated = <true/false>
original_artifacts_modified = false
recovery_paths_isolated = <true/false>
recovery_attempt_claim_present = <true/false>
recovery_attempt_claim_created_by_this_run = <true/false>
recovery_attempt_consumed = <true/false>
auth_preflight_invoked = <true/false>
auth_preflight_success = <true/false>
allow_root_login_used = false
ac2_lookup_invoked = <true/false>
approved_batch_size = 1
queue_rows_read_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_existing_member_review_count = <aggregate-count-only>
lookup_manual_review_count = <aggregate-count-only>
lookup_ready_for_create_review_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
recovery_result_rows_written_count = <aggregate-count-only>
recovery_processed_artifact_count = <aggregate-count-only>
recovery_failed_artifact_count = <aggregate-count-only>
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
n8n_result_mapping_run = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
final_write_automation = false
no_row_values_printed = true
```

For one successful recovery lookup, expect `status = ok`, `original_failure_validated = true`, `original_artifacts_modified = false`, `recovery_paths_isolated = true`, `recovery_attempt_claim_present = true`, `recovery_attempt_claim_created_by_this_run = true`, `recovery_attempt_consumed = true`, `auth_preflight_invoked = true`, `auth_preflight_success = true`, `allow_root_login_used = false`, `ac2_lookup_invoked = true`, `queue_rows_read_count = 1`, `lookup_attempt_count = 1`, `lookup_success_count = 1`, exactly one of the three review routing counts equal to `1`, `lookup_error_count = 0`, `recovery_result_rows_written_count = 1`, `recovery_processed_artifact_count = 1`, and `recovery_failed_artifact_count = 0`. A rerun after a completed successful recovery reports `status = already_processed` with `lookup_attempt_count = 0`, `auth_preflight_invoked = false`, `recovery_attempt_claim_present = true`, and `recovery_attempt_claim_created_by_this_run = false`. Every validation, isolation, preflight, claim, or lookup failure reports `status = needs_fix` (or `refused` for missing opt-ins) and never becomes `already_processed`.

### Stop Conditions

Stop immediately and do not treat the run as recovery pass evidence if any of the following appear:

- `status = refused` or `status = needs_fix`, `original_failure_validated = false`, `recovery_paths_isolated = false`, `auth_preflight_success = false` on an expected run, `lookup_error_count` above `0`, or an unexpected evidence shape.
- `ac2_lookup_invoked = true` with anything other than exactly one lookup attempt and exactly one recovery result row, or `ac2_lookup_invoked = true` together with `recovery_attempt_claim_created_by_this_run = false`.
- `recovery_attempt_claim_present = true` on what was expected to be the first attempt, or any sign the claim was removed, renamed, edited, or reset.
- Any indication of a second recovery attempt, a `recovery2`-style path, a modified original artifact, a member create/update/delete reference, an AutoCount write attempt, a direct SQL indication, n8n activation or result mapping, scheduler enablement, or webhook/tunnel exposure of the AC2 host.

If the recovery lookup itself fails (`lookup_error_count = 1`, a recovery dead-letter marker exists), or the claim exists without a completed successful pair (an interrupted run), the one authorized recovery attempt is consumed. Do not delete the claim or any recovery artifact to retry; any further attempt requires a new reviewed PR.

### Recovery Boundaries

This recovery does not approve n8n result mapping, Google Sheets writeback, n8n activation, any scheduler, Windows service, webhook, Cloudflare Tunnel, or public inbound access, does not approve any AutoCount member create, update, or delete, no AutoCount write, no direct SQL, no final production automation, and no automatic retries beyond this single reviewed recovery attempt. n8n result mapping remains the next separate gate.

Do not paste the queue row, original or recovery result rows, processed or failed markers, raw/encoded/decoded/normalized member values, names, emails, phone numbers, birthday values, `AC2_PROBE_*` values, credentials, AC2 server/database/user values, command transcripts, stdout/stderr transcripts, execution payloads, Sheet IDs/URLs, screenshots, secrets, or PII. Keep every original and `recovery1` artifact local and ignored.

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
