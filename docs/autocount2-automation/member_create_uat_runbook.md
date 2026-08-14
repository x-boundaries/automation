# Single-Member Creation UAT Runbook

Status: UAT scaffolding, not production automation. This runbook covers one bounded,
manually triggered, single-member creation UAT: proving that exactly one real
form-derived member can be created in AutoCount and read back safely. It is not the
permanent production member-intake workflow (see
[member intake automation blueprint](member_intake_automation_blueprint.md),
"Future production member-intake workflow boundary").

Everything ships inactive by default. Nothing here contacts the live AutoCount
environment before the separately approved step-5 no-write preflight, which runs on
the AutoCount VM; that preflight may authenticate and read only, and it does not
authorise `SaveMember`. No AutoCount write occurs before the separately approved
step-7 write, and no AutoCount write is performed by development, tests, or CI.

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
  **audit record only** and never grants authority. The decision store's own operational
  authority is its single append-only **admission row** — a readable canonical file is not
  enough. There is also one durable publication reservation per build (the write-ahead
  single-use marker described in step 5). All of it is local private state and is never
  committed.
- The **approval-ledger directory is the required pre-existing state parent**. This tooling
  admits it and never creates it, so the reviewer's chosen state home is the only place a
  decision store can appear.
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

**Separate current-turn owner approval required (host-sync gate).** The command below runs
on the physical host `DESKTOP-Q43QKQF`, contacts the remote, and fast-forwards (mutates)
that host's checkout. It is a change to an external machine, not a read-only check. Before
running it, obtain an explicit current-turn owner approval that names the physical host
(`DESKTOP-Q43QKQF`) and the pull/sync operation on it. This approval is distinct and is
**not** implied by any other gate:

- the PR review and merge decision (step 2) does **not** cover this host sync;
- the VM deployment stage (step 4) does **not** cover this host sync;
- the no-write dry-run preflight (step 5) does **not** cover this host sync;
- the separate current-turn write approval (step 7) does **not** cover this host sync.

This host-sync approval does not authorise deployment, package execution, preflight or a
member write. A prior-turn approval is not reusable. Without the named current-turn
approval, stop before contacting `DESKTOP-Q43QKQF` and do not run
`git pull --ff-only origin main`.

**`PHYSICAL HOST — DESKTOP-Q43QKQF`** After the PR is reviewed and merged, and only after
the host-sync approval above, the physical host fast-forwards to the reviewed, merged
`main`. It is never used for implementation or manual edits.

```bash
git pull --ff-only origin main
```

### 4. Deploy the inactive UAT components

**Separate current-turn owner approval required (deployment gate).** The instructions
below change an external machine: they place reviewed files on the AutoCount VM
`DESKTOP-4I042L6` and prepare a directory that the VM then owns. Before any of them,
obtain an explicit current-turn owner approval that names the AutoCount VM
(`DESKTOP-4I042L6`) and binds this exact deployment operation:

- copying or replacing `scripts/ac2_member_create_uat_runner.ps1` on that VM;
- copying or replacing `scripts/member_create_uat_runner_lib.ps1` on that VM;
- copying or replacing `config/member_create_uat_business_confirmation.json` on that VM;
- creating or preparing the VM-owned state directory `C:\XB\create_uat\state`.

This approval is distinct and is **not** implied by any other gate:

- the PR review and merge decision (step 2) does **not** authorise this deployment;
- the physical-host sync approval (step 3) does **not** authorise this deployment;
- the no-write preflight approval (step 5) does **not** authorise this deployment;
- the separate current-turn write approval (step 7) does **not** authorise this deployment.

A prior-turn approval is not reusable. This deployment approval authorises no runner
execution, no AutoCount environment configuration and no AutoCount contact; running the
runner, configuring the connection environment and reaching AutoCount are gated separately
in step 5 and step 7. Without the named current-turn deployment approval, stop before
copying or replacing files or creating or preparing state on the VM.

Copy the reviewed `scripts/ac2_member_create_uat_runner.ps1`,
`scripts/member_create_uat_runner_lib.ps1`, and
`config/member_create_uat_business_confirmation.json` to the AutoCount VM working
area. Create the VM-owned state directory once (an operator prerequisite; the runner
never creates it):

**`AUTOCOUNT VM — DESKTOP-4I042L6`**

