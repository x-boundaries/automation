# Single-Member Creation UAT Runbook

Status: UAT scaffolding, not production automation. This runbook covers one bounded,
manually triggered, single-member creation UAT: proving that exactly one real
form-derived member can be created in AutoCount and read back safely. It is not the
permanent production member-intake workflow (see
[member intake automation blueprint](member_intake_automation_blueprint.md),
"Future production member-intake workflow boundary").

Everything ships inactive by default. Nothing here contacts the live AutoCount
environment until the operator performs the explicit, separately approved write
step on the AutoCount VM. No AutoCount write is performed by development, tests, or
CI.

## Components

| Component | Path | Runs on |
| --- | --- | --- |
| Package JSON Schema (language-neutral contract) | `schemas/member_create_uat_package.schema.json` | reference |
| Contract library | `scripts/member_create_uat_contract.py` | LAPTOP DEVELOPMENT MACHINE |
| Reviewer approval + package builder | `scripts/member_create_uat_approval.py` | LAPTOP DEVELOPMENT MACHINE |
| Business confirmation config (fail-closed) | `config/member_create_uat_business_confirmation.json` | reference |
| Runner state-machine library (pure) | `scripts/member_create_uat_runner_lib.ps1` | AUTOCOUNT VM — DESKTOP-4I042L6 |
| AutoCount create UAT runner | `scripts/ac2_member_create_uat_runner.ps1` | AUTOCOUNT VM — DESKTOP-4I042L6 |
| Sanitized result precheck | `scripts/member_create_uat_result_precheck.py` | LAPTOP DEVELOPMENT MACHINE or operator PC |
| Inactive result-mapping workflow (UAT only) | `n8n-workflows/member_create_uat_result_mapping.workflow.json` | operator PC n8n (non-AC2) |

## State ownership

- The LAPTOP DEVELOPMENT MACHINE owns the human decision only: the append-only
  approval ledger (approve / reject / hold) and a record of which immutable package
  was built. The ledger is local and never committed.
- The AUTOCOUNT VM owns the exclusive execution lock, the write-intent marker, the
  consumed marker, and the terminal result. Two runners cannot both pass the lock:
  the second attempt terminates `EXECUTION_LOCKED`.
- Identity is the stable `source_record_id` (a one-way hash of the member number)
  plus the change-detection `source_fingerprint`. The spreadsheet row number is only
  a location hint and is never used as identity.

## Blocking business decisions (fail-closed)

While any confirmation in `config/member_create_uat_business_confirmation.json` is
`false`, every write attempt stops at `OPERATOR_CONFIG_REQUIRED` before any AutoCount
contact. Recording a confirmation requires an explicit documented X-Boundaries
business decision, edited into that file in a reviewed change (never invented by an
agent or operator on the fly).

| Field | Intended value | Why it is blocked |
| --- | --- | --- |
| `MemberType` | `Default` | API-confirmed to exist by read-only browse, but business approval is still pending. |
| `RegisterDate` | `2026-07-01` | Intended membership start date is not yet business-confirmed. |
| `ExpiryDate` | `2028-06-30` | Part of the prepared intended assignment/read-back contract, but its AutoCount persistence is not yet proven. Excluded from the active assignment payload and never assigned until the capability flag is flipped after synthetic proof, regardless of confirmation. |
| `OpeningPoints` | `0` | The field mapping states it must not be set by intake unless separately approved. |

`ExpiryDate` is now part of the prepared intended assignment and read-back contract
(`INTENDED_ASSIGNMENT_FIELDS` / `READBACK_VERIFICATION_FIELDS` in
`scripts/member_create_uat_contract.py`, mirrored in the runner library), and the
package still records it as a desired business field covered by the fingerprint and
required to equal `2028-06-30`. It remains excluded from the **active** assignment
payload and is never assigned while the code-level capability flag
(`$script:CreateUatExpiryDateAssignmentImplemented`) is `false` and confirmations are
`false`. Its persistence is proven separately by the synthetic
[ExpiryDate capability probe](member_expiry_capability_probe_runbook.md) before any
follow-up PR may flip the flag.

## Approval expiry

Reviewer approvals expire. The default time-to-live is
`DEFAULT_APPROVAL_TTL_HOURS = 72` (defined in `scripts/member_create_uat_contract.py`).
An operator may shorten it per approval with `--ttl-hours`, never lengthen it beyond
the default. An expired approval yields `APPROVAL_INVALID` and requires re-approval.

## Terminal result codes

