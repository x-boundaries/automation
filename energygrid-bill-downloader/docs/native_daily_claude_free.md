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
every command behaves exactly as before. The daily config is its **own file**,
`runtime\config\native.daily.json` (the classic config plus `daily`, same state path). The classic
`runtime\config\native.json` that `egnative.cmd` reads stays byte-unchanged: the installed `af84e547` code
refuses any config that carries `daily` (offline proof, 17 Oct 2026: `ConfigError`), so writing the block
into the shared file would stop the Claude route before it is disabled. With `daily`:

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

The latch is enforced by this code only. The installed `af84e547` code has no latch table and does not read
it: offline proof (17 Oct 2026) on a state latched by `daily`, with a classic config, `af84e547` acquired a
new EB bill and **sealed its upload on host-attested evidence**. Running `af84e547` against a latched state
therefore bypasses Google-side verification; it is not a rollback (section 9.2).

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
| 401, 403 or 404 (Header Auth refused or no active webhook: the workflow never ran), or connection refused | DENIED -> HOLD |
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

### 4.4 Workflow build notes (official n8n Skills 1.2.0)

Both exports were checked against the official `n8n-io/skills` 1.2.0 guidance (error handling, Data
Tables, node configuration, HTTP pagination, the lifecycle antipattern scan) and the n8n 2.39.10 node
sources. Deliberate choices:

- Every fallible node's error output is wired and every path ends in a Respond node with an explicit,
  cause-mapped code (4xx caller, 409 duplicate, 502 upstream, 500 internal).
- The read-only Google GETs retry at most 3 times (5 s apart). The Gmail send never retries and the claim
  writes do not retry: at most one attempt.
- No workflow-level error workflow is set: every failure answers the Core, which is the single owner of
  owner visibility (section 5). An error workflow would add a second notifier.
- Request validation and the template render stay in one Code node, as in this repository's other
  exports: closed-vocabulary checks across many fields plus three templates are not a single expression.
  Small answer builders are expression-driven Respond nodes, and the render check is an IF node.
- Data Table columns are camelCase (`notificationId`, `dispatchId`, `state`).
- Not yet done (needs the instance): `validate_workflow` and `get_workflow_details` through the n8n MCP,
  and the import-time checks of the official lifecycle checklist. These run at the inactive import step.

### 4.3 Legacy history (C5)

- The two historic `SENT` notifications are never re-sent: `dedup_key` uniqueness plus the CAS states
  already guarantee it, and replay tests cover it.
- The v4 `GMAIL_SEND` intents (two legacy PENDING on the UAT host, and one per new invoice) are never read
  for dispatch by the bridge or the daily runner. Tests cover it.
- `daily-preflight` requires notification PENDING = 0 and SEALED = 0 and a SUCCESS row for every VERIFIED
  invoice; otherwise it answers HOLD for an owner decision. It never deletes or suppresses a row. It is
  **stateful**, not read-only (section 8).

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
  **contains** `xb-energygrid-drive-verify-v1` or `xb-energygrid-owner-email-v1`, **case-insensitively**, on every
  tunnel hostname that reaches n8n (path regex `(?i)xb-energygrid-(drive-verify|owner-email)-v1`, service
  `http_status:404`, ordered before the n8n catch-all). A prefix rule anchored on `^/webhook...` is **not**
  enough: n8n routes `/WEBHOOK/...`, `/WEBHOOK-TEST/...` and `/Webhook-Waiting/...` case-insensitively, so
  case variants passed a case-sensitive Cloudflare rule and reached n8n (owner UAT, 18 Oct 2026); or
- a private-only listener or network for these paths.

Proof at cutover (section 9): a public request to each path is refused at Cloudflare and never reaches n8n,
and the same-PC authenticated positive control passes. The workflows save no executions, so "no execution
recorded" is not evidence on its own: the proof is the response fingerprint (Cloudflare's empty 404 versus
n8n's own JSON or HTML 404), with an unregistered control path that must reach n8n. The probe set covers the
canonical paths, `-test`/`-waiting`, trailing slash, query, suffix, upper and mixed case, percent-encoding,
repeated slashes, dot segments and backslash, both with the workflows inactive and active. The monitor webhook's own
exposure is a separate follow-up and is not changed here.

