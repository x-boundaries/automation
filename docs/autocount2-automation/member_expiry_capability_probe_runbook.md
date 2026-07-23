# Synthetic ExpiryDate Capability Probe Runbook

Status: manual, inactive-by-default capability probe. Not production automation.

This runbook covers one bounded, operator-run test that proves a single `ExpiryDate`
value can be assigned to a new AutoCount member and read back after `SaveMember`, using
**synthetic, non-personal data only**. It exists to de-risk the `ExpiryDate` field
before the real single-member creation UAT enables it.

- Script: `scripts/ac2_member_expiry_capability_probe.ps1`
- Intended value under test: `ExpiryDate = 2028-06-30`
- Related: [Single-member creation UAT runbook](member_create_uat_runbook.md)

## What this probe is (and is NOT)

- It uses hard-coded, clearly synthetic values (a distinctive non-numeric member number,
  an `@example.invalid` email, a synthetic name, and a Note marker). **No real,
  form-derived member is ever used in this capability probe.**
- It is **not the permanent production member-intake workflow**. It is a one-off
  capability proof.
- It performs **no member update, no delete, no rollback, and no automatic cleanup**,
  and it **never removes the attempt claim**. The one synthetic member it may create
  will **remain** in AutoCount and must be reviewed and removed manually by the owner.
- An **uncertain save outcome is terminal and must never be retried automatically.** A
  confirmed save whose read-back then fails is reported as
  `WRITE_CONFIRMED_READBACK_FAILED`, never as a pre-write failure.
- Before the irreversible `SaveMember` it atomically creates a **permanent single-use
  attempt claim** in the operator-provided `-StateDirectory` (`FileMode.CreateNew`,
  write-through). The claim is the concurrent-execution exclusion and the permanent
  no-retry boundary: it is never overwritten or deleted, so a crash after it is created
  makes every future invocation **fail closed** with `ATTEMPT_ALREADY_CLAIMED`. There is
  no automatic claim removal or stale-claim recovery.
- The process **exit code is truthful**: `0` only for a **durably persisted**
  `EXPIRY_VERIFIED`; nonzero for every other outcome, including a refusal. If the
  read-back verified but the durable result could not be written, the final outcome is
  `EVIDENCE_PERSISTENCE_FAILED` (nonzero) and the capability is not proven. Do not rely on
  parsing the JSON to detect failure; check `$LASTEXITCODE`.

## Prepared contract, still gated

This probe is the proof step for the prepared `ExpiryDate` assignment/read-back contract
(`INTENDED_ASSIGNMENT_FIELDS` / `READBACK_VERIFICATION_FIELDS` in
`scripts/member_create_uat_contract.py`, mirrored in
`scripts/member_create_uat_runner_lib.ps1`). Preparing that contract does **not** enable
`ExpiryDate` in the main runner: the code-level capability flag
(`$script:CreateUatExpiryDateAssignmentImplemented`) stays `false` and every business
confirmation stays `false`, so the main runner still stops every real write at
`OPERATOR_CONFIG_REQUIRED` and never assigns `ExpiryDate`.

## Staged procedure

### 1. Laptop development and PR review

**`LAPTOP DEVELOPMENT MACHINE`** Develop and review on the laptop checkout only. Run the
focused and full tests, then open the PR. No AutoCount, n8n, Google, host, or VM action
occurs at this stage.

```bash
python -m unittest tests.test_member_create_uat_contract tests.test_member_create_uat_runner_ps tests.test_ac2_member_expiry_capability_probe
```

### 2. Host pull of reviewed `origin/main`

**`PHYSICAL HOST — DESKTOP-Q43QKQF`** After the PR is reviewed and merged, the physical
host fast-forwards to the reviewed, merged `main`. The host is never used for
implementation or manual edits.

```bash
git pull --ff-only origin main
```

### 3. Deploy the reviewed probe files to the AutoCount VM

Deploy **both** files together (the probe dot-sources the library by relative path, so
they must live in the same VM directory) and prove exact-version equality before any
preflight or approval. Do not execute the probe in this step.

- **Source files (from the reviewed, merged physical-host checkout only):**
  - `scripts/ac2_member_expiry_capability_probe.ps1`
  - `scripts/member_expiry_capability_probe_lib.ps1`
- **Source host:** `PHYSICAL HOST — DESKTOP-Q43QKQF` (the fast-forwarded `origin/main`
  checkout from stage 2). Never the internet, a public share, or manual editing on the VM.
