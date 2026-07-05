# Member Intake Field Mapping

Status: planning draft, partially superseded. Confirmed fields come from AutoCount wiki pages, AOTG public Swagger, installed AC2 2.2 local reflection, and local read-only probes. The live Google Form contract and dry-run validator behavior are now defined in [member_form_intake_contract.md](member_form_intake_contract.md); the form field tables below have been updated to match it. Inferred write mappings still require sandbox validation before use.

Current member-intake lookup rule: AC2 / AutoCount 2.0 is the source of truth. The Google Form mobile/member number maps to AutoCount `MemberNo`; AutoCount `MobilePhone` is intentionally unused for the duplicate-check and intake identity path. The old POS member list and side sheet are reference-only. This lookup boundary does not create/update/delete members.

## Installed `MemberEntity` Support

Local reflection found `MemberEntity` in `AutoCount.Invoicing.dll` under namespace `AutoCount.BonusPoint.Member`. The installed entity supports the current mapped fields as follows:

| Intake field | Installed AC2 field | Status |
| --- | --- | --- |
| Member number | `MemberNo` | Supported. Generation path is confirmed through no-save `MemberCommand.GetNextMemberNo()` without exposing the actual generated number. |
| Member type | `MemberType` | Supported. `MemberType = Default` is API-confirmed by read-only MemberType browse; production/default usage still requires operator confirmation. |
| Name | `Name` | Supported. |
| Mobile phone | `MobilePhone` | Supported by the installed entity, but intentionally unused for the current member intake duplicate-check path. |
| Email address | `EmailAddress` | Supported. |
| Date of birth | `DOB` | Supported. |
| Active flag | `IsActive` | Supported. |
| Register date | `RegisterDate` | Supported. |
| Intake note | `Note` | Supported for sanitized operational markers only. Do not store raw consent evidence or sensitive intake payloads here without approval. |
| UDF | `UserData` / UDF carrier | Supported as an entity extension surface, but exact UDF keys and serialization shape still require local confirmation. |

Additional writable properties exist for address, profile, debtor linkage, opening points, title, photo, and related member metadata. They are out of the current minimum intake mapping unless separately approved.

## PR #69 No-Save Schema Constraints

The no-save schema probe confirmed the following in-memory column constraints for fields relevant to fake-data assignment:

| AutoCount field | Data type | Nullability / length | Assignment note |
| --- | --- | --- | --- |
| `MemberNo` | `System.String` | non-null, max_length 20 | Use generated number internally for no-save assignment; do not output it. |
| `MemberType` | `System.String` | non-null, max_length 20 | `Default` is API-confirmed by read-only browse; business approval still pending. |
| `Name` | `System.String` | nullable, max_length 100 | Fake assignment uses synthetic name only. |
| `MobilePhone` | `System.String` | nullable, max_length 25 | Fake assignment uses synthetic phone only. |
| `EmailAddress` | `System.String` | nullable, max_length 200 | Fake assignment uses `.invalid` synthetic address only. |
| `DOB` | `System.DateTime` | nullable | Fake assignment uses a synthetic date. |
| `IsActive` | `System.String` | non-null, max_length 1 | Prefer existing no-save row value; fallback to a one-character synthetic active flag. |
| `RegisterDate` | `System.DateTime` | nullable | Fake assignment uses a synthetic date. |
| `Note` | `System.String` | nullable, max_length 2147483647 | Fake assignment uses a no-save marker only. |
| `OpeningPoints` | `System.Decimal` | non-null | Fake assignment uses zero only. |
| `Individual` | `System.String` | non-null, max_length 1 | Prefer existing no-save row value; fallback to a one-character synthetic flag. |

## Live Google Form Fields

The earlier planned `FullName`/`MobileCountryCode`/`MobileNumber`/`BirthDate` form design is superseded. The live form collects the mobile number directly as the AutoCount member number and collects birthday month only. Full contract: [member_form_intake_contract.md](member_form_intake_contract.md).

