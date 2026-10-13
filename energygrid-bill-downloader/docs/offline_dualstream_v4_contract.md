# EnergyGrid dual-stream v4 offline core: contract

Status: offline construction slice for #226 G3 (run `2026-10-13-xb-226-dualstream-offline-core-g3-001`).
Scope: deterministic local core with fake provider adapters only. This document does not grant
live Gmail, Drive, SMTP, n8n, portal or Shopify effects, a Desktop scheduled task, a project hook,
a production migration or a G4 or UAT pass. Live delivery stays HOLD until the separate G2
permission boundary is accepted by Web.

## 1. Streams and identity

- Streams are `EB_BILL` and `TENANT_BILL`. Each stream has its own admissions and its own
  independent blocking. A blocked stream never blocks the other stream.
- Source identity is `(stream, source_namespace, source_invoice_key)`. `source_invoice_key` is the
  NFC plus casefold key of the portal filename (`publication.filename_key`).
- Archive identity is date-only and frozen: `<stream folder>/YYYY-MM-DD.pdf`, where the folder is
  `EB Bill` or `Tenant Bill`. Archive files are never renamed or rewritten by v4.
- Archive identity is SHA-256 plus byte size. The same bytes are used for the on-disk archive, the
  simulated Drive copy and the simulated email attachment.

## 2. Presentation filename (`naming.py`)

`MMM YYYY - <source filename>`, prefix applied exactly once:

| Rule | Behaviour |
|---|---|
| Month | Uppercase English abbreviation `JAN`..`DEC` from the validated source invoice date |
| Year | Four digits |
| Source name | Kept verbatim after the prefix, extension included (`.pdf` or `.PDF`) |
| Already prefixed, same month and year | Accepted; the prefix is normalised to uppercase, no second prefix |
| Any other `MMM YYYY - ` prefix, or a doubled prefix | Refused, `EG_V4_DISPLAY_PREFIX_CONFLICT` |
| Empty stem, non-PDF, control or path characters, reserved device name | Refused, `EG_V4_DISPLAY_NAME_INVALID` |
| Result longer than 260 characters | Refused, never truncated |
| Collision within one stream (NFC plus casefold) | Refused at admission, `EG_V4_DISPLAY_NAME_COLLISION` |

The display name is computed once and stored in the immutable admission row.

## 3. SQLite v4 (additive)

v4 is a migration of a complete v3 database (`migrate_state_database_v4`). It adds these tables,
indexes and triggers and sets `user_version = 4`. It does not alter any v1-v3 object or row.

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

Guards that the database enforces, so a direct SQL write cannot skip them:

- Historical v4 rows cannot be updated or deleted.
- A status change must follow a legal edge and advance the version by exactly one.
- `DISPATCHED` requires a dispatch seal. `VERIFIED` requires a `VERIFIED` receipt.
- A dispatch can be sealed only from `PENDING`.
- A `GMAIL_SEND` seal requires its invoice's `DRIVE_UPLOAD` to be `VERIFIED`.
- A receipt requires a prior dispatch seal.

### Predecessor proof

- The fixture `tests/fixtures/predecessor_v3_schema.sql` is generated from the unmodified frozen-main
  v3 code (`2753923`) and pinned by SHA-256 over LF-normalised bytes. It is schema and configuration
  rows only, with no invoice, Drive or delivery data.
- `predecessor_digest` covers every v1-v3 schema object and every predecessor row. A migration
  applies only when that digest is unchanged inside the same transaction. An exclusive-create
  backup is written first, and any failure rolls back.
- The v3 store refuses a v4 database, and the v4 store refuses a plain v3 database.

## 4. Provider state machine

```
PENDING --seal--> DISPATCHED --VERIFIED (sha exact)--> VERIFIED
   |                  |  \--UNKNOWN------------------> UNKNOWN --late VERIFIED (sha exact)--> VERIFIED
   |                  \----REJECTED / bad sha / conflict--> HOLD
   \--held before dispatch (archive unreadable, identity changed, Drive not verified)--> HOLD
UNKNOWN --REJECTED / conflict--> HOLD;   VERIFIED --conflicting outcome--> HOLD;   HOLD is terminal
```

