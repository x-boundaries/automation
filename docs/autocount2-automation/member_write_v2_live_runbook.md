# Member-write v2 live runbook (AC2_VM worker, primitive and test book)

Status: repository-only (G3). Nothing in this runbook has been executed. Every
live step below needs its own current-turn approval naming the host, the target
and the operation (contract W-G2-149 section 9). Placeholders only: never write
real book names, user IDs, host names, addresses or credentials into this file.

Logical hosts: `SERVER_PC` (gateway), `AC2_VM` (worker, primitive, AutoCount).

## 1. Components

| Component | File | Installed |
|---|---|---|
| Worker entry | `scripts/ac2_member_gateway_worker.ps1` | yes |
| Worker library | `scripts/ac2_member_gateway_worker_lib.ps1` | yes |
| AutoCount adapter (the only `SaveMember(` call site) | `scripts/ac2_member_gateway_autocount_adapter.ps1` | yes |
| Primitive `xb-ac2-member create` | `scripts/ac2_member_create_primitive.ps1` | yes |
| Launcher (DPAPI custody) | `scripts/launch_ac2_member_gateway_worker.ps1` | yes |
| Dependency probe | `scripts/test_ac2_member_gateway_autocount_dependencies.ps1` | yes |
| Installer (CI1-CI7) | `scripts/install_ac2_member_gateway_worker.ps1` | no (run from the reviewed checkout) |
| Test cleanup / absence check | `scripts/ac2_member_test_cleanup.ps1` | never (CI4 refuses it) |

Worker cycle (section 4.5): `GET /readyz` -> mutex probe without waiting -> `POST /v2/worker/claim`
-> primitive child (`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile`,
300 s hard deadline, kill + wait up to 30 s) -> `POST /v2/jobs/{job_id}/result` (identical body, at
most 3 posts, only on transport failure or HTTP 5xx, only within the lease; HTTP 409 is discarded).
No heartbeat. The worker never opens AutoCount itself.

## 2. Primitive I/O contract (closed)

- Invocation: `powershell.exe -NoLogo -NoProfile -NonInteractive -File ac2_member_create_primitive.ps1 -Book production|test [-EnableProductionAdapter]`.
- stdin: one JSON line, ASCII only (non-ASCII escaped `\uXXXX`), exactly these keys: the claim v2
  `request` fields `rule, base_member_no, name_component, phone, name, email, MemberType, DOB,
  RegisterDate, ExpiryDate, OpeningPoints, IsActive, Individual` plus `job_id, attempt_no,
  first_claimed_at, server_time_utc`. Any other shape -> `MANUAL_REVIEW(request_contract_violation)`.
- stdout: one JSON line, exactly `outcome, rule, branch, member_no, member_guid, save_invoked,
  save_invocation_count, readback, reason_code, dq_flags, primitive{release_sha256, rule_version},
  error_code`, enums from `schemas/member_gateway_result.v2.schema.json`. No personal data except
  `member_no`. Exit code 0 whenever a line was written.
- The worker adds `schema_version, job_id, attempt_no, lease_id, state_version` to form result v2.
- Worker-synthesised results (`child_deadline_exceeded`, `child_termination_unconfirmed`,
  `primitive_output_invalid` -> `OUTCOME_UNCERTAIN`; `primitive_launch_failed`,
  `fault_injection_refused` -> `FAILED_BEFORE_WRITE`): rule/branch `NONE`, member_no/guid/readback
  null; uncertain ones report `save_invoked=true, save_invocation_count=1`.

## 3. Release identity (`primitive.release_sha256`)

`release_sha256` = lowercase SHA-256 hex of the ASCII text made of one line
`<file name>:<lowercase SHA-256 of the file bytes>` + LF for each of the six installed package
files, file names sorted ordinally (`ac2_member_create_primitive.ps1`,
`ac2_member_gateway_autocount_adapter.ps1`, `ac2_member_gateway_worker.ps1`,
`ac2_member_gateway_worker_lib.ps1`, `launch_ac2_member_gateway_worker.ps1`,
`test_ac2_member_gateway_autocount_dependencies.ps1`). The primitive and the worker compute it
from their own folder at run time; the installer records it in `installation-manifest.json`
(schema `xb.member.gateway.worker.installation.v2`) and CI7 recomputes it. D-3 compares it with
the value computed from the merged PR commit.

