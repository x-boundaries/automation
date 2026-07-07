# Member Form Intake Contract (Dry-Run Validator)

Status: implemented dry-run contract for `scripts/member_intake_validate.py`. This contract supersedes the earlier planned `FullName`/`MobileCountryCode`/`MobileNumber` JSON row contract in [member_intake_field_mapping.md](member_intake_field_mapping.md).

## Purpose And Boundary

The validator reads the member registration Google Form response sheet CSV, normalizes each row toward the AutoCount 2.0 (AC2) member shape, optionally dry-run matches rows against a private local AC2 member extract, and writes local-only review outputs.

The validator is dry-run only. It does not create, update, or delete members. It does not connect to AutoCount, does not load AutoCount assemblies, does not call `SaveMember`, `DeleteMember`, or `NewMember`, and does not run SQL.

## Source Of Truth

- The AC2 current member database is the source of truth for members.
- The old POS system and the side member record sheet are reference-only evidence. They may inform manual review but must never be treated as authoritative or written back to.
- If the Google Sheet, the old POS, or any side record disagrees with AC2, AC2 wins.

## Identifier Policy

- The member's phone/mobile number is intentionally used as the AutoCount `MemberNo`. The Google Form field is therefore named `AutoCount MemberNo` and collects the mobile number.
- AutoCount `MobilePhone` is intentionally blank and unused. Blank `MobilePhone` is by design, not missing data. Do not "fix", backfill, or flag it as a data-quality gap.
- AC2 `DOB` stores the birthday month only, using the sentinel shape `2000-MM-01` (year fixed to 2000, day fixed to 01). Any AC2 DOB with year 2000 and day 01 means "birthday month captured, full birthdate not collected".

## Google Form Response Contract

The input CSV is the Google Form response sheet export. All seven headers below must be present exactly (surrounding whitespace and a UTF-8 BOM are tolerated; extra columns are ignored):

| Header | Required value | Normalization | Target |
| --- | --- | --- | --- |
| `Timestamp` | Optional | Trim. Kept as submission metadata. | Review metadata only |
| `Full Name` | Required | Trim; collapse repeated spaces. | AC2 `Name` |
| `AutoCount MemberNo` | Required | See member number rules below. | AC2 `MemberNo` |
| `Email Address` | Required | Trim; lowercase; basic format check. | AC2 `EmailAddress` |
| `Birthday Month` | Required | Full month name January to December, case-insensitive. | AC2 `DOB` as `2000-MM-01` |
| `Marketing Consent` | Required | Explicit `Yes` / `No`, case-insensitive. | Consent flag (not an AC2 field) |
| `PDPA Acknowledged` | Required | `Yes` (current live form value), case-insensitive. Legacy `I agree` may be accepted only for older exported rows. | Compliance gate (not an AC2 field) |

## Member Number Normalization

1. Trim whitespace.
2. Remove spaces, plus signs, dashes, brackets, dots, underscores, and all other symbols; only digits and letters are kept.
3. Apply shape rules to the cleaned value:

| Cleaned shape | Result | Status |
| --- | --- | --- |
| Exactly 8 digits starting with `8` or `9` | Canonicalized to `65XXXXXXXX` | `canonical` |
| Exactly 10 digits starting with `65` | Kept as-is | `canonical` |
| Any other all-digit shape | Kept cleaned | `manual_review` |
| Contains letters | Kept cleaned | `manual_review` |
| Longer than 20 characters | Row invalid (`member_no_too_long`) | error |
| Empty after cleaning | Row invalid (`missing_member_no` / `member_no_empty_after_cleaning`) | error |

The 20-character cap matches the AC2 `MemberNo` column constraint confirmed by the PR #69 no-save schema probe (`System.String`, non-null, max_length 20).

## Consent Semantics

- `PDPA Acknowledged` accepts `Yes` as the current live Google Form value, case-insensitively. Legacy `I agree` may be accepted only for older exported rows. Anything else, including blank, means not acknowledged.
- PDPA not acknowledged is a compliance gate, not a data-format error: the row stays structurally valid, is flagged `pdpa_blocked`, is counted in `pdpa_blocked_count`, and can never be `sync_eligible`.
- `Marketing Consent` must be an explicit `Yes` or `No`. Missing or unrecognized values make the row invalid.
- `Marketing Consent = No` never blocks member registration. It only records that marketing is not allowed.

## Sync Eligibility

`sync_eligible = valid AND pdpa_acknowledged AND NOT manual_review`

