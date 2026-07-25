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
  was built, plus one durable publication reservation per build attempt (the write-ahead
  single-use marker described in step 5). Both are local and never committed.
- The AUTOCOUNT VM owns the exclusive execution lock, the write-intent marker, the
  consumed marker, and the terminal result. Two runners cannot both pass the lock:
  the second attempt terminates `EXECUTION_LOCKED`.
- Identity is the stable `source_record_id` (a one-way hash of the member number)
  plus the change-detection `source_fingerprint`. The spreadsheet row number is only
  a location hint and is never used as identity.

## Business decisions (recorded, fail-closed)

All four business decisions are now explicitly recorded by the owner in
`config/member_create_uat_business_confirmation.json`. While any confirmation in that
file is `false`, every write attempt stops at `OPERATOR_CONFIG_REQUIRED` before any
AutoCount contact. Recording a confirmation requires an explicit documented
X-Boundaries business decision, edited into that file in a reviewed change (never
invented by an agent or operator on the fly).

| Field | Confirmed value | Basis |
| --- | --- | --- |
| `MemberType` | `Default` | Owner explicitly confirmed; API-confirmed to exist by read-only browse. |
| `RegisterDate` | `2026-07-01` | Owner explicitly confirmed the intended membership start date. |
| `ExpiryDate` | `2028-06-30` | Owner explicitly confirmed the intended membership end date. AutoCount ExpiryDate assignment and persistence were proven by the synthetic capability probe, so ExpiryDate is now an active assignable field. |
| `OpeningPoints` | `0` | Owner explicitly confirmed (intake sets no opening points). |

`ExpiryDate` is now an **active** assignable field: it is in `ASSIGNABLE_FIELDS`,
`INTENDED_ASSIGNMENT_FIELDS`, `READBACK_VERIFICATION_FIELDS`, and the immutable package
`member_payload` (`scripts/member_create_uat_contract.py`, mirrored in the runner
library), required to equal `2028-06-30`. Its AutoCount persistence was proven by the
synthetic [ExpiryDate capability probe](member_expiry_capability_probe_runbook.md);
that reviewed synthetic result reported `terminal_outcome = EXPIRY_VERIFIED` with a
matching read-back, and its durable result SHA-256 is
`48CC0185EFF59C3A21AC087BC0C120A00B70801599C3F5513675F7950CD1541B`. The code-level
capability flag (`$script:CreateUatExpiryDateAssignmentImplemented` /
`EXPIRYDATE_ASSIGNMENT_IMPLEMENTED`) is now `true` in exact agreement across the
PowerShell and Python contracts.

The package payload shape therefore changed, so the package schema version was bumped
to `member_create_uat_package/v2`. A package built under the previous `v1` contract has
a different shape (no `ExpiryDate` in the payload / whitelist) and is refused
fail-closed: the runner rejects the unrecognised schema version and the exact
field-set checks reject the old shape. Any package built before this change cannot be
reused; a new reviewer decision and a freshly built immutable `v2` package are
required.