## 4. Installer checks CI1-CI7 (binding made in G3)

The contract names CI1-CI7 without defining them; G3 binds them as follows.

| Check | Meaning | Code |
|---|---|---|
| CI1 | install layout and fixed roots | `Assert-XbWorkerInstallLayout` |
| CI2 | reviewed-package identity: commit, tree, git blob, sha256 membership | `Read-XbReviewedPackageIdentity` |
| CI3 | staged package bytes equal the reviewed identity | `Assert-XbStagedPackageIdentity` |
| CI4 | release content: exactly one `SaveMember(` (in the adapter), no `DeleteMember`, no UAT/probe/cleanup file | `Assert-XbReleaseContent` |
| CI5 | task contract: disabled, no triggers, IgnoreNew, 10 min limit, no retries, absolute interpreter, `-Mode DisabledProof` only | `Assert-XbWorkerTaskContract` |
| CI6 | runtime custody: protected ACLs, no broad-group grants, secrets are DPAPI SecureString CLIXML | `Assert-XbRuntimeCustody` |
| CI7 | install verifier: CI1, installed bytes vs manifest, release identity, CI4-CI6, worker effective rights | `Invoke-XbInstallVerifier` (`-Operation Verify`) |

Operations: `Install` (fresh), `Upgrade` (in place: snapshot to `rollback\upgrade-<utc>`,
replace package files, re-register the task disabled, keep `config`, `secrets`, `logs`),
`Verify` (read-only CI7, `-RequireSecrets` after L8), `Uninstall`, `ValidateOnly`.

## 5. DPAPI custody