- **Destination:** a documented private directory on `AUTOCOUNT VM — DESKTOP-4I042L6`,
  e.g. `C:\XB\create_uat\probe\`.
- **Transfer:** use the already-established private Hyper-V / SMB shared-folder bridge
  between the host and the VM (see the shared-folder lookup bridge runbook); no public
  network, no email, no copy-paste editing.
- **No unverified overwrite:** if the destination files already exist and do not match,
  take a bounded timestamped backup (e.g. copy to `...\probe\backup_<UTC>\`) before
  replacing them; never overwrite an unverified destination in place.
- **Exact-version verification (required before stage 4):** compute SHA-256 of both the
  source and the destination copies and require exact equality for each file.

```powershell
# On the physical-host checkout (source) and on the VM (destination), then compare:
Get-FileHash .\ac2_member_expiry_capability_probe.ps1, .\member_expiry_capability_probe_lib.ps1 -Algorithm SHA256
```

Record only filenames, SHA-256 hashes, timestamps, and the host/VM machine identities in
the deployment note — never credentials, connection values, or PII. Proceed only when
both destination hashes exactly equal the reviewed source hashes.

### 4. VM dry-run / preflight

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Using the verified VM copy from stage 3, prove the
environment with the main runner's dry-run (no write switches) per the
[Single-member creation UAT runbook](member_create_uat_runbook.md). Confirm the AutoCount
connection through the process environment only (`AC2_PROBE_SERVER_NAME`,
`AC2_PROBE_DATABASE_NAME`, `AC2_PROBE_USER_ID`, and the password environment variable
named by `-PasswordEnvVar`); values are never printed or committed.

### 5. Explicit owner approval for one synthetic write

**A current-turn owner approval is required before the synthetic `SaveMember`.** The
approval must name, in the current action:

- the AutoCount target (the intended server and database / account book);
- exactly one synthetic record (this probe's single synthetic member).

Without this explicit, current-turn approval, do not run the probe in write mode. The
`-ApprovalReference` you pass must **correspond to that explicit current-turn owner
approval**, but it must be an **opaque, non-secret approval identifier** (for example a
ticket or approval-record ID). Do **not** put the server or database/account-book names
into `-ApprovalReference`: it is copied verbatim into stdout and the durable evidence, and
the target is already bound through the hashed `target_fingerprint`. The target names
belong only in the separate current-turn approval record, never in the emitted evidence.

Create the operator-owned private evidence directory once (the probe never creates it),
outside the repository:

```powershell
New-Item -ItemType Directory -Path "C:\XB\create_uat\expiry_probe_state" -Force
```

### 6. One synthetic ExpiryDate persistence test

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Run the probe with every explicit switch. It
authenticates, performs a `GetMember` duplicate check, stops if the synthetic member
already exists, constructs one new member, assigns the narrow synthetic fields including
`ExpiryDate = 2028-06-30`, rechecks for duplicates, and calls `SaveMember` at most once
behind one narrowly scoped function. **It never retries `SaveMember`.**

```powershell
# Use the VERIFIED VM destination path from stage 3, not a repository-relative path.
& C:\XB\create_uat\probe\ac2_member_expiry_capability_probe.ps1 -EnableExpiryCapabilityProbe -ConfirmSyntheticExpiryDateTest -ConfirmSingleSyntheticMember -ConfirmAutoCountWrite -ConfirmDryRunPreflightPassed -ConfirmNoUpdateOrDelete -ApprovalReference "<opaque-approval-id>" -StateDirectory "C:\XB\create_uat\expiry_probe_state"
```

`-ApprovalReference` and `-StateDirectory` are required. The durable, non-overwriting
result is written into `-StateDirectory` as `expiry_probe_result_<operation_id>.json`
(temporary file plus atomic move; a pre-existing result or attempt claim fails closed
before AutoCount contact). Check `$LASTEXITCODE` after the run: `0` means
`EXPIRY_VERIFIED`; any nonzero value means the capability was not proven.

If the probe reports `WRITE_OUTCOME_UNCERTAIN`, the save began but could not be
confirmed. **Do not retry** (the permanent attempt claim already blocks any rerun for
this target/record). Treat it as terminal and perform a separate read-only check in
AutoCount (Bonus Point > Member Maintenance, Note marker
`XB_AUTOMATION_EXPIRYDATE_PROBE_SYNTHETIC`). If the probe reports
`WRITE_CONFIRMED_READBACK_FAILED`, the write completed but the read-back could not be
performed; the synthetic member almost certainly exists and must be reviewed manually.

### Terminal outcomes, exit codes, and recovery

| Terminal outcome | Exit | Meaning / recovery |
| --- | --- | --- |
| `EXPIRY_VERIFIED` | 0 | Synthetic member created; ExpiryDate read back and matched. Capability proven. |
| `EXPIRY_READBACK_MISMATCH` | nonzero | Created and read back, but ExpiryDate did not match. Investigate before any flip. |
| `WRITE_CONFIRMED_READBACK_FAILED` | nonzero | SaveMember returned but read-back failed. The member likely exists; verify manually. Never retried. |
| `WRITE_OUTCOME_UNCERTAIN` | nonzero | SaveMember began but did not return normally. Verify manually. Never retried; claim blocks rerun. |
| `BLOCKED_MEMBER_EXISTS` | nonzero | The synthetic member already exists. Review/remove it manually. |
| `ATTEMPT_ALREADY_CLAIMED` | nonzero | A permanent attempt claim already exists for this target/record. Fails closed before AutoCount contact. |
| `CLAIM_PERSISTENCE_FAILED` | nonzero | The attempt claim was created but could not be durably persisted; no save was reached. The partial claim remains as a fail-closed marker (never deleted). |
| `FAILED_BEFORE_WRITE` | nonzero | A confirmed failure before any write (config, auth, assembly, or setup). No member created. |
| `EVIDENCE_PERSISTENCE_FAILED` | nonzero | The read-back may have verified, but the authoritative durable result could not be written, so the capability is NOT proven. `underlying_terminal_outcome` records the original outcome; a stderr diagnostic is emitted. |
| `REFUSED` | nonzero | Not all explicit switches were supplied; inactive by default. |

Under any uncertain or post-save state the probe never retries automatically and never
removes the attempt claim; recovery is a separate, read-only, owner-directed manual check.

### 7. Read-back evidence

The probe performs a `GetMember` read-back and verifies `ExpiryDate` after explicit
normalisation, then emits **sanitised aggregate evidence only**: booleans, the masked
member number, the terminal outcome, and the normalised `ExpiryDate` values. The
evidence is bound to the run and target through `operation_id`, `approval_reference`,
`executed_at_utc`, and a non-secret `target_fingerprint` (SHA-256 of the server and
database), so an `EXPIRY_VERIFIED` record cannot be mistaken for proof of a different or
stale target. No raw target (server/database), credential, synthetic member number,
name, or email is ever printed or written. Expect `terminal_outcome = EXPIRY_VERIFIED`
and `expiry_match = true` on success.

### 8. Follow-up PR (only after proof)

Only after this probe proves `ExpiryDate` persists may a **separate follow-up PR** open
the write gate. Flipping the capability flag alone is **not sufficient and is unsafe**:
the main runner `scripts/ac2_member_create_uat_runner.ps1` currently builds a hard-coded
`$assignments` set without `ExpiryDate`, sets `expiry_date_assigned` to `false`, and
verifies only `$assignments.Keys` on read-back. If the gate opened without wiring the
field through, the runner could save a member with **no** ExpiryDate and still report
`CREATED_VERIFIED`.

The follow-up PR must therefore, in one change, do **all** of the following before or with
the flag flip, with tests:

1. Flip `$script:CreateUatExpiryDateAssignmentImplemented` to `true` (and the Python
   mirror `EXPIRYDATE_ASSIGNMENT_IMPLEMENTED`).
2. Move `ExpiryDate` from `NEVER_ASSIGN_FIELDS` into the active `ASSIGNABLE_FIELDS`, and
   update the package **schema and builder** (`schemas/member_create_uat_package.schema.json`,
   `scripts/member_create_uat_approval.py`) so `ExpiryDate` is an assignable field with the
   exact intended value.
3. Wire the runner's **assignment** path to assign `ExpiryDate` and set
   `expiry_date_assigned = true`.
4. Wire the runner's **read-back** path so `ExpiryDate` is part of the verified set and a
   read-back mismatch on `ExpiryDate` yields `CREATED_READBACK_MISMATCH`, never
   `CREATED_VERIFIED`.
5. Record the `ExpiryDate` business confirmation.
6. Add tests proving a member cannot reach `CREATED_VERIFIED` without a matching
   `ExpiryDate`.

This PR only prepares the contract and proves persistence; it does not flip the flag.

## Residual synthetic record

This probe never deletes or edits the member. After the test, a synthetic member may
remain in AutoCount. The owner must review and remove it manually if desired; there is no
automatic cleanup path.

## Safety boundary

- No AutoCount write occurs in development, tests, or CI; the probe's AutoCount path is
  never executed by the test suite.
- Exactly one synthetic member is supported; there is no batch, update, delete, or
  rollback path.
- `SaveMember` is called at most once and is never automatically retried.
- All output is sanitised and PII-free; the member number is masked and the synthetic
  name and email are never printed.
