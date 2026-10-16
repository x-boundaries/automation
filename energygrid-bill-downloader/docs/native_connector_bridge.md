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
| `next` | Recover (Drive and notifications), then plan one step: a Drive step first (fair round-robin, FIFO per stream), then the oldest pending notification, then a `LOCAL_SYNC` reconcile of a sealed-but-unrecorded upload (section 4c). Never seals. Outcomes: `DRIVE_PRECHECK`, `NOTIFY_DRAFT`, `DRIVE_RECONCILE_PENDING`, `HOLD`, `RETRY_LATER` (also when the synced folder is unavailable), `CEILING_REACHED`, `NOTIFY_CEILING_REACHED`, `IDLE`. |
| `drive-seal --intent I --evidence F` | Needs a complete name-occupancy precheck. Matches whose title differs are ignored, because Drive search is loose. No match: seals and returns the `create_file` arguments (`NATIVE_UPLOAD`), or writes the synced file and returns `await_file` (`LOCAL_SYNC`). Exactly one same-name file: `RECONCILE` with its id (no state change). Several: HOLD. `LOCAL_SYNC` with the synced folder missing, not a directory or behind a reparse point: `RETRY_LATER`, nothing sealed (section 4c). |
| `drive-readback --intent I --file-id D --base64-file F` | The Core hashes what Drive returns and writes a write-once attestation for that exact id. |
| `drive-record --intent I --evidence F` | `CREATED` needs matching metadata and a matching readback: VERIFIED. Mismatch: REJECTED, HOLD. `DENIED`: HOLD. `ERROR`/`NO_RESPONSE`: UNKNOWN. |
| `drive-adopt --intent I --evidence F` | For `RECONCILE`: the existing file is adopted as this invoice's upload only if its metadata and Core readback match exactly (no upload, so no duplicate). Otherwise HOLD; nothing is overwritten. |
| `notify-draft-evidence --notification N --draft-id R --raw-file F` | The Core parses the Gmail `get_draft` **RAW** message (sender, recipients, subject, text body, attachments with SHA-256) into write-once evidence. |
| `notify-seal --notification N --draft-id R` | Verifies the Core-parsed draft: sender is the configured account, exactly the one recipient, no cc/bcc, exact subject, the Core body (with the Drive link for SUCCESS) and the attachment rule. Then seals and binds that draft. A wrong draft changes nothing. |
| `notify-record --notification N --evidence F` | `SENT` for the bound draft: SENT (also resolves UNKNOWN). `DENIED`: HOLD. `ERROR`/`NO_RESPONSE`: UNKNOWN. |
| `status` | Per-stream Drive truth with receipts and named-archive state, the notification list, and dispatch counts. |
| `backfill-run --drive-evidence F --max N` | Manual one-off only (section 4a). Writes the older bills of each bound stream into the synced folder, at most N (1 to 50). |
| `backfill-record --drive-evidence F` | Verifies each backfilled file against a fresh Drive listing and its local mirror. |
| `migrate-hold-recovery [--rollback B]` | Operator only (section 4d), scheduled task disabled. Switches the state database to version 5 under the **same filename**, keeping the v4 file as a read-only backup; `--rollback` restores that backup when no effect has happened since. |
| `hold-release --intent I --evidence F --reason R --operator O` | Operator only (section 4d), version-5 database only. Audited release of one Drive HOLD; `NO_DISPATCH` also stages one owner notice. |

## 4. Host protocol (one loop)

1. `acquire` once.
2. `next`.
   - `IDLE`, `CEILING_REACHED`, `NOTIFY_CEILING_REACHED` or `RETRY_LATER`: go to step 5.
   - `HOLD`: run `next` again.
   - `DRIVE_RECONCILE_PENDING`: run the exact poll below with its `await_file`. If it records the file, continue; otherwise go to step 5.
