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
| Transactional reviewer-decision authority store | `scripts/member_create_uat_decision_store.py` | LAPTOP DEVELOPMENT MACHINE |
| Business confirmation config (fail-closed) | `config/member_create_uat_business_confirmation.json` | reference |
| Runner state-machine library (pure) | `scripts/member_create_uat_runner_lib.ps1` | AUTOCOUNT VM — DESKTOP-4I042L6 |
| AutoCount create UAT runner | `scripts/ac2_member_create_uat_runner.ps1` | AUTOCOUNT VM — DESKTOP-4I042L6 |
| Sanitized result precheck | `scripts/member_create_uat_result_precheck.py` | LAPTOP DEVELOPMENT MACHINE or operator PC |
| Inactive result-mapping workflow (UAT only) | `n8n-workflows/member_create_uat_result_mapping.workflow.json` | operator PC n8n (non-AC2) |

## State ownership

- The LAPTOP DEVELOPMENT MACHINE owns the human decision only. Reviewer **authority** is
  the transactional, append-only SQLite decision store; the JSONL approval ledger
  (approve / reject / hold, plus which immutable package was built) is an append-only
  **audit record only** and never grants authority. There is also one durable publication
  reservation per build (the write-ahead single-use marker described in step 5). All three
  are local private state and are never committed.
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

Expiry is read from the **transactional decision store**, not from the JSONL audit ledger, so
hand-editing an audit line can neither expire nor extend an approval.

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

#### Transactional reviewer-decision authority (SQLite) — JSONL is audit-only

Reviewer **authority** lives in a private, git-ignored, append-only SQLite database beside
the approval ledger:

`member_create_uat_decisions.sqlite3`

Its legitimate private runtime companions (`-journal`, `-wal`, `-shm`) are git-ignored by
exact name. The store holds **sanitised decision metadata only** — decision sequence,
decision id, decision type, reviewer handle, timestamps, approval id, source record id,
source fingerprint, schema version and a canonical record hash. It contains no member
number, name, mobile number, email address, birthday, credential or absolute path.

**Why this exists.** `append_ledger` writes the complete JSON line *before* `flush()` and
`os.fsync()` return, so a real persistence failure can leave a complete, perfectly readable
`approved` line on disk even though the command reported failure and durability was never
confirmed. A later process read that line back and could reserve and publish a package from
an approval that was never durably granted. Chaining more marker files cannot fix this —
each new file needs its own acknowledgement, indefinitely — so the decision boundary moved
inside a real transaction.

> **The JSONL approval ledger is an append-only AUDIT RECORD ONLY. It never grants
> package-building authority.** A decision authorises a package only when a committed
> **activation row** exists for it in the decision store.

**Schema (`member_create_uat_decisions/v2`).** Three append-only history tables plus schema
metadata: `decision` (every approve, reject and hold attempt, with one shared monotonic
`sequence`), `decision_activation` (the decisions that became authoritative) and `build_claim`
(the single exclusive authorisation to build one package — see below). `UPDATE` and `DELETE`
are rejected on all three by database triggers, so even a direct `sqlite3` session cannot
rewrite or erase history. Durability settings: `journal_mode=DELETE`, `synchronous=FULL`,
`foreign_keys=ON`, explicit `BEGIN IMMEDIATE` transactions and a bounded busy timeout.

**Amendment 7 raised the version deliberately.** Adding transactional build claims changes the
authority model, so it is a new version rather than a disguised v1. A v1 store — like an empty,
zero-byte, partial or foreign one — is refused **untouched**. There is no migration.

**Pure pre-open triage decides before SQLite is opened (Amendment 8).** Amendment 7 opened an
existing file with SQLite and only then decided whether it was canonical — but the first pragma
it applied, `journal_mode=DELETE`, is *persistent*. Against a WAL-mode database it rewrote the
header and removed the `-wal`/`-shm` companions, so a store the tool then refused had already
been changed, and a foreign database's write-ahead log could be destroyed. Deleting a hot
journal or WAL is the documented way to lose crash recovery.

Every access to an existing store now begins with ordinary, non-following filesystem calls and
**no SQLite call at all**:

1. safe-local-path validation, and rejection of a symlink, junction or reparse point;
2. a plain regular file with **exactly one hard link** (a multiply-named database has undefined
   behaviour, because each name derives its own journal path);
3. stable file identity captured, and re-checked after the header read;
4. the complete 100-byte SQLite header read with a plain file handle, requiring the
   `SQLite format 3\0` magic, a plausible page size, and header bytes 18 and 19 both equal to
   `1` (rollback format; `2` means WAL);
