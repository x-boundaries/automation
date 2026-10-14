# EnergyGrid dual-stream v4 offline core: contract

Status: offline construction slice for #226 G3 (run `2026-10-13-xb-226-dualstream-offline-core-g3-001`),
corrected by the Web-directed v4 continuation (run `2026-10-14-xb-226-dualstream-offline-v4-web-directed-g3-001`)
against the accepted targeted G2 contract (#226:6158953231).
Scope: a deterministic local core with fake provider adapters only. This document does not grant
live Gmail, Drive, SMTP, n8n, portal or Shopify effects, a Desktop scheduled task, a project hook,
a production migration, or a G4 or UAT pass. Live delivery stays on HOLD until Web accepts the
separate G2 permission boundary.

## 1. Streams and identity

- Streams are `EB_BILL` and `TENANT_BILL`. Each has its own admissions and its own blocking, so a
  blocked stream never blocks the other.
- Source identity is `(stream, source_namespace, source_invoice_key)`. `source_invoice_key` is the
  NFC plus casefold key of the portal filename (`publication.filename_key`).
- Archive identity is date-only and frozen: `<folder>/YYYY-MM-DD.pdf`, where the folder is
  `EB Bill` or `Tenant Bill`. v4 never renames or rewrites an archive file.
- Archive identity is SHA-256 plus byte size. The same bytes are used for the on-disk archive, the
  simulated Drive copy and the simulated email attachment. The core hashes the bytes it sends and
  refuses any mismatch.

## 2. Presentation filename (`naming.py`)

`MMM YYYY - <source filename>`, with the prefix applied exactly once:

| Rule | Behaviour |
|---|---|
| Month | Uppercase English abbreviation `JAN`..`DEC` from the validated source invoice date |
| Year | Four digits |
| Source name | Kept verbatim after the prefix, extension included (`.pdf` or `.PDF`) |
| Already prefixed, same month and year (any letter case) | Accepted once; the prefix is normalised to uppercase (`oct 2026 - x.pdf` in October 2026 gives `OCT 2026 - x.pdf`) |
| A `MMM YYYY - ` token (any case, month or year) anywhere except as that one accepted leading prefix: another period, doubled, or embedded (`Invoice OCT 2026 - EB.pdf`, `x OCT 2026 - y.pdf`) | Ambiguous, `EG_V4_DISPLAY_PREFIX_CONFLICT` |
| The source name, or the remainder after the accepted prefix, starts with a month abbreviation, a full English month name or `SEPT`, then a non-letter and a four-digit year with any or no ASCII separator (`OCT 2026 invoice.pdf`, `OCTOBER 2026 - x.pdf`, `SEPT 2026 - x.pdf`, `OCT2026-invoice.pdf`, `OCT 2026 - OCT2026 x.pdf`) | Ambiguous, `EG_V4_DISPLAY_PREFIX_CONFLICT`. Never guessed. |
| Accepted controls | `XYZ 2026 - invoice.pdf`, `Invoice Oct.pdf`, `Octopus 2026.pdf`, `10-2026 invoice.pdf`, `oct 2026 - x.pdf` each get exactly one canonical prefix |
| Non-ASCII source name (only qualified ASCII names are supported) | `EG_V4_DISPLAY_SHAPE_UNSUPPORTED` |
| Leading whitespace, or any format (Cf) or control (Cc) character | `EG_V4_DISPLAY_NAME_INVALID` |
| Empty stem, non-PDF, control or path characters, reserved device name | `EG_V4_DISPLAY_NAME_INVALID` |
| Result longer than 260 characters | `EG_V4_DISPLAY_NAME_INVALID`, never truncated |
| Collision within one stream (NFC plus casefold) | `EG_V4_DISPLAY_NAME_COLLISION` |

Every `EG_V4_DISPLAY_*` fault of a stream's current latest candidate is a typed stream HOLD (section 5):
zero fetch, zero archive write, zero admission and zero Drive or Gmail effect for that candidate. The
display name is computed once and stored in the immutable admission row.

## 3. SQLite v4 (additive)

v4 is a migration of a complete v3 database (`migrate_state_database_v4`). It adds tables, indexes
and triggers and sets `user_version = 4`. It does not alter any v1-v3 object or row.

| Table | Role | Mutability |
|---|---|---|
| `energygrid_v4_invoice` | Admission: identity, date, archive path, display name, SHA, size, admission sequence | Immutable |
| `energygrid_v4_intent` | One `DRIVE_UPLOAD` and one `GMAIL_SEND` intent per admission | Immutable |
| `energygrid_v4_intent_status` | Current state and version (compare-and-set) | Mutable through guarded triggers |
| `energygrid_v4_transition` | Append-only state history | Immutable |
| `energygrid_v4_dispatch` | Dispatch seal, one per intent, written before any provider call | Immutable |
| `energygrid_v4_receipt` | Provider outcome records, one per distinct digest per intent | Immutable |
| `energygrid_v4_lease` | Single-session lease for a run | Mutable |
| `energygrid_v4_cursor` | Round-robin position (last stream served) | Mutable |

Guards the database enforces, so a direct SQL write cannot skip them:

- Historical v4 rows cannot be updated or deleted. A BEFORE INSERT guard also refuses any insert
  that would collide with an existing key, so `INSERT OR REPLACE` cannot overwrite a row.
- A status row can be inserted only as `PENDING` at version 1, once per intent. A status change must
  follow a legal edge and advance the version by exactly one.
- `DISPATCHED` requires a dispatch seal. `VERIFIED` requires a `VERIFIED` receipt whose SHA equals
  the intent's archive SHA.
- A dispatch can be sealed only from `PENDING`. A `GMAIL_SEND` seal requires its invoice's
  `DRIVE_UPLOAD` to be `VERIFIED`. A receipt requires a prior dispatch seal.

### Predecessor proof and cutover

- The fixture `tests/fixtures/predecessor_v3_schema.sql` is generated from the unmodified frozen-main
  v3 code (`2753923`) and pinned by SHA-256 over LF-normalised bytes. It holds schema and
  configuration rows only, with no invoice, Drive or delivery data.
- `predecessor_digest` covers every predecessor schema object and every row of every predecessor table. The only objects excluded are
  the exact (type, name) keys that v4 adds. An extra trigger on a v3 table changes the digest.
- Migration lock (F1). `migrate_state_database_v4(path, apply=True)` first takes the shipped single-run
  lock, `RunLock` on the resolved state directory (`path.resolve(strict=False).parent`). That is the same
  lock file every shipped v3 writer holds for its whole run: `cli.py` (`RunLock(config.state_path.parent)`
  around `StateV3Store` and the v3 migration apply) and `orchestration.run_command`
  (`core.lock_factory(config.state_path.parent)`, default `RunLock`), where `config.state_path` is already
  resolved. The lock is taken before SQLite is opened and held until the commit or rollback and the
  close; it is released on every exit. If it is held, the apply is refused with `EG_V4_RUN_LOCKED`
  (status `RUN_IN_PROGRESS`, exit 10): no backup, no schema change, `user_version` stays 3 and the
  predecessor digest is unchanged. A plan (`apply=False`) stays read-only and lock-free. The v1 store
  refuses any database newer than v1, so it cannot write a v3 or v4 database.
- Inside the lock the apply takes the SQLite write lock first. Inside that transaction it refuses only
  local integrity blockers, each on its own (plan and apply): `archive_state` PREPARED, CONFLICT or
  UNVERIFIED; `migration_state` UNCLASSIFIED, MOVE_PLANNED or HOLD; a stream with `admission='HOLD'`;
  a non-`DRIVE_STAGE` file operation that is PREPARED or HOLD (and an unclassified invoice). Provider
  evidence, including a historical v2 `DRIVE_STAGE` operation in any state, is not a blocker, because
  cutover classifies it. It accepts a COMPLETE_V3 or RESUMABLE_V3 database, writes an exclusive backup,
  applies the v4 objects, and commits only if the predecessor digest and the v4 inspection both still
  pass. Any failure rolls back.
- Trust assumption: only the enumerated shipped v3 entry points write v3 state, and each holds the
  single-run lock. A v3 handle opened without that lock before the migration (a non-cooperating stale
  handle) can still write after it; that case is OUT_OF_MODEL here and is not fixed (section 8, D2).
- v4 never re-admits v3 history by portal identity. The stream's CURRENT latest v3 invoice is
  classified from frozen v3 evidence. Older history is never classified, so it is never backfilled:

  | Frozen v3 evidence for the current latest | Result |
  |---|---|
  | No delivery row, no Drive operation, no DRIVE_STAGE operation, archive COMMITTED | ADOPTABLE: one v4 delivery, no fetch |
  | Delivery PENDING_SEND with no dispatch start | Not a dispatch HOLD. v3 accepts a delivery only after a DRIVE_VERIFIED receipt, so it is held by the Drive rule below, never adopted |
  | Delivery DELIVERED | DELIVERED: no action |
  | Delivery DELIVERY_OUTCOME_UNCERTAIN, REQUEST_REJECTED, or PENDING_SEND with a claimed dispatch | HOLD (`EG_V4_PREDECESSOR_DELIVERY_UNCERTAIN`, `_REJECTED`, `_DISPATCHED`) |
  | Drive operation not DRIVE_VERIFIED (partial or uncertain) | HOLD (`EG_V4_PREDECESSOR_DRIVE_PARTIAL`) |
  | Drive operation DRIVE_VERIFIED, delivery not sent | HOLD (`EG_V4_PREDECESSOR_DRIVE_VERIFIED_NOT_SENT`). Web decision needed. |
  | DRIVE_STAGE operation (any state), or drive state not NOT_STAGED, each on its own | HOLD (`EG_V4_PREDECESSOR_DRIVE_EVIDENCE_PRESENT`) |
  | Archive not COMMITTED | HOLD (`EG_V4_PREDECESSOR_ARCHIVE_NOT_COMMITTED`). v4 never fetches at cutover. |
  | Two latest invoices with the same day | HOLD (`EG_V4_PREDECESSOR_LATEST_AMBIGUOUS`, no invoice). Defence in depth: the unique v3 archive path index prevents it in a valid v3 database. A tie on an older day does not block the unique latest. |

- First adoption validates the existing archive bytes against the frozen row (SHA-256 and size). It
  never refetches, recopies or renames the archive. A failed check is a HOLD
  (`EG_V4_PREDECESSOR_ARCHIVE_UNREADABLE` or `EG_V4_PREDECESSOR_ARCHIVE_IDENTITY_MISMATCH`). Inside the
  fenced write the latest is classified again: a different or no-longer-ADOPTABLE latest is
  `EG_V4_PREDECESSOR_ADOPTION_STALE`, and a SHA-256 or size that differs from the frozen row is
  `EG_V4_PREDECESSOR_ARCHIVE_IDENTITY_MISMATCH`.
- Adoption-first lookup (F3). Before any archive I/O, each run looks up a v4 admission for the same stream
  and `predecessor_invoice_id`. If one exists the result is `ADOPTED_EARLIER`: no archive read, no path
  resolution and no state write. So a delivered adopted invoice whose old archive was later changed,
  deleted, locked or made unreadable never stops a newer legitimate invoice.
- Send-time integrity. A pending adopted (or native) delivery is checked again when it is sent: the
  exact bytes read must match the admitted SHA-256 and size. A mismatch or an unreadable file holds that
  intent before any seal (`EG_V4_ARCHIVE_IDENTITY_CHANGED` or `EG_V4_ARCHIVE_UNREADABLE`). Nothing is
  resent, repaired or discarded. A newer legitimate invoice is still admitted, but it waits behind the
  held older work in that stream (oldest first).
- An adopted v4 admission links to its predecessor row (`predecessor_invoice_id`). It writes no
  predecessor table and creates no second invoice identity. A stream whose latest is HOLD gets no new
  admission and no inventory call.
- An older invoice that the portal offers is refused with `EG_V4_PREDECESSOR_IDENTITY_PRESENT`. An
  invoice older than the newest predecessor invoice is refused with `EG_V4_SOURCE_REGRESSION`.
- The v3 store refuses a v4 database, and the v4 store refuses a plain v3 database.

## 4. Provider state machine

```
PENDING --seal--> DISPATCHED --VERIFIED (sha exact)--> VERIFIED
   |                  |  \--UNKNOWN------------------> UNKNOWN --late VERIFIED (sha exact)--> VERIFIED
   |                  \----REJECTED / bad sha / conflict--> HOLD
   \--held before dispatch (archive unreadable, identity changed, Drive not verified)--> HOLD
UNKNOWN --REJECTED / conflict--> HOLD;   VERIFIED --conflicting outcome--> HOLD;   HOLD is terminal
```

- A dispatch is sealed (the seal is inserted, then PENDING moves to DISPATCHED) before the provider call.
- A provider exception after the seal records `UNKNOWN`. A process crash after the seal leaves
  `DISPATCHED` with no receipt, and the next run's recovery turns it into `UNKNOWN`.
- `UNKNOWN` is never resent. It blocks only its own stream until a late receipt resolves it.
- An identical replayed outcome is a no-op (`REPLAY`). A different outcome after a terminal state is
  kept as evidence and holds the intent.

## 5. Run order (`orchestration.run_offline_dual_stream_v4`)

1. Refuse any live profile (`config.assert_offline_fake_profile`), any connector that is not one of
   the two fakes (`connector_actions.require_fake_connectors`), and any portal adapter that does not
   declare `OFFLINE_FAKE = True` (`connector_actions.require_fake_adapters`). Validate the run identity.
2. Take the lease. The stored owner token is unique to this session. A lease held by a live session
   stops the run with no mutation (`EG_V4_LEASE_HELD`, exit 10).
3. Fence every later step. Each write re-checks, inside its own transaction, that this session still
   owns the lease. The session also checks ownership before each portal call, before each archive
   publication and before each provider call. A session whose lease was taken over therefore stops
   before any further external effect, and the run ends with `EG_V4_LEASE_LOST`. A takeover is possible
   only after the lease expires.
4. Recover: every `DISPATCHED` intent with no receipt becomes `UNKNOWN`.
5. Classify, adopt and admit: for each stream, first classify the current latest v3 invoice (section 3).
   An ADOPTABLE latest is adopted once, with no portal call or fetch. A DELIVERED latest is no action.
   A HOLD stops that stream's admissions and inventory call. Otherwise take one inventory, pick the
   unique latest candidate and decide it in this precedence, before any fetch:
   1. the same identity already admitted with the same names: `ALREADY_ADMITTED`;
   2. an identity conflict, a predecessor identity or archive, or a source regression: `ADMISSION_REFUSED`
      (an older candidate is refused for regression even when its name is invalid or non-ASCII);
   3. display computation, then display collision: any `EG_V4_DISPLAY_*` is a typed stream `HOLD`;
   4. archive path collision: `ADMISSION_REFUSED`.
   A display HOLD fetches nothing and writes no archive, admission or effect for that candidate, does
   not stop older admitted work in that stream from draining, and is not stored: a later newer valid
   candidate proceeds. Then fetch and admit, subject to section 3. A portal or fetch failure is recorded
   against its stream, and pending work still
   drains. The archive is published before its admission row. A crash between them leaves a file on
   disk that the next run can reuse, but only while that invoice is still the stream's latest and the
   portal returns the same bytes. If a newer invoice is admitted first, the older archive file stays
   unadmitted and is not sent. If the portal returns different bytes for that identity, the stream is
   refused with `EG_V4_ARCHIVE_CONFLICT`, the existing file is never overwritten, and the invoice is not
   sent until an operator decides.
6. Drain: each step picks the stream after the last served one (durable round-robin). Within a
   stream, only the oldest invoice that is not fully handled is considered, so earlier admissions
   always drain before newer ones. Per invoice, Drive dispatches before Gmail, and Gmail attaches
   only the bytes of the VERIFIED Drive object.
7. Release the lease on a normal return or an ordinary exception. A simulated crash leaves it to
   expire, as a real crash would.

An immediate second run with nothing new performs zero dispatches.

### Run report (F5/F7)

The result keeps acquisition and drain truth separate:

- `acquisition[stream]`: status and `support_ref` of this run's admission step (and the predecessor result).
- `drain.streams[stream]`: the drain truth after the drain, `BLOCKED` (with the held or UNKNOWN intent's
  reference), `PENDING` or `DRAINED`. It is never overwritten by acquisition.
- `streams[stream]`: `outcome`, `acquisition_status`, `acquisition_ref`, `drain_state`, `drain_ref`. Both
  references stay visible, for example an acquisition display HOLD on a stream whose older dispatch is
  BLOCKED by an UNKNOWN Drive upload.
- `outcome` precedence: `BLOCKED` > `HOLD` > `PENDING` > `ACQUISITION_FAILED` / `REFUSED` > `DRAINED`.
  `REFUSED` is `ADMISSION_REFUSED`; every other unhandled acquisition status (`PORTAL_ACQUISITION_FAILED`,
  `FETCH_FAILED`, `ARCHIVE_CONFLICT`, `INVALID_PDF`, `INVENTORY_INVALID`, `LATEST_AMBIGUOUS`,
  `ADAPTER_UNAVAILABLE`, `ARCHIVE_PUBLISH_FAILED`) is `ACQUISITION_FAILED`. `DRAINED` needs an idle drain
  and an acquisition of `ADMITTED`, `ALREADY_ADMITTED`, `PREDECESSOR_DELIVERED` or `EMPTY`.
- `overall` is `DRAINED` only when both streams are `DRAINED`; otherwise it is the most severe stream outcome.

A predecessor HOLD stays permanent and fail-closed offline. Its audited operator resolution is a later
live-cutover G2 decision (D1) and is not implemented here.

## 6. Connector facade (`connector_actions.py`)

- Only in-memory fakes exist: `FakeDriveConnector`, `FakeGmailConnector`, `FakeProviderLedger`.
  The module imports no provider, network or process module.
- Portal adapters must declare `OFFLINE_FAKE = True` on their class. The offline core refuses any
  other adapter before it takes the lease. This is a declared boundary, not an enforced one. The
  runtime proof is what shows no real call is made.
- The ledger records every simulated effect. It is the evidence for "zero" and "exactly once".
- `SimulatedCrash` is a `BaseException`, so the core never swallows it.

## 7. Evidence

| Area | Test module |
|---|---|
| Month, prefix, conflicts, ambiguity rules and positive controls (M14), collisions, length, leading whitespace | `tests/test_naming.py` |
| Pinned predecessor, migration and open-work refusal, each blocker in plan and apply (M5a-M5i), run lock refusal, identity and release (M9, M9b), shipped v3 lock call sites (M9c), classifier (M1-M3), stale adoption (M4), digest, immutability (including `INSERT OR REPLACE`), guards, lease fencing, receipts, admission, predecessor cutover and regression | `tests/test_state_v4.py` |
| Fake contract, live-profile refusal, adapter declaration, static import and name proof (including adoption, classification, admission and migration functions), runtime no-call proofs for a native run and for migration plus adoption of an ADOPTABLE predecessor | `tests/test_connector_actions.py` |
| End to end: fairness, catch-up, second run, crashes at each checkpoint, lost response, lease takeover, portal failure and regression, plain-adapter refusal, predecessor cutover, M1-M3 run holds, adopted-archive liveness (M10), send-time integrity and FIFO (M11), display HOLD (M12, M14), regression precedence (M13), truthful report (M15), two-stream isolation with HOLD, UNKNOWN and no backfill | `tests/test_orchestration_v4.py` |

Commands (run from `energygrid-bill-downloader/`):

```bash
python -m unittest discover -s tests -p "test_naming.py"
python -m unittest discover -s tests -p "test_state_v4.py"
python -m unittest discover -s tests -p "test_connector_actions.py"
python -m unittest discover -s tests -p "test_orchestration_v4.py"
python -m unittest discover -s tests
```

## 8. Known exclusions

- No live provider, and no real Gmail, Drive, SMTP, n8n, portal or Shopify call. The v4 surface is
  proven by source inspection and a runtime patch of the socket, process and urllib entry points.
  Other package modules keep their v3 behaviour and are not reached by v4.
- No production database inspection or migration. The migration is proven only on the fixture and
  synthetic databases.
- No CLI entry point for v4. `cli.py`, `egcore.cmd` and `claude_supervisor.ps1` are unchanged.
- Cutover decisions still open for Web: a stream whose current latest is DRIVE_VERIFIED in v3 with no
  delivery sent is HOLD, not adopted, because seeding a v4 Drive receipt from v3 is not implemented. A
  REQUEST_REJECTED or otherwise dispatched-but-unsettled delivery is HOLD and is never resent. An archived
  latest that v3 never archived is HOLD, because v4 does not fetch at cutover.
- No automatic reconciliation of `UNKNOWN`. Resolution needs a late receipt or an operator decision,
  which is out of this slice.
- Source content changes for an already admitted identity are not detected. Admission is by
  identity, so a later run does not refetch.
- Audit rows (transitions and receipts) are append-only through the store and the triggers. A
  process with direct write access to the local SQLite file is outside this threat model.
- The lease is not renewed during a run. Its duration (600 s by default) must exceed the longest run.
  A longer run is taken over, and it then stops before its next external effect with `EG_V4_LEASE_LOST`.
  The intent it left DISPATCHED is recovered as UNKNOWN by the new owner, and it is not resent.
- Filename shapes: only qualified ASCII source names are supported, and only the exact `MMM YYYY - <source>`
  form is produced. For a current candidate, any non-ASCII name and any ambiguous month-year shape is a
  typed HOLD; an older candidate is still a source-regression refusal. No real portal basename
  compatibility is declared proven. An authorised read-only observation of real shapes is needed before
  live UAT (D7). A filename is never invented.
- Only the Windows `MoveFileExW` publication path is used, as in v3.
- OUT_OF_MODEL: a non-cooperating stale v3 handle. A `StateV3Store` opened before the migration by code
  that does not hold the single-run lock keeps writing after the migration. The original reproducer still
  reproduces this. It is outside the enumerated shipped-v3-entry-point trust assumption (section 3) and is
  not claimed fixed.

### Deferred live-cutover decisions (not resolved or bypassed by the offline core)

| Ref | Decision | Offline behaviour now |
|---|---|---|
| D1 | Audited operator resolution of a predecessor HOLD | Permanent fail-closed HOLD, no resolution command |
| D2 | Defence in depth against a non-cooperating stale v3 handle (for example a per-write schema fence) | Deferred; OUT_OF_MODEL |
| D3 | Real v3 Scheduler shutdown, proof that the live lock path equals the v4 lock path, and a supported v4 live entry point | Not implemented; no v4 CLI entry point |
| D4 | Manual recovery of an unreadable or changed archive for pending work | Intent HOLD before seal, never resent |
| D5 | Policy for a same-day invoice with a different identity | `EG_V4_ARCHIVE_PATH_COLLISION` refusal |
| D6 | SQLite-level identity constraints (for example triggers for `predecessor_invoice_id` and v3 identities) | Deferred; enforced in Python |
| D7 | Observation of actual EnergyGrid portal filenames | Not observed; ambiguous shapes HOLD |

The separate native Gmail and Drive managed-guard G2 HOLD and the Owner host-consent requirement are
independent of this document. Offline Core completion is not live UAT readiness.
