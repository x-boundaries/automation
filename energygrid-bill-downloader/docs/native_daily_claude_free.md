# EnergyGrid Claude-free daily run (#226 V3 G1)

Status: source design and offline build for Web #226:6233858196 (owner-adopted Toolkit V3 three-role
governance). It carries the historic G1 discovery (#226:6233468958) and the C1-C9 design challenge
(#226:6233675384, Web adjudication #226:6233801525) into code. **It authorises no live effect.** The
installed `af84e547` UAT host, its Claude WORK-profile task and the n8n run monitor stay unchanged until a
separately authorised, verified single-writer cutover (section 9).

## 1. Outcome

The normal daily run uses no model:

    Windows Task Scheduler (11:30 SGT, same interactive user)
      -> egdaily.ps1 (fixed paths, no arguments, admin-write locked)
      -> python -m energygrid_bill_downloader.native_cli daily --config <private native config>
           acquire (EB direct-HTTP, Tenant HTTP + Playwright render; latest only, no backfill)
           Drive steps: LOCAL_SYNC write by the Core, then Google-side verification through n8n
           owner email: at most one attempt per notification through n8n
           status
      -> egmonitor.cmd <run folder> (unchanged reporter and unchanged Telegram run monitor)

Everything the Claude host used to decide is already decided by the Core (`native_bridge.py`,
`native_notify.py`). The daily runner (`native_daily.py`) only drives the same Core functions in a bounded
loop and talks to two authenticated loopback n8n webhooks through `n8n_loopback.py`. The v4 SQLite schema,
the state path and every dependent store (notifications, provenance, backfill) are unchanged and reused.

## 2. Daily configuration block

The private native config (`energygrid.native.v4.config.v1`) gains one optional key, `daily`. Without it
every command behaves exactly as before. With it:

```json
"daily": {
  "schema": "energygrid.native_daily.v1",
  "runs_root": "<absolute private path: EG_HOME\\data\\runs>",
  "drive_reader_account": "<the Google account the n8n Drive credential is bound to>",
  "n8n": {"port": 5678, "token_file": "<absolute private path>", "header_name": "X-EnergyGrid-Monitor-Key"},
  "verify_window_minutes": 20,
  "verify_poll_seconds": 60,
  "run_deadline_minutes": 75
}
```

Refusals at load (closed shape, nothing coerced):

- `drive.mode` must be `LOCAL_SYNC`, and both streams must name the **same** Drive folder (the owner's flat
  folder). Drive Verify lists one fixed folder.
- `gmail.attach_pdf` must be `false` (status-only mail).
- `verify_window_minutes` 1..60, `verify_poll_seconds` 15..300, `run_deadline_minutes` 10..150 and the
  window must fit inside the deadline. With an 11:30 start the run ends before 12:45, well before the
  monitor's 14:00 SGT deadline.
- `header_name` matches `X-[A-Za-z0-9-]{1,60}`; `port` 1..65535; paths are absolute, local and outside the
  checkout, as for every other native path.

The n8n host is fixed to `127.0.0.1` and the webhook paths are fixed in code:
`/webhook/xb-energygrid-drive-verify-v1` and `/webhook/xb-energygrid-owner-email-v1`. The client sends no
proxy-derived traffic (an empty proxy map), follows no redirect, bounds the response size and times out.
C1: both new webhooks reuse the existing `httpHeaderAuth` credential of the run monitor (same host
principal), so `token_file` is the monitor's token file and `header_name` its header.

## 3. Google-side verification (C2, C3)

### 3.1 Drive Verify wire contract

Request (exact keys):

```json
{"schema": "energygrid.drive_verify.request.v1", "nonce": "<32 lowercase hex>"}
```

The workflow takes **no** query input. Its folder id is fixed in the workflow. It runs Drive v3
`about.get?fields=user(emailAddress)` and `files.list` with
`q = '<folder>' in parents and trashed = false`, `supportsAllDrives=true`,
`includeItemsFromAllDrives=true`, `pageSize=1000` and
`fields=nextPageToken,incompleteSearch,files(id,name,mimeType,parents,size,sha256Checksum,md5Checksum,trashed,shortcutDetails)`,
following `nextPageToken` for at most 20 pages. Answer (HTTP 200):

```json
{"schema": "energygrid.drive_verify.response.v1", "nonce": "<echo>", "folder_id": "<fixed folder>",
 "reader_account": "<emailAddress>", "complete": true, "pages": 1, "incomplete_search": false,
 "next_page_token_present": false,
 "files": [{"id": "...", "name": "...", "mimeType": "application/pdf", "parents": ["<folder>"],
            "size": "48211", "sha256Checksum": "<hex>", "md5Checksum": "<hex>", "trashed": false}]}
```

`complete` is true only when the last page had no `nextPageToken` and no page reported
`incompleteSearch`. File objects are passed through as Google returned them (only the listed fields). Any
other status, a malformed body, a refused request (header auth) or a proxy-marked request (section 6) is
not a listing.

### 3.2 What the Core accepts

`record_remote_listing` accepts a listing only with the exact schema, the echoed nonce, `folder_id` equal
to the configured folder, `reader_account` equal to `daily.drive_reader_account` (case-insensitive),
`complete: true`, `incomplete_search: false`, `next_page_token_present: false`, at least one page and
well-formed file entries whose `parents` contain the folder. It writes the canonical listing write-once to
the outbox (`<nonce>.drive-listing.json`) and appends a `drive_remote_listing` row (nonce, folder, listing
SHA-256, file count, time) to the provenance store. Later steps name the listing by nonce; the Core re-reads
the file and re-checks its digest, so no step trusts a host-written listing.

Name occupancy uses the existing case-insensitive display key (`_other_title` semantics): every file whose
name has the invoice's display key occupies the name, including a shortcut, a non-PDF or a different case.

| Occupants of the name | DRIVE_PRECHECK (`drive-seal`) | after the seal (`drive-record`) |
|---|---|---|
| 0 | seal, Core writes the synced file once | not there yet: pending (late-sync path) |
| 1 | `RECONCILE` (adopt only if exact) | exact checks below |
| 2 or more | HOLD `EG_NATIVE_DRIVE_NAME_OCCUPIED` | stop, record nothing (as host rule P1) |

A single occupant is **VERIFIED** only with all of: exact `name`, `parents == [folder]`,
`mimeType == application/pdf`, not trashed, no `shortcutDetails`, `size` equal to the Core byte size,
`sha256Checksum` present and equal to the Core archive SHA-256, and the local mirror holding exactly the
archive bytes. `md5Checksum` and size alone never verify. A missing `sha256Checksum` leaves the intent
pending (refusal, no state change), never VERIFIED. A present but different `sha256Checksum` is REJECTED
(HOLD). For `drive-record` the listing must have been obtained at or after the dispatch seal.

The Core writes the `drive_remote_attestation` row (intent, file id, Google SHA-256, size, listing nonce)
**before** the hash-provenance row and the VERIFIED receipt. A crash between them leaves an attestation
without a receipt, never a receipt without an attestation. The existing provenance `method` stays
`LOCAL_MIRROR_SHA256` (the CHECK constraint is unchanged and nothing is relabelled); `status` adds
`remote_attestation: GOOGLE_SHA256` beside it. The two receipts already VERIFIED on the UAT host keep their
labels (`UNRECORDED_LEGACY`) and are never re-attested.

### 3.3 Enforcement at every Core boundary

Remote verification is **required** when the config has `daily`, and it is latched: the first daily
command writes an append-only `remote_verify_policy` row (`GOOGLE_SHA256_REQUIRED`) to the provenance
store, and from then on every config that opens this state requires it. When required:

- `drive-seal`, `drive-record`, `drive-adopt` and `hold-release LATE_EVIDENCE` accept only
  `{"listing_nonce": ...}` evidence (plus `outcome` for `drive-record`, `mode` for `hold-release`). The
  v2 host-attested evidence (`query`, `matches`, `file`) is refused with `EG_DRIVE_REMOTE_VERIFY_REQUIRED`.
- `drive-readback`, the native Gmail draft path (`notify-draft-evidence`, `notify-seal`) and the manual
  backfill (`backfill-run`, `backfill-record`) are refused (`EG_DAILY_*_DISABLED`).
- The operator command `drive-verify` obtains one listing through the same client and records it; it is
  not in the scheduled launcher's allow-list.

The installed `af84e547` rollback code does not open the new tables, so rollback reads the state unchanged.

## 4. Owner email through n8n (C4, C5)

### 4.1 Effect owner and at-most-once

The Core stays the only decider. For each PENDING notification:

1. **Probe** (no send): `{"schema": "energygrid.owner_email.request.v1", "mode": "PROBE", "nonce": ...}`.
   The answer must echo the nonce, report `ready: true`, name `sender_account` equal to `gmail.account`
   (case-insensitive; the workflow reads it from the sender credential's OpenID userinfo) and give
   `recipient_sha256` equal to the SHA-256 of the lower-cased configured recipient. The workflow's fixed
   recipient is therefore bound to the private config without the address leaving the PC. A failed probe
   seals nothing: the notification stays PENDING and is offered again next run.
2. **Seal** (`seal_n8n`): a write-once outbox dispatch marker `<notification_id>.n8n-dispatch.json` is
   created exclusively, then in one transaction PENDING -> SEALED with `draft_id` set to a Core-minted
   dispatch id `egd-<32 hex>` and the seal-ledger row. A marker that already exists for a PENDING row
   (a restored store, or a crash between the two writes) moves it to HOLD
   (`EG_DAILY_EMAIL_DISPATCH_MARKER_EXISTS`) and nothing is sent.
3. **One POST** (`mode: SEND`), no retry, then `record` with typed evidence
   `{"transport": "N8N_OWNER_EMAIL", "outcome": ..., "draft_id": <dispatch id>, "message_id": ...}`:

| n8n answer | Core outcome |
|---|---|
| 200 `outcome: SENT`, echoed ids, valid `message_id` | SENT |
| 422 `outcome: REFUSED` (validation, render digest, claim refused; before any send) | DENIED -> HOLD |
| 409 `outcome: DUPLICATE` (the workflow already holds this notification id) | ERROR -> UNKNOWN |
| 502 `outcome: SEND_UNCERTAIN`, timeout, lost answer, any other status or body | ERROR / NO_RESPONSE -> UNKNOWN |

UNKNOWN and HOLD are never resent. A crash after the seal leaves SEALED, which the next run makes UNKNOWN.

Why this is genuinely at most once: concurrent or replayed Core runs cannot both seal (the v4 lease
serialises commands and the seal is a compare-and-set inside `BEGIN IMMEDIATE`). The POST happens only
after this process's own seal committed. A restored notification store is caught by the outbox marker. A
restore of both the store and the outbox is caught, sequentially, by the workflow's Data Table claim
(lookup, then insert, before the Gmail node). That claim is **secondary evidence only**: n8n Get and Insert
are not one atomic unique operation, so the workflow does not by itself guarantee uniqueness under
concurrent POSTs. Concurrent POSTs for one notification cannot come from the Core. Restoring Core state
from a backup is prohibited (`runbook.md`); `daily-preflight` re-checks the invariant before cutover.

`seal_n8n` refuses draft ids that look like Gmail ids, and the Gmail draft `seal` refuses `egd-` ids.
`record` requires the `N8N_OWNER_EMAIL` transport for an `egd-` row and refuses it for any other row.

### 4.2 Owner Email wire contract

SEND request (exact keys; `null` where a field does not apply):

```json
{"schema": "energygrid.owner_email.request.v1", "mode": "SEND", "notification_id": "egn-<32 hex>",
 "dispatch_id": "egd-<32 hex>", "kind": "SUCCESS", "stream": "EB_BILL", "status_text": "PROCESSED",
 "support_ref": null, "display_filename": "AUG 2026 - EnergyGrid - EB Bill - x.pdf",
 "drive_file_id": "<id>", "render_sha256": "<hex>"}
```

- Closed vocabularies: `kind` SUCCESS|FAILURE, `stream` EB_BILL|TENANT_BILL, `status_text`
  `[A-Z][A-Z0-9_]{2,63}`, `support_ref` `EG_[A-Z0-9_]{2,72}`, `drive_file_id` `[A-Za-z0-9_-]{10,200}`,
  `display_filename` 1..255 printable characters with no control character, ending `.pdf`.
- SUCCESS needs `display_filename` and `drive_file_id` and has `support_ref: null`. FAILURE needs
  `support_ref` and has `drive_file_id: null`; `status_text: HOLD_RELEASED_NO_DISPATCH` also needs
  `display_filename`.
- The workflow renders subject and body from these fields with the same three templates as
  `native_notify.message_for` and its fixed `SUBJECT_PREFIX`. `render_sha256` is the SHA-256 (hex) of the
  UTF-8 bytes `subject + "\n" + body`. A different digest is refused (422) before any send: the Core and the
  workflow must agree on every byte the owner receives.
- Recipient and sender are fixed in the workflow (the Gmail node uses its own credential's account and a
  fixed `sendTo`), never taken from the request. The Gmail node has no retry and no attribution.

Responses carry `schema: energygrid.owner_email.response.v1`, `mode`, the echoed `notification_id` and
`dispatch_id`, `outcome` and, for SENT, `message_id`.

### 4.3 Legacy history (C5)

- The two historic `SENT` notifications are never re-sent: `dedup_key` uniqueness plus the CAS states
  already guarantee it, and replay tests cover it.
- The v4 `GMAIL_SEND` intents (two legacy PENDING on the UAT host, and one per new invoice) are never read
  for dispatch by the bridge or the daily runner. Tests cover it.
- `daily-preflight` (read-only) requires notification PENDING = 0 and SEALED = 0 and a SUCCESS row for every
  VERIFIED invoice; otherwise it answers HOLD for an owner decision. It never deletes or suppresses a row.

## 5. Failure visibility (C6)

The run monitor and its reporter are byte-unchanged: the monitor alerts on a missing run report by 14:00
SGT and on a bill still PENDING, and stays silent on FAILURE streams because the Core emails them. The
daily runner keeps that contract true:

- It **submits** the run report (the PowerShell script runs `egmonitor.cmd` once) only when every FAILURE
  notification this run called for was delivered (SENT).
- It **withholds** the report when, at the end of the run, a FAILURE notification is still undelivered
  (`EG_DAILY_FAILURE_NOTICE_UNDELIVERED`: n8n email unavailable, probe failed, ceiling reached), or a
  FAILURE notification became UNKNOWN or HOLD in this run (`EG_DAILY_FAILURE_NOTICE_UNCERTAIN`). It also
  withholds when the run itself failed (refused acquire, unexpected error, deadline). The unchanged monitor
  then raises its one missed-report Telegram alert at 14:00 SGT. Nothing else sends a second message for
  the same run.
- An undelivered PENDING notification is retried by the next run (it was never sealed), and the report
  resumes once it is delivered. UNKNOWN and HOLD are never retried.
- Accepted limit (unchanged owner choice): with the PC off or n8n stopped across 14:00 there is no alert.

This uses the owner-selected email channel. It does not move owner notices to Telegram.

## 6. Non-public webhooks (C7)

Header Auth is required on both webhooks. Each workflow's first node after the webhook also refuses
(404, empty body) any request carrying `cf-ray`, `cf-connecting-ip`, `cf-ipcountry`, `x-forwarded-for`,
`x-forwarded-host` or `x-real-ip`. That is defence in depth only. It is **not** the non-public boundary.
The cutover requires one of:

- a validated, read-back Cloudflare rule on the remotely managed tunnel that refuses every request whose path
  starts with `/webhook/xb-energygrid-drive-verify-v1` or `/webhook/xb-energygrid-owner-email-v1` (and the
  `/webhook-test/` and `/webhook-waiting/` equivalents) on every tunnel hostname that reaches n8n; or
- a private-only listener or network for these paths.

Proof at cutover (section 9): a public request to each path is refused at Cloudflare and never reaches n8n
(no n8n execution recorded), and the same-PC authenticated positive control passes. The monitor webhook's own
exposure is a separate follow-up and is not changed here.

## 7. Scheduler and host (C8, C9)

- `runtime/scheduler/egdaily.ps1.template`: no parameters (any argument exits 64), fixed install paths, runs
  the venv Python with `-E -s -X utf8 -m energygrid_bill_downloader.native_cli daily`. It reads the one
  result line and runs `monitor\egmonitor.cmd <run folder>` once only when the line says
  `monitor_report: SUBMIT` and the folder is a direct `YYYYMMDD-HHMMSS` child of the runs root.
- `task-scheduler/energygrid_daily_native.task.example.xml`: disabled, one daily trigger at 11:30 +08:00,
  same interactive user (`InteractiveToken`, LeastPrivilege) because the Tenant DPAPI credential and Drive
  for desktop live in that session, `IgnoreNew`, `StartWhenAvailable`, a hard `PT2H` limit.
- M-5 (C9) is inapplicable to this model-free task only after the install proves: task action and principal,
  the locked no-argument script and installed code (blob-verified), and negative controls showing the
  standard user cannot modify the script, the app folder or the config. M-5 stays in force for the Claude
  rollback route.

## 8. Commands

| Command | Effect |
|---|---|
| `daily` | The whole run (section 1). One JSON line with `run_dir` and `monitor_report`. Exit 0 done, 10 retry later, 20 HOLD/uncertainty/withheld, 64 refused. |
| `daily-preflight` | Read-only cutover check: config, latch state, notification invariants (section 4.3), n8n health, both probes and one Drive listing that must show the latest VERIFIED invoice of each stream with a matching Google SHA-256. Records only the listing evidence. |
| `drive-verify` | Operator: obtain and record one listing; prints its nonce and counts. |

## 9. Cutover holds and order (each step needs its own grant)

Typed holds (none blocks the source PR or offline UAT):

- `C1_DRIVE_READ_CAPABILITY_UNPROVEN`: an existing Google credential must be proved (service Google, exact
  scope text, OAuth client Internal or In production, bound account via `about.get`, list access to the
  folder). n8n 2.39.10 supports `googleDriveOAuth2Api` with `customScopes` and a free-text scope on the
  generic `googleOAuth2Api`; reuse a qualifying credential, otherwise the smallest new binding is
  `drive.metadata.readonly` on the account that can read the folder (owner consent).
- `C1_OWNER_EMAIL_SEND_CAPABILITY_UNPROVEN`: the Gmail node at 2.39.10 accepts `gmailOAuth2` (or a service
  account) only. Reuse a `gmailOAuth2` credential whose account equals `gmail.account`; otherwise the smallest
  new binding is `gmail.send openid email` on that account (owner consent).
- `C7_NONPUBLIC_INGRESS_UNVERIFIED`: section 6.
- `C8_WORK_PROFILE_TASK_UNVERIFIED`: the WORK-profile Claude task is disabled and read back before the
  Windows task is enabled; never two writers.
- Existing: production effect ceilings are not chosen (the daily needs `PER_PERIOD`), the n8n Data Table for
  the email claim must be created, image digests recorded.

Order: source PR reviewed and merged -> credentials qualified -> both workflows imported **inactive**, Data
Table created -> C7 rule read back -> install `app\<merge commit>` beside `af84e54`, render and lock
`egdaily.ps1` -> `daily-preflight` passes -> activate both workflows -> disable and read back the Claude
task -> register the Windows task disabled, run it once by hand (expected IDLE, zero effects, monitor OK)
-> enable at 11:30.

Rollback: disable the Windows task -> deactivate the two workflows -> re-enable the WORK-profile Claude task
on the unchanged `af84e54` launcher. State is untouched apart from additive rows in the provenance and
notification stores, which `af84e54` does not read for dispatch.