5. exact absence of `<store>-journal`, `<store>-wal` and `<store>-shm`, checked by exact path —
   never by listing, globbing or sweeping a directory.

> **A WAL header or any sidecar object is controlled-recovery-only.** The tool never opens,
> checkpoints, rolls back, deletes, renames, recreates or repairs such a store — even when the
> database otherwise looks canonical.

**Read-only inspection and trusted writing are separate paths.** Non-mutating work — the build
preflight, authority reads, decision-sequence reporting and all three COMMIT-recovery lookups —
opens `mode=ro&cache=private`, sets only connection-local protections (`busy_timeout`,
`foreign_keys`, and `query_only` as defence in depth *only*), **queries** `PRAGMA journal_mode`
and requires `delete`, runs the complete global validator plus the bounded read inside one read
transaction, closes, and then re-proves the file's identity, link count and sidecar absence.
Writers repeat the pure triage, open `mode=rw&cache=private`, apply the same connection-local
settings plus `synchronous=FULL`, verify the journal mode, take one `BEGIN IMMEDIATE`, re-run
the **complete global validator inside that transaction**, and only then resolve and insert.
`journal_mode` is assigned in exactly one place in the codebase: the brand-new temporary a
creation operation exclusively owns. `immutable=1` is never used, because it disables change
detection and can silently omit committed WAL-resident state.

**Canonical validation is store-global.** Validation covers schema *meaning*, not just object
names, through two independent mechanisms: the exact canonical `sqlite_schema` DDL text of every
application object (which pins declared types, `NOT NULL`, defaults, primary keys,
`AUTOINCREMENT`, `UNIQUE`, `CHECK` bodies, foreign-key columns and actions, index columns/order/
uniqueness and trigger timing/event/target/body all at once), plus pragma-derived checks
(`table_info`, `index_list`, `index_info`, `foreign_key_list`). The deterministic order is:
`integrity_check`; `foreign_key_check`; the exact permitted object set and canonical DDL;
columns, indexes, constraints and foreign keys; the **exact** `schema_meta` row set (exactly one
row, key `schema_version`, value `member_create_uat_decisions/v2`, no other key); every
`decision` row in sequence order; every activation row in activation-sequence order; every claim
row in claim-sequence order; then the cross-table orphan and binding checks. Only SQLite's own
`sqlite_sequence` and the implicit `sqlite_autoindex_*` indexes are tolerated.

Amendment 7 validated decision and activation content only inside the source-filtered authority
query, so a malformed row under an unrelated source record survived into an authorised build.
**A malformed row anywhere now blocks every operation**, whichever source record was requested.
Timestamps are parsed into aware instants by one central parser and compared as instants, never
as text: `approved_at >= recorded_at`, `expires_at >= approved_at`, `activated_at >=
recorded_at`, and — new in Amendment 8 — `claimed_at >= approved_at` and `claimed_at >=
activated_at`, both for every stored claim and, immediately before insertion, for the claim a
build is about to mint. Two valid timestamps written in different UTC offsets order differently
as strings than in time, so a lexical comparison is never the authority.

Any mismatch refuses fail-closed and the store is **never** recreated, replaced, migrated,
augmented or repaired.

**Creation is allowed only at a positively absent path.** The complete canonical store is built
in an operation-owned temporary in the same directory, validated in full, closed, proven to have
no sidecar and exactly one link, flushed durably, and only then published. Exclusively creating
the final path and *then* running DDL on it would leave a window in which a concurrent process
opens a zero-byte file and correctly concludes it is not a canonical store; publishing an
already-complete store removes that window, so the final path only ever appears fully formed.
Publication is platform-specific:

| Platform | Primitive | Durability reported | Temporary |
| --- | --- | --- | --- |
| Windows | `MoveFileExW` **without** `MOVEFILE_REPLACE_EXISTING`, with `MOVEFILE_WRITE_THROUGH` | `windows_move_write_through` | none survives a move, so no second name is ever created |
| POSIX | no-replace `os.link` with every SQLite connection closed, parent-directory fsync, unlink of the operation-owned temporary, parent-directory fsync again | `posix_link_and_directory_fsync` | `unlinked`; a failed unlink is reported, never suppressed |

The published store must be a plain regular file with **exactly one link** and the identity this
operation created, and it must pass the same locked read-only inspection before it may be used.
If a competitor wins the race, their store is left byte-for-byte untouched and only this
operation's own temporary is removed. If schema setup fails, the final path is never created and
the temporary is deliberately left in place as evidence. A publication whose durability cannot
be proven, or a temporary that cannot be removed, is reported as a controlled-recovery state —
never silently treated as an authorised empty store. Only a durability primitive actually
confirmed on the running platform is ever named; Windows offers no directory-handle fsync, so
none is claimed there.