3. `DRIVE_PRECHECK`:
   1. Drive `search_files` with exactly the `precheck_query` from `next`, every page. Write
      `{"folder_id", "title", "query", "complete": true, "matches": [{"id", "title", "parentId"}]}`.
   2. `drive-seal`.
      - `SEALED` in `LOCAL_SYNC` (`sync_written: true`): the Core has written the file. This is rule P1 of the installed host prompt, carried in `runtime/native_host/host_prompt.template.md`:
        - Poll Drive with the same query. Keep only results whose title equals `await_file.title` character for character and whose `parentId` equals `await_file.parentId`. The search also returns other files, for example the other stream's bill for the same month.
        - Exactly one such file whose `get_file_metadata` also has `mimeType: application/pdf` and `fileSize` equal to `await_file.fileSize`: `drive-record` it once.
        - None yet: wait and poll again, at most 20 polls, then stop without recording.
        - Two or more: stop without recording, adopting or deleting anything.
      - `SEALED` in `NATIVE_UPLOAD`: Drive `create_file` with `title`, `parentId`, `contentMimeType: application/pdf`,
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

## 4c. Production hardening (Web #226:6186622535)

- **Synced folder unavailable (P-2).** Before any `LOCAL_SYNC` seal, the synced folder must exist, be a directory and have no reparse point in its path.
  - Otherwise `next` and `drive-seal` answer `RETRY_LATER` (exit 10) with `EG_NATIVE_SYNC_FOLDER_UNAVAILABLE` or `EG_NATIVE_SYNC_FOLDER_UNSAFE`. Nothing is sealed, no effect is counted and there is no HOLD.
  - The manual backfill reports the same per item and writes nothing.
- **Staged publication (H-4).** The synced file and each backfill file are first written and flushed under a Core staging name (`.eg-stage-<32 hex>.part`) in the same folder.
  - They are then moved to the final name with `MoveFileExW` (no replace, write-through), so a crash never leaves a partial file under the final name.
  - A leftover staging file is removed by exact name at the next seal or backfill write, under the lease.
  - A cross-volume move is refused.
- **Sealed but never recorded (P-3).** `next` offers `DRIVE_RECONCILE_PENDING` for the oldest unresolved `LOCAL_SYNC` Drive intent of a stream. All of these must hold:
  - it is `DISPATCHED` or `UNKNOWN` and has a sealed dispatch;
  - its archive still has the Core hash;
  - its local mirror holds exactly those bytes.

  The host runs the exact poll and a late `drive-record`; only a VERIFIED late receipt advances it. Nothing is re-sealed, rewritten, re-uploaded or re-sent. Without the mirror nothing is offered: the stream stays BLOCKED, and the owner keeps the one `DRIVE_UPLOAD_UNKNOWN` incident already raised.
- **Effect ceilings (P-4).** `effect_ceiling` keeps its lifetime UAT meaning by default.
  - **Opt-in shape:** `{"mode": "PER_PERIOD", "period_days": 1..31, "drive_uploads": 0..20, "gmail_sends": 0..20}`. It counts per UTC bucket of `period_days` whole days from the Unix epoch, so no time zone, DST or wall-clock reset moves the boundary.
  - **What counts:** **sealed** actions, including any whose outcome is UNKNOWN or REJECTED. Drive counts come from the v4 dispatch table. Gmail counts come from an append-only seal ledger; an older seal with no ledger time counts unless it is provably earlier.
  - **Drive cap reached:** the held-back stream reports BLOCKED with `EG_NATIVE_EFFECT_CEILING_REACHED`, and the owner gets at most one `DRIVE_CEILING_REACHED` notification per stream per period.
  - **Gmail cap reached:** reported in `status.ceiling` only, never by email.
  - **Limits:** no production limit is chosen in this repository.
- **Hash provenance (H-3).** A Drive receipt's `provider_sha256` must equal the Core hash for VERIFIED (v4 trigger), so a bridge-owned append-only store (`<state>.provenance.sqlite`) records how that hash was obtained:
  - `LOCAL_MIRROR_SHA256` (`LOCAL_SYNC`): from the synced local copy. This is not an independent remote byte hash.
  - `PROVIDER_READBACK_SHA256` (`NATIVE_UPLOAD`): from the bytes Drive returned.

  `status` shows the method for each VERIFIED upload. Receipts recorded before the store existed show `UNRECORDED_LEGACY` and are never relabelled.