```powershell
New-Item -ItemType Directory -Path "C:\XB\create_uat\state" -Force
```

### 5. No-write preflight (dry-run)

**Separate current-turn owner approval required (preflight gate).** The whole of this step is
gated. It reads the selected private form response and its decision row, mutates the local
reviewer-decision store and the approval ledger, builds an immutable package, configures the
AutoCount connection in the process environment, moves that package onto the AutoCount VM
`DESKTOP-4I042L6`, and then authenticates to AutoCount and reads live data. Laptop locality does
not waive the approval for the private-data work. Before any of it, obtain an explicit
current-turn owner approval that names the AutoCount VM (`DESKTOP-4I042L6`) and binds:

- the bounded access to the selected private form response and its decision row for this one
  package, identified in the approval itself by its non-PII `source_record_id`, which a row
  number alone does not supply; the private field values are never written into this runbook;
- the local reviewer-decision store and approval-ledger operations and the immutable package
  build they produce;
- the AutoCount process-environment configuration, by variable name only:
  `AC2_PROBE_SERVER_NAME`, `AC2_PROBE_DATABASE_NAME`, `AC2_PROBE_USER_ID`, and the password
  environment variable named by `-PasswordEnvVar`;
- the intended AutoCount target (the server and database / account book), named in the approval
  itself and never written into this runbook as a connection value or secret;
- the bounded transfer of the approved package to that VM, copying or replacing the fixed VM
  working copy `C:\XB\create_uat\member_create_uat_package.json` that the runner always reads;
  this replacement authority covers that one VM working copy only, never the laptop-side
  package build, which stays strictly no-clobber;
- the no-write dry-run / preflight operation.

This approval is distinct and is **not** implied by any other gate:

- the PR review and merge decision (step 2) does **not** authorise this preflight;
- the physical-host sync approval (step 3) does **not** authorise this preflight;
- the VM deployment approval (step 4) does **not** authorise this preflight;
- the separate current-turn write approval (step 7) does **not** authorise this preflight.

A prior-turn approval is not reusable. The dry-run may authenticate, check the duplicate and
construct the member in memory, but it does **not** authorise or call `SaveMember`; that write
remains gated by step 7. Without the named current-turn preflight approval, stop before reading
the private form response or decision row, before building the package, before setting the
AutoCount environment, and before transferring the package to the VM or contacting AutoCount.

**`LAPTOP DEVELOPMENT MACHINE`** Only after the preflight approval above, build the approved
package on the laptop, using the decision-review output that shows the chosen row as
`READY_FOR_CREATE_REVIEW`:

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

**`AUTOCOUNT VM — DESKTOP-4I042L6`** Every remaining preflight operation runs on the
AutoCount VM, under the same preflight approval, in the VM process that runs the runner.

Set the AutoCount connection through the process environment only (never in files, never in
this runbook): `AC2_PROBE_SERVER_NAME`, `AC2_PROBE_DATABASE_NAME`, `AC2_PROBE_USER_ID`, and
the password environment variable named by `-PasswordEnvVar`. `ac2_member_create_uat_runner.ps1`
defaults `ServerName`, `DatabaseName` and `UserId` from these variables in its own VM process,
so values set on the laptop configure nothing.

Then copy the approved package to the VM and run the runner in dry-run mode (the default;
no write switches). Dry-run authenticates, checks the duplicate, constructs the new member,
assigns only the whitelisted fields, and stops without SaveMember. The build authority
below governs which package may be transferred at all.

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

**Schema (`member_create_uat_decisions/v2`).** Three append-only history tables, one singleton
admission table, plus schema metadata: `store_admission` (the store's own operational authority —
see below), `decision` (every approve, reject and hold attempt, with one shared monotonic
`sequence`), `decision_activation` (the decisions that became authoritative) and `build_claim`
(the single exclusive authorisation to build one package — see below). `UPDATE` and `DELETE`
are rejected on all four by database triggers, so even a direct `sqlite3` session cannot
rewrite or erase history or admission. Durability settings: `journal_mode=DELETE`,
`synchronous=FULL`, `foreign_keys=ON`, explicit `BEGIN IMMEDIATE` transactions and a bounded
busy timeout.

### Store admission: the store's own operational authority (Amendment 9)