| Form field (exact header) | Required for dry-run | Normalization | Target use | Status |
| --- | --- | --- | --- | --- |
| `Timestamp` | No | Trim; kept as submission metadata. | Review metadata only. | Implemented |
| `Full Name` | Yes | Trim and collapse repeated spaces. | AutoCount `Name`. | Implemented dry-run mapping |
| `AutoCount MemberNo` | Yes | Strip symbols; canonicalize SG mobiles to `65XXXXXXXX`; other shapes kept but flagged `manual_review`; max 20 characters. | AutoCount `MemberNo` (phone-as-MemberNo business decision). | Implemented dry-run mapping |
| `Email Address` | Yes | Trim, lowercase, basic format validation. | AutoCount `EmailAddress`. | Implemented dry-run mapping |
| `Birthday Month` | Yes | Full month name January to December, case-insensitive. | AutoCount `DOB` as month-only sentinel `2000-MM-01`. | Implemented dry-run mapping |
| `Marketing Consent` | Yes | Explicit `Yes` / `No`, case-insensitive. `No` never blocks registration. | Consent flag for X-Boundaries process; not a confirmed AutoCount field. | Implemented; storage open |
| `PDPA Acknowledged` | Yes | Checkbox export `I agree` (or legacy `Yes`), case-insensitive. Not acknowledged blocks sync eligibility only. | Compliance gate; not a confirmed AutoCount field. | Implemented; storage open |

## Internal Processing Fields

| Internal field | Purpose | Commit/logging guidance |
| --- | --- | --- |
| `IntakeID` | Stable idempotency key for one form submission. | Safe to log if it contains no PII. |
| `SubmittedAt` | Original form timestamp. | Safe as metadata. |
| `NormalizedMemberNo` | Canonical phone-as-MemberNo string (`65XXXXXXXX`). | Treat as PII; do not dump in logs. |
| `NormalizedEmail` | Lowercase validated email. | Treat as PII; do not dump in logs. |
| `ValidationStatus` | Validation state before approval. | Safe. |
| `ApprovalStatus` | Human approval state. | Safe. |
| `SyncStatus` | Dry-run/live sync state. | Safe. |
| `AutoCountMemberNo` | Member number returned or chosen. | Business identifier; log sparingly. |
| `AutoCountResponseCode` | Future bridge/API code. | Safe if it contains no payload. |
| `ErrorMessage` | Redacted failure detail. | Must not contain full payload, secrets, or raw PII. |
| `SyncedAt` | Future sync timestamp. | Safe. |

## AutoCount Member Fields

| AutoCount field | Evidence | Mapping candidate | Status |
| --- | --- | --- | --- |
| `MemberNo` | Wiki member tables; v2 create/edit/delete examples; AOTG models; installed entity; PR #69 and PR #70 local probes. | Submitted mobile number used directly as `MemberNo`, canonicalized to `65XXXXXXXX` for SG mobiles (business decision; see intake contract). | Confirmed field; phone-as-MemberNo decided for form intake |
| `MemberType` | Wiki member tables; v2 examples; AOTG models; installed entity; PR #68 read-only browse. | Candidate configured default member type for intake. | Required field; `Default` API-confirmed, business approval pending |
| `Name` | Wiki member tables; v2 examples; AOTG models; installed entity. | `FullName`. | Confirmed field, inferred mapping |
| `ID` / `Id` | Wiki examples/tables; AOTG models; installed entity has `ID`. | Not planned for public form unless a membership identifier is later added. | Confirmed field, open usage |
| `Address1`-`Address4` | Wiki examples/tables; AOTG models; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `PostCode` | AOTG model; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `AreaCode` | AOTG model; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `Race` | Wiki member tables; AOTG models; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `CompanyName` | Wiki member table; AOTG models; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `MobilePhone` | Wiki member table; AOTG models; installed entity. | Intentionally blank and unused: the phone number already serves as `MemberNo`. Blank is by design, not missing data. | Confirmed field, intentionally not mapped |
| `EmailAddress` | Wiki member table; AOTG models; installed entity. | `Email Address` (required, trimmed, lowercased). | Confirmed field, implemented dry-run mapping |
| `DOB` | AOTG model; installed entity. | `Birthday Month` as month-only sentinel `2000-MM-01`. | Confirmed field, implemented dry-run mapping |
| `DebtorCode` | Wiki v2 sample; AOTG models; installed entity. | Not planned for member intake. | Confirmed field, open usage |
| `IsActive` | Wiki v2 sample; AOTG models; installed entity. | Default active for approved new member. | Confirmed field, inferred default |
| `Note` | AOTG model; installed entity. | Maybe sanitized `Remarks` or internal reference only. | Confirmed field, open |
| `Gender` | AOTG model; installed entity. | Not captured. | Confirmed field, not mapped |
| `RegisterDate` | AOTG model; installed entity. | Maybe `SubmittedAt` or approval date. | Confirmed field, open |
| `ExpiryDate` | AOTG model; installed entity. | Not planned. | Confirmed field, open |
| `OpeningPoints` | AOTG model; installed entity. | Must not be set by intake unless separately approved. | Confirmed field, out of scope |
| `UserData` | Installed entity. | Possible UDF carrier. | Confirmed installed property; exact UDF shape open |

