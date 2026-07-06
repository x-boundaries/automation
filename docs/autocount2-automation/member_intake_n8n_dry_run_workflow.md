# Member Intake n8n Dry-Run Workflow

Status: documentation/specification only. This PR does not add an n8n workflow export, does not activate a production workflow, and does not authorize AutoCount writes.

## Purpose

This document defines the local self-hosted n8n dry-run workflow shape for Google Form member intake duplicate-check review. The runtime direction is local n8n calling `scripts/ac2_member_lookup_review.ps1` directly and routing the sanitized JSON result.

AC2 / AutoCount 2.0 is the source of truth. Google Form mobile/member number maps to AutoCount `MemberNo`. AutoCount `MobilePhone` is intentionally unused. Birthday Month maps to future `DOB` as `2000-MM-01`, but DOB is outside this lookup-only step. Old POS and side sheet data are reference-only.

## Runtime Boundary

- The workflow belongs only in a local self-hosted n8n runtime on the Windows AutoCount host, or on a locked-down Windows host that can safely load the approved AutoCount Accounting 2.x assemblies.
- Cloud n8n cannot directly execute local AC2 PowerShell. Any cloud-to-local path requires a separately approved local bridge, private route, VPN, or equivalent reviewed adapter.
- The submitted Google Form mobile/member value is untrusted input.
- The submitted value must be encoded as UTF-8 base64 before PowerShell execution.
- The encoded value must be validated with a strict base64-safe allowlist before shell execution.
- Base64 is not encryption and is not secret. It only reduces quoting and shell interpolation risk.
- `AC2_PROBE_PASSWORD` must be configured as a local environment secret for the n8n runtime or Windows service account. It must not be passed as a command argument.
- n8n must not log raw form values, encoded values, full command arguments, credentials, connection details, or unsanitized stderr.

## Template Decision

No n8n workflow template is included in this PR. The repository does not yet have a safe convention for disabled, reference-only n8n exports, and an importable workflow artifact would create unnecessary activation risk.

## Node-By-Node Outline

1. Google Sheets new-row trigger or poller receives the latest Google Form response row.
2. Row intake guard verifies the expected sheet columns exist and carries only spreadsheet row number, an internal intake ID if available, and validation status metadata forward.
3. Untrusted member value step reads the submitted mobile/member field for immediate lookup preparation only. It must not persist or log the raw value.
4. UTF-8 base64 encode step encodes the submitted value. The encoded value is still sensitive operational data and must not be logged or written back to the sheet.
5. Base64 allowlist step rejects encoded values that are empty, not length-multiple-of-four, or do not match `^[A-Za-z0-9+/]+={0,2}$`. Rejections route to `LOOKUP_ERROR_REVIEW`.
6. Local PowerShell lookup step calls `scripts/ac2_member_lookup_review.ps1` with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8`. Local AC2 connection parameters come from local configuration or environment, and the password comes only from `AC2_PROBE_PASSWORD`.
7. Process-result guard treats process failure, timeout, nonzero exit, empty stdout, or unsanitized stderr as `LOOKUP_ERROR_REVIEW`.
8. JSON parse guard accepts only one sanitized JSON object and requires the fields defined in [member_intake_n8n_node_contract.md](member_intake_n8n_node_contract.md).
9. Route decision step maps sanitized status fields to review codes only:
   - process failure, invalid JSON, missing fields, `status != ok`, or unexpected shape -> `LOOKUP_ERROR_REVIEW`
   - `manual_review_required = true` -> `MANUAL_REVIEW_REQUIRED`
   - `member_exists = true` -> `EXISTING_MEMBER_REVIEW`
   - `member_exists = false` and `warning_count = 0` -> `READY_FOR_CREATE_REVIEW`
10. Review output step may update only review/status fields or notify a reviewer with row number and decision code. It must not include raw member values or any AC2 write action.

## Review Codes

`READY_FOR_CREATE_REVIEW` is not approval to create a member. It means only that the dry-run duplicate-check lookup did not find the submitted `MemberNo` and emitted no warning. A human review decision is still required.

Real create/update automation remains blocked pending a separate PR, explicit business approval, idempotency design, consent/audit handling, and write guardrails.

## Offline JSONL Use

Sanitized JSONL remains useful for offline tests and for `scripts/member_intake_decision_review.py`, but it is not the main runtime path for local self-hosted n8n. Runtime n8n should call the PowerShell lookup directly and route the sanitized JSON outcome.

## Out Of Scope

- AutoCount member writes.
- Final write automation.
- Direct SQL or SQL read/write query examples.
- Production n8n workflow creation or activation.
- Enabled or import-ready workflow exports.
- Credentials, connection strings, real member values, names, emails, raw phone numbers, or other PII.
