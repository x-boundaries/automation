# Member Intake Field Mapping

Status: planning draft. Confirmed fields come from AutoCount wiki pages and AOTG public Swagger. Inferred mappings require sandbox validation before use.

## Planned Google Form Fields

| Form field | Required for dry-run | Planned normalization | Target use | Status |
| --- | --- | --- | --- | --- |
| `FullName` | Yes | Trim and collapse spaces. | AutoCount `Name`. | Inferred mapping to confirmed field |
| `MobileCountryCode` | Yes | Digits only, stored with `+` in canonical payload. | Compose AutoCount `MobilePhone`. | Inferred |
| `MobileNumber` | Yes | Digits only; local trunk prefix removed for canonical payload. | Compose AutoCount `MobilePhone`. | Inferred |
| `Email` | No | Trim/lowercase; validate if present. | AutoCount `EmailAddress`. | Inferred mapping to confirmed field |
| `BirthDate` | No | Preserve ISO date if supplied. | AutoCount `DOB` if supported in selected API path. | Confirmed in AOTG model; open for desktop bridge mapping |
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
| `MemberNo` | Wiki member tables; v2 create/edit/delete examples; AOTG models. | Auto-running via desktop `GetNextMemberNo()` or explicit future value. | Confirmed field; strategy open |
| `MemberType` | Wiki member tables; v2 examples; AOTG models. | Configured default member type for X-Boundaries intake. | Confirmed required field; actual value open |
| `Name` | Wiki member tables; v2 examples; AOTG models. | `FullName`. | Confirmed field, inferred mapping |
| `ID` / `Id` | Wiki examples/tables; AOTG models. | Not planned for public form unless a membership identifier is later added. | Confirmed field, open usage |
| `Address1`-`Address4` | Wiki examples/tables; AOTG models. | Not captured in current form. | Confirmed field, not mapped |
| `PostCode` | AOTG model. | Not captured in current form. | Confirmed in AOTG, open for desktop |
| `AreaCode` | AOTG model. | Not captured in current form. | Confirmed in AOTG, open for desktop |
| `Race` | Wiki member tables; AOTG models. | Not captured in current form. | Confirmed field, not mapped |
| `CompanyName` | Wiki member table; AOTG models. | Not captured in current form. | Confirmed field, not mapped |
| `MobilePhone` | Wiki member table; AOTG models. | `+{MobileCountryCode}{MobileNumber}`. | Confirmed field, inferred mapping |
| `EmailAddress` | Wiki member table; AOTG models. | `Email`. | Confirmed field, inferred mapping |
| `DOB` | AOTG model. | `BirthDate`. | Confirmed in AOTG, open for desktop bridge |
| `DebtorCode` | Wiki v2 sample; AOTG models. | Not planned for member intake. | Confirmed field, open usage |
| `IsActive` | Wiki v2 sample; AOTG models. | Default active for approved new member. | Confirmed field, inferred default |
| `Note` | AOTG model. | Maybe sanitized `Remarks` or internal reference only. | Confirmed in AOTG, open |
| `Gender` | AOTG model. | Not captured. | Confirmed in AOTG, not mapped |
| `RegisterDate` | AOTG model. | Maybe `SubmittedAt` or approval date. | Confirmed in AOTG, open |
| `ExpiryDate` | AOTG model. | Not planned. | Confirmed in AOTG, open |
| `OpeningPoints` | AOTG model. | Must not be set by intake unless separately approved. | Confirmed in AOTG, out of scope |

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

`member_type` is intentionally not hard-coded to a production value in docs. The real value must be confirmed in Bonus Point > Member Type Maintenance.

If `MemberType` is missing, the validator uses `OPEN_MEMBER_TYPE` only as a visible placeholder, returns a `member_type_unconfirmed` warning, and marks the payload `sync_eligible: false` and `dry_run_only: true`. That output is useful for planning and review, not for live member creation.

Checkbox-style long acknowledgement text is intentionally unsupported as direct validator input. If the Google Form uses a checkbox acknowledgement, n8n must normalize the exported text to exact `Yes` or `No` before calling the validator.

## Open Questions

- Which `MemberType` should be used for X-Boundaries form signups?
- Should `MemberNo` always be AutoCount auto-running, or should some legacy/external numbers be explicit?
- Should duplicate detection use mobile, email, name, or a combination?
- Does desktop assembly `MemberEntity` expose `DOB`, `Note`, `RegisterDate`, and all AOTG fields in the installed version?
- Where should PDPA and marketing consent be stored if AutoCount has no dedicated consent fields?
- Should `Remarks` be internal-only rather than synced to AutoCount?