| Code | Meaning |
| --- | --- |
| `DRY_RUN_VALIDATED` | Validation passed; no write attempted. |
| `BLOCKED_MEMBER_EXISTS` | A member with that number already exists; stop. |
| `APPROVAL_INVALID` | Approval missing, malformed, or expired. |
| `SOURCE_FINGERPRINT_MISMATCH` | Package integrity or identity/fingerprint check failed (source drift or tamper). |
| `CREATED_VERIFIED` | Member created and read-back matched the approved safe fields. |
| `CREATED_READBACK_MISMATCH` | Member created but read-back did not match. |
| `FAILED_BEFORE_WRITE` | A confirmed failure occurred before any write. |
| `WRITE_OUTCOME_UNCERTAIN` | SaveMember began but success could not be proven. See recovery below. |
| `OPERATOR_CONFIG_REQUIRED` | A business confirmation is still false; write blocked. |
| `WRITE_NOT_CONFIRMED` | Write mode invoked without all five confirmation switches. |
| `PACKAGE_ALREADY_CONSUMED` | The VM consumed marker already exists for this source record. |
| `EXECUTION_LOCKED` | Another runner holds the exclusive execution lock. |
| `APPROVAL_REJECTED` / `APPROVAL_ON_HOLD` / `APPROVAL_ALREADY_CONSUMED` | Recorded by the laptop approval tooling; no package is built. |

## Procedure

### 1. Laptop development and PR review

**`LAPTOP DEVELOPMENT MACHINE`** Develop on the laptop checkout only; never edit files
directly on the physical host. Run the focused and full tests, then open the PR.

```bash
python -m unittest tests.test_member_create_uat_contract tests.test_member_create_uat_approval tests.test_member_create_uat_result_precheck tests.test_member_create_uat_runner_ps
```

```bash
python -m unittest discover -s tests
```

### 2. Merge to `main`

Merge only after review. The merge is performed through the reviewed PR, never by an
agent and never on the physical host.

### 3. Physical host pulls reviewed `main`

**`PHYSICAL HOST — DESKTOP-Q43QKQF`** The physical host only ever fast-forwards to the
reviewed, merged `main`. It is never used for implementation or manual edits.

```bash
git pull --ff-only origin main
```

### 4. Deploy the inactive UAT components

Copy the reviewed `scripts/ac2_member_create_uat_runner.ps1`,
`scripts/member_create_uat_runner_lib.ps1`, and
`config/member_create_uat_business_confirmation.json` to the AutoCount VM working
area. Create the VM-owned state directory once (an operator prerequisite; the runner
never creates it):

**`AUTOCOUNT VM — DESKTOP-4I042L6`**

```powershell
New-Item -ItemType Directory -Path "C:\XB\create_uat\state" -Force
```

Set the AutoCount connection through the process environment only (never in files,
never in this runbook): `AC2_PROBE_SERVER_NAME`, `AC2_PROBE_DATABASE_NAME`,
`AC2_PROBE_USER_ID`, and the password environment variable named by `-PasswordEnvVar`.

### 5. No-write preflight (dry-run)

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Build the approved package on the laptop first
(steps below), copy it to the VM, then run the runner in dry-run mode (the default;
no write switches). Dry-run authenticates, checks the duplicate, constructs the new
member, assigns only the whitelisted fields, and stops without SaveMember.

Laptop package build (**`LAPTOP DEVELOPMENT MACHINE`**), using the decision-review
output that shows the chosen row as `READY_FOR_CREATE_REVIEW`:

```bash
python scripts/member_create_uat_approval.py approve --reviewer <handle> --input <form.csv> --decision-rows <member_intake_decision_rows.csv> --row-number <N> --ledger <ledger.jsonl>
```

```bash
python scripts/member_create_uat_approval.py build-package --input <form.csv> --decision-rows <member_intake_decision_rows.csv> --row-number <N> --ledger <ledger.jsonl> --package-out <member_create_uat_package.json>
```

Copy the package to the VM, then dry-run:

```powershell
& scripts\ac2_member_create_uat_runner.ps1 -PackagePath "C:\XB\create_uat\member_create_uat_package.json" -StateDir "C:\XB\create_uat\state" -JsonOut "C:\XB\create_uat\member_create_uat_result.json"
```

Expect `DRY_RUN_VALIDATED`. If it reports `BLOCKED_MEMBER_EXISTS`, stop: the member
already exists and no creation is warranted.

### 6. Review aggregate evidence

The runner prints and writes a sanitized aggregate result only (masked member number,
hashes, booleans, terminal code). Confirm the terminal code and that no raw member
values appear anywhere.

### 7. Separate current-turn write approval

