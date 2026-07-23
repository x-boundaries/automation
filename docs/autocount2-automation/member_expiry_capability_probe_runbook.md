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
- It performs **no member update, no delete, no rollback, and no automatic cleanup**.
  The one synthetic member it may create will **remain** in AutoCount and must be
  reviewed and removed manually by the owner.
- An **uncertain save outcome is terminal and must never be retried automatically.**

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

Without this explicit, current-turn approval, do not run the probe in write mode.

### 5. One synthetic ExpiryDate persistence test

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Run the probe with every explicit switch. It
authenticates, performs a `GetMember` duplicate check, stops if the synthetic member
already exists, constructs one new member, assigns the narrow synthetic fields including
`ExpiryDate = 2028-06-30`, rechecks for duplicates, and calls `SaveMember` at most once
behind one narrowly scoped function. **It never retries `SaveMember`.**

```powershell
& scripts\ac2_member_expiry_capability_probe.ps1 -EnableExpiryCapabilityProbe -ConfirmSyntheticExpiryDateTest -ConfirmSingleSyntheticMember -ConfirmAutoCountWrite -ConfirmDryRunPreflightPassed -ConfirmNoUpdateOrDelete -JsonOut "C:\XB\create_uat\expiry_capability_probe_result.json"
```

If the probe reports `SAVE_UNCERTAIN`, the save began but could not be confirmed. **Do
not retry.** Treat it as terminal and perform a separate read-only check in AutoCount
(Bonus Point > Member Maintenance, Note marker `XB_AUTOMATION_EXPIRYDATE_PROBE_SYNTHETIC`).

### 6. Read-back evidence

The probe performs a `GetMember` read-back and verifies `ExpiryDate` after explicit
normalisation, then emits **sanitised aggregate evidence only**: booleans, the masked
member number, the terminal outcome, and the normalised `ExpiryDate` values. No raw
synthetic member number, name, or email is printed or written. Expect
`terminal_outcome = EXPIRY_VERIFIED` and `expiry_date_readback_match = true` on success.

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
