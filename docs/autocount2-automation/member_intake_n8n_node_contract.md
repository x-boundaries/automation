# Member Intake n8n Node Contract

Status: static contract for a local self-hosted n8n dry-run lookup workflow. This is not a workflow export and is not final write automation.

## Inputs

The workflow may carry only the minimum fields needed for review routing:

| Field | Source | Handling |
| --- | --- | --- |
| `row_number` | Google Sheet row metadata | Safe to route and log as review metadata. |
| `intake_id` | Internal row key, if available | Safe only if it contains no PII. |
| submitted member value | Google Form mobile/member field | Untrusted and sensitive; encode immediately and do not log. |
| local AC2 target settings | Local n8n environment/config | Do not store real server, database, or user values in workflow JSON. |
| `AC2_PROBE_PASSWORD` | Local environment secret | Required for the n8n runtime/service account; never pass as a command argument. |

## Encoded Member Value

n8n must pass form-submitted values with `MemberNoBase64Utf8`.

Before shell execution, validate the encoded value:

- it is present,
- its length is a multiple of four,
- it matches `^[A-Za-z0-9+/]+={0,2}$`,
- it is not logged, persisted, or written back to Google Sheets.

Base64 is not encryption and is not secret. It is only a shell-safety measure that keeps raw form input out of command text and reduces quoting/interpolation risk.

## PowerShell Invocation

The command shape must use the base64 input path:

```powershell
.\scripts\ac2_member_lookup_review.ps1 `
  -EnableMemberLookupReview `
  -MemberNoBase64Utf8 "<utf8-base64-submitted-member-value>" `
  -ServerName "<local-config-server>" `
  -DatabaseName "<local-config-database>" `
  -UserId "<local-config-user>"
```

`AC2_PROBE_PASSWORD` must already exist in the local n8n runtime environment. Do not add it to command text, workflow fields, logs, or JSON payloads.

## Sanitized JSON Schema

The PowerShell script returns sanitized JSON only. The n8n parser must reject missing fields, invalid types, invalid JSON, multiple JSON objects, process failure, and unexpected shape.

| Field | Required type | Notes |
| --- | --- | --- |
| `status` | string | Expected `ok` for successful lookup processing. Anything else routes to lookup error review. |
| `authentication_success` | boolean | Status field only. |
| `user_session_available` | boolean | Status field only. |
| `member_command_found` | boolean | Status field only. |
| `get_member_found` | boolean | Status field only. |
| `submitted_member_no_status` | string or null | Shape status only; never the submitted value. |
| `normalized_member_no_length` | integer | Length only; never the normalized value. |
| `member_exists` | boolean | Duplicate-check outcome. |
| `member_found_by` | string or null | Expected method label only when found. |
| `manual_review_required` | boolean | Routes to manual review when true. |
| `warning_count` | integer | Any warning blocks ready-for-create review. |
| `error` | object or null | Sanitized error object only. |

## Routing Contract

Evaluate routes in this order:

| Condition | Review code |
| --- | --- |
| Process failure, timeout, nonzero exit, invalid JSON, missing required fields, invalid field types, `status != ok`, or unexpected shape | `LOOKUP_ERROR_REVIEW` |
| `manual_review_required = true` | `MANUAL_REVIEW_REQUIRED` |
| `member_exists = true` | `EXISTING_MEMBER_REVIEW` |
| `member_exists = false` and `warning_count = 0` | `READY_FOR_CREATE_REVIEW` |
| Anything not matched above | `LOOKUP_ERROR_REVIEW` |

`READY_FOR_CREATE_REVIEW` is still a review decision, not permission to create a member.

## Logging Contract

n8n logs, execution data, notifications, and sheet updates must avoid:

- raw form-submitted member/mobile values,
- base64 encoded values,
- full command arguments,
- credentials or connection strings,
- local server/database/user values,
- unsanitized stderr,
- names, emails, DOB values, addresses, or raw phone numbers.

Allowed review outputs are row number, decision code, sanitized status code, warning/error code, and counts.

## Blocked Surfaces

This contract does not permit AutoCount writes, final write automation, direct SQL, SQL read/write query examples, production workflow activation, or an enabled/import-ready n8n workflow artifact.