Through Amendment 8, a decision store was operational because it *existed and looked canonical*.
That is unsound in exactly the same shape as the JSONL defect it replaced. First-use creation
publishes the completed store and only then proves its durability; if the process dies, the
durability step fails, or the final acknowledgement is never heard, a **complete, perfectly
readable, correctly versioned, singly linked, sidecar-free canonical store** is left on disk. The
next process could not tell it apart from a fully proven one, so publication uncertainty was not
sticky: it lasted only as long as the process that discovered it.

> **A readable canonical file is never authority.** The store is operational only while it holds
> the exact canonical **admission row** in `store_admission`, bound to the file now at that path.
> Absence of that row is the durable blocking state, and it survives restart, reboot and any
> number of later invocations.

`store_admission` is append-only and mechanically **singleton**: `singleton INTEGER PRIMARY KEY
CHECK (singleton = 1)`, so the primary key refuses a second row and the `CHECK` refuses any other
key. `BEFORE UPDATE` and `BEFORE DELETE` triggers abort. There is no mutable "admitted" flag. The
row binds:

| Field | Meaning |
| --- | --- |
| `admission_id` | `adm_` + 32 hex; unique |
| `operation_id` | `sop_` + 32 hex; the creation or reconciliation operation, unique |
| `admission_mode` | closed enum: `created` or `reconciled` |
| `admitted_at` | aware ISO-8601 instant (a naive value is an invalid admission) |
| `schema_version` | must equal `member_create_uat_decisions/v2` |
| `durability` | closed enum naming the primitive **actually confirmed** before admission |
| `volume_identity` | normalised `dev:<hex>` device/volume id of the published file |
| `file_identity` | normalised `ino:<hex>` inode / file index of the published file |
| `record_hash` | canonical SHA-256 over **every** field above |

The identity fields are **mismatch detectors, not cryptographic proof of provenance**. They
detect that the file now at the path is not the file admission was written against — the ordinary
replacement case in the supported threat model. A current mismatch fails closed.

Admission is written **last**, in its own `BEGIN IMMEDIATE` transaction under `synchronous=FULL`,
after publication and after the platform's durability primitive is confirmed. A raised `COMMIT` is
resolved the same way every other commit in this tool is resolved — by closing, reopening through
pure triage, and looking for the exact row and hash — never by inferring from the exception, and
never with an automatic retry.

**Two explicit validation modes, and no circular trust.** These are separate functions, not a
flag a caller could forget:

| Mode | Used by | Admission cardinality required |
| --- | --- | --- |
| Structural zero-admission (**internal only**) | a newly created operation-owned temporary; a just-published store before its first admission; controlled reconciliation; admission-COMMIT recovery | exactly **zero** (recovery alone may see zero *or* one) |
| Operational one-admission | reviewer decisions, authority reads, build preflight, build claim, decision-sequence reads, and all three of decision / activation / claim COMMIT recovery | exactly **one**, with valid mode, aware instant, exact version, allowed durability primitive, correct canonical hash and a matching current identity binding |

Both modes run the complete global validator first. Ordinary code can never reach the
zero-admission mode. Nothing about file readability, a valid SQLite header, the canonical schema,
the schema version, one hard link, absent sidecars, empty history or a successful publication is
sufficient operational authority — **only the exact admission row is**.

Two new sanitised classifiers report the two failures: `store_not_admitted` (canonical but no
admission row at all) and `store_admission_invalid` (an admission row that is not the exact
canonical fact, including an identity-binding mismatch). A third, `store_admission_uncertain`,
reports an admission commit that could not be resolved.

Alongside them, three fixed **final-path** classifiers say what happened to the store path itself —
`published_not_admitted`, `published_and_admitted` and `competitor_published_untouched` — because
"this operation left a non-operational store", "this operation left an operational one it could not
re-verify" and "a competitor's store is intact" require different operator responses. They are
tabulated under *Build outcomes and exit codes* below.

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
row, key `schema_version`, value `member_create_uat_decisions/v2`, no other key); the admission
row's shape and canonical hash; every `decision` row in sequence order; every activation row in
activation-sequence order; every claim row in claim-sequence order; then the cross-table orphan
and binding checks. Only SQLite's own `sqlite_sequence` and the implicit `sqlite_autoindex_*`
indexes are tolerated.