## Dry-Run Normalized Row

The current validator normalizes each Google Form CSV row to the shape below without calling AutoCount (synthetic example values only):

```json
{
  "submitted_at": "2026/07/01 10:00:00",
  "member_no": "6590000001",
  "member_no_status": "canonical",
  "name": "Synthetic Alpha",
  "email_address": "synthetic.alpha@example.invalid",
  "dob": "2000-03-01",
  "birthday_month": "March",
  "mobile_phone": "",
  "pdpa_acknowledged": true,
  "marketing_allowed": true,
  "sync_eligible": true
}
```

`mobile_phone` is always empty on purpose: the phone number already serves as `MemberNo`, and AutoCount `MobilePhone` is intentionally unused. Blank is by design, not missing data.

`MemberType` is not collected by the live form and is not part of the validator output. MemberType unresolved for production write use: PR #68 confirmed `MemberType = Default` exists by read-only API browse, but it is not yet approved as the business default for form signups.

Read-only member lookup review: `scripts/ac2_member_lookup_review.ps1` uses the proven local API path and `MemberCommand.GetMember(normalizedMemberNo)` to check whether the submitted mobile/member number already exists as an AutoCount `MemberNo`. Output is sanitized and PII-free. Local self-hosted n8n should pass form values with `MemberNoBase64Utf8` for direct duplicate checking, cloud n8n requires a separately approved local bridge, and this lookup does not create/update/delete members or authorize final writes. It must not be used as final write automation.

For now, production member creation remains blocked. PR #70 proved no-save synthetic field assignment and PR #71 proved a save-gated fake create, but live writeback requires a separate approval PR/runbook.

The PDPA checkbox on the live form exports `I agree`, which the validator accepts directly (case-insensitive; legacy `Yes` also accepted). Other values, including blank, block sync eligibility without invalidating the row. See [member_form_intake_contract.md](member_form_intake_contract.md).

## Resolved Decisions

- `MemberNo` strategy: the member's mobile number is used directly as `MemberNo` (canonical `65XXXXXXXX` for SG mobiles). AutoCount auto-running numbers are not used for form intake.
- `MobilePhone` is intentionally blank/unused; blank is by design, not missing data.
- `DOB` stores birthday month only as `2000-MM-01`.
- Dry-run duplicate detection matches on canonical `MemberNo` (phone) and email against a private local AC2 extract; AC2 stays the source of truth, and the old POS plus side records are reference-only evidence.

## Open Questions

- Should API-confirmed `MemberType = Default` be the approved business default for form signups?
- Where should PDPA and marketing consent be stored if AutoCount has no dedicated consent fields?
- PDPA/marketing consent storage remains open. Possible options are UDF, `Note` with a sanitized marker, or external audit sheet only. Do not decide yet.
- Should name similarity be added to dry-run duplicate detection, or is `MemberNo` plus email matching sufficient?
- Live writeback remains blocked until a separate production approval PR/runbook exists.

## Safety Notes

- Do not commit real member PII or consent evidence.
- Do not store production member exports in this repository.
- Do not use direct SQL writes for member intake.
- Do not call `SaveMember` for production member creation until the save-gated fake member proof is reviewed and explicit business approval exists.
