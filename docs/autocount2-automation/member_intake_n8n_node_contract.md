# Member Intake n8n Queue Contract

Status: static contract for review-only AC2 member lookup orchestration. This is not a workflow export, not production activation, and not final write automation.

## Runtime Model

n8n is the orchestration brain and may run in cloud, VPS, or another non-AC2 environment. The local Windows AC2 lookup bridge is the only component allowed to load AutoCount assemblies or invoke the existing PowerShell lookup script.

Google Forms / Google Sheets are a temporary UAT intake surface only. The bridge contract should not permanently depend on Google Forms because a future custom web form or hosted intake API may need to reject duplicate mobile/member numbers before submission. That future intake and synchronous rejection behavior are not implemented here.

Preferred flow:

```text
n8n validates form row -> n8n queues PENDING_LOOKUP -> Windows bridge polls outbound
Windows bridge runs read-only lookup -> bridge posts sanitized result -> n8n routes review state
```

Cloud n8n cannot directly run local AC2 PowerShell. n8n Execute Command runs on the n8n host/container where n8n runs, not on the AutoCount host. A cloud/VPS/non-AC2 Execute Command node is therefore invalid for AC2 member lookup.

Hosting n8n on the AutoCount host was useful for local proof work, but it is not the final architecture for this lookup bridge.

## Allowed Request Fields

n8n may queue only the minimum fields needed for review routing:

| Field | Required | Handling |
| --- | --- | --- |
| `job_id` | Yes | Stable idempotency key. Must not contain PII. |
| `intake_source` | Yes | Safe source label, for example `google_sheets_uat`; must not contain PII. |
| `source_reference` | Yes | Source-agnostic row/submission reference; must not contain PII. |
| `source_row_ref` | No | Optional row/reference label for source systems. |
| `row_number` | No | Spreadsheet row metadata for the first Google Sheets UAT only. |
| `intake_id` | No | Internal idempotency key. Safe only if it contains no PII. |
| `state` | Yes | Must be `PENDING_LOOKUP` for new lookup work. |
| `submitted_member_no_base64_utf8` | Yes | Encoded submitted member value. Sensitive; never log or echo. |
| `consent_status` | Yes | Sanitized consent category only; legacy/import markers are blocked. |
| `pdpa_status` | Yes | Sanitized PDPA category only; `Imported` is not consent. |
| `attempt` | No | Nonnegative retry counter. |
| `created_at` | No | Queue metadata. |
| `payload_hash` | No | Hash of allowed request fields for idempotency checks. |

The queue request must not contain raw member values, normalized member values, names, emails, raw phone numbers, DOB, addresses, AutoCount internal identifiers, local AutoCount target details, credentials, connection strings, stderr, stdout, tokens, or arbitrary payload dumps.

## Encoded Member Value

n8n must encode the form-submitted mobile/member value as UTF-8 base64 before queuing it in `submitted_member_no_base64_utf8`.

Before queueing, validate the encoded value:

- it is present,
- its length is a multiple of four,
- it matches `^[A-Za-z0-9+/]+={0,2}$`,
- it is not logged, persisted outside the lookup request, or written back to Google Sheets.

Base64 is not encryption and is not secret. It is only a shell-safety measure for the local bridge and does not relax logging or data-handling rules.

## Bridge Lookup Invocation

The Windows bridge, not cloud n8n, may invoke the existing lookup script:

```powershell
.\scripts\ac2_member_lookup_review.ps1 `
  -EnableMemberLookupReview `
  -MemberNoBase64Utf8 "<utf8-base64-submitted-member-value>"
```

Local AC2 target settings and `AC2_PROBE_PASSWORD` must come from runtime-only local configuration or environment. Do not add real server, database, user, password, token, or connection values to queue payloads, workflow text, command text, logs, docs, or tests.

The lookup script returns sanitized JSON only and calls the proven read-only member lookup path. It must not be replaced with direct database access or any member write path.

## Allowed Response Fields

The bridge may post only sanitized metadata:

