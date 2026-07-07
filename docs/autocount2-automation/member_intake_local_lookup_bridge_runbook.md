# Member Intake Local Lookup Bridge Runbook

Status: design and dry-run worker skeleton only. This does not activate production automation and does not authorize AutoCount member writes.

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

The queue can be implemented later with an approved cloud queue, private API, n8n data table, or similar service. This PR does not choose or configure a live queue provider.

## Explicitly Non-Preferred Inbound Pattern

An inbound local webhook, private tunnel, reverse proxy, or VPN callback to the Windows bridge is not the preferred design. It may be considered only as a separately approved private-network exception with documented firewall rules, authentication, rate limits, audit logging, and rollback.

A public inbound webhook on the AutoCount host is not recommended.

## n8n Runtime Boundary

- n8n can orchestrate Google Sheet intake, validation, queue creation, reviewer notifications, and review-only status updates.
- Cloud n8n cannot directly run local AC2 PowerShell.
- n8n Execute Command runs on the n8n host/container where n8n runs, not on the AutoCount host.
- Therefore a cloud/VPS/non-AC2 n8n Execute Command node is invalid for local AC2 lookup.
- n8n must not store raw member values, normalized member values, AutoCount credentials, local host details, or full bridge command text in workflow data.

## Windows Bridge Boundary

The bridge is intentionally small:

- runs only on the AutoCount host or an approved Windows host that can load the installed AutoCount assemblies,
- polls outbound for pending lookup jobs,
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
| `LOOKUP_IN_PROGRESS` | Bridge leased or started the job. |
| `LOOKUP_ERROR_REVIEW` | Process failure, timeout, invalid JSON, schema mismatch, sanitized lookup error, or retry exhaustion. |
| `MANUAL_REVIEW_REQUIRED` | Lookup or normalization requires human review before any create review state. |
| `EXISTING_MEMBER_REVIEW` | AC2 lookup found an existing `MemberNo`; route to duplicate review. |
| `READY_FOR_CREATE_REVIEW` | Lookup did not find the member and emitted no warning; still review-only. |

No state authorizes member creation.

## Retry And Idempotency

- `job_id` or `intake_id` must be stable and non-PII.
- The queue should lease a job before lookup so two bridge instances do not process it at the same time.
- The same job payload should produce the same sanitized result.
- A job with the same idempotency key and different payload hash must be rejected or held for review.
- Timeouts and transient local failures may retry up to a small configured limit.
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

## Review-Only Routing

The bridge posts results for review routing only:

- `LOOKUP_ERROR_REVIEW`: operator investigates lookup failure.
- `MANUAL_REVIEW_REQUIRED`: operator reviews unusual member number shape or lookup warning.
- `EXISTING_MEMBER_REVIEW`: operator reviews possible duplicate.
- `READY_FOR_CREATE_REVIEW`: operator may review as a new-member candidate, but this is not create approval.

Real create/update automation remains blocked pending a separate PR, explicit business approval, idempotency and consent/audit handling, production write guardrails, and an independently reviewed activation plan.

## Out Of Scope

- production queue provider selection,
- public inbound webhook,
- private tunnel activation,
- production n8n workflow export,
- AutoCount member writes,
- direct database write paths,
- real endpoints,
- secrets,
- row-level output commits,
- production activation.
