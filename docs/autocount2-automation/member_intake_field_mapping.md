# Member Intake Field Mapping

This draft maps member-intake fields to the installed AC2 2.2 member entity surface. It is not approval for production writeback.

## Installed `MemberEntity` support

Local reflection found `MemberEntity` in `AutoCount.Invoicing.dll` under namespace `AutoCount.BonusPoint.Member`. The installed entity supports the current mapped fields as follows:

| Intake field | Installed AC2 field | Status |
| --- | --- | --- |
| Member number | `MemberNo` | Supported. Generation path still needs bootstrap/session confirmation through `MemberCommand.GetNextMemberNo()`. |
| Member type | `MemberType` | Supported. `MemberType = Default` was observed in the UI, but production/default usage still requires operator confirmation. |
| Name | `Name` | Supported. |
| Mobile phone | `MobilePhone` | Supported. |
| Email address | `EmailAddress` | Supported. |
| Date of birth | `DOB` | Supported. |
| Active flag | `IsActive` | Supported. |
| Register date | `RegisterDate` | Supported. |
| Intake note | `Note` | Supported for sanitized operational markers only. Do not store raw consent evidence or sensitive intake payloads here without approval. |
| UDF | `UserData` / UDF carrier | Supported as an entity extension surface, but exact UDF keys and serialization shape still require local confirmation. |

Additional writable properties exist for address, profile, debtor linkage, opening points, title, photo, and related member metadata. They are out of the current minimum intake mapping unless separately approved.

## Open decisions

- `MemberType = Default` is observed in UI, but production/default usage still requires operator confirmation.
- PDPA/marketing consent storage remains open. Possible options are UDF, `Note` with a sanitized marker, or external audit sheet only. Do not decide yet.
- Constructor/bootstrap for `MemberCommand` remains open, so writeback mapping cannot be tested until the session pattern is proven in a sandbox.

## Safety notes

- Do not commit real member PII or consent evidence.
- Do not store production member exports in this repository.
- Do not use direct SQL writes for member intake.
- Do not call `SaveMember` until a separately approved sandbox write PR exists.