This change performs **no live write**. Recording the confirmations and flipping the
capability flag do not create a member. The runner still performs a real `SaveMember`
only when all five write-confirmation switches are supplied for a valid current
package/approval on the AutoCount VM, and a real write still requires the separate
explicit current-turn owner approval below that names the exact target and operation.

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
python scripts/member_create_uat_approval.py build-package --input <form.csv> --decision-rows <member_intake_decision_rows.csv> --row-number <N> --ledger <ledger.jsonl> --package-out <member_create_uat_package_v2.json>
```

Use a fresh, version-distinct `--package-out` filename (for example
`member_create_uat_package_v2.json`). The build is strictly **no-clobber**: it refuses
fail-closed if the output path already exists as any filesystem object (file, directory,
symlink/reparse point), never deletes, truncates, renames, or overwrites it, and appends
no build ledger event on a collision. **One approval builds exactly one package.**
`--rebuild` is **retired and always refused** (see the terminal reservation rule below); a
further package requires a fresh reviewer decision, a new approval id and a fresh output
pathname. Any package built under the previous `member_create_uat_package/v1` contract,
and its hash, are preserved as historical evidence and remain non-executable under the
`v2` runner (the runner refuses the unrecognised schema version). Because the `v2` schema
bump changes both `source_record_id` and `source_fingerprint` (each binds the schema
version), a fresh reviewer decision is mechanically required; a `v1` decision or build
cannot mint a `v2` package.

#### Durable publication reservation and the terminal reservation rule

The append-only ledger is the audit log, but it is written *after* the package is
published, so it cannot be the only durable record that an approval was consumed. Before
any final package can be published, the builder therefore creates one **durable, exclusive
publication reservation** for that build, beside the ledger:

`member_create_uat_reservation_<approval_id>.1.reservation`

It is a write-ahead intent marker, not a second ledger: created exactly once with
`O_CREAT | O_EXCL`, fsynced (plus a directory-entry fsync on POSIX; on Windows NTFS
journals the entry with the file's own fsync, and the achieved mode is always reported as
`reservation_durability`), then never rewritten, truncated or deleted by the tool. It binds
`approval_id`, `source_record_id`, `source_fingerprint`, `operation_id`,
`bound_package_payload_hash` and the output **basename** only — no member value, no
credential, no absolute path. It is local operational state and is git-ignored.

**The terminal reservation rule.** Once a reservation object for an approval exists — or
may exist — that approval is **permanently consumed** for package-building purposes:

- a plain build is refused;
- `--rebuild` is refused;
- a different, absent output pathname does not bypass it;
- a new process, or a machine restart, does not bypass it;
- **a readable ledger build event does not release it.**

That last point is the reason the rule is absolute. A ledger `flush()`/`fsync()` failure can
leave a *complete, perfectly readable* JSON build event whose durability was never
confirmed, and on restart that record is indistinguishable from a properly persisted one.
Treating it as proof of a finished build would let the same approval mint a second package.
Confirming the confirmation cannot fix this — whatever acknowledges the ledger would itself
need acknowledging — so the ledger simply stops being authority over approval reuse. It
remains the audit record.

The rule applies whether the reservation is confirmed durable, durability-uncertain,
reconciled to a `build` event, reconciled to a `build_cleanup_incomplete` event, unmatched,
malformed, foreign, accompanied by a complete-but-unconfirmed ledger line, or accompanied by
a torn one. The reported `reservation_status` is recovery guidance only, never permission.

On every build the tool checks that approval's reservation slots by direct path lookup; it
never lists, globs or sweeps the directory, and never touches an unrelated reservation,
package or temporary file. A consumed approval reports `status = approval_consumed`
(exit 7). Recovery is always a fresh reviewer decision, or a controlled reconciliation under
review — never a silent retry.

Single use is a property of the **approval**, not of the member row: a fresh reviewer
decision mints a new approval id and can build once, which is what makes recovery possible.

#### Ledger integrity is fail-closed

The ledger is read as an intact sequence of newline-terminated JSON objects. A torn
(partially appended) final record, a malformed record, a record that is not a JSON object,
or a read/decode failure all produce `status = ledger_integrity_uncertain` (exit 8) with
`approval_blocked`, `do_not_retry` and `controlled_recovery_required` — never a traceback.

In that state the tool does **not** discard the malformed record, repair, truncate, rewrite
or replace the ledger, delete any reservation, touch any published package, or continue to
package construction. Only a fixed shape classifier is reported (`ledger_integrity`); no
ledger content, member value, credential or absolute path is ever printed. Reconcile the
ledger under review, then start a fresh reviewer decision.

#### Build outcomes and exit codes

Only `status = ok` (exit 0) means the build is complete: reservation durable, package
published, temporary file removed, and exactly one `build` ledger event fsynced. Every
other outcome is a distinct nonzero exit so no partial state can be mistaken for success.

| Exit | `status` | Published? | Meaning and required action |
| --- | --- | --- | --- |
| 0 | `ok` | yes | Complete and durably recorded. Nothing to do. |
| 2 | `error` | no | Ordinary refusal before the reservation boundary (no approval state consumed). Fix the cause and re-run. |
| 3 | `cleanup_incomplete` | see below | Temporary file could not be removed. The ledger event **was** recorded. |
| 4 | `ledger_record_incomplete` | yes | Published, but the durable ledger event could not be persisted. **Do not retry.** |
| 5 | `reservation_incomplete` | no | This attempt's reservation was not created, or not confirmed durable. |
| 6 | `publication_failed_after_reservation` | no | Reservation is durable but publication failed; the approval is blocked. |
| 7 | `approval_consumed` / `rebuild_requires_fresh_approval` | no | The approval is terminally consumed, or `--rebuild` (retired) was passed. Nothing was created or touched. |
| 8 | `ledger_integrity_uncertain` | no | The ledger is not an intact append-only record. Nothing was created, read further or repaired. |

Exit 3 (`cleanup_incomplete`) reports `stale_temp_basename` (a PII-free `.mcuat_pkg_*.tmp`
name in the output directory) with `manual_cleanup_required`:

- `publication = not_published`: nothing was published and nothing was reserved. Manually
  delete the named stray temporary file, then re-run the build.
- `publication = succeeded`: the final package WAS published and is recorded in the ledger
  as `build_cleanup_incomplete`. Do NOT rebuild this operation (the builder refuses it):
  manually delete the named stray temporary file, and if a new package is genuinely needed,
  start a fresh reviewer decision.

Exit 4 (`ledger_record_incomplete`) means the final package is published and complete but
its durable ledger event was lost, or landed without confirmed durability. Never delete,
move, rename or edit the published package. The durable reservation has terminally consumed
this approval, so it cannot build again at any path, by any invocation. If
`temp_cleanup = failed`, manually delete the named stray temporary file as well. A new
package requires a fresh reviewer decision.

Exits 5 and 6 publish nothing. Where `reservation = uncertain` or
`publication_failed_after_reservation` is reported, the reservation entry is deliberately
left in place: never delete, recreate or retry it, because removing it would turn "may have
been consumed" into "definitely free". Reconcile it under review, or start a fresh reviewer
decision. Where `reservation = not_created`, reservation creation demonstrably did not begin,
no reservation object exists, no approval state was consumed, and a re-run is safe once the
underlying filesystem cause is resolved.

Exit 7 creates and touches nothing at all: no reservation slot, no temporary file, no output
file and no ledger event. `approval_consumed` names the blocking reservation basename and its
diagnostic `reservation_status`, and sets `manual_temp_cleanup_required` when the prior
attempt also left a stray temporary. `rebuild_requires_fresh_approval` is the retired
`--rebuild` flag being refused outright.

Exit 8 also creates and touches nothing, and leaves the ledger byte-for-byte as found.

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
evidence and single-use guards; do not delete them. The laptop-side publication
reservations remain beside the approval ledger for the same reason; do not delete or
sweep them either.

## Safety boundary

- No AutoCount write occurs in development, tests, or CI. Enabling the ExpiryDate path
  (recording the business confirmations and flipping the capability flag) performs no
  live write; a real write still requires the explicit VM write step above and a
  separate current-turn owner approval naming the exact target and operation.
- Exactly one member is supported; there is no batch path, no update-member path, no
  delete, and no rollback automation.
- SaveMember is called at most once and is never automatically retried. An uncertain
  save outcome is terminal (`WRITE_OUTCOME_UNCERTAIN`) and is resolved only by the
  separate read-only recovery check, never by an automatic retry.
- No final package can be published before its durable publication reservation is
  confirmed, so a publication or ledger persistence failure can never leave a published
  package that the same approval is free to build again. An already-published package is
  never deleted, rolled back, truncated, renamed or modified by any failure path.
- A package built under the previous `member_create_uat_package/v1` contract cannot be
  reused; the runner refuses it fail-closed. Build a fresh `v2` package at a new,
  version-distinct path after a new reviewer decision. The package builder is strictly
  no-clobber and never overwrites an existing package, so the old `v1` artifact and its
  hash are preserved as historical evidence and remain non-executable under `v2`.
- The synthetic member and permanent single-use claim created by the earlier
  [ExpiryDate capability probe](member_expiry_capability_probe_runbook.md) are left
  exactly as they are; this UAT path does not read, modify, or clean them up.
- All console, evidence, test, and workflow output is sanitized and PII-free; member
  numbers are masked and names, emails, and birthdays are never printed.