`sync_eligible` is a forward-planning flag for a future, separately approved write path. Nothing in this validator acts on it.

## Dry-Run Matching Against A Local AC2 Extract

Matching is optional and runs only when `--ac2-extract` is supplied.

- The extract is a private local CSV in the `ac2_member_browse_extract.csv` shape with at least a `MemberNo` column; `EmailAddress` is used when present (see [member_browse_extract_review_runbook.md](member_browse_extract_review_runbook.md)).
- The extract contains PII and must never be committed. Keep it under `C:\XB\autocount_outputs\review\member_browse_extract` or approved secure storage.
- AC2 `MemberNo` values are canonicalized with the same rules as submitted rows, so formatting or legacy 8-digit storage differences cannot defeat matching. Emails are compared trimmed and lowercased.
- Extract rows with an unusable `MemberNo` are skipped and reported in `ac2_extract_skipped_rows`.

Decision model, evaluated per valid row (conflict outranks a member number match):

| Condition | Decision |
| --- | --- |
| Submitted email exists in AC2 under a different `MemberNo` | `POSSIBLE_CONFLICT_REVIEW` |
| Submitted canonical `MemberNo` found in AC2 | `EXISTING_MEMBER_REVIEW` |
| Neither found | `NEW_MEMBER_CANDIDATE` |
| No extract supplied | `VALIDATION_ONLY` |
| Row failed validation | `INVALID` |

All decisions are review outcomes for a human operator. No decision triggers any AutoCount action.

## Outputs

Console output is counts only. Raw or normalized rows, names, emails, and member numbers never print to the console.

Console counters:

- `total_rows`
- `valid_rows`
- `invalid_rows` (rows with at least one field error)
- `existing_member_review_count`
- `new_member_candidate_count`
- `conflict_review_count`
- `pdpa_blocked_count`
- `manual_review_count`
- `error_count` (total field-level errors; can exceed `invalid_rows` when one row has several problems)

When `--output-dir` is supplied, the validator writes local review files into it:

| File | Contents | Sharing |
| --- | --- | --- |
| `member_intake_validation_rows.csv` | One row per input row with decision, flags, and normalized fields. | Contains PII. Local only; never commit. |
| `member_intake_validation_report.md` | Counts plus rows-needing-attention by row number and issue code only. | Pasteback-safe after review; contains no member values. |
| `member_intake_validation_manifest.json` | Run metadata and counts. | Local; never commit. |
| `PRIVATE_DO_NOT_COMMIT_MEMBER_INTAKE_VALIDATION.txt` | Warning marker. | Local. |

Row numbers use spreadsheet-style numbering (header is row 1, first response row is row 2) so operators can find rows directly in the Google Sheet. Invalid rows keep row number and issue codes only; their raw values are not copied forward.

Formula-prefix values (`=`, `+`, `-`, `@`) in the review CSV are neutralized via `scripts/csv_safety.py`.

Exit codes: `0` = ran, all rows valid; `1` = ran, some rows invalid; `2` = input file or header contract failure.

## Example Command

```powershell
python scripts\member_intake_validate.py `
  --input "C:\XB\autocount_outputs\review\member_intake_validation\form_responses.csv" `
  --ac2-extract "C:\XB\autocount_outputs\review\member_browse_extract\ac2_member_browse_extract.csv" `
  --output-dir "C:\XB\autocount_outputs\review\member_intake_validation"
```

Keep the form export, the AC2 extract, and all outputs under `C:\XB\autocount_outputs` (outside the repository) or in ignored local folders such as `data/`.

## Guardrails

- No AutoCount writes of any kind; no `SaveMember`, `DeleteMember`, or `NewMember`.
- No AutoCount assembly loading, no session creation, no direct SQL.
- No secrets or credentials; the validator takes only local file paths.
- No real customer PII in the repository: no form exports, no AC2 extracts, no generated CSV/XLSX/JSON outputs committed.
- Tests use synthetic fake rows only (`.invalid` emails, `9000000x`/`8000000x` numbers) built at runtime; no fixture files are committed.

## Testing

`tests/test_member_intake_validate.py` covers member number canonicalization and manual-review shapes, email and name normalization, Birthday Month to `2000-MM-01`, PDPA and marketing consent semantics, AC2 extract matching with fake rows, counts, PII-free console output, formula neutralization, and static guardrail scans of the validator source.

Run: `python -m unittest discover -s tests`