| Field | Required | Notes |
| --- | --- | --- |
| `job_id` | Yes | Echoes the non-PII job id. |
| `intake_source` | Yes | Echoes safe source label only. |
| `source_reference` | Yes | Echoes safe source reference only. |
| `source_row_ref` | No | Echoes safe source row/reference label only. |
| `row_number` | No | Spreadsheet row metadata for Google Sheets UAT only. |
| `state` | Yes | One of the review states below. |
| `status` | Yes | Expected `ok` for successful lookup processing. |
| `authentication_success` | Yes | Boolean status field only. |
| `user_session_available` | Yes | Boolean status field only. |
| `member_command_found` | Yes | Boolean status field only. |
| `get_member_found` | Yes | Boolean status field only. |
| `submitted_member_no_status` | Yes | Shape status only; never the submitted value. |
| `normalized_member_no_length` | Yes | Length only; never the normalized value. |
| `member_exists` | Yes | Duplicate-check outcome. |
| `member_found_by` | No | Expected method label only when found. |
| `manual_review_required` | Yes | Routes to manual review when true. |
| `warning_count` | Yes | Any warning blocks ready-for-create review. |
| `error_code` | No | Sanitized category only; no raw exception text. |
| `consent_status` | Yes | Sanitized category only. |
| `pdpa_status` | Yes | Sanitized category only. |
| `attempt` | No | Retry attempt metadata. |
| `dry_run_only` | Yes | Must be true. |
| `final_write_automation` | Yes | Must be false. |

Forbidden response fields are the same as forbidden request fields. The response must not include the encoded member value.

## Job States

| State | Owner | Meaning |
| --- | --- | --- |
| `PENDING_LOOKUP` | n8n | Job is queued for review-only duplicate lookup. |
| `LOOKUP_IN_PROGRESS` | Bridge/queue | Job is leased or being processed. |
| `LOOKUP_ERROR_REVIEW` | Bridge/n8n | Timeout, process failure, invalid JSON, schema mismatch, sanitized lookup error, retry exhaustion, or unexpected shape. |
| `MANUAL_REVIEW_REQUIRED` | Bridge/n8n | Lookup or normalization requires human review. |
| `EXISTING_MEMBER_REVIEW` | Bridge/n8n | Lookup found an existing AC2 member number. |
| `READY_FOR_CREATE_REVIEW` | Bridge/n8n | Lookup did not find the member and emitted no warning. Review-only, not create approval. |

`READY_FOR_CREATE_REVIEW` is still a review decision, not permission to create a member.

## Routing Contract

Evaluate routes in this order:

| Condition | Review code |
| --- | --- |
| Queue timeout, bridge timeout, process failure, nonzero exit, invalid JSON, missing required fields, invalid field types, `status != ok`, retry exhaustion, or unexpected shape | `LOOKUP_ERROR_REVIEW` |
| `manual_review_required = true` | `MANUAL_REVIEW_REQUIRED` |
| `warning_count > 0` | `MANUAL_REVIEW_REQUIRED` |
| `member_exists = true` | `EXISTING_MEMBER_REVIEW` |
| `member_exists = false` and `warning_count = 0` | `READY_FOR_CREATE_REVIEW` |
| Anything not matched above | `LOOKUP_ERROR_REVIEW` |

## Timeout, Retry, And Idempotency

- n8n must assign a stable `job_id` or non-PII `intake_id`.
- The queue should lease one `PENDING_LOOKUP` job to one bridge instance at a time.
- If a bridge lease expires, the job may retry until the configured retry limit.
- Retry exhaustion routes to `LOOKUP_ERROR_REVIEW`.
- Same idempotency key plus same payload hash should return the prior sanitized result.
- Same idempotency key plus different payload hash should be blocked for review.
- A duplicate result post must not create a second review action.

## Logging Contract

n8n logs, bridge logs, execution data, notifications, and sheet updates must avoid:

- raw form-submitted member/mobile values,
- base64 encoded values,
- normalized member values,
- full command arguments,
- credentials or connection strings,
- local server/database/user values,
- unsanitized stderr/stdout,
- names, emails, DOB values, addresses, or raw phone numbers.

Allowed review outputs are row number, job id if non-PII, decision code, sanitized status code, warning/error code, attempt count, and counts.

## Blocked Surfaces

This contract does not permit AutoCount writes, final write automation, direct database write paths, production workflow activation, public inbound webhooks to the AutoCount host, or an enabled/import-ready n8n workflow artifact.

Real create/update automation remains blocked pending a separate PR, explicit business approval, idempotency, consent/audit handling, write guardrails, and an activation plan.
