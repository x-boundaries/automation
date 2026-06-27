# Member Intake Discovery Runbook

Status: manual/local investigation guide. Run only on a sandbox/test account book unless a step is explicitly read-only metadata inspection.

## Safety Rules

- Do not run writeback against production.
- Do not write directly to AutoCount SQL tables.
- Do not commit local screenshots, raw member rows, PII, sheet IDs, n8n credentials, API keys, connection strings, DLL inventories with sensitive local paths, or runtime outputs unless sanitized.
- Keep evidence under a local ignored folder such as:

```text
C:\XB\autocount_outputs\review\member_intake_discovery
```

Commit only sanitized conclusions to docs.

## Identify Installed AutoCount Accounting 2.x Folder

Check common install paths:

```powershell
$candidateRoots = @(
  'C:\Program Files\AutoCount\Accounting',
  'C:\Program Files (x86)\AutoCount\Accounting'
)

$candidateRoots | ForEach-Object {
  if (Test-Path $_) {
    Get-Item $_ | Select-Object FullName, LastWriteTime
  }
}
```

If those paths do not exist, inspect Start Menu shortcut properties or ask the local admin/operator for the installed AutoCount Accounting folder. Do not modify files in the install folder.

## Locate Relevant DLLs Safely

Read-only listing:

```powershell
$acRoot = 'C:\Program Files\AutoCount\Accounting'
Get-ChildItem -LiteralPath $acRoot -Filter '*.dll' -File |
  Select-Object Name, Length, LastWriteTime |
  Sort-Object Name
```

Save only sanitized DLL name/version notes. Do not commit a full machine-specific inventory unless paths and environment details are reviewed.

## Inspect DLL Names And Versions Without Modifying Anything

```powershell
$acRoot = 'C:\Program Files\AutoCount\Accounting'
Get-ChildItem -LiteralPath $acRoot -Filter '*.dll' -File |
  Select-Object Name,
    @{Name='ProductVersion'; Expression={$_.VersionInfo.ProductVersion}},
    @{Name='FileVersion'; Expression={$_.VersionInfo.FileVersion}} |
  Sort-Object Name
```

This only reads file metadata.

## Search For Member/Bonus Point Namespaces And Classes

First search by DLL filename:

```powershell
$acRoot = 'C:\Program Files\AutoCount\Accounting'
Get-ChildItem -LiteralPath $acRoot -Filter '*.dll' -File |
  Where-Object {
    $_.Name -match 'Member|Bonus|Point|General|Maint|Accounting'
  } |
  Select-Object Name, Length, LastWriteTime
```

If Visual Studio, ILSpy, JetBrains dotPeek, or another approved local .NET assembly browser is available, inspect likely DLLs read-only for:

- `AutoCount.GeneralMaint.MemberMaintenance`
- `MemberCommand`
- `MemberEntity`
- `NewMember`
- `GetNextMemberNo`
- `SaveMember`
- `GetMember`
- `DeleteMember`
- `MemberType`
- `BonusPoint`
- `MemberMaintenance`
- `MemberTypeMaintenance`

Do not decompile proprietary code into the repo. Capture only namespace/class/method names needed for integration planning.

## Confirm Bonus Point Module In AC2 UI

In the sandbox/test account book:

1. Open AutoCount Accounting 2.x.
2. Confirm the account book is not production.
3. Look for Bonus Point menu/module availability.
4. Open Bonus Point > Member Type Maintenance if available.
5. Record sanitized member type codes/descriptions needed for testing, or use placeholders if the values are sensitive.
6. Do not create, edit, or delete production members.

If Bonus Point is not enabled, member API writeback is blocked until the module/license state is resolved.

## Confirm API Module/Access

The Integration Methods wiki notes API Module licensing for account book access. In sandbox:

1. Ask the AutoCount admin/operator to confirm API Module availability.
2. If an API test account exists, confirm least privilege.
3. Do not use administrator credentials for automation unless a temporary sandbox-only test requires it and the exception is documented locally.

## Sandbox Test Only

All future API experiments must use:

- sandbox/test account book,
- synthetic member data,
- no real member PII,
- no production database,
- dry-run first,
- no direct SQL writes.

Suggested synthetic values:

```text
Name: XB TEST MEMBER
MobilePhone: +6591234567
EmailAddress: xb-test-member@example.invalid
MemberType: <sandbox-confirmed test member type>
```

## Evidence To Capture Locally

Store local evidence under:

```text
C:\XB\autocount_outputs\review\member_intake_discovery\<date-run-folder>
```

Capture:

- AutoCount version/build,
- DLL names likely containing member APIs,
- relevant namespace/class/method names,
- Bonus Point enabled yes/no,
- sandbox member type values,
- API Module/access status,
- dry-run test notes,
- AOTG entitlement status if investigated.

Do not capture:

- real customer/member names,
- real mobile/email/birth dates,
- credentials,
- API keys,
- full connection strings,
- raw screenshots with PII,
- production SQL table dumps.

## Optional AOTG Investigation

The public AOTG Swagger lists member endpoints, but X-Boundaries entitlement is unverified. If AOTG is considered:

1. Confirm AOTG subscription and account book activation with the AutoCount admin.
2. Confirm API key location without copying it into Git or chat.
3. Authenticate only from a secure local environment.
4. Test only against sandbox/test account book.
5. Confirm member create/update validation and duplicate behavior with synthetic data.

Do not choose AOTG for production until entitlement, sandbox behavior, error handling, and audit requirements are documented.