- **Gmail body (H-1).** The draft body must equal the Core body exactly, after whitespace normalisation and unwrapping Gmail's link redirect. Added or removed text gives `EG_NATIVE_DRAFT_BODY_MISMATCH`.
- **Tenant (H-2, H-5, H-6).**
  - The Invoice No. must appear as a whole token in the rendered PDF's text, decoded per text operator.
  - The credential reader runs `powershell.exe` from the Windows system directory (`GetSystemDirectoryW`), never via PATH.
  - The Tenant sign-in, list and browser portal URLs must be `https`.
- **Host policy (P-5).** `runtime/native_host/` holds the least-privilege launcher, permission and prompt templates and the installation qualification. They are not installed by this repository. Until the qualification passes on the host, M-5 (managed native guard) remains a production HOLD.

## 4d. Audited HOLD recovery (P-7, operator only)

A v4 HOLD is terminal. Recovery needs the state database switched to version 5 by `migrate-hold-recovery`.

The configured `state_path` never changes. The notification store (`<state>.notifications.sqlite`, with the sent-email history and the Gmail ceiling count), the hash-provenance store (`<state>.provenance.sqlite`) and the backfill ledger (`<state>.backfill.sqlite`) are named after it. A differently named state file would start all three empty, so earlier emails could be sent again, the Gmail ceiling would restart from zero and provenance would read `UNRECORDED_LEGACY`. Never repoint the configuration to a copy.

1. **Prepare.** Disable the scheduled task. Run `status` and check that nothing is in flight. A sealed upload or email with no recorded outcome refuses the switch (`EG_V5_OPERATION_IN_FLIGHT`); a normal `next` records it as UNKNOWN first, and it is never resent.
2. **Switch** with `migrate-hold-recovery` (no other argument). In one command it:
   1. Takes the state file's SQLite write lock and checks it: COMPLETE_V4, no SQLite sidecar files, no live lease and no sealed upload without an outcome. The lock is held, with nothing written, until step 5.
   2. Checks each dependent store without creating or changing it (SQLite still rolls back a journal left by a crash, as any open does). A missing notification store for a state with admitted invoices refuses with `EG_V5_HISTORY_MISSING`. A store row naming an invoice or intent this state does not have refuses with `EG_V5_HISTORY_MISMATCH`. A sealed email without an outcome refuses with `EG_V5_OPERATION_IN_FLIGHT`. An absent provenance store is legitimate: older receipts stay `UNRECORDED_LEGACY` and are never relabelled.
   3. Copies the file under the lock into a new temporary file beside it.
   4. Migrates the copy in one transaction. It adds the append-only `hold_release` audit, the superset status guard and an immutable `history_binding` table. That table records the state filename, the v4 hash, the backup name, and for each store whether it existed plus a digest of every row it held.
   5. Checks that the copy's rows and the source bytes are unchanged. A changed source (`EG_V5_SOURCE_CHANGED`) removes this attempt's copy and leaves every other file as it was.
   6. Renames the v4 file to `<state>.v4-backup-<id>.bak`. Windows refuses the rename while any process has the file open (`EG_V5_SOURCE_IN_USE`, exit 10).
   7. Checks the backup still has the verified hash. If it changed, it is put back at its name and the switch is refused.
   8. Renames the v5 file to the state filename (never over a file), makes the backup read-only and reopens the result as version 5.

   The result names `v4_backup`. Keep it.
3. **Before every later command,** a version-5 state is verified before the lease is taken, so a refusal writes nothing and makes no effect:
   - **Filename:** it must be opened under its bound filename; another name refuses with `EG_V5_STATE_REPOINTED`, before any store is opened or created.
   - **Stores:** each store that existed at the switch must still exist and still hold the same first rows. Sent emails must not have returned to pending. Otherwise the command refuses with `EG_V5_HISTORY_MISSING`.
   - **Provenance:** every Drive upload verified since the switch must still have its provenance row, because provenance is always recorded before the receipt. A provenance store lost after the switch is therefore refused (`EG_V5_HISTORY_MISSING`), never read as `UNRECORDED_LEGACY`.
   - **Limit:** notification and backfill rows added after the switch are protected by their own stores, exactly as in version 4. Putting an older copy of a store back after the switch is not detected for those later rows.
