# AutoCount 2.0 Member Intake API Research

Research checked: 2026-06-27 SGT. Local installed AC2 2.2 discovery added after machine inspection.

## Research Purpose

This document records discovery for the X-Boundaries AutoCount 2.0 Member Intake Automation workstream. The goal is to understand the officially documented AutoCount member surfaces before designing any write path from Google Form, Google Sheets, or n8n into AutoCount.

This PR is discovery, planning, dry-run validation, and local metadata probing only. It does not implement production writeback, does not create an n8n production workflow, and does not write to AutoCount SQL tables.

## Non-Negotiable Safety Rule

Direct writes to AutoCount SQL tables are forbidden. Future member creation or update must use an official AutoCount API surface, must be tested only in a sandbox/test account book first, and must retain a human approval gate until production rollout is separately approved.

## Pages Checked

- [Programmer](https://wiki.autocountsoft.com/wiki/Programmer)
- [Integration Methods](https://wiki.autocountsoft.com/wiki/Integration_Methods)
- [Programmer:Member v2](https://wiki.autocountsoft.com/wiki/Programmer:Member_v2)
- [Programmer:Member](https://wiki.autocountsoft.com/wiki/Programmer:Member)
- [Programmer:Member List and Point Balance v2](https://wiki.autocountsoft.com/wiki/Programmer:Member_List_and_Point_Balance_v2)
- [Programmer:Bonus Point Adjustment](https://wiki.autocountsoft.com/wiki/Programmer:Bonus_Point_Adjustment)
- [Programmer:Earn Point with Sale Invoice v2](https://wiki.autocountsoft.com/wiki/Programmer:Earn_Point_with_Sale_Invoice_v2)
- [Introduction to AOTG API](https://wiki.autocountsoft.com/wiki/Introduction_to_AOTG_API)
- [Begin AutoCount Accounting Integration via AOTG API](https://wiki.autocountsoft.com/wiki/Begin_AutoCount_Accounting_Integration_via_AOTG_API)
- [AOTG API Authenticate](https://wiki.autocountsoft.com/wiki/AOTG_API_Authenticate)
- [AOTG public Swagger](https://aotgapi.autocountcloud.com/swagger/docs/v1)

## Confirmed Facts

- The AutoCount Programmer page separates AutoCount Accounting desktop/.NET Framework API resources from AOTG Web API REST resources.
- Integration Methods confirms AutoCount Accounting desktop exposes assemblies/DLLs that programmers can reference from .NET Framework projects.
- Integration Methods says the AutoCount Accounting API can read and write master data and documents, and that the account book requires the API Module license for API access.
- Integration Methods gives the default local assembly folder as `C:\Program Files\AutoCount\Accounting\`.
- `Programmer:Member_v2` is an AutoCount Accounting 2.0 assembly example for member maintenance.
- The member pages require the Bonus Point module for the member/bonus-point member APIs.
- The member examples use `AutoCount.GeneralMaint.MemberMaintenance.MemberCommand` and `MemberEntity`.
- The v2 sample creates a new member with `cmd.NewMember(false)`, sets member fields, uses `cmd.GetNextMemberNo()` when assigning an auto-running member number, and saves with `cmd.SaveMember(member)`.
- The v2 sample edits by loading `cmd.GetMember(memberNo)`, mutating fields, then calling `cmd.SaveMember(member)`.
- The v2 sample deletes through `cmd.DeleteMember(memberNo)`.
- Member fields confirmed by wiki examples/tables include `MemberNo`, `MemberType`, `ID`, `Name`, `Address1` through `Address4`, `Race`, `Company Name`, `MobilePhone`, and `EmailAddress`.
- The older member table and v2 member reporting table both mark `MemberNo` and `MemberType` as mandatory.
- `MemberType` values must exist in Bonus Point > Member Type Maintenance.
- AOTG API is RESTful and cloud-mediated through AOTG Server plus an AOTG Client installed near the AutoCount Accounting server.
- AOTG setup requires account book activation and an API key, and authentication returns an access token.
- The public AOTG Swagger currently lists member endpoints including get/list, create, update, batch create/update, delete, and member type endpoints.
- The public AOTG Swagger lists create/update member models with fields overlapping the desktop API, including `MemberNo`, `MemberType`, `Name`, `DOB`, `MobilePhone`, `EmailAddress`, and `IsActive`.

## Installed AC2 2.2 local discovery

Local discovery on the X-Boundaries AC2 machine confirmed AutoCount Accounting 2.2 64-bit is installed at:

`C:\Program Files\AutoCount\Accounting 2.2\`

Important assemblies observed in that root include:

- `AutoCount.dll`
- `AutoCount.Accounting.dll`
- `AutoCount.Invoicing.dll`
- `AutoCount.ImportExport.dll`
- `AutoCount.Tools.dll`

The earlier wiki/example assumption that member maintenance lived under `AutoCount.GeneralMaint.MemberMaintenance` did not match this installed AC2 2.2 build. AutoCount.GeneralMaint.MemberMaintenance was not found in this install.

The installed member API surface was found in `AutoCount.Invoicing.dll` under namespace `AutoCount.BonusPoint.Member`.

Discovered command/entity/record types:

- `AutoCount.BonusPoint.Member.MemberCommand`
- `AutoCount.BonusPoint.Member.MemberEntity`
- `AutoCount.BonusPoint.Member.MemberTypeCommand`
- `AutoCount.BonusPoint.Member.MemberTypeEntity`
- `AutoCount.BonusPoint.Member.MemberRecord`
- `AutoCount.BonusPoint.Member.MemberTypeRecord`

Confirmed `MemberCommand` public methods from local reflection:

- `DeleteMember(System.String memberNo)`
- `GetMember(System.String memberNo)`
- `GetNextMemberNo()`
- `LoadBrowseTable()`
- `NewMember(System.Boolean isUpgrade)`
- `SaveMember(MemberEntity member)`
- `get_DBSetting`
- `get_UserSession`
- `get_UpdateMemberBalPoint`
- `set_UpdateMemberBalPoint`

Confirmed `MemberTypeCommand` public methods from local reflection:

- `DeleteMemberType(System.String memberType)`
- `GetMemberType(System.String memberType)`
- `LoadBrowseTable()`
- `NewMemberType()`
- `SaveMemberType(MemberTypeEntity memberType)`

Confirmed writable `MemberEntity` properties include:

- `MemberNo`
- `MemberType`
- `Name`
- `MobilePhone`
- `EmailAddress`
- `DOB`
- `IsActive`
- `RegisterDate`
- `ExpiryDate`
- `ID`
- `Address1`
- `Address2`
- `Address3`
- `Address4`
- `PostCode`
- `AreaCode`
- `Gender`
- `Race`
- `CompanyName`
- `DirectPhone`
- `DirectFax`
- `Department`
- `DebtorCode`
- `Note`
- `OpeningPoints`
- `Individual`
- `Title`
- `IMAddress`
- `Photo`
- `UserData`

Confirmed writable `MemberTypeEntity` properties include:

- `MemberType`
- `Description`
- `Level`

Follow-up local reflection found the command bootstrap shape:

- `MemberCommand` has an internal constructor requiring `AutoCount.Authentication.UserSession` plus `AutoCount.Data.DBSetting`.
- `MemberCommand` has a public static Create factory requiring `AutoCount.Authentication.UserSession` plus `AutoCount.Data.DBSetting`.
- `MemberTypeCommand` has an internal constructor requiring `AutoCount.Authentication.UserSession` plus `AutoCount.Data.DBSetting`.
- `MemberTypeCommand` has a public static Create factory requiring `AutoCount.Authentication.UserSession` plus `AutoCount.Data.DBSetting`.

Do not reflect-call internal constructors. The next blocker is obtaining the official UserSession and DBSetting safely. Standard constructor/bootstrap instantiation remains open and must be resolved before any API call attempt.

PR #64 confirmed bootstrap/session metadata:

- `AutoCount.Authentication.UserSession` was found.
- `AutoCount.Data.DBSetting` was found.
- `DBSetting` exposes `CreateAutoCountDefaultDBSetting(serverName, dbName)`.
- `UserSession` exposes `UserSession.Authenticate(dbSetting, userID, password)`.
- `UserSession` exposes `get_CurrentUserSession()`.

The next safe live step is an explicit opt-in session/auth probe using runtime-only credentials. This is auth only; member read/write blocked until a later separately approved PR.

The first local session/auth run proved DLL loading and DBSetting creation. Static Authenticate returned false with no exception and no current session. Metadata also shows an instance `UserSession.Login(userID, password)` path with `SetAsCurrent`, `CheckHasLogined`, and `IsLogin`, so the next auth-only diagnostic is to try instance `UserSession.Login` while keeping member read/write blocked.

The instance login diagnostic also returned false with no exception: `instance_login_method_found` was true, but `instance_login_success`, `instance_is_login`, `authentication_success`, and `user_session_available` stayed false. The operator confirmed testing with an admin/root-style user. Metadata shows `AllowRootLogin` is a public writable property on `UserSession`, so the next auth/session-only diagnostic is an explicit `-AllowRootLogin` option before calling `UserSession.Login`. Member factories, member read/list/write, SQL, and DBSetting data methods remain blocked.

The preferred direction remains a local desktop bridge running on the AC2 machine, using the official AutoCount assemblies and session/bootstrap path once confirmed. The next unknown is constructor/bootstrap: how to obtain the required `DBSetting`/`UserSession` context and instantiate the member command types without bypassing AutoCount application rules.

## What Each Page Confirms

| Source | Confirmation |
| --- | --- |
| Programmer | Official API resources include AutoCount Accounting API for .NET Framework and a separate AOTG Web API REST section. |
| Integration Methods | Desktop AutoCount Accounting exposes assemblies for inner classes/methods/API access; default assembly path is under `C:\Program Files\AutoCount\Accounting\`; API Module license is required. |
| Programmer:Member v2 | AutoCount 2.0 member maintenance exists through desktop assemblies; examples show load, create, edit, delete, `GetNextMemberNo`, `MemberCommand`, and `SaveMember`. |
| Programmer:Member | Older member API table confirms required `MemberNo` and `MemberType`, plus likely core field names and size limits. |
| Programmer:Member List and Point Balance v2 | AutoCount 2.0 reporting API confirms member field names and load/list patterns for members and balances. |
| Programmer:Bonus Point Adjustment | Bonus Point transactions require valid `MemberNo` maintained in Member Maintenance. |
| Programmer:Earn Point with Sale Invoice v2 | Sale invoice bonus point API requires Bonus Point module and valid `MemberNo`. |
| Introduction to AOTG API | AOTG is RESTful and cloud-mediated via AOTG Server plus local AOTG Client. |
| Begin AutoCount Accounting Integration via AOTG API | AOTG requires client setup, account book activation, and API key retrieval. |
| AOTG API Authenticate | AOTG token authentication uses username/password/API key and returns an expiring access token. |
| AOTG public Swagger | Member and MemberType REST endpoints and models are publicly listed. |

## Desktop API vs AOTG REST API

Member API appears to be both:

- **Desktop assembly API: confirmed for AutoCount Accounting 2.0 member maintenance, but namespace differs in the installed AC2 2.2 build.** The wiki shows v2 code using AutoCount assemblies directly, while local reflection found the installed member types under `AutoCount.BonusPoint.Member` in `AutoCount.Invoicing.dll`.
- **AOTG REST API: publicly listed, but locally unverified.** The AOTG public Swagger lists member endpoints. However, X-Boundaries still must confirm AOTG subscription/licensing, account book activation, endpoint availability for their tenant, and write behavior in a sandbox before choosing it.

Until AOTG member write access is confirmed in the real X-Boundaries environment, the preferred implementation plan is a local-only bridge near the AutoCount server that calls official AutoCount Accounting 2.x assemblies. The local bridge should start in dry-run mode and must not write SQL directly.

## What Remains Unverified

- Whether the account book has the API Module license enabled.
- Whether the Bonus Point module is enabled in the target account book.
- The exact configured `MemberType` values available in Bonus Point > Member Type Maintenance.
- Whether `MemberType = Default`, observed in the UI, is the correct production/default member type for form signups.
- Whether `GetNextMemberNo()` works without additional numbering setup in the target account book.
- How to obtain official `AutoCount.Authentication.UserSession` and `AutoCount.Data.DBSetting` instances for the installed `AutoCount.BonusPoint.Member.MemberCommand` and `MemberTypeCommand` public static Create factories.
- Whether a runtime-only session/auth probe can authenticate and make a `UserSession` available without member reads, member writes, SQL queries, or command factory invocation.
- Whether mobile/email duplicate checks exist in AutoCount or must be enforced by the bridge.
- Whether AOTG is subscribed, activated, and allowed to create/update members for the X-Boundaries account book.
- Whether AOTG member create/update behavior matches the desktop assembly behavior for required fields, validation, duplicate handling, and error messages.
- Whether production member writeback should be desktop bridge or AOTG after sandbox validation.

## Evidence Table

| URL | Title | Finding | Status |
| --- | --- | --- | --- |
| https://wiki.autocountsoft.com/wiki/Programmer | Programmer | Lists AutoCount Accounting API (.NET Framework) separately from AOTG Web API REST resources. | Confirmed |
| https://wiki.autocountsoft.com/wiki/Integration_Methods | Integration Methods | Desktop API uses AutoCount assemblies/DLLs; default assembly path documented; API Module license noted. | Confirmed |
| https://wiki.autocountsoft.com/wiki/Programmer:Member_v2 | Programmer:Member v2 | AutoCount 2.0 member maintenance sample uses `MemberCommand`, `NewMember(false)`, `GetNextMemberNo`, `SaveMember`, `GetMember`, and `DeleteMember`. | Confirmed by docs; installed namespace differs |
| https://wiki.autocountsoft.com/wiki/Programmer:Member | Programmer:Member | Older API table confirms required `MemberNo` and `MemberType`, and member/contact/address field names. | Confirmed for 1.8/1.9; field names corroborate v2 reporting table |
| https://wiki.autocountsoft.com/wiki/Programmer:Member_List_and_Point_Balance_v2 | Programmer:Member List and Point Balance v2 | Confirms v2 member list/balance load APIs and member fields including mobile/email. | Confirmed |
| https://wiki.autocountsoft.com/wiki/Programmer:Bonus_Point_Adjustment | Programmer:Bonus Point Adjustment | Bonus point adjustment uses valid `MemberNo` maintained in Member Maintenance. | Context confirmed |
| https://wiki.autocountsoft.com/wiki/Programmer:Earn_Point_with_Sale_Invoice_v2 | Programmer:Earn Point with Sale Invoice v2 | Bonus Point module and valid `MemberNo` required for earning points on sale invoice. | Context confirmed |
| https://wiki.autocountsoft.com/wiki/Introduction_to_AOTG_API | Introduction to AOTG API | AOTG is RESTful and mediated by cloud server plus local AOTG Client. | Confirmed |
| https://wiki.autocountsoft.com/wiki/Begin_AutoCount_Accounting_Integration_via_AOTG_API | Begin AutoCount Accounting Integration via AOTG API | AOTG requires setup, account book activation, API key, and AOTG user login. | Confirmed |
| https://wiki.autocountsoft.com/wiki/AOTG_API_Authenticate | AOTG API Authenticate | Token authentication endpoint requires username, password, and API key; token expires. | Confirmed |
| https://aotgapi.autocountcloud.com/swagger/docs/v1 | AOTG public Swagger | Lists member/member-type REST endpoints and member model fields. | Public endpoint confirmed; X-Boundaries entitlement unverified |

## Guardrails

- Do not implement production writeback in this repo slice.
- Do not reflect-call internal constructors or invoke command factories until the official `UserSession`/`DBSetting` path is confirmed.
- Do not call `SaveMember`, `DeleteMember`, `SaveMemberType`, or `DeleteMemberType`.
- Do not write directly to SQL for member intake.
- Do not commit real member PII, screenshots, account book credentials, SQL credentials, API keys, or local runtime outputs.
- Keep probes metadata/reflection-only by default.
- Any future session/live probe must require explicit flags and synthetic/test data only.
