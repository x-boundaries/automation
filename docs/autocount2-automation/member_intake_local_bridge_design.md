# Member Intake Local Bridge Design

Status: design only. This PR does not implement a production bridge or write to AutoCount.

## Purpose

The bridge is a future local adapter that receives an already validated, manually approved member-intake payload and calls official AutoCount Accounting 2.x APIs. It exists to keep AutoCount-specific assembly loading, credentials, licensing checks, and dry-run/live gates out of Google Sheets and n8n.

## Placement

- Run on the AutoCount server or a locked-down Windows host near the AutoCount Accounting 2.x installation.
- Bind to `localhost` by default.
- If n8n is on a separate host, expose only through a private network allowlist, reverse proxy, VPN, or equivalent controlled path.
- Do not expose the bridge publicly.

## Request Flow

```text
n8n approved row -> HTTP POST /member-intake/dry-run -> bridge validation -> proposed AutoCount payload
n8n approved row -> HTTP POST /member-intake/sync -> bridge writes only after separate approval
```

Only the dry-run endpoint is in scope for the first implementation. The live sync endpoint is a future design placeholder and must stay disabled until approved.

## Bridge Responsibilities

- Verify idempotency key is present.
- Revalidate required fields even if n8n already validated them.
- Reject consent fields unless they are normalized to explicit `Yes` or `No`.
- Load AutoCount Accounting 2.x assemblies from the installed environment.
- Use official AutoCount APIs, not direct SQL writes.
- Confirm dry-run behavior without saving member records.
- Return structured success/error responses.
- Write redacted audit logs.
- Reject payloads with missing PDPA acknowledgement or non-explicit marketing consent.
- Reject or keep dry-run-only any payload where `MemberType` is still `OPEN_MEMBER_TYPE`.
- Enforce least-privilege service account and local-only config.

## Dry-Run Requirement

Dry-run mode must be mandatory for first rollout.

Dry-run should check as much as possible without committing:

- required field presence,
- normalized mobile/email,
- selected `MemberType`,
- proposed member number strategy,
- duplicate lookup candidates if API supports lookup,
- API/session availability,
- Bonus Point module/API Module readiness if detectable.

Dry-run must not call `SaveMember`, direct SQL `INSERT/UPDATE`, import tools, or any equivalent write surface.

## AutoCount Bootstrap Boundary

Local reflection after PR #63 found that the installed member command factories require `AutoCount.Authentication.UserSession` and `AutoCount.Data.DBSetting`.

Bridge design constraints:

- Do not reflect-call internal constructors.
- Prefer the official public Create factory once the safe `UserSession` and `DBSetting` path is confirmed.
- No live read/list/create until the bootstrap path is understood and separately approved.
- Do not instantiate `MemberCommand` or `MemberTypeCommand` during metadata probes.
- Keep bootstrap/session discovery metadata-only until a sandbox validation PR explicitly enables a session probe.

## Session/Auth Boundary

Authentication/session bootstrap is now proven with explicit `AllowRootLogin`. Earlier auth-only probes may create a `DBSetting` with the official `CreateAutoCountDefaultDBSetting` factory and call `UserSession.Authenticate` using runtime-only credentials.

Session/auth constraints:

- Do not call member factories until authentication/session is proven.
- No member list/read until a later read-only PR.
- No member create/update/delete.
- Auth/session instance login diagnostics may call `UserSession.Login`, `SetAsCurrent`, and `CheckHasLogined` only after explicit opt-in and runtime-only credentials.
- Admin/root-style user diagnostics may set `AllowRootLogin` only when the explicit `-AllowRootLogin` switch is supplied.
- Do not call `UserSession.Load` or `CurrentUserTable` in the auth probe.
- No SQL queries or direct SQL writes.
- No DBSetting data methods in the auth probe.
- Passwords must be supplied only through a runtime environment variable.
- Sanitized output must not include server name, database name, user ID, password, connection strings, account book names, member data, or machine usernames.

## First Read-Only Member API Boundary

After auth/session is proven, the first read-only member API boundary is member type browse only.

Allowed behavior:

- Create `MemberTypeCommand` with the proven session and DBSetting.
- Call `MemberTypeCommand.LoadBrowseTable` to confirm configured member type values.
- Return sanitized member type summary fields only.

Still blocked:

- No member/customer records are read.
- No member create/update/delete.
- No member type create/update/delete.
- No direct SQL.
- No DBSetting data methods.
- No runtime credentials, account book names, or generated browse output in Git.

## No-Save MemberCommand Schema Boundary

The next permitted live probe is no-save schema discovery for `MemberCommand`. It may use the proven `UserSession` and `DBSetting` flow, create `MemberCommand` through the public factory, call `GetNextMemberNo()` without returning the actual generated number, and call `NewMember(false)` only to obtain an in-memory `MemberEntity`.

No-save schema constraints:

- No existing member/customer records are read.
- No member browse is allowed.
- No member create/update/delete is allowed.
- No member or MemberType save path is allowed.
- No SQL queries, direct SQL writes, or DBSetting data methods are allowed.
- Output is limited to sanitized booleans, generated-number length/nonempty flags, entity availability, and column schema metadata.
- `MemberType = Default` is API-confirmed by read-only browse, but it must not be written into an entity in this probe.
- Member creation automation remains blocked until this no-save schema probe and a later dry-run mapping pass are reviewed.