4. **Release the HOLD** with `hold-release`. Each release writes one audit row bound to the HOLD's exact version, so it is one-time, and triggers re-check the same rules for any direct SQL write.
   - `{"mode": "NO_DISPATCH"}` returns a HOLD to PENDING only if the intent was never dispatched, never received a receipt and never passed through DISPATCHED, UNKNOWN or VERIFIED. It stages exactly one owner notice (`hold released`, reference `EG_V5_HOLD_RELEASED`) to the configured recipient. The notice goes through the normal draft, seal and send steps and is never sent by the operator command. It is derived from the audit row, so a crash right after the release is healed once by the next run.
   - `{"mode": "LATE_EVIDENCE", "file": {...}}` with reason `LATE_EVIDENCE_VERIFIED` makes a dispatched HOLD VERIFIED only with exact Drive metadata plus the exact local mirror (or a matching Core readback). There is no new effect, and the normal success notification follows.
5. **Re-enable** the scheduled task. The next run is `IDLE` unless a released intent is now PENDING; such an intent is uploaded once.
6. **Rollback:** `migrate-hold-recovery --rollback <v4_backup path>`, with the task disabled. It undoes the switch only. It refuses in two cases:
   - `EG_V5_ROLLBACK_AFTER_DISPATCH`: the v5 file holds a dispatch or provider receipt that the backup lacks, so restoring would forget an effect.
   - `EG_V5_ROLLBACK_AFTER_RELEASE`: the v5 file holds a release audit row, which is append-only and is never erased.

   The v5 file must also name this exact backup and hash (`EG_V5_ROLLBACK_BACKUP_MISMATCH`), and be closed in every other process (`EG_V5_SOURCE_IN_USE`). If all checks pass:
   - one Windows `ReplaceFileW` step puts a hash-verified copy of the untouched backup at the state filename, so the name is never left empty;
   - the same step keeps the v5 file as `<state>.v5-retired-<id>.bak`;
   - the dependent stores are not touched.

   With no file at the state name, only an interrupted switch can be undone. That is a switch that stopped after step 2.6.
   - Every command refuses while the file is missing.
   - The rollback needs exactly one `<state>.v5-switch-<id>.tmp` copy that names this backup, and runs the same checks against it.
   - It renames that copy to `.consumed` before restoring, so the copy cannot authorise a second rollback.
   - Anything else is refused (`EG_V5_ROLLBACK_STATE_MISSING`), so an older backup can never be restored over later effects.

## 4a. Manual backfill of older bills (owner request, not scheduled)

The scheduled loop above stays latest-only. On the owner's request, `native_backfill.py` fills in the
older bills once. It is never part of the scheduled prompt and never touches the v4 admission state; its
ledger is `<state>.backfill.sqlite`, and it runs inside the v4 lease. `LOCAL_SYNC` only.

1. Drive `search_files` with exactly `parentId = '<folder id>' and title contains 'EnergyGrid'`, every
   page (Drive search matches whole words; `'EnergyGrid - '` matches nothing). Write `{"folder_id", "query", "complete": true, "files": [{"id", "title",
   "parentId", "fileSize"}]}`.
2. `backfill-run --max N`. One portal inventory per bound stream. The newest invoice date of each stream
   is skipped (`SKIPPED_LATEST`): it belongs to the scheduled Core. A name already in the listing or the
   ledger is skipped without a fetch. Each other bill is fetched, validated and written with exclusive
   create into the synced folder and the named archive under the owner name. A different local file at
   that name is a HOLD and is never overwritten. Bills beyond N are `DEFERRED` to a later run.
3. After Drive for desktop syncs, repeat step 1 and run `backfill-record`. A file is VERIFIED only with a
   single exact-title match in the configured folder, the exact size and a Core-hashed local mirror.

A later backfill run skips only the then-newest date, so a bill that was newest at an earlier run (and
was missed by the schedule) is picked up then. A file the owner added by hand under another name cannot
be recognised and would be written again under the owner name.

