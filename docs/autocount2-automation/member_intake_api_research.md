# Member Intake API Research

This note tracks the AutoCount 2 member-intake API surface for a future desktop bridge. The current scope is discovery only: no production writeback, no direct SQL writes, and no committed member PII or local runtime output.

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

PowerShell reflection showed no public constructors for `MemberCommand` and `MemberTypeCommand`. Standard constructor/bootstrap instantiation remains open and must be resolved before any API call attempt.

The preferred direction remains a local desktop bridge running on the AC2 machine, using the official AutoCount assemblies and session/bootstrap path once confirmed. The next unknown is constructor/bootstrap: how to obtain the required `DBSetting`/`UserSession` context and instantiate the member command types without bypassing AutoCount application rules.

## Guardrails

- Do not implement production writeback in this repo slice.
- Do not call `SaveMember`, `DeleteMember`, `SaveMemberType`, or `DeleteMemberType`.
- Do not write directly to SQL for member intake.
- Do not commit real member PII, screenshots, account book credentials, SQL credentials, API keys, or local runtime outputs.
- Keep probes metadata/reflection-only by default.
- Any future session/live probe must require explicit flags and synthetic/test data only.