- A dispatch is sealed (insert the seal, then PENDING to DISPATCHED) before the provider call.
- A provider exception after the seal records `UNKNOWN`. A process crash after the seal leaves
  `DISPATCHED` with no receipt, and the next run's recovery turns it into `UNKNOWN`.
- `UNKNOWN` is never resent. It blocks only its own stream until a late receipt resolves it.
- A replayed identical outcome is a no-op (`REPLAY`). A different outcome after a terminal state is
  kept as evidence and holds the intent.

## 5. Run order (`orchestration.run_offline_dual_stream_v4`)

1. Refuse any live profile (`config.assert_offline_fake_profile`) and any connector that is not one
   of the two fakes (`connector_actions.require_fake_connectors`). Validate the run identity.
2. Take the lease. A lease held by another live session stops the run with no mutation
   (`EG_V4_LEASE_HELD`, exit 10).
3. Recover: every `DISPATCHED` intent with no receipt becomes `UNKNOWN`.
4. Admit: for each stream, take one inventory, pick the unique latest candidate and admit it.
   A portal or fetch failure is recorded for that stream, and pending work still drains. The
   archive is published before its admission row. A crash between them is reused on the next run.
5. Drain: each step picks the stream after the last served one (durable round-robin). Within a
   stream, only the oldest invoice that is not fully handled is considered, so earlier admissions
   always drain before newer ones. Per invoice, Drive dispatches before Gmail, and Gmail attaches
   only the bytes of the VERIFIED Drive object.
6. Release the lease on a normal return or an ordinary exception. A simulated crash leaves it to
   expire, as a real crash would.

An immediate second run with nothing new performs zero dispatches.

## 6. Connector facade (`connector_actions.py`)

- Only in-memory fakes exist: `FakeDriveConnector`, `FakeGmailConnector`, `FakeProviderLedger`.
  The module imports no provider, network or process module.
- The ledger records every simulated effect. It is the evidence for "zero" and "exactly once".
- `SimulatedCrash` is a `BaseException`, so the core never swallows it.

## 7. Evidence

| Area | Test module |
|---|---|
| Month, prefix, conflicts, collisions, length | `tests/test_naming.py` |
| Pinned predecessor, migration, digest, immutability, guards, lease, receipts, admission | `tests/test_state_v4.py` |
| Fake contract, live-profile refusal, static import and name proof, runtime no-call proof | `tests/test_connector_actions.py` |
| End to end: fairness, catch-up, second run, crashes at each checkpoint, lost response, portal failure, lease, refusals | `tests/test_orchestration_v4.py` |

Commands (run from `energygrid-bill-downloader/`):

```bash
python -m unittest discover -s tests -p "test_naming.py"
python -m unittest discover -s tests -p "test_state_v4.py"
python -m unittest discover -s tests -p "test_connector_actions.py"
python -m unittest discover -s tests -p "test_orchestration_v4.py"
python -m unittest discover -s tests
```

## 8. Known exclusions

- No live provider, no real Gmail, Drive, SMTP, n8n, portal or Shopify call. The v4 surface is
  proven by source inspection and a runtime patch of the socket, process and urllib entry points.
  Other package modules keep their v3 behaviour and are not reached by v4.
- No production database inspection or migration. The migration is proven only on the fixture and
  synthetic databases.
- No CLI entry point for v4. `cli.py`, `egcore.cmd` and `claude_supervisor.ps1` are unchanged.
- No automatic reconciliation of `UNKNOWN`. Resolution needs a late receipt or an operator decision,
  which is out of this slice.
- Source content changes for an already-admitted identity are not detected. Admission is by
  identity, so a later run does not refetch.
- Only the Windows `MoveFileExW` publication path is used, as in v3.