## 4b. Tenant Bill source (`tenant_source.py`)

The Tenant Bill sits in the main EMS portal, not the EB helper, and the portal has no server PDF: it
renders the bill in the browser. The owner chose an HTTP check with a browser only for the download.
Binding it is optional: `tenant_config_path` in a `UAT_PORTAL` config points at the private Tenant
config (`config/energygrid.tenant.example.json`); without it Tenant Bill stays UNBOUND.

- **Daily check (HTTP, no browser).** One sign-in with the stored credential, then one LIST call. The
  sign-in must succeed for an enabled account, and the private list claim must name that same user and one
  of its roles (`EG_TENANT_CLAIM_ACCOUNT_MISMATCH` otherwise). The list asks for one page larger than the
  inventory ceiling; the returned count must equal the rows returned (`EG_TENANT_LIST_INCOMPLETE`). Every
  row must belong to the configured tenant. Only `released` rows are candidates.
- **Identity and name.** The printed Invoice No. is the source identity (`<Invoice No>.pdf`); the invoice
  date is the first day of the billing cycle, so the name follows the billing month:
  `AUG 2026 - EnergyGrid - Tenant Bill - <Invoice No>.pdf`.
- **Render (only in `acquire`, only for an admitted bill).** A headless browser signs in through the
  reviewed login, opens the Tenant Bill tab and that row's preview, and saves the preview's own download
  (`bill.pdf`). The render must request exactly that row id (`EG_TENANT_RENDER_ROW_MISMATCH`), and the PDF
  text, read through its ToUnicode maps, must show the row's Invoice No. (`EG_TENANT_PDF_INVOICE_MISMATCH`).
  The render needs Playwright, so the host runs the CLI with the runtime venv Python. A bill must be on
  the portal's first table page to be rendered (`EG_TENANT_RENDER_ROW_NOT_VISIBLE`).
- **Credential.** The existing DPAPI-protected credential file is read in memory by a child PowerShell
  process. The pair is never put in an environment variable, a command line, a file or a log, and the
  browser never inherits it.
- **Backfill** (section 4a) uses the same adapter, so older Tenant bills are rendered one by one.

All Tenant endpoints, the list claim, the service headers and the credential path are private
configuration. Failures use the closed `EG_TENANT_*` support references.

## 5. Filenames and destinations (owner requirement)

- The user-facing downloaded archive, the Drive title and (when attached) the email attachment use the
  stored v4 display name `MMM YYYY - EnergyGrid - <EB Bill|Tenant Bill> - <original portal filename>`
  (owner naming decision, UAT Stage B), for example
  `AUG 2026 - EnergyGrid - EB Bill - 2026-08-26_ACCOUNT_00001.pdf`. The month is applied exactly once.
- User-facing archive: `<named_archive_root>/<EB Bill|Tenant Bill>/<display name>`, the exact
  archive bytes, published with no-replace semantics and re-hashed. An identical existing copy is kept; a
  different file at that name is never overwritten and HOLDs that invoice's Drive step
  (`EG_NATIVE_NAMED_ARCHIVE_CONFLICT`). A transient I/O failure is `RETRY_LATER`. It is a precondition of
  every Drive seal or adoption. The internal immutable `archive_root` identity stays `YYYY-MM-DD.pdf`.