A real write requires a separate, explicit operator decision that names, in the
current action:

- exactly one member-create operation (the built package's operation id);
- the intended AutoCount account book / environment (server and database);
- permission to call `SaveMember` exactly once.

Only after this is confirmed may the operator proceed, and only if the business
confirmations are all recorded (otherwise the runner returns `OPERATOR_CONFIG_REQUIRED`).

### 8. One write attempt

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Invoke write mode with all five confirmation
switches. The runner revalidates the package and approval, performs a fresh duplicate
recheck, records a durable write-intent marker, marks the approval consumed before the
irreversible section, calls `SaveMember` at most once, and never retries.

```powershell
& scripts\ac2_member_create_uat_runner.ps1 -PackagePath "C:\XB\create_uat\member_create_uat_package.json" -StateDir "C:\XB\create_uat\state" -EnableMemberCreateUat -ConfirmAutoCountWrite -ConfirmExactlyOneMember -ConfirmDryRunPassed -ConfirmNoExistingMemberUpdate -JsonOut "C:\XB\create_uat\member_create_uat_result.json"
```

### 9. Read-back and terminal result mapping

On `CREATED_VERIFIED`, the runner has already read the member back and compared the
approved safe fields. Map the sanitized terminal result to the Sheet:

1. **`LAPTOP DEVELOPMENT MACHINE`** or operator PC: precheck the sanitized result and
   revalidate identity and fingerprint against the expected source record.

   ```bash
   python scripts/member_create_uat_result_precheck.py --result-json <member_create_uat_result.json> --expect-source-record-id <srcrec_...> --expect-source-fingerprint <fp_...>
   ```

2. Operator PC n8n (non-AC2): import a local copy of
   `n8n-workflows/member_create_uat_result_mapping.workflow.json` after replacing
   `REPLACE_WITH_SOURCE_TAB_NAME` with the source tab title. Add the controlled
   columns named in the workflow boundary sticky note and, on the approved row,
   seed `uat_create_operation_id`, `uat_create_source_record_id`, and
   `uat_create_source_fingerprint` from the built package (leave the review columns
   blank). Bind only the Google credential and spreadsheet, copy the sanitized
   result file into `/home/node/.n8n-files/`, and run the workflow manually. It reads
   and updates the one row whose `uat_create_operation_id` equals the result's
   operation id (the single-use mapping key, never a shared marker or row number),
   revalidates the identity and fingerprint hashes, recomputes the terminal code, and
   stays inactive.

### 10. Recovery for `WRITE_OUTCOME_UNCERTAIN`

If the runner returns `WRITE_OUTCOME_UNCERTAIN`, the SaveMember call began but success
could not be proven. Do not retry, delete, or update anything automatically. Perform a
separate read-only recovery check:

1. **`AUTOCOUNT VM — DESKTOP-4I042L6`** In AutoCount Bonus Point > Member Maintenance,
   search for the member number (masked in the runner output; the operator knows the
   real number from the approved source row) and determine whether the member exists.
2. If it exists and matches the approved safe fields, treat the operation as created;
   record the outcome manually. If it does not exist, the write did not commit; the
   consumed marker still blocks an accidental second attempt, so a fresh, separately
   approved operation with a new package is required to proceed.
3. Never delete or edit the member as part of recovery.

The VM keeps durable state files in the state directory: `write_intent_<operation_id>.marker`,
`consumed_<source_record_id>.marker`, and `result_<operation_id>.json` (each written
exclusive-create, never overwritten, containing only sanitised identifiers). On any
re-run the runner classifies these deterministically and never auto-retries a save:
a terminal result present yields `PACKAGE_ALREADY_CONSUMED`; a consumed marker without
a terminal result yields `WRITE_OUTCOME_UNCERTAIN`; a write-intent marker without a
consumed marker yields `FAILED_BEFORE_WRITE`; a malformed marker yields
`WRITE_OUTCOME_UNCERTAIN`. Do not delete these markers; they are the single-use guard.

### 11. UAT shutdown and inactivity

After completion, leave the result-mapping workflow inactive, remove any temporary
copies of the package and result from shared locations, and take no further create
action. The consumed marker and write-intent marker remain on the VM as durable
evidence and single-use guards; do not delete them.

## Safety boundary

- No AutoCount write occurs in development, tests, or CI.
- Exactly one member is supported; there is no batch path, no update-member path, no
  delete, and no rollback automation.
- SaveMember is called at most once and is never automatically retried.
- All console, evidence, test, and workflow output is sanitized and PII-free; member
  numbers are masked and names, emails, and birthdays are never printed.