A store written by the earlier draft of this same `v2` version — carrying the identical
`schema_version` value but no `store_admission` table — is refused with `missing_object` and is
**never augmented**. Adding the admission table to a database this tool did not create is exactly
the migration this contract forbids.

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

### The state parent is admitted before anything is created (Amendment 9)

Through Amendment 8, creation called `mkdir(parents=True, exist_ok=True)` and asked whether the
parent was a plain directory *afterwards*. That ordering cannot be made safe: recursive creation
materialises a whole chain of directories, and a **pre-existing redirected component** — a symlink
on POSIX, a junction or any other reparse point on Windows — is followed by every subsequent open,
so the store could be created somewhere other than the state home the reviewer's ledger
designates.

> **The approval-ledger directory IS the required pre-existing state parent. This tooling never
> creates it.** If it is missing, the command returns `store_parent_missing` and produces zero new
> directories, zero files, zero temporaries, zero SQLite connections, zero audit appends and zero
> reviewer-decision state.

Before any directory creation, file creation or SQLite connection, every component from the
platform's traversal anchor down to the state parent is classified **in order** and
**non-following**. Refused: `.` and `..`, symlinks, junctions, any other reparse point,
non-directories, unsupported device or volume transitions, and **any classification error** — an
`lstat` failure is fail-closed, never "probably fine". The final parent's identity is captured and
re-checked at four points: before the temporary is created, after it is created, before
publication and after publication.

Platform support is a narrow, closed boundary:

| Platform | Supported | Refused fail-closed | Mechanism |
| --- | --- | --- | --- |
| Windows | fixed local **NTFS** drive-letter volume (`GetDriveTypeW == DRIVE_FIXED`, `GetVolumeInformationW` name `NTFS`) | UNC paths, mapped drives, remote/removable/CD-ROM/RAM/unknown drive classes, non-NTFS volumes, any reparse component, volume-query failures | pathname-based ordered classification; identity from the volume serial and file index |
| POSIX | Linux local **persistent** filesystems on one device from `/` (`ext2/3/4`, `xfs`, `btrfs`, `zfs`, `f2fs`, `jfs`, `reiserfs`, `bcachefs`, `ubifs`, `overlay`) | every other or **unprovable** filesystem, including the memory-backed `tmpfs` and `ramfs` (whose contents do not survive a restart, so `fsync` there cannot support the restart-safe single-use boundary), `nfs`, `cifs`/`smb*`, `9p`, `ceph`, `glusterfs`, `lustre`, FUSE remotes and WebDAV; any device transition | descriptor-relative walking with `O_DIRECTORY` plus `O_NOFOLLOW` and `dir_fd`, and exclusive create, `link`, `unlink` and the parent `fsync` through the verified descriptor |

The filesystem type is proven from `/proc/self/mountinfo` by longest-mount-point match. An
unrecognised type, or one that cannot be determined at all, is **unsupported** — the allowlist is
deliberate, because an unprovable filesystem cannot support the durability and identity claims the
admission fact records.

**Documented residual race boundaries.** These are stated, not closed:

- Python's `sqlite3` accepts a **pathname**, not a directory descriptor. Opening the temporary and
  the published store is therefore pathname-based even on POSIX, so a classify-to-open race
  remains between the identity checks and the SQLite open. No claim of descriptor-relative SQLite
  is made anywhere in the code or in this document.
- Windows operations are entirely pathname-based. **No Windows guarantee here is equivalent to
  POSIX `dir_fd` or directory-fsync semantics.**
- A privileged or otherwise non-cooperating process able to substitute a path component *during*
  an open system call is **out of the supported threat model**. What is in scope, and detected, is
  ordinary replacement or redirection by a cooperating or careless process.
- `/proc/self/fd` paths, `openat2`, a custom SQLite VFS and undocumented native APIs are all
  deliberately not used.

### Creation is allowed only at a positively absent path

The complete canonical store is built in an operation-owned temporary inside the **admitted**
parent, validated in full in zero-admission structural mode, closed, proven to have no sidecar and
exactly one link, flushed durably, and only then published. Exclusively creating the final path and
*then* running DDL on it would leave a window in which a concurrent process opens a zero-byte file
and correctly concludes it is not a canonical store; publishing an already-complete store removes
that window, so the final path only ever appears fully formed. Publication is platform-specific:

| Platform | Primitive | Durability reported | Temporary |
| --- | --- | --- | --- |
| Windows | `MoveFileExW` **without** `MOVEFILE_REPLACE_EXISTING`, with `MOVEFILE_WRITE_THROUGH` | `windows_move_write_through` | none survives a move, so no second name is ever created |
| POSIX | no-replace `os.link` with every SQLite connection closed, parent-directory fsync, unlink of the operation-owned temporary, parent-directory fsync again — all descriptor-relative | `posix_link_and_directory_fsync` | `unlinked`; a failed unlink is reported, never suppressed |

The published store must be a plain regular file with **exactly one link** and the identity this
operation created. It is then re-validated in zero-admission structural mode, and only then is the
**admission row** inserted and proven. Creation reports success only after that proof.

If a competitor wins the race, their store is left byte-for-byte untouched and only this
operation's own temporary is removed. If schema setup fails, the final path is never created and
the temporary is deliberately left in place as evidence. A publication whose durability cannot
be proven, a temporary that cannot be removed, or an admission commit that cannot be resolved is
reported as a controlled-recovery state — never silently treated as an authorised store. Only a
durability primitive actually confirmed on the running platform is ever named; Windows offers no
directory-handle fsync, so none is claimed there.

> **Anything that fails after the final path becomes visible but before admission completes leaves
> the store non-operational across process restart.** No later process may treat it as ordinary
> merely because it is readable and canonical. Recovery is the controlled reconciliation command
> below, under explicit owner authority.

### Truthful lost-race cleanup (Amendment 9)

Amendment 8's cleanup helper had a quiet mode (`required=False`) used on the lost-race path, which
**swallowed a real unlink failure** — so a surviving temporary was invisible to the operator at
exactly the moment a competitor had become the authority. That mode is gone. There is one helper
and every outcome is explicit.

Immediately before unlinking, the exact pathname is re-classified and required to still be the
regular file this operation exclusively created, compared by the identity captured at creation. A
replacement object is **never** unlinked. Nothing is ever listed, globbed or swept, and no other
pathname is touched.

**Separate current-turn destructive-cleanup approval required (destructive-cleanup gate).** Before
the deletion or removal below, obtain an explicit current-turn owner approval that names the exact
target basename or path and the exact delete or remove operation. A step-5 preflight approval, a
build or reviewer decision, a step-7 write approval, repository review or merge, and any prior-turn
approval are none of them reusable for it.

| Outcome | Reported as | Operator action |
| --- | --- | --- |
| Our temporary removed, or already absent | `store_not_absent` (lost race) / normal success (publication) | none |
| Our temporary could not be removed after a **lost race** | `store_temp_cleanup_incomplete` with `decision_store_final_path_state = competitor_published_untouched` and `decision_store_modified: false` | delete exactly the one named `.mcuat_decisions_*` basename |
| Our temporary could not be removed after **our own** publication | `store_temp_cleanup_incomplete` with `decision_store_final_path_state = published_not_admitted` | delete exactly that one file; until then the store is refused with `store_multiple_links` |
| The temporary pathname now holds a different object | `store_temp_identity_changed` | review that one named basename by hand; it is never removed automatically |

A losing creator's stale temporary is **operator hygiene evidence, not a global authority block**:
the competing store keeps its own admission and its own single name. A self-published,
multiply-linked store *is* globally blocked, by the existing one-link invariant.

### Controlled reconciliation (`reconcile-store-admission`)

A store that was published but never admitted is permanently blocked by design. The only way out
is one explicit, separately named command. It is never invoked automatically and is not reachable
from any ordinary command.

```bash
python scripts/member_create_uat_approval.py reconcile-store-admission --ledger <ledger-path> --confirm-controlled-reconciliation
```

> **Real use against a real store requires explicit current-turn owner authority naming that exact
> store.** The confirmation switch is a deliberate second action, not a convenience default;
> without it the command refuses and changes nothing. The exact store path is **derived** from the
> ledger path, so the command cannot be pointed at an arbitrary database.

It **mutates admission state only**. It never repairs, migrates, checkpoints, truncates, rewrites,
renames or replaces the database image, and it never touches a sidecar-bearing store. Every
precondition is proven before anything is written:

- [ ] the state parent passes trusted-parent admission
- [ ] pure pre-open triage passes (header, one link, no sidecar, stable identity)
- [ ] the platform and filesystem are supported
- [ ] the schema is exactly the canonical revised `v2`
- [ ] admission cardinality is exactly **zero**
- [ ] `decision`, `decision_activation` and `build_claim` are **all empty**
- [ ] complete global validation passes
- [ ] the file identity is unchanged across the whole precondition phase

Any history at all returns `store_reconciliation_history_present` and changes nothing — admitting a
store that already carries history would retroactively bless authority nobody proved. Then
durability is **re-established before** admission:

| Platform | Re-established | Recorded primitive |
| --- | --- | --- |
| POSIX | `fsync` of the final database file **and** of the verified parent directory descriptor | `posix_file_and_directory_fsync` |
| Windows | flush of the final database file only, on a proven fixed local NTFS volume | `windows_file_flush_no_directory_fsync` — named for what it is; **no directory-fsync equivalent is claimed** |

The admission COMMIT is resolved by the same exact-row lookup. If it stays unresolved, ordinary
operations remain blocked and the command reports `store_admission_uncertain`. Reconciliation grants
no reviewer authority: a fresh reviewer decision is still required afterwards.

**Concurrency consequence you should expect.** Because a sidecar is refused unconditionally, a
second tool process that meets a peer *mid-transaction* now fails **closed** with
`store_sidecar_present` instead of waiting on the busy timeout and then committing. Nothing is
written and nothing is changed, but the second command does not succeed. Run reviewer decisions
one at a time. Pure lock contention — a peer holding the write lock without having written a
page, so no journal exists — still reports the retryable `decision_not_recorded` /
`build_claim_not_recorded` outcome.

Three further transient refusals mean the same thing — "a peer is mid-operation, nothing was
changed, try again deliberately": `store_locked`; `store_unreadable`, which Windows can report
while a peer's no-replace move is in flight; and, on POSIX only, `store_multiple_links` during
first-use creation, because the hard-link route briefly gives the completed store two names
before the operation-owned temporary is unlinked. That window exists only while a store is being
created; afterwards the link count is one permanently, and a persistent `store_multiple_links`
means a real second name that an operator must remove.

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
| 9 | `decision_audit_incomplete` / `decision_not_activated` / `decision_not_recorded` / `decision_store_integrity_uncertain` / `claim_timestamp_order_invalid` | no | Decision authority cannot be trusted: a decision is pending and non-authoritative, a commit outcome was unresolved, the store is not intact **or not admitted**, the state parent is missing, untrusted or unsupported, or a claim instant precedes the authority it binds. `decision_not_recorded` is the one retryable member of this row. |
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

**Whether a NEW file exists is reported explicitly, not guessed from the reason.** Amendment 9
reports `decision_store_final_path_state` whenever a refusal has something to say about the final
store path, and derives `decision_store_modified` from it:

**Separate current-turn destructive-cleanup approval required (destructive-cleanup gate).** Before
the deletion or removal below, obtain an explicit current-turn owner approval that names the exact
target basename or path and the exact delete or remove operation. A step-5 preflight approval, a
build or reviewer decision, a step-7 write approval, repository review or merge, and any prior-turn
approval are none of them reusable for it.

| `decision_store_final_path_state` | `decision_store_modified` | Meaning and required action |
| --- | --- | --- |
| `published_not_admitted` | `true` | **This** operation published a store and then failed before its admission fact was proven. The store exists, is complete, and is **not operational**. Nothing was rolled back or deleted. Recovery is the controlled reconciliation command under owner authority, or removal of the non-operational store under review. |
| `published_and_admitted` | `true` | This operation published a store **and** proved its admission row committed, but could not complete its own final operational verification — in practice because a peer opened a write transaction the moment the admission appeared. **No reconciliation is needed:** the admission fact exists, so the next invocation simply finds an operational store. Re-run the reviewer decision. |
| `competitor_published_untouched` | `false` | A concurrent operation published the store first. Its store is byte-for-byte intact; only this operation's own temporary is at issue. |
| absent | `false` | The store was left exactly as it was found. |

Store states you may see at exit 9, and what to do:

**Separate current-turn destructive-cleanup approval required (destructive-cleanup gate).** Before
the deletion or removal below, obtain an explicit current-turn owner approval that names the exact
target basename or path and the exact delete or remove operation. A step-5 preflight approval, a
build or reviewer decision, a step-7 write approval, repository review or merge, and any prior-turn
approval are none of them reusable for it.