## Fake-Data No-Save Assignment Boundary

After the no-save schema is confirmed, the next permitted live probe is synthetic fake-data assignment into the in-memory member row only.

Allowed behavior:

- Create `MemberCommand` with the proven session and DBSetting.
- Call `GetNextMemberNo()` but return only length and nonempty flags.
- Call `NewMember(false)` to obtain an in-memory `MemberEntity`.
- Assign synthetic fake data only into fields needed for future intake mapping.
- Return sanitized per-field assignment status only.

Still blocked:

- No real customer/member data.
- No existing member/customer records are read.
- No member browse is allowed.
- No member create/update/delete is allowed.
- No member or MemberType save path is allowed.
- No SQL queries, direct SQL writes, or DBSetting data methods are allowed.
- No generated member number, credentials, account book names, connection details, or runtime output in Git.
- Member creation automation remains blocked until fake-data no-save assignment and a later save-gated proof are separately reviewed.

## Save-Gated Fake Member Create Boundary

After fake-data no-save assignment was confirmed, a single synthetic fake member create proof was permitted and has been proven.

Allowed behavior:

- Require all write opt-ins before loading AutoCount assemblies.
- Create `MemberCommand` with the proven session and DBSetting.
- Call `GetNextMemberNo()` but return only length, nonempty, and masked-number status in the safe summary.
- Call `NewMember(false)` to obtain a new in-memory `MemberEntity`.
- Assign exactly one synthetic fake member with the marker `XB_AUTOMATION_FAKE_CREATE_PROBE_DELETE_ME`.
- Find and invoke `SaveMember(MemberEntity)` exactly once.
- Record that manual cleanup may be needed in AutoCount UI.

Still blocked:

- No real customer/member data.
- No batch mode.
- No Google Form, n8n, or external payload input.
- No existing member/customer records are read.
- No member browse is allowed.
- No delete or cleanup automation is allowed.
- No member type create/update/delete path is allowed.
- No direct SQL, SQL queries, or DBSetting data methods are allowed.
- No production member creation automation exists until the proof is reviewed and explicit business approval is granted.

## Read-Only Member Browse Extract Review Boundary

For migration reconciliation, a separate local-only review script may browse existing members after the auth/session and member command path are proven.

Allowed behavior:

- Require explicit opt-in before loading AutoCount assemblies.
- Read the AutoCount password only from the runtime `AC2_PROBE_PASSWORD` environment variable.
- Create `MemberCommand` with the proven session and DBSetting.
- Call `LoadBrowseTable` after successful authentication.
- Write private local CSV and summary files only under the requested output directory.

Still blocked:

- No member create/update/delete path.
- No member entity creation or save path.
- No direct SQL, SQL queries, or DBSetting data methods.
- No generated output in Git.
- No raw member rows in PR comments, chat, tickets, or screenshots.

The output contains PII and personal data and must not be committed. Share only sanitized status fields after local review.

## Read-Only Member Lookup Review Boundary

For future n8n/local bridge duplicate checking, a separate local-only review script may check one submitted member number after the auth/session and `MemberCommand` path are proven.

Business rules:

- AC2 / AutoCount 2.0 is the source of truth.
- The Google Form mobile/member number maps to AutoCount `MemberNo`.
- AutoCount `MobilePhone` is intentionally unused for this duplicate-check path.
- Birthday Month maps to future `DOB` as `2000-MM-01`, but DOB is outside this lookup probe.
- Old POS and side sheet data are reference-only.
- The read-only lookup review does not create/update/delete members.

Runtime direction:

- Self-hosted local n8n should call `scripts/ac2_member_lookup_review.ps1` directly for duplicate lookup review. This is the main dry-run runtime path for the current lookup-only step.
- Sanitized JSONL remains useful for offline tests and decision-review rehearsal, but it is not the main runtime path for local self-hosted n8n.
- Cloud n8n cannot call local AC2 PowerShell unless routed through a separately approved local bridge, private route, VPN, or equivalent reviewed adapter.
- n8n should pass `MemberNoBase64Utf8`, not raw `MemberNo`, for Google Form values to reduce shell quoting and interpolation risk.
- `AC2_PROBE_PASSWORD` must be configured as a local environment secret, not passed in command arguments.
- The script returns sanitized JSON only; n8n should parse status fields and route to lookup error review, manual review, existing member review, or ready-for-create review without performing any AutoCount write.

Allowed behavior:

- Require explicit `-EnableMemberLookupReview` before loading AutoCount assemblies.
- Read the AutoCount password only from the runtime `AC2_PROBE_PASSWORD` environment variable.
- Accept exactly one member input source: raw `-MemberNo` for manual local tests or `-MemberNoBase64Utf8` for local n8n calls.
- Normalize the submitted value the same way as the intake validator: remove symbols while keeping letters and digits, canonicalize Singapore 8-digit mobile shapes to `65XXXXXXXX`, keep valid 10-digit `65` values, keep other cleaned shapes as `manual_review`, and reject cleaned values over 20 characters before lookup.
- Create `MemberCommand` with the proven session and DBSetting.
- Call `MemberCommand.GetMember(normalizedMemberNo)` only.
- Return sanitized and PII-free status JSON only.

