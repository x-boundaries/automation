# EnergyGrid v4: native Claude connector bridge and controlled UAT

Status: #226 V3 G1 Core-to-UAT build (Web #226:6165246771), updated for the owner decisions in Web
#226:6167661750 (user-facing archive name), #226:6168033136 (email is optional, Drive is Core success)
and #226:6168238127 (success and failure notifications; flat Drive folder). This document adds a
delivery path to the offline v4 Core (`offline_dualstream_v4_contract.md`). It authorises no live effect.

## 1. Why a bridge

The owner chose the native claude.ai **Google Drive** and **Gmail** connectors, not SMTP, a custom Drive
API transport or n8n. Only the Claude host (the model's tool calls) can reach them, so delivery is a
stepwise protocol:

- The **Core** (`native_bridge.py`, `native_notify.py`, `native_cli.py`) owns every decision: latest
  selection, admission, archive integrity, the dispatch seal, verification and the receipt.
- The **host** (a Claude Code session) performs exactly one native connector call per sealed step and
  reports what the connector returned.

The v4 SQLite state machine and schema are unchanged (only the read-only `StateV4Store.dispatch_count`
was added). Notifications live in a separate bridge-owned SQLite file next to the state
(`<state>.notifications.sqlite`).

## 2. What success means

- **Core success** for a stream: its latest invoice is archived and **verified in Drive**. `status`
  reports `DRAINED`, `PENDING` or `BLOCKED` per stream from Drive state only. The v4 `GMAIL_SEND`
  intents are not used by the bridge.
- **Email** is optional owner feedback to exactly one private recipient. It never gates Drive work, never
  blocks the other stream or later invoices, is never resent automatically, and a Gmail failure never
  sends a notification about itself.
  - SUCCESS: once per invoice when its Drive upload is VERIFIED; the body carries the Drive link built
    from the Core receipt (`https://drive.google.com/file/d/<id>/view`).
  - FAILURE: once per incident: a Drive intent in HOLD or UNKNOWN (once per intent and state), or a failed
    acquisition (once until the stream acquires successfully again; a later failure is a new incident).
    The body holds only the stream, a status word and the closed `EG_` support reference.
  - A PDF attachment is off by default (`attach_pdf: false`); when on, it is byte-checked before sending.

## 3. Commands

`python -m energygrid_bill_downloader.native_cli <command> --config <private native config>`

Every command prints one JSON line. Exit codes: `0` done or nothing to do, `10` lease held or retry
later, `20` HOLD or uncertainty recorded, `64` refused with **no state change**.

| Command | Effect |
|---|---|
| `init-uat` | Creates a fresh isolated v3 database and migrates it to v4. Refuses an existing state file and a non-empty outbox, archive or named archive. |
| `make-synthetic-fixture` | `UAT_SYNTHETIC` only: writes `inventory.json` and synthetic PDFs (two per stream). |
| `acquire` | Recover, admit the latest invoice of each stream (synthetic files or the real direct-HTTP portal), publish the user-facing named archive copy (section 5), and record any notifications. No provider effect. |
| `next` | Recover (Drive and notifications), then plan one step: a Drive step first (fair round-robin, FIFO per stream), otherwise the oldest pending notification. Never seals. Outcomes: `DRIVE_PRECHECK`, `NOTIFY_DRAFT`, `HOLD`, `RETRY_LATER`, `CEILING_REACHED`, `NOTIFY_CEILING_REACHED`, `IDLE`. |
| `drive-seal --intent I --evidence F` | Needs a complete name-occupancy precheck. No match: seals and returns the `create_file` arguments. Exactly one same-name file: `RECONCILE` with its id (no state change). Several: HOLD. |
| `drive-readback --intent I --file-id D --base64-file F` | The Core hashes what Drive returns and writes a write-once attestation for that exact id. |
| `drive-record --intent I --evidence F` | `CREATED` needs matching metadata and a matching readback: VERIFIED. Mismatch: REJECTED, HOLD. `DENIED`: HOLD. `ERROR`/`NO_RESPONSE`: UNKNOWN. |
| `drive-adopt --intent I --evidence F` | For `RECONCILE`: the existing file is adopted as this invoice's upload only if its metadata and Core readback match exactly (no upload, so no duplicate). Otherwise HOLD; nothing is overwritten. |
| `notify-draft-evidence --notification N --draft-id R --raw-file F` | The Core parses the Gmail `get_draft` **RAW** message (sender, recipients, subject, text body, attachments with SHA-256) into write-once evidence. |
| `notify-seal --notification N --draft-id R` | Verifies the Core-parsed draft: sender is the configured account, exactly the one recipient, no cc/bcc, exact subject, the Core body (with the Drive link for SUCCESS) and the attachment rule. Then seals and binds that draft. A wrong draft changes nothing. |
| `notify-record --notification N --evidence F` | `SENT` for the bound draft: SENT (also resolves UNKNOWN). `DENIED`: HOLD. `ERROR`/`NO_RESPONSE`: UNKNOWN. |
| `status` | Per-stream Drive truth with receipts and named-archive state, the notification list, and dispatch counts. |

## 4. Host protocol (one loop)

1. `acquire` once.
2. `next`. On `IDLE`, `CEILING_REACHED` or `NOTIFY_CEILING_REACHED` go to step 5. On `HOLD`, run `next` again.
3. `DRIVE_PRECHECK`:
   1. Drive `search_files` with exactly the `precheck_query` from `next`, every page. Write
      `{"folder_id", "title", "query", "complete": true, "matches": [{"id", "title", "parentId"}]}`.
   2. `drive-seal`.
      - `SEALED`: Drive `create_file` with `title`, `parentId`, `contentMimeType: application/pdf`,
        `disableConversionToGoogleType: true` and `base64Content` set to the exact contents of
        `base64Content_path`. Then `download_file_content`, save the base64, `drive-readback`, and
        `drive-record` with `{"outcome": "CREATED", "file": {"id", "title", "parentId", "mimeType", "fileSize"}}`.
        If the call was refused use `DENIED`; if it failed or gave no answer use `ERROR` or `NO_RESPONSE`.
      - `RECONCILE`: `download_file_content` of `reconcile_file_id`, `drive-readback`, then `drive-adopt`
        with that file's metadata.
4. `NOTIFY_DRAFT`:
   1. Gmail `create_draft` with exactly `to`, `subject` and `body` from `next` (and the attachment when
      `attach_pdf` is true).
   2. Gmail `get_draft` with `messageFormat: RAW` (the `FULL_CONTENT` view omits attachments). Save the
      `raw` value and run `notify-draft-evidence`.
   3. `notify-seal`. Only on `SEALED`: Gmail `send_message` with `draftId` set to `send_draft_id`.
   4. `notify-record` with `{"outcome": "SENT", "draft_id", "message_id"}`.
5. `status` once.

Never call a connector for a step whose seal command did not answer `SEALED` (or `RECONCILE` for the
read-only download). A crash or lost answer after a seal leaves the Drive intent `DISPATCHED` (or the
notification `SEALED`); the next `acquire` or `next` makes it `UNKNOWN`, which is never resent. A later
record with full evidence for the same object resolves `UNKNOWN` (late receipt). An unsealed draft is not
delivery; discard it.

## 5. Filenames and destinations (owner requirement)

- The user-facing downloaded archive, the Drive title and (when attached) the email attachment use the
  stored v4 display name `MMM YYYY - <original portal filename>`, prefix applied exactly once.
- User-facing archive: `<named_archive_root>/<EB Bill|Tenant Bill>/MMM YYYY - <original>.pdf`, the exact
  archive bytes, published with no-replace semantics and re-hashed. An identical existing copy is kept; a
  different file at that name is never overwritten and HOLDs that invoice's Drive step
  (`EG_NATIVE_NAMED_ARCHIVE_CONFLICT`). A transient I/O failure is `RETRY_LATER`. It is a precondition of
  every Drive seal or adoption. The internal immutable `archive_root` identity stays `YYYY-MM-DD.pdf`.
- Drive: `drive.folder_ids` may name the same existing folder for both streams (the owner's flat folder).
  No folder is created. Collisions are guarded by the precheck: a same-name file is adopted only when its
  bytes are identical; otherwise the invoice HOLDs and nothing is overwritten, renamed or duplicated.

## 6. Trust model

- The PDF base64 is carried by the model (Drive `create_file` accepts inline base64 only). A copy error
  that keeps the length is invisible to a size check; the Core readback hash catches it.
- Evidence is host-attested: the Core verifies that what the host reports is consistent and exact, but it
  cannot independently query Google. `drive-readback` hashes whatever base64 the host supplies; it proves
  the bytes only if the host really saved the `download_file_content` result. Draft evidence is parsed by
  the Core from the RAW message the host saved.
- Invoice bytes are model-visible in the host session. Keep payloads out of GitHub and progress logs.
- The native Drive connector returns `fileSize`, `mimeType`, `parentId` and `title`, but no checksum.
- The Gmail connector cannot choose the sender; Gmail uses the account's default send-as address, which
  the config must name as `gmail.account`.
- Call-time permission: in a session with bypassed permissions, connector writes run without a prompt.
- A later `drive-record` with a different outcome for an already VERIFIED intent is kept as evidence and
  moves the intent to HOLD (v4 rule). Report each step once.

## 7. Configuration (`config/energygrid.native_uat.example.json`)

Closed shape, every key required. Paths (including `named_archive_root`) must be absolute, local (no
UNC), free of reparse points and outside the Git checkout (and the main checkout of a worktree). Modes:
`UAT_SYNTHETIC` (`synthetic_root`) or `UAT_PORTAL` (`portal_config_path`, the existing private dual-stream
config used to build the reviewed direct-HTTP adapters). There is no production mode. `gmail` is
`{account, to: [one recipient], subject_prefix, attach_pdf}`. `effect_ceiling` caps sealed Drive
dispatches and sealed notifications (0 to 20) for the whole database. The real recipient and folder IDs
belong only in the private config, never in this public repository.

## 8. Controlled UAT

Each stage needs its own owner grant naming the Drive account and folder, the Gmail sender and recipient,
the effect ceiling, synthetic or real data, transcript acceptance and the permission mode. Stage A
(synthetic) was demonstrated on 14 Oct 2026 against the earlier per-stream-folder design (#226). Real
portal access, real bills, production state, Scheduler, merge and release each need separate authority.

## 9. Evidence

`tests/test_native_bridge.py` (58 tests): complete dual-stream journey with one success notification per
bill and the Drive link; latest-only and no backfill; second run zero effects; newer invoice later; Drive
success independent of email (denied, uncertain and capped email); failure notifications once per
incident with non-sensitive content; flat folder shared by both streams; adoption of an identical
hand-filed file; no adoption for different bytes (including same size) or several same-name files;
cross-stream name collision; Drive crash, lost answer, corruption, metadata, denied, error, racing seals,
precheck and tampering; junctions; notification draft refusals (recipient, cc, subject, sender, body,
attachment), Core-parsed evidence required, bound draft, undecodable RAW; optional attachment byte check;
Drive and email ceilings; lease; v3 predecessor adoption; named archive; config and CLI refusals; no
network imports in the bridge modules.