- `store_not_admitted`: the store is readable and canonical but carries **no admission fact**, so
  it has never been admitted to operational use. This is the expected, correct state after any
  interrupted first-use creation. Verify the state directory, then either remove the
  non-operational store under review and start a fresh reviewer decision, or run the controlled
  reconciliation command above **under explicit owner authority naming that exact store**.
- `store_admission_invalid`: an admission row exists but is not the exact canonical fact — most
  often because the file at that path is not the file admission was written against. Do not
  repair it. Establish what replaced the store, then reconcile under review.
- `store_admission_uncertain`: an admission commit could not be resolved, or reconciliation could
  not re-establish durability. The store remains blocked. Re-run the controlled reconciliation
  command under owner authority once the underlying cause is resolved.
- `store_parent_missing`: the reviewer's approval-state directory does not exist. **Nothing at all
  was created.** Create or restore that directory deliberately, then re-run.
- `store_parent_untrusted`: a component of the state path is a symlink, junction, other reparse
  point or not a directory, or could not be classified. Resolve the redirection deliberately;
  never point the state path through a link.
- `store_parent_unsupported`: the state path is on an unsupported volume or filesystem — a UNC
  path, mapped or remote drive, removable drive, non-NTFS Windows volume, a device transition, or
  a POSIX filesystem that is not a supported local one. Move the state directory to a fixed local
  volume.
- `store_parent_identity_changed`: the state directory was replaced mid-operation. Establish why
  before re-running.
- `store_publication_uncertain`: a first-use store was published but its durability could not be
  proven. It is also, necessarily, not admitted.
- `store_temp_cleanup_incomplete` / `store_temp_identity_changed`: see the cleanup table above.
  `decision_store_temp_basename` names exactly one file (a basename, never a path).
- `store_reconciliation_history_present`: reconciliation was attempted on a store that already
  holds reviewer-decision, activation or claim history. Nothing was changed, and nothing should
  be: that store's admission must not be manufactured after the fact.

For `store_sidecar_present` or `store_journal_mode_unsupported`, do **not** delete, rename,
checkpoint or roll back the journal, write-ahead log or shared-memory file. Establish why they
are there — usually a crashed process or a concurrent writer — resolve it deliberately, and then
start a fresh reviewer decision.

Exit 3 (`cleanup_incomplete`) reports `stale_temp_basename` (a PII-free `.mcuat_pkg_*.tmp`
name in the output directory) with `manual_cleanup_required`:

**Separate current-turn destructive-cleanup approval required (destructive-cleanup gate).** Before
the deletion or removal below, obtain an explicit current-turn owner approval that names the exact
target basename or path and the exact delete or remove operation. A step-5 preflight approval, a
build or reviewer decision, a step-7 write approval, repository review or merge, and any prior-turn
approval are none of them reusable for it.

- `publication = not_published`: nothing was published and nothing was reserved. Manually
  delete the named stray temporary file, then re-run the build.
- `publication = succeeded`: the final package WAS published and is recorded in the ledger
  as `build_cleanup_incomplete`. Do NOT rebuild this operation (the builder refuses it):
  manually delete the named stray temporary file, and if a new package is genuinely needed,
  start a fresh reviewer decision.

**Separate current-turn destructive-cleanup approval required (destructive-cleanup gate).** Before
the deletion or removal below, obtain an explicit current-turn owner approval that names the exact
target basename or path and the exact delete or remove operation. A step-5 preflight approval, a
build or reviewer decision, a step-7 write approval, repository review or merge, and any prior-turn
approval are none of them reusable for it.

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

**Separate current-turn owner approval required (result-mapping gate).** The mapping below runs
live operations on the operator PC n8n instance and writes to the intended Google Sheet. Before any
of it, obtain an explicit current-turn owner approval that names the intended result-mapping
workflow `n8n-workflows/member_create_uat_result_mapping.workflow.json`, the intended spreadsheet
and source tab, the intended Google credential by its non-secret operator-recognisable
credential name or identity, the exact non-secret `uat_create_operation_id` whose spreadsheet
row is to be updated, and the intended n8n instance or environment by its non-secret
operator-recognisable name. Those identities are named in that approval itself, and the
instance URL, connection details, credential values, OAuth tokens and API keys are never
written into this runbook. That approval binds:

- importing and using the local copy of that workflow on the operator PC n8n instance;
- binding the intended Google credential by name or identity only, never by secret value;
- copying the sanitised result file into the approved n8n file location `/home/node/.n8n-files/`;
- running that workflow manually, with the workflow left inactive;
- updating the one intended spreadsheet row selected by `uat_create_operation_id`.

This approval is distinct and is **not** implied by any other gate:

- the PR review and merge decision (step 2) does **not** authorise this result mapping;
- the physical-host sync approval (step 3) does **not** authorise this result mapping;
- the VM deployment approval (step 4) does **not** authorise this result mapping;
- the no-write preflight approval (step 5) does **not** authorise this result mapping;
- the separate current-turn write approval (step 7) does **not** authorise this result mapping.

A prior-turn approval is not reusable. Repository review or merge is not this approval, and this
approval authorises no AutoCount contact and no further member write. Without the named current-turn
result-mapping approval, stop before importing the workflow, before binding any credential, before
copying the result file into the n8n file location, before running the workflow and before updating
the spreadsheet row.

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

**Separate current-turn owner approval required (recovery gate).** This step is conditional and
applies only when the runner returned `WRITE_OUTCOME_UNCERTAIN`. When it applies, and before any
AutoCount contact, obtain an explicit current-turn owner approval that names the intended AutoCount
account book and environment. The server and database are named in that approval itself, and are
never written into this runbook. That approval binds the read-only member lookup and recovery
operation for that one uncertain write outcome, and nothing else.

This recovery is read-only. It grants no new `SaveMember` authority, authorises no create, update or
delete, and authorises no second write attempt. This approval is distinct and is **not** implied by
any other gate:

- the PR review and merge decision (step 2) does **not** authorise this recovery lookup;
- the physical-host sync approval (step 3) does **not** authorise this recovery lookup;
- the VM deployment approval (step 4) does **not** authorise this recovery lookup;
- the no-write preflight approval (step 5) does **not** authorise this recovery lookup;
- the separate current-turn write approval (step 7) does **not** authorise this recovery lookup;
- the step-9 result-mapping approval (step 9) does **not** authorise this recovery lookup.

A prior-turn approval is not reusable. Repository review or merge is not this approval. Without the
named current-turn recovery approval, stop before opening the account book and before searching for
the member.

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

After completion, leave the result-mapping workflow inactive and take no further
create action.

**Separate current-turn destructive-cleanup approval required (destructive-cleanup gate).** Before
the deletion or removal below, obtain an explicit current-turn owner approval that names the exact
target basename or path and the exact delete or remove operation. A step-5 preflight approval, a
build or reviewer decision, a step-7 write approval, repository review or merge, and any prior-turn
approval are none of them reusable for it.

Then remove any temporary copies of the package and result from shared locations.

The consumed marker and write-intent marker remain on the VM as durable
evidence and single-use guards; do not delete them. The laptop-side publication
reservations remain beside the approval ledger for the same reason; do not delete or
sweep them either.

## Safety boundary

- No AutoCount write occurs in development, tests, or CI. Enabling the ExpiryDate path
  (recording the business confirmations and flipping the capability flag) performs no
  live write; a real write still requires the explicit VM write step above and a
  separate current-turn owner approval naming the exact target and operation.
- The host sync on `DESKTOP-Q43QKQF` in step 3 and the `SaveMember` write in step 7
  each require their own prior current-turn owner approval. Neither implies the other,
  and a prior-turn approval is never reusable for either.
- Four baseline approval surfaces are always required: the step-3 host sync, the step-4
  VM deployment, the step-5 preflight surface (selected private form/decision-row access,
  the reviewer-decision and approval-ledger operation, the immutable package build, the
  AutoCount environment setup, the package transfer and the no-write AutoCount preflight),
  and the step-7 `SaveMember` write.
- Further conditional approval surfaces arise wherever the procedure reaches them: every
  operator-directed destructive cleanup or removal, each one scoped locally to its exact
  target and its exact delete or remove operation; the step-9 live n8n result mapping;
  and the step-10 conditional read-only AutoCount recovery lookup.
- This runbook states no fixed total number of approval surfaces. Each surface named
  above requires its own current-turn owner approval, none implies or covers another, and
  a prior-turn approval is never reusable for any of them.
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
