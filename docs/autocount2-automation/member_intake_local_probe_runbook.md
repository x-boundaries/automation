# Member Intake Local Probe Runbook

Use this runbook on the X-Boundaries AC2 machine to collect sanitized metadata about the installed member API surface. The probe is reflection-only by default.

## Output location

Write generated output under:

`C:\XB\autocount_outputs\review\member_intake_discovery\`

Do not commit generated JSON, console captures, screenshots, member rows, credentials, or machine-specific runtime dumps.

## Metadata-only reflection probe

Script path: `scripts/ac2_member_api_probe.ps1`

From the repo root on the AC2 machine:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\ac2_member_api_probe.ps1 `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\member_api_metadata.json"
```

If AutoCount is installed somewhere else, pass `-AcRoot`:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\ac2_member_api_probe.ps1 `
  -AcRoot "C:\Program Files\AutoCount\Accounting 2.2" `
  -JsonOut "C:\XB\autocount_outputs\review\member_intake_discovery\member_api_metadata.json"
```

The probe reports:

- Expected DLL presence.
- Loaded `AutoCount.Invoicing.dll` assembly metadata.
- Whether each expected `AutoCount.BonusPoint.Member` type is found.
- Public and non-public constructors for target types.
- Public declared methods for target types.
- Public declared properties for target types.

The probe does not instantiate `MemberCommand`, does not call `LoadBrowseTable`, does not call `GetMember`, and performs no SaveMember/DeleteMember operations.

## Sanitized capture

Keep captures limited to metadata. Before sharing any output back into the repo or a PR comment:

- Remove local user names if they appear in paths.
- Remove account book names if any assembly path or exception includes them.
- Do not include screenshots of the member UI.
- Do not include real member numbers, names, phone numbers, emails, dates of birth, addresses, notes, or consent records.
- Prefer pasting only the found/missing type summary and constructor signatures.

## Future session/live probe flags

Any future probe that creates an AutoCount session or touches a member command must be added in a separate PR and require explicit opt-in flags. Acceptable future shape:

- Default remains metadata-only.
- A session probe requires a flag such as `-EnableSessionProbe`.
- A sandbox create probe requires a stronger flag such as `-EnableSandboxCreate`.
- Any write-capable mode must require synthetic/test data supplied at runtime.
- Any write-capable mode must refuse to run unless the operator confirms the sandbox account book target.

## Evidence required before sandbox create

Before attempting any sandbox create, collect and review:

- Non-public constructor signatures for `MemberCommand` and `MemberTypeCommand`.
- The official AutoCount bootstrap/session path for this installed build.
- Evidence that the command instance uses the intended sandbox account book.
- A synthetic test member payload with no real PII.
- A rollback/removal plan for the synthetic member.
- Operator confirmation that `MemberType = Default` is valid for sandbox testing.
- Decision on PDPA/marketing consent storage, or explicit exclusion from sandbox create.

## Writeback boundary

No SaveMember/DeleteMember outside a separately approved sandbox write PR.

No `SaveMember`, `DeleteMember`, `SaveMemberType`, or `DeleteMemberType` call belongs in the default metadata probe.

No direct SQL writes are allowed for member intake.