- Secrets live under `C:\ProgramData\X-Boundaries\MemberGatewayWorker\secrets\` as CLIXML exports
  of SecureStrings created by the worker account (`worker-token.clixml`,
  `autocount-password.clixml`). Create them while logged on as the worker account, for example
  `Read-Host -AsSecureString | Export-Clixml -LiteralPath <path>`; never type them into a file.
- The launcher decrypts them only into the worker process environment. The worker moves the
  integration-user (IU) password out of its own environment at start and passes it only in the
  primitive child environment; the primitive clears it right after login. It is never logged and
  never placed on a command line.
- Config: `config\worker.config.json` from `config/ac2_member_gateway_worker.production.example.json`
  (or `.test_book.example.json`); `autocount_user_id` must equal `autocount_integration_user_id`.

## 6. Fault hooks and cleanup

- `XB_AC2_FAULT` (primitive: `save_error_after_commit`, `exit_after_save`, `hang_after_save`,
  `readback_error`) and `XB_WORKER_FAULT` (worker: `skip_result_post`) are inert unless
  `-Book test`, the configured database is on the test allowlist and is not the production book,
  and the request is fully synthetic (name starts `ZZTEST ` and email ends `@example.invalid`).
  Otherwise the run returns `FAILED_BEFORE_WRITE(fault_injection_refused)` without a session.
- `000`-prefixed phone bases are ordinary (amendment); they are not a synthetic marker.
- Absence check before each test batch (required, writes nothing):
  `ac2_member_test_cleanup.ps1 -Book test -VerifyAbsent -EnableProductionAdapter` -> `status=absent`.
- Cleanup after the batch: `ac2_member_test_cleanup.ps1 -Book test -ConfirmDelete -EnableProductionAdapter`
  deletes only fully synthetic rows whose `CreatedUserID` is the IU, in an allowlisted
  non-production book; then rerun `-VerifyAbsent`.

## 7. Live steps (each separately approved)

| Step | Host | Operation | Evidence | Rollback |
|---|---|---|---|---|
| L1 | AC2_VM, SERVER_PC | read-only checks: AC2 to gateway health, admin state, prior UAT member lookup, clock skew, malformed MemberNo count | counts only | n/a |
| L2 | AC2_VM | neutralise the test book (T0-T6) | receipt | restore the test book |
| L3 | AC2_VM | create the test-book IU; install the release to a test path | CI7 output | delete the user |
| L4 | AC2_VM | absence check, T-1..T-17, cleanup, absence check | matrix below | cleanup script |
| L5 | SERVER_PC | gateway database backup and restore rehearsal | receipt | n/a |
| L6 | SERVER_PC or DEV | disposable end-to-end stack E-1..E-10 | receipt | tear down |
| L7 | SERVER_PC | rebuild the production gateway stack, dark | readyz | previous stack |
| L8 | AC2_VM | create the production IU; DPAPI custody | CI6 | disable the user |
| L9 | SERVER_PC | pre-migration dump; zero non-terminal jobs | receipt | n/a |
| L10 | SERVER_PC | apply 0006 and deploy v2, dark | readyz | restore L9 dump and image |
| L11 | AC2_VM | `install_ac2_member_gateway_worker.ps1 -Operation Upgrade`; task stays disabled; then `-Operation Verify -RequireSecrets` | CI1-CI7 pass, release_sha256 | restore `rollback\upgrade-<utc>` |
| L12 | SERVER_PC | read live n8n state; re-import inactive only if it differs | diff | n/a |
| L13 | both | collect D-1..D-5 | receipts | n/a |

None of L1-L13 turns off the kill switch, turns on activation, enables the worker task,
activates an n8n workflow or sends an email.

## 8. Test-book matrix T-1..T-17 (L4)

| ID | Confirms | Pass evidence |
|---|---|---|
| T-1 | a 20-character alphanumeric MemberNo is accepted | CREATED_VERIFIED with a 20-char name-appended MemberNo |
| T-2 | Guid, CreatedUserID, CreatedTime readable; CreatedUserID = IU | readback created_by_integration_user=true |
| T-3 | `MemberCommand.LoadBrowseTable()` returns MobilePhone, Guid, CreatedUserID, CreatedTime for every row | no `probe_unavailable` |
| T-4 | duplicate-save error type; a new entity never updates an existing row | NOT_CREATED_CONFLICT, row unchanged |
| T-5 | case sensitivity of the MemberNo unique index | recorded |
| T-6 | POS finds an alphanumeric MemberNo | staff check |
| T-7 | R1 create | CREATED_VERIFIED R1 |
| T-8 | replay -> R0 | CREATED_VERIFIED_PRIOR_ATTEMPT |
| T-9 | crash right after save (`exit_after_save`) -> R0 on retry | uncertain then prior attempt |
| T-10 | format variant -> link | LINKED_EXISTING |
| T-11 | R4 create | CREATED_VERIFIED R4 |
| T-12 | R3, R3b, R2b review cases | MANUAL_REVIEW with those reasons |
| T-13 | two concurrent primitives serialise; mutex rights correct | one MUTEX_BUSY |
| T-14 | time zone of CreatedTime vs gateway UTC (W0 window) | recorded offset |
| T-15 | p99 child run time under 120 s | timings |
| T-16 | IU can only create and view | denied edit/delete |
| T-17 | cleanup removes only synthetic IU rows | cleaned, then absent |

Fail-closed until T-1, T-2, T-3, T-6 and T-14 pass: the primitive refuses on missing probe
columns, missing Guid or foreign CreatedUserID. `DeleteMember` signature is confirmed by T-17.

## 9. Production dark exit evidence (D-1..D-5)

D-1 readyz ready with dispatch disabled; D-2 a manual idle cycle returns `idle` and opens no
AutoCount session; D-3 CI7 passes and release_sha256 matches the PR; D-4 migrations 0001-0006
applied with zero non-terminal jobs; D-5 worker task disabled and n8n workflows inactive.
