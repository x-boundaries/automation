# Member Intake Field Mapping

Status: planning draft. Confirmed fields come from AutoCount wiki pages, AOTG public Swagger, and installed AC2 2.2 local reflection. Inferred mappings require sandbox validation before use.

## Installed `MemberEntity` Support

Local reflection found `MemberEntity` in `AutoCount.Invoicing.dll` under namespace `AutoCount.BonusPoint.Member`. The installed entity supports the current mapped fields as follows:

| Intake field | Installed AC2 field | Status |
| --- | --- | --- |
| Member number | `MemberNo` | Supported. Generation path still needs no-save confirmation through `MemberCommand.GetNextMemberNo()` without exposing the actual generated number. |
| Member type | `MemberType` | Supported. `MemberType = Default` is API-confirmed by read-only MemberType browse; production/default usage still requires operator confirmation. |
| Name | `Name` | Supported. |
| Mobile phone | `MobilePhone` | Supported. |
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

## Planned Google Form Fields

| Form field | Required for dry-run | Planned normalization | Target use | Status |
| --- | --- | --- | --- | --- |
| `FullName` | Yes | Trim and collapse spaces. | AutoCount `Name`. | Inferred mapping to confirmed field |
| `MobileCountryCode` | Yes | Digits only, stored with `+` in canonical payload. | Compose AutoCount `MobilePhone`. | Inferred |
| `MobileNumber` | Yes | Digits only; local trunk prefix removed for canonical payload. | Compose AutoCount `MobilePhone`. | Inferred |
| `Email` | No | Trim/lowercase; validate if present. | AutoCount `EmailAddress`. | Inferred mapping to confirmed field |
| `BirthDate` | No | Preserve ISO date if supplied. | AutoCount `DOB` if supported in selected API path. | Confirmed in installed entity and AOTG model |
| `CountryOfResidence` | No | Trim. | Address/country handling, reporting, or notes. | Open |
| `SignupSource` | No | Trim. | Audit/bridge note; not confirmed AutoCount member field. | Open |
| `MarketingConsent` | Yes | Multiple choice field with exact values `Yes` / `No`; validator accepts case-insensitive normalized values. | Consent flag for X-Boundaries process; not confirmed AutoCount field. | Open |
| `PDPAAcknowledged` | Yes | Multiple choice field with exact values `Yes` / `No`; must be `Yes` before sync eligibility. | Compliance gate; not confirmed AutoCount field. | Open |
| `Remarks` | No | Trim; avoid logging full text. | Potential AutoCount `Note` or internal-only review note. | Inferred |

## Internal Processing Fields