- Drive: `drive.folder_ids` may name the same existing folder for both streams (the owner's flat folder).
  No folder is created. Collisions are guarded by the precheck: a same-name file is adopted only when its
  bytes are identical; otherwise the invoice HOLDs and nothing is overwritten, renamed or duplicated.
- Drive modes (`drive.mode`):
  - `LOCAL_SYNC` (for real bills): Google Drive for desktop syncs `drive.sync_folders[stream]` with the
    destination folder. For a folder shared with the account, add it to My Drive as a shortcut; Drive for
    desktop then mirrors it under `G:\.shortcut-targets-by-id\<folder id>\<folder name>`. `drive-seal`
    seals, then the Core creates the file there once (exclusive create, never overwrite) and returns
    `sync_written` with the expected title, parent and size; the host polls `search_files` until the
    file appears and calls `drive-record` with its metadata. The Core verifies the metadata and hashes the
    local mirror. No PDF bytes pass through the host; no `drive-readback` is needed. A same-name Drive
    file is adopted (`RECONCILE`, `drive-adopt`) only if its local mirror is byte-identical.
  - `NATIVE_UPLOAD`: the host sends the base64 through `create_file` and reads it back. Practical only
    for very small files: a real bill (about 47 KB, 62,000 base64 characters) is too large to carry
    reliably in one model tool call.

## 6. Trust model

- `NATIVE_UPLOAD`: the PDF base64 is carried by the model (Drive `create_file` accepts inline base64
  only). A copy error that keeps the length is invisible to a size check; the Core readback hash catches
  it. `LOCAL_SYNC`: the bytes are written by the Core and uploaded by Google's own sync client; the Core
  hashes the synced local mirror, so verification relies on Drive for desktop serving the Drive copy.
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
config used to build the reviewed direct-HTTP adapters). `UAT_PORTAL` alone accepts one optional key,
`tenant_config_path` (section 4b), with the single-stream portal config only. There is no production mode. `gmail` is
`{account, to: [one recipient], subject_prefix, attach_pdf}`. `effect_ceiling` caps sealed Drive
dispatches and sealed notifications (0 to 20) for the whole database. The real recipient and folder IDs
belong only in the private config, never in this public repository.

## 8. Controlled UAT

Each stage needs its own owner grant naming the Drive account and folder, the Gmail sender and recipient,
the effect ceiling, synthetic or real data, transcript acceptance and the permission mode. Stage A
(synthetic) was demonstrated on 14 Oct 2026 against the earlier per-stream-folder design (#226). Real
portal access, real bills, production state, Scheduler, merge and release each need separate authority.

## 9. Evidence

`tests/test_native_bridge.py` (67 tests): complete dual-stream journey with one success notification per
bill and the Drive link; latest-only and no backfill; second run zero effects; newer invoice later; Drive
success independent of email (denied, uncertain and capped email); failure notifications once per
incident with non-sensitive content; flat folder shared by both streams; adoption of an identical
hand-filed file; no adoption for different bytes (including same size) or several same-name files;
the same source name on both streams gets two distinct names; Drive crash, lost answer, corruption, metadata, denied, error, racing seals,
precheck and tampering; junctions; notification draft refusals (recipient, cc, subject, sender, body,
attachment), Core-parsed evidence required, bound draft, undecodable RAW; optional attachment byte check;
Drive and email ceilings; lease; v3 predecessor adoption; named archive; config and CLI refusals; no
network imports in the bridge modules; LOCAL_SYNC (Drive for desktop): upload through the synced folder, second run writes nothing, unsynced upload UNKNOWN then late receipt, record before the mirror exists refused, local name conflict never overwritten, identical Drive file adopted through its mirror, different bytes held.
`tests/test_native_backfill.py`: the latest is left to the schedule; older bills written once under the
owner name; verification after sync; second run fetches nothing; names already in Drive skipped without a
fetch; cap and deferral; a previously newest bill picked up later; no overwrite; fetch failure isolated;
Drive duplicates held; both streams in one flat folder; exact Drive listing evidence; bounded cap;
LOCAL_SYNC required.
`tests/test_production_hardening.py`: P-2 to P-5, P-7 and H-1 to H-6. For the P-7 switch, it covers the following.

- Notification, Gmail ceiling, provenance and backfill history are kept.
- No resend after the switch, and the next run is `IDLE`.
- Each of these is refused with zero effects: a repointed or moved state, a missing or emptied store, in-flight effects, and foreign store rows.
- A source change during the copy or before the rename leaves no v5 file.
- The write lock is held during the copy.
- A crash after the v4 rename fails closed.
- Rollback works only without new effects. With the state file absent it refuses any backup except the one named by the single interrupted copy, and it never leaves the state name empty.
- The audit rows and binding rows are immutable.
- The `NO_DISPATCH` owner notice is staged once, healed once after a crash and never resent when uncertain.