## 7. Scheduler and host (C8, C9)

- `runtime/scheduler/egdaily.ps1.template`: no parameters (any argument exits 64), fixed install paths, runs
  the venv Python with `-E -s -X utf8 -m energygrid_bill_downloader.native_cli daily --config
  runtime\config\native.daily.json` (its own config file, section 2). It reads the one
  result line and runs `monitor\egmonitor.cmd <run folder>` once only when the line says
  `monitor_report: SUBMIT` and the folder is a direct `YYYYMMDD-HHMMSS` child of the runs root.
- `task-scheduler/energygrid_daily_native.task.example.xml`: disabled, one daily trigger at 11:30 +08:00,
  same interactive user (`InteractiveToken`, LeastPrivilege) because the Tenant DPAPI credential and Drive
  for desktop live in that session, `IgnoreNew`, `StartWhenAvailable`, a hard `PT2H` limit.
- M-5 (C9) is inapplicable to this model-free task only after the install proves: task action and principal,
  the locked no-argument script and installed code (blob-verified), and negative controls showing the
  standard user cannot modify the script, the app folder or the config. M-5 stays in force for the Claude
  rollback route.
- **Every executed file counts, including its parents** (G2 CB1, 18 Oct 2026). Locking the runner and app is
  not enough when the venv, its base CPython, Chromium or the monitor are user-writable, or when any parent
  folder can be renamed by the standard user (the folder could then be swapped for another tree at the same
  path). The production install therefore uses one admin-owned `EG_HOME` under `C:\Program Files\EnergyGrid`
  holding the runner, the app slot, the configs, a dedicated CPython (administrative MSI extraction of the
  signed release, no registration), a venv with hash-locked wheels, the Playwright Chromium and the monitor.
  Only its `data` folder (runs and the runner log) is writable by the task principal. State, archives and
  outbox stay at their own path (data, not code). Proof required at install: every installed item and every
  parent refuses write, delete, rename, WRITE_DAC and WRITE_OWNER for the non-elevated principal, and the
  interpreter's `sys.path` is closed inside the tree.

## 8. Commands

| Command | Effect |
|---|---|
| `daily` | The whole run (section 1). One JSON line with `run_dir` and `monitor_report`. Exit 0 done, 10 retry later, 20 HOLD/uncertainty/withheld, 64 refused. |
| `daily-preflight` | **Stateful** cutover check (it does not latch): config, latch state, notification invariants (section 4.3), n8n health, both probes and one Drive listing that must show the latest VERIFIED invoice of each stream with a matching Google SHA-256. |
| `drive-verify` | Operator: obtain and record one listing; prints its nonce and counts. Stateful in the same way. |

`daily-preflight` is not a read-only command. Against a state it can:

- create the provenance store's remote-verification tables and triggers if they are absent (additive SQLite
  schema in `<state>.provenance.sqlite`);
- write one listing file to the outbox and one `drive_remote_listing` row;
- run two live n8n executions: an Owner Email `PROBE` (no send) and Drive Verify (a live Google read).

Run it only against the production state under an explicit grant that names those effects (UAT step 7),
never under a read-only grant.

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
  first `daily` against the production state; never two writers.
- `D1_POST_LATCH_ROLLBACK_POLICY_UNDECIDED`: the owner's recorded choice in section 9.3, before UAT step 9.
- Existing: production effect ceilings are not chosen (the daily needs `PER_PERIOD`), the n8n Data Table for
  the email claim must be created, image digests recorded.

### 9.1 Order (Web #226:6234979007 and #226:6235489184)

Source review and UAT preparation need **no merge and no scheduler activation**. The unmerged PR head is
installed in an isolated, inactive candidate slot beside `af84e54` and owner UAT runs on it. The fresh
final V3 G2 follows the owner's UAT acceptance. Merge, promotion and the 11:30 schedule come after that,
each with its own approval and read-back:

1. Owner-PC UAT on the exact candidate head (section 10, steps 1 to 9). The Windows task is not registered
   or enabled.
