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
- The process **exit code is truthful**: `0` only for `EXPIRY_VERIFIED`; nonzero for
  every other outcome, including a refusal. Do not rely on parsing the JSON to detect
  failure; check `$LASTEXITCODE`.

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

### 3. VM dry-run / preflight

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Before running this write-capable probe, prove the
environment with the main runner's dry-run (no write switches) per the
[Single-member creation UAT runbook](member_create_uat_runbook.md). Confirm the AutoCount
connection through the process environment only (`AC2_PROBE_SERVER_NAME`,
`AC2_PROBE_DATABASE_NAME`, `AC2_PROBE_USER_ID`, and the password environment variable
named by `-PasswordEnvVar`); values are never printed or committed.

### 4. Explicit owner approval for one synthetic write

**A current-turn owner approval is required before the synthetic `SaveMember`.** The
approval must name, in the current action:

- the AutoCount target (the intended server and database / account book);
- exactly one synthetic record (this probe's single synthetic member).

Without this explicit, current-turn approval, do not run the probe in write mode. The
`-ApprovalReference` you pass must **correspond to that explicit current-turn owner
approval** naming the exact AutoCount target and one synthetic record; it is a
non-secret label recorded in the durable evidence (never a credential).

Create the operator-owned private evidence directory once (the probe never creates it),
outside the repository:

```powershell
New-Item -ItemType Directory -Path "C:\XB\create_uat\expiry_probe_state" -Force
```

### 5. One synthetic ExpiryDate persistence test

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Run the probe with every explicit switch. It
authenticates, performs a `GetMember` duplicate check, stops if the synthetic member
already exists, constructs one new member, assigns the narrow synthetic fields including
`ExpiryDate = 2028-06-30`, rechecks for duplicates, and calls `SaveMember` at most once
behind one narrowly scoped function. **It never retries `SaveMember`.**

```powershell
& scripts\ac2_member_expiry_capability_probe.ps1 -EnableExpiryCapabilityProbe -ConfirmSyntheticExpiryDateTest -ConfirmSingleSyntheticMember -ConfirmAutoCountWrite -ConfirmDryRunPreflightPassed -ConfirmNoUpdateOrDelete -ApprovalReference "<approval-ref-naming-target-and-one-record>" -StateDirectory "C:\XB\create_uat\expiry_probe_state"
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
| `FAILED_BEFORE_WRITE` | nonzero | A confirmed failure before any write (config, auth, assembly, or setup). No member created. |
| `REFUSED` | nonzero | Not all explicit switches were supplied; inactive by default. |

Under any uncertain or post-save state the probe never retries automatically and never
removes the attempt claim; recovery is a separate, read-only, owner-directed manual check.

### 6. Read-back evidence

The probe performs a `GetMember` read-back and verifies `ExpiryDate` after explicit
normalisation, then emits **sanitised aggregate evidence only**: booleans, the masked
member number, the terminal outcome, and the normalised `ExpiryDate` values. The
evidence is bound to the run and target through `operation_id`, `approval_reference`,
`executed_at_utc`, and a non-secret `target_fingerprint` (SHA-256 of the server and
database), so an `EXPIRY_VERIFIED` record cannot be mistaken for proof of a different or
stale target. No raw target (server/database), credential, synthetic member number,
name, or email is ever printed or written. Expect `terminal_outcome = EXPIRY_VERIFIED`
and `expiry_match = true` on success.

### 7. Follow-up PR (only after proof)

Only after this probe proves `ExpiryDate` persists may a **separate follow-up PR** flip
the main runner's capability flag (`$script:CreateUatExpiryDateAssignmentImplemented`)
and move `ExpiryDate` from `NEVER_ASSIGN_FIELDS` into the active `ASSIGNABLE_FIELDS`,
alongside the business confirmation decision. This PR only prepares the contract; it does
not flip the flag.

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