| Internal field | Purpose | Commit/logging guidance |
| --- | --- | --- |
| `IntakeID` | Stable idempotency key for one form submission. | Safe to log if it contains no PII. |
| `SubmittedAt` | Original form timestamp. | Safe as metadata. |
| `NormalizedMobile` | Canonical E.164-like phone string. | Treat as PII; do not dump in logs. |
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
| `MemberNo` | Wiki member tables; v2 create/edit/delete examples; AOTG models; installed entity. | Auto-running via desktop `GetNextMemberNo()` or explicit future value. | Confirmed field; no-save generation diagnostic pending |
| `MemberType` | Wiki member tables; v2 examples; AOTG models; installed entity; PR #68 read-only browse. | Candidate configured default member type for intake. | Required field; `Default` API-confirmed, business approval pending |
| `Name` | Wiki member tables; v2 examples; AOTG models; installed entity. | `FullName`. | Confirmed field, inferred mapping |
| `ID` / `Id` | Wiki examples/tables; AOTG models; installed entity has `ID`. | Not planned for public form unless a membership identifier is later added. | Confirmed field, open usage |
| `Address1`-`Address4` | Wiki examples/tables; AOTG models; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `PostCode` | AOTG model; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `AreaCode` | AOTG model; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `Race` | Wiki member tables; AOTG models; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `CompanyName` | Wiki member table; AOTG models; installed entity. | Not captured in current form. | Confirmed field, not mapped |
| `MobilePhone` | Wiki member table; AOTG models; installed entity. | `+{MobileCountryCode}{MobileNumber}`. | Confirmed field, inferred mapping |
| `EmailAddress` | Wiki member table; AOTG models; installed entity. | `Email`. | Confirmed field, inferred mapping |
| `DOB` | AOTG model; installed entity. | `BirthDate`. | Confirmed field, inferred mapping |
| `DebtorCode` | Wiki v2 sample; AOTG models; installed entity. | Not planned for member intake. | Confirmed field, open usage |
| `IsActive` | Wiki v2 sample; AOTG models; installed entity. | Default active for approved new member. | Confirmed field, inferred default |
| `Note` | AOTG model; installed entity. | Maybe sanitized `Remarks` or internal reference only. | Confirmed field, open |
| `Gender` | AOTG model; installed entity. | Not captured. | Confirmed field, not mapped |
| `RegisterDate` | AOTG model; installed entity. | Maybe `SubmittedAt` or approval date. | Confirmed field, open |
| `ExpiryDate` | AOTG model; installed entity. | Not planned. | Confirmed field, open |
| `OpeningPoints` | AOTG model; installed entity. | Must not be set by intake unless separately approved. | Confirmed field, out of scope |
| `UserData` | Installed entity. | Possible UDF carrier. | Confirmed installed property; exact UDF shape open |

## Dry-Run Canonical Payload

The current validator emits the future bridge payload shape without calling AutoCount:

```json
{
  "intake_id": "INT-001",
  "member_no_strategy": "auto",
  "member_type": "STANDARD",
  "full_name": "Jane Tan",
  "mobile": "+6591234567",
  "email": "jane.tan@example.com",
  "birth_date": "1990-01-02",
  "country": "Singapore",
  "signup_source": "Google Form",
  "sync_eligible": true,
  "dry_run_only": false,
  "consent_flags": {
    "pdpa_acknowledged": true,
    "marketing_allowed": true
  }
}
```

`member_type` is intentionally not hard-coded to a production write value in docs. PR #68 confirmed `MemberType = Default` exists by read-only API browse, but using it for form signups still needs business approval.

MemberType unresolved for production write use: `Default` is API-confirmed, but it is not yet approved as the business default for form signups.

For now, member creation remains blocked until the fake-data no-save assignment dry run proves synthetic field assignment and a later save-gated proof is separately approved.

If `MemberType` is missing, the validator uses `OPEN_MEMBER_TYPE` only as a visible placeholder, returns a `member_type_unconfirmed` warning, and marks the payload `sync_eligible: false` and `dry_run_only: true`. That output is useful for planning and review, not for live member creation.

Checkbox-style long acknowledgement text is intentionally unsupported as direct validator input. If the Google Form uses a checkbox acknowledgement, n8n must normalize the exported text to exact `Yes` or `No` before calling the validator.

## Open Questions

- Should API-confirmed `MemberType = Default` be the approved business default for form signups?
- Should `MemberNo` always be AutoCount auto-running, or should some legacy/external numbers be explicit?
- Should duplicate detection use mobile, email, name, or a combination?
- Where should PDPA and marketing consent be stored if AutoCount has no dedicated consent fields?
- PDPA/marketing consent storage remains open. Possible options are UDF, `Note` with a sanitized marker, or external audit sheet only. Do not decide yet.
- Should `Remarks` be internal-only rather than synced to AutoCount?
- Fake-data no-save assignment probing is the next step; writeback mapping cannot be tested until that probe and a later save-gated proof are reviewed.

## Safety Notes

- Do not commit real member PII or consent evidence.
- Do not store production member exports in this repository.
- Do not use direct SQL writes for member intake.
- Do not call `SaveMember` until a separately approved sandbox write PR exists.