2. Owner UAT acceptance (an explicit statement, never inferred).
3. Fresh, independent final V3 G2 on the exact candidate.
4. Web: merge, then guarded promotion: install the reviewed commit (blob-verified; the candidate slot when
   it is the same tree), register the Windows task **disabled**, run it once by hand (expected IDLE, zero
   effects, monitor accepted), then enable at 11:30 and observe the next bill end to end.

Between UAT step 9 and the 11:30 enable nothing files bills on a schedule. When a bill is due, the owner
runs the attended `daily` of section 9.2, which is the same command the task will run.

### 9.2 Recovery after the latch (tested)

The first `daily` against a state writes the append-only latch (section 3.3), whatever its outcome,
including IDLE. From then on this code verifies every Drive step by Google's SHA-256 under **every**
config. The Claude bridge commands are refused with no state change (`EG_DRIVE_REMOTE_VERIFY_REQUIRED`,
`EG_DAILY_READBACK_DISABLED`, `EG_DAILY_GMAIL_DRAFT_PATH_DISABLED`, `EG_DAILY_BACKFILL_DISABLED`). The
latch and the provenance rows cannot be updated or deleted (triggers) and are never cleared by hand.

- **The recovery route is the native route itself.** If a run fails (scheduler, n8n, Google, Gmail),
  fix the cause and run `daily` once by hand: `egdaily.ps1` with no arguments, or the Windows task's Run.
  Offline proof `tests/test_native_daily.py::PostLatchRecoveryTests` covers this. New EB and Tenant bills
  found while Drive Verify is down are acquired and archived, but nothing is written to the synced folder,
  filed or emailed. Status says PENDING, so the unchanged monitor alerts. A classic-config Claude bridge
  session in between is refused and changes nothing, and the latch survives it. After the repair, one
  attended `daily` files each new bill once, VERIFIED by Google's SHA-256, with one email each. A further
  run makes zero effects. A listing without Google's SHA-256 never verifies a bill (md5 or size alone never
  do); the next run records it once Google reports it.
- **Never run a manual `daily` while the scheduled one is running** (G2 H2). Task Scheduler's `IgnoreNew`
  only stops a second *scheduled* instance, and the Core's v4 lease serialises each command, not the whole
  run. Two overlapping runs still file one Drive copy and send one success email, but the second can record a
  spurious FAILURE notice. Before a manual run, check that the task is not `Running` (Task Scheduler, or
  `Get-ScheduledTask -TaskPath '\XB\' -TaskName 'EnergyGrid Daily Bills Native'`), and that no newer run
  folder than the one you are recovering exists under the runs root.
- **Fail-closed limitation.** While Drive Verify cannot answer (n8n down, Drive credential revoked, Google
  unavailable), no new bill reaches the Drive folder or the owner email. No verification-preserving
  fallback route exists. The bill waits in the internal archive, visible to the monitor, until the native
  route is repaired.
- **The WORK-profile Claude task is not a rollback after the latch.** The installed `af84e547` code does
  not read the latch. On a latched state it would seal a new bill on host-attested evidence (offline
  proof, section 3.3), which bypasses Google-side verification. With `native.daily.json` separate, its
  classic `native.json` still loads, so nothing stops it technically. After UAT step 9 it stays disabled.
  Re-enabling it is the owner policy downgrade of section 9.3 (c), never a rollback step.
- Never restore the state, the notification store, the provenance store or the outbox from a backup, and
  never re-seal or re-send by hand (`runbook.md`).

### 9.3 Rollback by phase and the owner decision `D1`

- **Before the first `daily` on the production state** (UAT steps 1 to 8): genuine rollback. Deactivate
  the two workflows, leave or remove the candidate slot and `native.daily.json`, and keep (or re-enable)
  the WORK-profile Claude task on the unchanged `af84e54` launcher. The only state additions are the
  stateful preflight's listing rows, tables and files, which `af84e547` never reads.
- **After it** (UAT step 9 onward): section 9.2. Disable the Windows task if one exists and deactivate the
  workflows only to stop the native route; nothing old resumes by itself.