**Concurrency consequence you should expect.** Because a sidecar is refused unconditionally, a
second tool process that meets a peer *mid-transaction* now fails **closed** with
`store_sidecar_present` instead of waiting on the busy timeout and then committing. Nothing is
written and nothing is changed, but the second command does not succeed. Run reviewer decisions
one at a time. Pure lock contention — a peer holding the write lock without having written a
page, so no journal exists — still reports the retryable `decision_not_recorded` /
`build_claim_not_recorded` outcome.

**Threat-model boundary.** The supported location is a stable, local, operator-controlled state
directory, and the protections above cover malformed, foreign, partial and corrupt databases,
WAL and sidecar residue, crash-interrupted state, ordinary path or file replacement detected by
ordered identity checks, cooperating concurrent tool processes, competing first-store creators,
and post-publication cleanup and durability uncertainty. They do **not** make any sequence atomic
against a privileged, non-cooperating process that can substitute a trusted path component
during an open system call; that is outside the supported model and is not claimed.

**Decision state machine.** Each `approve` / `reject` / `hold` runs three ordered, separately
committed steps:

1. commit the **pending** decision row transactionally;
2. append the JSONL **audit** event;
3. commit the separate **activation** row — only after step 2 returned confirmed success.

If any `COMMIT` raises, the tool closes the connection, **reopens the database** and looks
for the exact row by its unique id: present with the expected canonical hash means it
committed; absent means it did not (a clean retry is safe); unreadable fails closed. The
outcome is never inferred from the exception, which proves only that the client did not hear
the answer. There is no external activation acknowledgement file — the SQLite transaction and
the reopen check are the commit authority.

If the audit append fails, the decision stays **pending**: it is not activated, not deleted
and not rewritten, the ledger is not truncated, replaced or repaired, and the tool reports
`decision_authority = pending` with `approval_blocked` and `do_not_retry`.

#### Exclusive build claim — the terminal approval-consumption fact

Resolving authority and then closing the connection left a time-of-check/time-of-use window: a
concurrent reviewer could commit an activated hold, an activated rejection, or a pending
hold/rejection **after** the build read its authority but **before** it reserved or published,
and the build would proceed on a stale approval snapshot.

Checking harder cannot close that window; the check and the irreversible effect must share one
atomic boundary. So `build-package` now runs in two phases.

**Phase 1 — non-mutating preflight.** Validate arguments and safe paths; read and validate the
source record; confirm the output basename is absent (strict no-clobber); open and validate the
store; read the current authority, any existing claim and any reservation; construct the entire
package in memory; validate it against the contract **and**, when `jsonschema` is installed, the
real JSON Schema; compute the canonical payload hash. **No temporary file, no reservation, no
claim and no ledger event is created in phase 1.**

**Phase 2 — one `BEGIN IMMEDIATE` transaction.** Validate the complete canonical store;
re-resolve the newest decision state; reject any newer pending decision; require the newest
activated decision to be the exact intended unexpired approval (approval id, decision id,
decision sequence, canonical decision hash, source id and fingerprint all matching); confirm no
claim exists for the approval; confirm the operation id is unused; insert the exclusive
`build_claim`; commit.