Still blocked:

- No member create/update/delete path.
- No member browse output for this lookup script.
- No member entity creation or save path.
- No direct SQL, SQL queries, write SQL, or DBSetting data methods.
- No raw `MemberNo`, name, email, phone, DOB, address, AutoKey, Guid, account book, credential, or local target details in output.
- This probe must not be used as final write automation.
- This design must not create production n8n workflows.
- `READY_FOR_CREATE_REVIEW` is not approval to create. It remains a review decision until a separate PR approves the write path, idempotency, consent/audit handling, and write guardrails.

## Dry-Run Decision Review Boundary

For the next dry-run orchestration layer, a Python decision runner may combine Google Form validator results with sanitized AC2 lookup JSONL or a planned lookup pass.

Business rules:

- AC2 / AutoCount 2.0 remains the source of truth.
- The Google Form mobile/member number maps to AutoCount `MemberNo`.
- AutoCount `MobilePhone` is intentionally unused.
- Birthday Month maps to future `DOB` as `2000-MM-01`, but DOB must not appear in row-level decision output.
- Old POS and side sheet data are reference-only.
- `Imported` in the PDPA field remains blocked and must not be treated as consent.

Allowed behavior:

- Reuse the Google Form validator's row validity and MemberNo normalization rules.
- Consume sanitized lookup JSONL keyed by row number, or emit `planned_live_lookup` review decisions without executing live lookup.
- Write local review artifacts containing counts, row numbers, decision codes, lookup status codes, and issue codes only.

Still blocked:

- No AutoCount writes.
- No member create/update/delete path.
- No live lookup execution inside the Python decision runner.
- No direct SQL or SQL read/write query.
- No n8n production workflow creation.
- No raw name, email, phone, MemberNo, DOB, address, AutoKey, Guid, server, database, user, password, or other PII in row-level output.
- This layer must not be used as final write automation.

## Idempotency

Every request must include a stable idempotency key, preferably `IntakeID`.

Bridge behavior:

- Same idempotency key + same payload hash: return the prior result.
- Same idempotency key + different payload hash: reject as conflict.
- Missing idempotency key: reject.

Store only redacted idempotency metadata:

- idempotency key,
- payload hash,
- operation mode,
- status,
- timestamps,
- AutoCount member number if created later,
- redacted error code/message.

## Structured Response Format

Dry-run response example:

```json
{
  "ok": true,
  "mode": "dry_run",
  "idempotency_key": "INT-001",
  "autocount_action": "create_member",
  "member_no_strategy": "auto",
  "member_no": null,
  "sync_eligible": false,
  "dry_run_only": true,
  "warnings": [
    {
      "code": "member_type_not_locally_confirmed",
      "message": "MemberType must be confirmed in sandbox before live sync."
    }
  ],
  "errors": [],
  "audit_id": "audit-20260627-001"
}
```

Failure response example:

```json
{
  "ok": false,
  "mode": "dry_run",
  "idempotency_key": "INT-001",
  "autocount_action": null,
  "member_no_strategy": null,
  "member_no": null,
  "warnings": [],
  "errors": [
    {
      "code": "invalid_pdpa_acknowledgement",
      "message": "PDPAAcknowledged must be normalized to exact Yes or No before dry-run validation."
    }
  ],
  "audit_id": "audit-20260627-002"
}
```

## Audit Logging Without Sensitive Payload Dumps

Audit logs may include:

- audit ID,
- intake ID/idempotency key,
- payload hash,
- request mode,
- validation status,
- AutoCount API surface/version if available,
- redacted response code,
- timestamps.

Audit logs must not include:

- full name,
- full mobile number,
- full email,
- birth date,
- free-text remarks,
- credentials,
- API keys,
- raw AutoCount exception traces containing payload values.

## Service Account And Least Privilege

Future bridge runtime should use a dedicated Windows/service identity and a dedicated AutoCount/API user where possible.

Principles:

- no local administrator rights unless the installed AutoCount API absolutely requires them and that exception is documented,
- no SQL write permissions to AutoCount production tables,
- no broad file share access,
- secrets stored outside Git in Windows Credential Manager, machine/user environment, or approved secret storage,
- separate sandbox and production configuration.

## Rollback And Deactivation

Before live sync exists, rollback is simple: stop the bridge and disable n8n calls.

For future live sync:

- retain a kill switch that forces dry-run mode,
- make n8n check a `BRIDGE_LIVE_SYNC_ENABLED` flag before live calls,
- preserve manual approval gate,
- keep created member numbers in the sheet for operator reconciliation,
- use AutoCount-supported deactivate/update behavior if a member was created incorrectly,
- do not delete members automatically without human review.

## Out Of Scope For This PR

- bridge executable/service,
- AutoCount DLL loading,
- live sync endpoint,
- n8n production workflow,
- real credentials/config,
- direct database access,
- production data tests.