- Earlier offline proof (G1 builder, 17 Oct 2026): after two synthetic daily runs with no new bill,
  `af84e547` reopened the state with the classic config: `DRAINED`, both bills `ALREADY_ADMITTED` and
  VERIFIED, `next` = `IDLE`, two SENT notices, no extra dispatch. It shows that settled history is read
  unchanged. It does not prove recovery for a bill that arrives after the latch (Web #226:6235489184). That
  case is the bypass in section 3.3.

`D1`, the owner's recorded choice before UAT step 9:

- (a) **Native-only recovery after the latch** (recommended). The WORK-profile Claude task retires from
  bill filing at UAT step 9. Recovery is fix-forward with the attended `daily`. During a Drive Verify outage
  the section 9.2 limitation applies. If final G2 later rejects the candidate, a return to `af84e547` is a
  new owner decision under (c), not an automatic rollback.
- (b) As (a), but UAT step 9 first runs against an isolated copy of the state in the candidate slot, so
  the production state stays unlatched until promotion. Copying private state needs its own grant.
- (c) Keep the Claude task as an emergency fallback after the latch. The owner then explicitly accepts
  host-attested (not Google SHA-256) verification for any bill it files. Each use is recorded in #226.

## 10. Owner UAT on the unmerged candidate (owner PC `DESKTOP-Q43QKQF`)

Each step names who acts and what it may change. Steps 1 to 3 change nothing live. Steps 4 to 5 had their own
owner grants. Web #226:6246056987 then replaced the per-step approvals with **one continuous, owner-authorised
UAT on an isolated state** (section 10.1). That authority does not cover the production-state steps 8 and 9,
the 11:30 schedule, Claude retirement or merge.

| Step | Who | Effect | Pass when |
|---|---|---|---|
| 1. Offline suite on the owner PC from the exact candidate head | G1 | none | all EnergyGrid suites green with the installed venv Python |
| 2. C1 read in the n8n UI: for each candidate existing credential, report service, exact scope text, OAuth client publishing status, and whether its account can open the flat folder (Drive) or equals the configured sender (Gmail) | owner | none (read only) | a Drive reader and a Gmail sender credential qualify, or the owner approves the smallest new one |
| 3. C7 read-back of the remotely managed Cloudflare tunnel's ingress rules for the n8n hostname | owner | none (read only) | the exact rule set is known |
| 4. Create the email claim Data Table; import both workflows **inactive**, with placeholders replaced and credentials bound in a local import copy | owner/G1 | n8n data and inactive workflows | the workflow JSON read back shows the bindings; MCP access stays off |
| 5. Add the Cloudflare path exclusion (section 6) | owner | Cloudflare | a public request to each path is refused at Cloudflare and n8n records no execution |
| 6. Install the candidate slot `app\<candidate head>` beside `af84e54` (blob-verified), render and lock `egdaily.ps1`, write the private `native.daily.json`; the classic `native.json` and the WORK task stay unchanged; register no Windows task | owner/G1 | host files | standard-user negative controls cannot modify script, app or configs; `af84e54` still loads `native.json` |
| 7. Activate both workflows, then `daily-preflight` (**stateful**: provenance tables, one listing row and file, n8n PROBE and Drive Verify executions; section 8) | owner/G1 | n8n activation, one Google read, additive local evidence | `PASS`: no PENDING or SEALED notice, both probes ready, Google SHA-256 matches the latest bill of each stream |
| 8. Record `D1` (section 9.3); disable the WORK-profile Claude task and read it back | owner | scheduler | `D1` recorded; disabled state read back |
| 9. One attended `daily` (run `egdaily.ps1` by hand; the first latch, section 9.2) | owner/G1 | one daily run (normally IDLE) | `daily.json` IDLE, zero Drive or email effects, monitor report accepted |

UAT acceptance is the owner's statement after step 9 (and after the first real bill, when the owner wants
that in UAT). Then: final fresh V3 G2, then section 9.1 item 4. Rollback before step 9 is section 9.3;
from step 9 onward it is section 9.2.