Decision writers and build claims serialise on this same boundary, so a concurrent reviewer
either loses the write lock (and the build's re-resolve observes its decision) or wins it (and
the build's re-resolve observes it). There is no interleaving in which a stale approval
authorises publication.

`build_claim` binds: claim id, monotonic claim sequence, decision sequence, decision id,
canonical decision hash, approval id, source record id, source fingerprint, operation id,
package payload hash, intended package basename, claimed timestamp, schema version and its own
canonical record hash. SQLite — not Python — enforces the invariants: `UNIQUE` on `approval_id`,
`claim_id`, `decision_id` and `operation_id` makes a second claim, a duplicate claim, a duplicate
decision and a reused operation id impossible; foreign keys onto `decision(decision_id)` and
`decision(approval_id)` make a claim on a non-existent decision impossible, and — because
`decision` already enforces that only an approval carries an `approval_id` — a claim on a
**non-approved** decision impossible too; and a `BEFORE INSERT` trigger requires the claim's
decision id, approval id, sequence, canonical hash, source id and fingerprint to describe one
single **activated** approved decision.

**Winner ordering.** If a reviewer decision commits first, the build observes it and refuses with
no claim, no temporary, no reservation, no package and no build audit event. If the claim commits
first, that one exact attempt is authorised and the approval is consumed; a later reviewer
decision governs subsequent work only and never retroactively releases or cancels the committed
claim. When two builds race, exactly one claim commits and the loser is blocked before creating
anything.

**Claim commit uncertainty.** If the claim's `COMMIT` raises, the connection is closed, the
database is **reopened**, and the exact claim is looked up by claim id and canonical claim hash:
present with matching bindings means committed (the attempt continues); absent means nothing was
claimed, the approval is untouched and a later **explicit** retry is allowed (exit 11); anything
indeterminate — unreadable store, hash mismatch or binding mismatch — fails closed with
`do_not_retry`. Lock contention makes exactly one bounded attempt; there is no retry loop.

**Post-claim publication, and post-claim failure.** Only after the claim is confirmed committed
does the build create its temporary, write/flush/fsync it, create and durably persist the
reservation, publish atomically with the existing no-replace mechanism, clean only its own
temporary, and append the JSONL build audit event.

**Every failure after the claim commits leaves the approval permanently consumed** — temporary
creation, write, flush or fsync; reservation create, write, flush, fsync, parent-directory
durability or path safety; publication; temporary cleanup; and every ledger open/write/flush/
fsync, visible-but-unconfirmed or torn-append failure. A second build with that approval is
blocked at any fresh output path. Nothing is ever deleted or altered: not the claim, a
reservation, a published package, a competing package, a historical package, a torn audit ledger
or an unrelated temporary. No directory is listed, globbed or swept; only exact-path checks are
used.

The filesystem reservation remains a crash and publication backstop. It is **no longer** the
build-authorisation point — the committed SQLite claim is.

#### Timestamps must be timezone-aware

One central parser validates every authority and audit timestamp. A value such as
`2026-07-28T00:00:00` parses through `datetime.fromisoformat` but has **no UTC offset**, so
comparing it to an aware "now" raises an uncontrolled `TypeError`. A valid timestamp must
therefore be a supported ISO-8601 string, parse successfully, **and** return a non-null
`utcoffset()`. Naive values are refused with a sanitised classifier and the offending value is
never printed.

This covers `recorded_at`, `approved_at`, `expires_at`, `activated_at`, `claimed_at` and every
timestamp-bearing JSONL audit event. Ordering is checked too: an expiry may not precede its
approval, and an activation may not precede the decision it activates. Malformed historical data
is never normalised into acceptance.

**Authoritative ordering.** The newest **activated** decision for the source record wins.
Approve, reject and hold share one monotonic sequence. A newer committed-but-unactivated
decision **blocks** the build (`decision_pending_or_uncertain`) rather than falling back to
an older activated approval — the pending decision may have been an attempted hold or
rejection, and treating "we could not confirm the reviewer's latest instruction" as "use the
previous approval" is the unsafe direction.

| Store state | Build outcome |
| --- | --- |
| Newest activated decision is `approved` | Proceeds to the normal fingerprint, expiry and reservation gates |
| Newest activated decision is `rejected` or `hold` | Refused (`decision_not_approved`) |
| Any newer pending decision | Refused (`decision_pending_or_uncertain`) |
| Malformed, inaccessible or incompatible store | Refused with sanitised integrity evidence |
| No store, or no activated decision | Fresh reviewer decision required |

`build-package` never creates the store: a missing store means no transactional approval
authority exists.

**Deliberate compatibility decision — a legacy JSONL-only approval is not authority.** A
well-formed `approved` line written by any earlier version of this tool grants nothing,
because no activation row exists for it. **After merge, a fresh reviewer decision is
required before the v2 package is built.** This is intentional and is not a migration gap:
the whole point is that a readable line can no longer authorise a package.

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

Where a competing reservation **definitely** occupies the slot (the exclusive create lost the
race), the approval is reported as `approval_consumed` with `reservation = consumed` (exit 7)
and **no retry guidance at all** — the competitor terminally consumed the approval and its
reservation is left byte-for-byte untouched. `not_created`, the only retryable reservation
outcome, is claimed solely when a non-following existence re-check (`os.path.lexists` on the
exact slot path) positively proves that no object is present. If existence cannot be
determined, the approval is treated as consumed/uncertain. No directory is listed, globbed or
swept at any point.

#### Ledger integrity and exact audit schemas are fail-closed

The ledger is read as an intact sequence of newline-terminated JSON objects **and every
record must match an exact supported audit schema**. Validating only "is it a JSON object"
left arbitrary dictionaries trusted, so a malformed decision or publication dictionary
reached downstream timestamp parsing and field lookups and produced uncontrolled exceptions
or partially trusted state.

Supported shapes (every one is a shape this tool has actually written):

| Event | Fields |
| --- | --- |
| `decision` | exactly the 10 decision fields; permitted decision values; reviewer/source-id/fingerprint formats; integer row hint in range; parseable timestamps; an approval requires a well-formed approval id plus parseable `approved_at` and `expires_at`; a rejection or hold requires all three approval fields to be null |
| `build` | the 8 core publication fields, plus `reservation_file_name` from Amendment 4 onwards |
| `build_cleanup_incomplete` | the same, plus `cleanup_incomplete = true` and `stale_temp_basename` |

Unknown event types, missing fields, extra undeclared fields, wrong types, malformed
timestamps, malformed ids and invalid hashes all produce
`status = ledger_integrity_uncertain` (exit 8) with `approval_blocked`, `do_not_retry` and
`controlled_recovery_required` — never a traceback. So do a torn (partially appended) final
record, a non-object record, and read/decode failures.

In that state the tool does **not** discard the malformed record, repair, truncate, rewrite
or replace the ledger, delete any reservation, touch any published package, mutate the
decision store, or continue to package construction. Only a fixed shape classifier is
reported (`ledger_integrity`); the offending record is never printed, and no ledger content,
member value, credential or absolute path is ever printed. Reconcile the ledger under review,
then start a fresh reviewer decision.

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
| 7 | `approval_consumed` / `rebuild_requires_fresh_approval` | no | The approval is terminally consumed (including by a competing reservation), or `--rebuild` (retired) was passed. Nothing was created or touched. |
| 8 | `ledger_integrity_uncertain` | no | The audit ledger is not an intact append-only record, or a record fails its exact audit schema. Nothing was created, read further or repaired. |
| 9 | `decision_audit_incomplete` / `decision_not_activated` / `decision_not_recorded` / `decision_store_integrity_uncertain` / `claim_timestamp_order_invalid` | no | Decision authority cannot be trusted: a decision is pending and non-authoritative, a commit outcome was unresolved, the store is not intact, or a claim instant precedes the authority it binds. `decision_not_recorded` is the one retryable member of this row. |
| 10 | `decision_store_missing` / `no_activated_decision` / `decision_not_approved` / `decision_pending_or_uncertain` / `decision_superseded_before_claim` | no | No activated decision authorises a build, or a reviewer decision won the race to the claim. A fresh reviewer decision is required. |
| 11 | `build_claim_not_recorded` | no | The exclusive build claim was **not** committed, so the approval was **not** consumed. Nothing was created. A later **explicit** retry is allowed. |

Exits 9, 10 and 11 create and touch nothing: no build claim, no temporary file, no reservation,
no output file, no ledger event and no decision-store mutation. Exit 11 and exit 9's
`decision_not_recorded` are the only build/decision outcomes that stay retryable; every other
nonzero outcome after a committed claim sets `approval_blocked` and `do_not_retry`.

Two post-claim statuses report a consumed approval with nothing published:
`reservation_failed_after_claim` and `post_claim_publication_failed` (both exit 6, each naming
the exact `failure_stage`).

Exit 9 and exit 10 both create and touch nothing: no reservation slot, no temporary file, no
output file, no ledger event and no decision-store mutation. Exit 9 with
`decision_not_recorded` is the one decision outcome that stays retryable — nothing committed,
so `approval_blocked` and `do_not_retry` are both false. Every other exit-9 status sets
`approval_blocked`, `do_not_retry` and `controlled_recovery_required`.

**Two exit-9 store states can leave a NEW file behind, and say so** — they are the only ones
that report `decision_store_modified: true`:

- `decision_store_integrity = store_publication_uncertain`: a first-use store was published but
  its durability could not be proven. The store exists and is complete; nothing is rolled back
  or deleted. Verify the state directory, then start a fresh reviewer decision.
- `decision_store_integrity = store_temp_cleanup_incomplete`: the operation-owned creation
  temporary could not be removed, so the store is reachable under a second name. The reported
  `decision_store_temp_basename` names exactly one file (a basename, never a path). Delete that
  one file by hand; until you do, the store is refused with `store_multiple_links` rather than
  being used under two names.

For `store_sidecar_present` or `store_journal_mode_unsupported`, do **not** delete, rename,
checkpoint or roll back the journal, write-ahead log or shared-memory file. Establish why they
are there — usually a crashed process or a concurrent writer — resolve it deliberately, and then
start a fresh reviewer decision.

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
