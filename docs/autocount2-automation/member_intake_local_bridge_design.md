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