### 10.1 Continuous owner UAT on an isolated state (18 Oct 2026, Web #226:6246056987)

Candidate `37322b2` was installed in its own admin-locked slot beside `af84e54` (blob-verified). The locked
no-argument `runtime\egdaily.ps1` reads a `native.daily.json` that points **only** at an isolated UAT state, its
own archive, outbox and runs root, and a temporary UAT Drive subfolder. Ceilings: `LIFETIME`, 2 Drive uploads
and 2 owner emails. The classic `native.json`, `egnative.cmd` and the production state stayed byte-identical,
and the production state was never latched. Every run used the installed runner exactly as the Windows task
action would. No Windows task was registered.

| Run | Posture | Result |
|---|---|---|
| R1 | Drive Verify and Owner Email inactive | real EB and Tenant portal acquisition (latest bill each), archived; Drive Verify 404, nothing written to Drive, no email; status PENDING (the monitor would alert) |
| R2 | Drive Verify active, Owner Email inactive | each bill sealed, written once by the Core, VERIFIED by Google's SHA-256; PROBE failed, so both notifications stayed PENDING, nothing sent |
| R3 | both active | PROBE READY; exactly one SUCCESS email per bill (2 SENT, owner received both); DRAINED |
| R4 | replay | IDLE, zero Drive or email effects |

Independent readback: the UAT Drive folder holds exactly two PDFs, each with Google SHA-256 equal to the Core
archive and the synced bytes; the n8n claim table holds two SENT rows whose dispatch ids match the Core. The
runner refused an argument (exit 64), and it never submitted a UAT run folder to the production monitor
(`RUN_DIR_REFUSED`, because the UAT runs root is not the production runs root). Each run was classified by
the unchanged monitor reporter in `--dry-run` (no POST): R1 PENDING, R2 to R4 OK.

Found and fixed during UAT:

- `init-uat` failed (`IntegrityError`) when the Tenant source is configured: the legacy v2 stream table can bind
  only `DIRECT_HTTP_V1`. Tenant now stays UNBOUND there, as on the production state; the Core binds Tenant
  through `tenant_config_path` at acquire (`37322b2`, with a test).
- C7: the Cloudflare rule must be case-insensitive and unanchored (section 6). The owner changed it, and 54 of
  54 public variants were then refused at Cloudflare with the workflows inactive and active.

Correction (G2 H3): notification ids are deterministic per invoice, so the two UAT claim rows carry the
**same** ids as production's two historic SENT notices for the same AUG bills; they do not "never collide".
They are terminal and are kept on purpose as an extra duplicate barrier. Never delete them or re-send.

Decisions taken afterwards: the owner accepted the UAT (#226:6248016743) and kept the n8n Gmail credential's
account (the Drive and recipient account) as the production sender instead of the classic config's
`gmail.account`. The final G2 passed the source with cutover holds (#226:6248548156). CB1 was closed by the
protected runtime (section 7), the OAuth app is Internal (owner attestation), and the owner accepted the
default (broader) Drive and Gmail OAuth scopes for the first release (least-privilege narrowing is a later item).
The UAT Drive subfolder, UAT state and staging copies were removed to the recycle bin and Drive trash after
their evidence was archived privately (19 Oct 2026).

### 10.2 Production cutover (19 Oct 2026, owner grant #226:6251345304)

PR #253 merged as `2749a27` (tree identical to the reviewed head). The protected production
`native.daily.json` binds the original state, archive, outbox and flat folder, EB and Tenant, the accepted
sender and `PER_PERIOD` 1 day / 4 uploads / 6 emails, with `runs_root` = the protected `data\runs`. Both
workflows were activated; C7 (54 of 54 public variants refused) and C1 were re-proved and the stateful
`daily-preflight` passed with no hold. The WORK-account Claude routine was paused, then deleted by the owner.
Task `\XB\EnergyGrid Daily Bills Native` was registered disabled, one attended `daily` ran (IDLE, latch
`GOOGLE_SHA256_REQUIRED` written, real monitor report accepted), then the task was enabled at 11:30 SGT.
From here on, recovery is native-only fix-forward (section 9.2); the old route is never resumed.
