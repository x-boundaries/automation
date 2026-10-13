# EnergyGrid dual-stream v4 offline core: contract

Status: offline construction slice for #226 G3 (run `2026-10-13-xb-226-dualstream-offline-core-g3-001`).
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
| Already prefixed, same month and year | Accepted; the prefix is normalised to uppercase, with no second prefix |
| Any other `MMM YYYY - ` prefix, or a doubled prefix | Refused, `EG_V4_DISPLAY_PREFIX_CONFLICT` |
| Non-ASCII source name (only qualified ASCII names are supported) | Typed HOLD, `EG_V4_DISPLAY_SHAPE_UNSUPPORTED`. Never guessed. |
| Leading whitespace, or any format (Cf) or control (Cc) character | Refused, `EG_V4_DISPLAY_NAME_INVALID` |
| Other month-year text at the start, in the exact or a non-standard shape (for example a doubled prefix with two spaces, `OCT2026-` or `OCT 2026-03`) | Refused, `EG_V4_DISPLAY_PREFIX_CONFLICT`. Fail closed. |
| Empty stem, non-PDF, control or path characters, reserved device name | Refused, `EG_V4_DISPLAY_NAME_INVALID` |
| Result longer than 260 characters | Refused, never truncated |
| Collision within one stream (NFC plus casefold) | Refused at admission, `EG_V4_DISPLAY_NAME_COLLISION` |

The display name is computed once and stored in the immutable admission row.

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
- A migration takes the write lock first. Inside that transaction it refuses only local integrity
  blockers: an unclassified invoice, an archive that is not settled, a stream held in v3, or a prepared
  file operation. Provider evidence is not a blocker, because cutover classifies it. It accepts a
  COMPLETE_V3 or RESUMABLE_V3 database, writes an exclusive backup, applies the v4 objects, and commits
  only if the predecessor digest and the v4 inspection both still pass. Any failure rolls back.
- v4 never re-admits v3 history by portal identity. The stream's CURRENT latest v3 invoice is
  classified from frozen v3 evidence. Older history is never classified, so it is never backfilled:

  | Frozen v3 evidence for the current latest | Result |
  |---|---|
  | No delivery row, no Drive operation, no DRIVE_STAGE operation, archive COMMITTED | ADOPTABLE: one v4 delivery, no fetch |
  | Delivery PENDING_SEND with no dispatch start, and no Drive evidence | ADOPTABLE |
  | Delivery DELIVERED | DELIVERED: no action |
  | Delivery DELIVERY_OUTCOME_UNCERTAIN, REQUEST_REJECTED, or dispatched | HOLD (`EG_V4_PREDECESSOR_DELIVERY_*`) |
  | Drive operation not DRIVE_VERIFIED (partial or uncertain) | HOLD (`EG_V4_PREDECESSOR_DRIVE_PARTIAL`) |
  | Drive operation DRIVE_VERIFIED, delivery not sent | HOLD (`EG_V4_PREDECESSOR_DRIVE_VERIFIED_NOT_SENT`). Web decision needed. |
  | DRIVE_STAGE operation, or drive state not NOT_STAGED | HOLD (`EG_V4_PREDECESSOR_DRIVE_EVIDENCE_PRESENT`) |
  | Archive not COMMITTED | HOLD (`EG_V4_PREDECESSOR_ARCHIVE_NOT_COMMITTED`). v4 never fetches at cutover. |
  | Two latest invoices with the same day | HOLD (`EG_V4_PREDECESSOR_LATEST_AMBIGUOUS`) |

- Adoption validates the existing archive bytes against the frozen row (SHA-256 and size). It never
  refetches, recopies or renames the archive. A failed check is a HOLD (`EG_V4_PREDECESSOR_ARCHIVE_UNREADABLE`
  or `EG_V4_PREDECESSOR_ARCHIVE_IDENTITY_MISMATCH`).
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
   unique latest candidate and admit it, subject
   to section 3. A portal or fetch failure is recorded against its stream, and pending work still
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
| Month, prefix, conflicts, collisions, length, leading whitespace | `tests/test_naming.py` |
| Pinned predecessor, migration and open-work refusal, digest, immutability (including `INSERT OR REPLACE`), guards, lease fencing, receipts, admission, predecessor cutover and regression | `tests/test_state_v4.py` |
| Fake contract, live-profile refusal, adapter declaration, static import and name proof, runtime no-call proof | `tests/test_connector_actions.py` |
| End to end: fairness, catch-up, second run, crashes at each checkpoint, lost response, lease takeover, portal failure and regression, plain-adapter refusal, predecessor cutover | `tests/test_orchestration_v4.py` |

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
  form is produced. Any non-ASCII name, and any other month-year shape, is a typed HOLD or refusal. No real
  portal basename compatibility is declared proven. An authorised read-only observation of real shapes is
  needed before live UAT. A filename is never invented.
- Only the Windows `MoveFileExW` publication path is used, as in v3.
