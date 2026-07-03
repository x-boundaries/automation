# Member Intake Automation Blueprint

Status: discovery and implementation planning only. No production writeback is implemented by this PR.

## Target Flow

```text
Google Form -> Google Sheets -> n8n validation workflow -> local bridge -> AutoCount 2.0 Member API
```

The first rollout should stop before live writeback:

```text
Google Form -> Google Sheets -> n8n draft workflow/design -> dry-run validator -> read-only AC2 member lookup -> manual review
```

## System Roles

| Component | Role | Source-of-truth status |
| --- | --- | --- |
| Google Form | User-friendly intake surface for new member registration. | Not source of truth |
| Google Sheets | Intake queue, audit queue, approval queue, and human review surface. | Not source of truth |
| n8n | Orchestration, validation, approval-state transitions, retry/dead-letter routing. | Not source of truth |
| Local bridge | Future controlled adapter between n8n and official AutoCount APIs. | Not source of truth |
| AutoCount 2.0 | Final member record, member number, member type, bonus point membership state. | Source of truth |

Current duplicate-check rule: the Google Form mobile/member number maps to AutoCount `MemberNo`, AutoCount `MobilePhone` is intentionally unused, Birthday Month maps to future `DOB` as `2000-MM-01`, and old POS or side-sheet values are reference-only. The read-only member lookup review does not create/update/delete members.

## Why Google Sheet Is Intake/Audit Queue Only

Google Sheets is useful for visibility, manual approval, and non-technical operations review, but it must not become a parallel member database. Sheet rows can be edited outside the form, formulas can drift, and access can expand beyond AutoCount administrators.

The sheet should store intake status and sync status only:

- one row per submitted registration,
- validation and approval status,
- future bridge request/response metadata,
- redacted failure details,
- AutoCount member number after successful approved sync.

Do not treat sheet values as authoritative after AutoCount accepts a member. If a conflict exists, AutoCount wins.

## Why AutoCount Remains Source of Truth

AutoCount controls member numbers, member types, Bonus Point module behavior, point balances, and downstream sales/bonus-point usage. The wiki confirms member APIs are part of AutoCount member maintenance and Bonus Point workflows. Creating a side database or direct SQL write path would risk bypassing validation, numbering rules, audit behavior, and module-specific logic.

## Preferred Write Path

Use a local-only bridge unless AOTG member write access is confirmed end-to-end in a sandbox.

Reasons:

- The AutoCount Accounting 2.0 desktop assembly API is confirmed for member create/update examples.
- The bridge can run near the installed AutoCount client/server and use the same official assemblies as documented.
- AOTG member endpoints are publicly listed, but X-Boundaries still must confirm subscription, account book activation, API key handling, and tenant-specific permissions.
- A local bridge can be locked to localhost or a private LAN allowlist and can enforce dry-run mode before any live writeback.

If AOTG is later selected, it must still follow the same approval, idempotency, dry-run, and audit requirements.

## Privacy And PDPA Considerations

Member intake contains personal data: name, mobile number, email, birth date, country, consent status, and free-text remarks. Treat every form row as sensitive.

Google Form consent controls must be configured as **Multiple choice** fields with exact operator-facing values:

- `PDPAAcknowledged`: `Yes` / `No`
- `MarketingConsent`: `Yes` / `No`

Do not use a checkbox-style long acknowledgement as direct validator input. Google Forms can export the full checkbox option text, which is intentionally rejected by the dry-run validator unless n8n or another normalization step converts it to exact `Yes` or `No` first.

Minimum controls:

- Form must require PDPA acknowledgement before sync eligibility.
- Marketing consent must be explicit `Yes` or `No`; missing consent is not acceptable.
- n8n and bridge logs must not dump full payloads.
- Audit logs may store intake ID, hash/idempotency key, field presence, status codes, and redacted errors.
- Limit Google Sheet access to approved operators.
- Avoid storing free-text remarks in bridge logs.
- Do not commit real form rows, sheet IDs, screenshots, n8n credentials, API keys, or bridge runtime outputs.
- Define retention for rejected/dead-letter rows before production rollout.

## Manual Approval Gate

First rollout must require manual approval before any member write:

1. Form submission lands in Google Sheet with `ApprovalStatus = Pending`.
2. n8n or a local validator computes `ValidationStatus`.
3. Operator reviews required fields, duplicate risk, consent flags, and selected `MemberType`.
4. Operator confirms `MemberType` is a sandbox-confirmed AutoCount member type; placeholder `OPEN_MEMBER_TYPE` payloads are dry-run-only and not sync-eligible.
5. Operator sets `ApprovalStatus = Approved` only when ready.
6. Dry-run bridge returns the proposed AutoCount payload.
7. Live writeback remains disabled until a separate production approval PR/runbook exists.

## Failure And Dead-Letter Handling

Use explicit statuses instead of overwriting rows in place.

Recommended statuses:

- `ValidationStatus`: `Pending`, `Valid`, `Invalid`
- `ApprovalStatus`: `Pending`, `Approved`, `Rejected`, `NeedsReview`
- `SyncStatus`: `NotStarted`, `DryRunPassed`, `DryRunFailed`, `Queued`, `Synced`, `Failed`, `DeadLetter`

Dead-letter rows should capture:

- `IntakeID`,
- failure stage,
- redacted error code/message,
- retry count,
- last attempted timestamp,
- operator decision,
- next action.

Do not retry indefinitely. Require operator review after repeated validation/bridge failures.

## Legacy Import Plan For Existing Members Later

Existing member import is a separate project. Do not mix it with first-time intake automation.

Later import should:

- export existing members from AutoCount through official API/reporting/read-only approved surfaces,
- normalize legacy identifiers, mobile/email duplicates, and consent state,
- dry-run match against AutoCount by `MemberNo`, mobile, email, and name,
- produce a manual review workbook without raw secrets or unnecessary PII,
- use sandbox-only write tests,
- require a separate approval gate before any production merge/update.

## Current PR Boundary

Included:

- API research and evidence notes.
- Field mapping plan.
- Local bridge design.
- Discovery runbook.
- Python dry-run validator with synthetic tests.
- Explicitly gated, sanitized, read-only AC2 `MemberNo` lookup review for future duplicate checking.

Excluded:

- Production AutoCount writeback.
- Final write automation.
- Direct SQL writes.
- n8n production workflow creation.
- AutoCount DLL dependency in CI.
- Real customer/member PII, sheet IDs, credentials, API keys, or runtime outputs.
