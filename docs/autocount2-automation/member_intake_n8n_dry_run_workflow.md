# Member Intake n8n Dry-Run Workflow

Status: documentation/specification only. This PR does not add an n8n workflow export, does not activate a production workflow, and does not authorize AutoCount writes.

## Purpose

This document defines the n8n-side dry-run workflow shape for Google Form member intake duplicate-check review using the local AC2 lookup bridge pattern.

n8n may run in cloud, VPS, or another non-AC2 runtime. It is the orchestration brain for validation, queueing, notifications, and review-only routing. The local Windows AC2 lookup bridge is the only component allowed to load AutoCount assemblies or run the existing AC2 lookup PowerShell script.

AC2 / AutoCount 2.0 is the source of truth. Google Form mobile/member number maps to AutoCount `MemberNo`. AutoCount `MobilePhone` is intentionally unused. Birthday Month maps to future `DOB` as `2000-MM-01`, but DOB is outside this lookup-only step. Old POS and side sheet data are reference-only.

## Runtime Boundary

- Do not host n8n on the AutoCount host as the final member lookup architecture.
- Cloud/VPS/non-AC2 n8n cannot directly execute local AC2 PowerShell.
- n8n Execute Command runs on the n8n host/container where n8n runs, not on the AutoCount host.
- Therefore a cloud/VPS/non-AC2 Execute Command node is invalid for local AC2 lookup.
- n8n must queue a review-only lookup job and wait for a sanitized bridge result.
- The submitted Google Form mobile/member value is untrusted input.
- The submitted value must be encoded as UTF-8 base64 before queueing.
- The encoded value must be validated with a strict base64-safe allowlist before queueing.
- Base64 is not encryption and is not secret. It only reduces quoting and interpolation risk for the local bridge.
- n8n must not log raw form values, encoded values, normalized values, full command arguments, credentials, connection details, or unsanitized stderr/stdout.

## Template Decision

No n8n workflow template is included in this PR. The repository does not yet have a safe convention for disabled, reference-only n8n exports, and an importable workflow artifact would create unnecessary activation risk.

## Node-By-Node Outline

1. Google Sheets new-row trigger or poller receives the latest Google Form response row.
2. Row intake guard verifies the expected sheet columns exist and carries only spreadsheet row number, an internal intake ID if available, and validation status metadata forward.
3. Form validator step applies the current member intake contract and blocks invalid rows from lookup queueing.
4. PDPA guard allows only valid new-form consent such as `Yes`. `Imported` remains a blocked legacy/import marker.
5. Untrusted member value step reads the submitted mobile/member field for immediate lookup preparation only. It must not persist or log the raw value.
6. UTF-8 base64 encode step encodes the submitted value. The encoded value is still sensitive operational data and must not be logged or written back to the sheet.
7. Base64 allowlist step rejects encoded values that are empty, not length-multiple-of-four, or do not match `^[A-Za-z0-9+/]+={0,2}$`. Rejections route to `LOOKUP_ERROR_REVIEW`.
8. Queue request step creates a `PENDING_LOOKUP` job with only the fields allowed by [member_intake_n8n_node_contract.md](member_intake_n8n_node_contract.md).
9. Wait/poll result step waits for the bridge to post a sanitized result, with a configured timeout and retry limit.
10. JSON/schema guard accepts only one sanitized result object and requires the fields defined in the queue contract.
11. Route decision step maps sanitized status fields to review codes only:
    - timeout, process failure, invalid JSON, missing fields, `status != ok`, retry exhaustion, or unexpected shape -> `LOOKUP_ERROR_REVIEW`
    - `manual_review_required = true` or `warning_count > 0` -> `MANUAL_REVIEW_REQUIRED`
    - `member_exists = true` -> `EXISTING_MEMBER_REVIEW`
    - `member_exists = false` and `warning_count = 0` -> `READY_FOR_CREATE_REVIEW`
12. Review output step may update only review/status fields or notify a reviewer with row number, non-PII job id, and decision code. It must not include raw member values or any AC2 write action.

## Review Codes

`READY_FOR_CREATE_REVIEW` is not approval to create a member. It means only that the dry-run duplicate-check lookup did not find the submitted `MemberNo` and emitted no warning. A human review decision is still required.

Real create/update automation remains blocked pending a separate PR, explicit business approval, idempotency design, consent/audit handling, write guardrails, and production activation guardrails.

## Offline JSONL Use

Sanitized JSONL remains useful for offline tests and for `scripts/member_intake_decision_review.py`, but it is not the long-term runtime interface for cloud/VPS/non-AC2 n8n. Runtime n8n should queue lookup jobs and route the sanitized bridge outcome.

## Out Of Scope

- AutoCount member writes.
- Final write automation.
- Direct database access or query examples.
- Production n8n workflow creation or activation.
- Enabled or import-ready workflow exports.
- Public inbound webhooks to the AutoCount host.
- Credentials, connection strings, real member values, names, emails, raw phone numbers, or other PII.
