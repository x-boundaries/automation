# Member Intake Decision Review Runbook

Status: dry-run review layer only. This must not be used as final write automation.

## Purpose

`scripts/member_intake_decision_review.py` ties validated Google Form member intake rows to sanitized AC2 member lookup results and produces review decisions by row number only.

AC2 / AutoCount 2.0 is the source of truth. Google Form mobile/member number maps to AutoCount `MemberNo`. AutoCount `MobilePhone` is intentionally unused. Birthday Month maps to future `DOB` as `2000-MM-01`, but DOB is not emitted by this review layer. Old POS and side sheet data are reference-only.

This runner is intended as a dry-run review layer for n8n/bridge duplicate-check review. The preferred runtime direction is cloud/VPS/non-AC2 n8n queueing lookup jobs for the outbound-polling Windows AC2 bridge; sanitized JSONL remains useful for offline tests and review rehearsal. It does not create, update, or delete members, and it must not be used as final write automation.

## Hard Boundaries

- No AutoCount writes.
- No member create/update/delete.
- No direct SQL or SQL read/write query.
- No n8n production workflow creation.
- No secrets in input, output, logs, docs, or Git.
- Row-level outputs must not contain PII.
- No raw name, email, phone, MemberNo, DOB, address, AutoKey, Guid, server, database, user, or password in row-level outputs.
- Lookup results are sanitized and PII-free before this runner consumes them.
- Live writes remain blocked.
- `Imported` in the PDPA field remains blocked and must not be treated as consent.

## Inputs

```powershell
python scripts/member_intake_decision_review.py `
  --input C:\path\to\google_form_responses.csv `
  --lookup-jsonl C:\path\to\sanitized_lookup_results.jsonl `
  --output-dir C:\XB\autocount_outputs\review\member_intake_decision
```

Required:

- `--input`: Google Form response CSV using the same contract as `scripts/member_intake_validate.py`.
- `--output-dir`: local directory for sanitized review artifacts.

Optional:

- `--lookup-jsonl`: sanitized lookup result JSONL keyed by spreadsheet row number.

Do not pass credentials to this script. It does not connect to AutoCount and does not execute the live lookup probe.

## Modes

`offline` mode is used when `--lookup-jsonl` is supplied. Each JSONL record must be sanitized and keyed by `row_number`. The runner uses only status booleans and warning/error codes from the lookup result.

`planned_live_lookup` mode is used when `--lookup-jsonl` is omitted. The runner does not execute live lookup. Otherwise valid rows that need AC2 duplicate evidence are marked `LOOKUP_REQUIRED` for a later local review pass.

Manual-review shapes may be carried forward only as sanitized status codes. They may be looked up by the separately gated AC2 lookup probe only when the cleaned MemberNo shape is allowed by that probe, and any manual-review outcome remains blocked from automatic create decisions.

## Decisions

- `READY_FOR_CREATE_REVIEW`: valid form row, PDPA acknowledged, canonical MemberNo shape, and sanitized lookup says the member does not exist. This is not approval to create.
- `EXISTING_MEMBER_REVIEW`: valid form row, PDPA acknowledged, and sanitized lookup says the member exists.
- `MANUAL_REVIEW_REQUIRED`: valid form row with manual-review MemberNo shape, non-SG/special shape, or lookup warning/manual-review status.
- `PDPA_BLOCKED`: PDPA was not acknowledged. `Imported` is blocked and is not consent.
- `INVALID_FORM_ROW`: required fields are missing or invalid.
- `LOOKUP_REQUIRED`: row is otherwise valid but no sanitized lookup result was supplied.
- `LOOKUP_ERROR_REVIEW`: lookup result had sanitized error status or an inconsistent schema.

## Outputs

The output directory contains:

- `member_intake_decision_report.md`: counts plus row numbers and decision codes only.
- `member_intake_decision_manifest.json`: status, mode, counts, output file names, `dry_run_only = true`, and `final_write_automation = false`.
- `member_intake_decision_rows.csv`: row number, validation status, lookup status, decision code, and issue codes only.
- `PRIVATE_DO_NOT_COMMIT_MEMBER_INTAKE_DECISION.txt`: local marker.

Row-level outputs must not contain raw names, emails, phone numbers, MemberNo values, DOB values, addresses, AutoKeys, Guids, server names, database names, users, or passwords.

## Review Use

1. Run the Google Form intake validator first and resolve invalid rows.
2. Produce sanitized lookup JSONL with the explicitly gated read-only AC2 member lookup review probe, or omit lookup JSONL for planned lookup review.
3. Run this decision review script.
4. Review counts and row-number decision codes only.
5. Keep all generated artifacts local under `C:\XB\autocount_outputs\review\member_intake_decision`.

No output from this runner authorizes live member creation. Any future write automation needs a separate reviewed PR, explicit business approval, idempotency, consent/audit handling, and independent write guardrails.
