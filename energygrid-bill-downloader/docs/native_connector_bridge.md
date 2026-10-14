# EnergyGrid v4: native Claude connector bridge and controlled UAT

Status: #226 V3 G1 Core-to-UAT build (Web #226:6165246771). This document adds a delivery path to
the offline v4 Core (`offline_dualstream_v4_contract.md`). It authorises no live effect. Real
portal access, Drive writes and Gmail sends each need the owner-scoped UAT grant in section 7.

## 1. Why a bridge

The owner chose the native claude.ai **Google Drive** and **Gmail** connectors for delivery, not SMTP,
a custom Drive API transport or n8n. Those connectors are reachable only by the Claude host (the
model's tool calls); a Python process cannot call them. So delivery is a stepwise protocol:

- The **Core** (`native_bridge.py`, `native_cli.py`) owns every decision: latest selection,
  admission, archive integrity, the dispatch seal, verification and the receipt. SQLite stays the
  only authority, under the same v4 lease and triggers.
- The **host** (a Claude Code session) performs exactly one native connector call per sealed step
  and reports what the connector returned.

The v4 state machine is unchanged. Only `StateV4Store.dispatch_count` (read only) was added.

## 2. Commands

`python -m energygrid_bill_downloader.native_cli <command> --config <private native config>`

Every command prints one JSON line. Exit codes: `0` done or nothing to do, `10` lease held (retry
later), `20` HOLD or uncertainty recorded, `64` refused with **no state change**.

| Command | Effect |
|---|---|
| `init-uat` | Creates a fresh isolated v3 database and migrates it to v4. Refuses an existing state file. |
| `make-synthetic-fixture` | `UAT_SYNTHETIC` only: writes `inventory.json` and synthetic PDFs (two per stream). |
| `acquire` | Recover, then admit the latest invoice of each stream (synthetic files or the real direct-HTTP portal). No provider effect. |
| `next` | Recover, then plan one step (fair round-robin, FIFO per stream, Drive before Gmail). Writes `<outbox>/<intent>.pdf.b64` with the exact archive bytes. Never seals. |
| `drive-seal --intent I --evidence F` | Needs a complete name-occupancy precheck. Occupied name: HOLD. Otherwise seals and returns the `create_file` arguments. |
| `drive-readback --intent I --file-id D --base64-file F` | The Core hashes what Drive returns and writes a write-once attestation. |
| `drive-record --intent I --evidence F` | `CREATED` needs matching metadata and a matching readback: VERIFIED. Mismatch: REJECTED, HOLD. `DENIED`: HOLD. `ERROR`/`NO_RESPONSE`: UNKNOWN. |
| `gmail-readback --intent I --draft-id R --base64-file F` | The Core hashes the draft attachment and writes a write-once attestation. |
| `gmail-seal --intent I --evidence F` | Verifies the draft (recipients, no cc/bcc, subject, one attachment, name, MIME, size, and the readback when required), then seals and binds that draft. A wrong draft is refused with no state change. |
| `gmail-record --intent I --evidence F` | `SENT` for the bound draft: VERIFIED. `DENIED`: HOLD. `ERROR`/`NO_RESPONSE`: UNKNOWN. |
| `status` | Per-stream `DRAINED`, `PENDING` or `BLOCKED` (with the support reference), delivered names and dispatch counts. |

## 3. Host protocol (one loop)

1. `acquire` once.
2. `next`. On `IDLE` go to step 6. On `HOLD`, run `next` again (that stream stopped).
3. `DRIVE_PRECHECK`:
   1. Drive `search_files` with `parentId = '<folder_id>' and title = '<display_filename>'`, every page.
      Write `{"folder_id", "title", "complete": true, "matches": [{"id", "title", "parentId"}]}`.
   2. `drive-seal`. Only on `SEALED`: Drive `create_file` with `title`, `parentId`,
      `contentMimeType: application/pdf`, `disableConversionToGoogleType: true` and `base64Content` set
      to the exact contents of `base64Content_path`.
   3. Drive `download_file_content` for the returned id; save the base64 to a file; `drive-readback`.
   4. `drive-record` with `{"outcome": "CREATED", "file": {"id", "title", "parentId", "mimeType", "fileSize"}}`
      copied from the `create_file` result. If the call was refused by the permission prompt use
      `DENIED`; if it failed or gave no answer use `ERROR` or `NO_RESPONSE`.
4. `GMAIL_DRAFT`:
   1. Gmail `create_draft` with `to`, `subject`, `body` and one attachment
      `{content: <payload base64>, filename: <display_filename>, mimeType: application/pdf}`.
   2. Gmail `get_draft` (`FULL_CONTENT`). Save the attachment base64 when the connector returns it and run
      `gmail-readback`. Write `{"draft_id", "to", "cc", "bcc", "subject", "attachments": [{"filename", "mimeType", "size"}]}`.
   3. `gmail-seal`. Only on `SEALED`: Gmail `send_message` with `draftId` set to `send_draft_id`.
   4. `gmail-record` with `{"outcome": "SENT", "draft_id", "message_id"}`.
5. Go to step 2. Never call a connector for a step whose seal command did not answer `SEALED`.
6. `status` once.

Recovery rules (Core-enforced): a crash or lost answer after a seal leaves `DISPATCHED`; the next
`acquire` or `next` makes it `UNKNOWN`. `UNKNOWN` and `HOLD` are never resent and block only their
own stream. A later `drive-record`/`gmail-record` with full evidence for the same object resolves
`UNKNOWN` to `VERIFIED` (late receipt). A draft that was never sealed is not delivery; discard it.

## 4. Trust model and what verification proves

- The base64 payload is carried by the model: Drive `create_file` and Gmail attachments accept only
  inline base64. A copy error that keeps the length is invisible to a size check. Drive readback
  (Core hash of the downloaded bytes) catches it before any email. Gmail readback, when the connector
  returns attachment bytes, catches it before the send.
- Evidence is **host-attested**: the Core verifies that what the host reports is consistent and exact,
  but it cannot independently query Google. A faulty or hostile host is outside this model.
- Invoice bytes are model-visible in the host session (tool arguments and results). Use a session
  whose transcript retention the owner accepts, and never paste payloads into GitHub or progress logs.
- The native Drive connector returns `fileSize`, `mimeType`, `parentId` and `title`, but no
  `md5Checksum` or `sha256Checksum` (observed read-only, 14 Oct 2026), hence the readback.
- Call-time permission: in a session with bypassed permissions, connector writes run without a
  prompt. For UAT use a session where Drive `create_file` and Gmail `send_message` ask each time.

## 5. Filenames

Drive title and attachment name are the stored v4 display name `MMM YYYY - <portal filename>`, prefix
applied once (`naming.py`). The local archive keeps the frozen v4 identity `<EB Bill|Tenant Bill>/YYYY-MM-DD.pdf`.

## 6. Configuration (`config/energygrid.native_uat.example.json`)

Closed shape, every key required. Paths must be absolute, local (no UNC), free of reparse points and
**outside the Git checkout**. Modes: `UAT_SYNTHETIC` (`synthetic_root`) or `UAT_PORTAL`
(`portal_config_path`, the existing private dual-stream config used to build the reviewed direct-HTTP
adapters). There is no production mode. `effect_ceiling` caps sealed Drive and Gmail dispatches
(0 to 20) for the whole database. `require_attachment_readback` is an explicit owner choice.

## 7. Controlled UAT

Stage A (synthetic, no private data): `init-uat`, `make-synthetic-fixture`, `acquire`, the host
loop, `status`, then an immediate second `acquire` and `next` (expect `ALREADY_ADMITTED`, `IDLE`, zero
new effects). Recovery demonstration: stop after one `drive-seal` without recording, run `next` and
confirm `UNKNOWN` with no second upload, then resolve with the late `drive-record`.

Stage B (real portal, real bills): `UAT_PORTAL`, a fresh isolated state database, the same loop.

Each stage needs, before any connector write: the Drive account and the two destination folder IDs;
the Gmail account and exact recipient(s); the effect ceiling; synthetic or real data; transcript
retention acceptance; and the `require_attachment_readback` choice. Stage B also needs consent for
real portal access and real bills. Unattended scheduling, production state and v3 cutover on the
production database are out of scope.

## 8. Evidence

`tests/test_native_bridge.py`: complete dual-stream journey, latest-only and no backfill, single
prefix, second run zero effects, newer invoice later, occupied Drive name, crash after seal, lost
answer, size-preserving corruption (Drive and Gmail), readback required, metadata mismatch, denied and
error outcomes, racing seals, precheck refusal, tampered archive, wrong drafts, bound-draft check,
replay, Gmail crash after seal, Gmail before Drive, effect ceiling, lease held, v3 predecessor adoption
without fetch, config refusals, no network imports in the bridge, closed CLI refusal output.
