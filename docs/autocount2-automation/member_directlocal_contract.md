# Member DirectLocal Contract (#155)

Status (2026-10-15): real-operation Core source for the development book
(`AED_TESTINGAPI`), built under #155:6195037656 and the scope delta
#155:6195382795 / 6195468717 / 6195563312. This document authorises no live
effect. Shopify is read-only (automation-originated Shopify writes are
forbidden). Every AutoCount login, `GetNextMemberNo`, `GetMember` or
`SaveMember` on any book, any production access and any scheduling still needs
its own scoped Owner approval. The previous gateway/n8n/PostgreSQL path is not
used and its files are unchanged. Operating steps: [UAT runbook](member_directlocal_uat_runbook.md).

## Ledger modes, environment binding and inertness

The mode, environment and book are fixed by `init-ledger` and can never change
(SQLite `CHECK` constraints and triggers).

| Mode | Environment / book | Bridge accepted | Store |
|---|---|---|---|
| `synthetic` | `synthetic` / none | Only the test-only `SYNTHETIC_BRIDGE_TARGET` fake; the real `BridgeClient` is refused (`real_autocount_refused_synthetic_ledger`) before any spawn or state change | `*.synthetic.invalid` |
| `connector` | `development` / `AED_TESTINGAPI` | Only a real bridge bound to the same book (`bridge_book_binding_mismatch` otherwise), and only while the state root is private | The qualified store pin `gid://shopify/Shop/48759472277` / `xboundariessg.myshopify.com` |
| `connector` | `production` / `AED_XBOUNDARIES` | Refused at init (`production_environment_not_authorised`). Gate E9 needs a separately approved source change | - |

Bridge entrypoint (`ac2_directlocal_member_bridge.ps1`): without
`-SessionFactory` and without `-TargetBook` it refuses
(`autocount_real_bridge_disabled`) before stdin is read, exactly as before. A
synthetic factory with `-TargetBook` is refused. With `-TargetBook`,
`ac2_directlocal_book_guard.ps1` runs, in order, before any member operation:
authorised book (`AED_TESTINGAPI` only; `production_book_not_authorised`), host
`DESKTOP-4I042L6`, no `XB_AC2_SESSION_FACTORY`, assembly pins equal to the E7
qualification, book pinned for the process, vendor login, telemetry off,
`DBSetting.DBName` = `DB_NAME()` = book and `MachineName` = host, then empty
`UserScript`/`MailQueue`/`EInvoiceQueue`. A failure before the claim leaves no
claim, so the parent proves `NOT_WRITTEN`. After `SaveMember` the queues are
re-read; any row (or an unreadable queue) makes the fence
`CREATED_OUTBOUND_QUEUE_CHANGED` (manual review).

Credentials reach the bridge only from `scripts/ac2_directlocal_owner_console.ps1`
(interactive, elevated, fresh `ISOLATED` evidence in `<state_root>\evidence`,
exact source head, credentials typed into a `SecureString` and passed only to
the child process environment).

## Data path

```text
Attended Claude session (dedicated folder, settings from the template)
  -> python directlocal/xb_directlocal.py next-request   (exact pinned tool input)
  -> connector graphql_query (PreToolUse guard: pinned queries only)
  -> PostToolUse hook writes hook-delivered JSON to <state_root>\inbox
python directlocal/xb_directlocal.py  (one OS-locked owner process)
  -> strict capture checks -> SQLite ledger <state_root>\ledger.sqlite3
  -> reused shopify_admission.capture_member_mg_baseline / evaluate_profile
     on the registration view (see below)
  -> powershell.exe -File scripts\ac2_directlocal_member_bridge.ps1 -TargetBook AED_TESTINGAPI
       -> ac2_directlocal_book_guard.ps1 -> ac2_member_gateway_autocount_adapter.ps1 (unchanged, shopify_m1)
```

| File | Role |
|---|---|
| `directlocal/xb_directlocal.py` | Capture parsing, ledger, custody, baseline/discovery, registration events, admission, standing policy, allocation, fence, recovery, reconcile, retention, UAT evidence, CLI |
| `scripts/ac2_directlocal_member_bridge.ps1` | Child bridge: `book-check`, `pos-sync-status`, `legacy-precheck`, `next-member-no`, `probe`, `reconcile`, `write-once` |
| `scripts/ac2_directlocal_book_guard.ps1` | Real-session guard (above) and the read-only POS sync state |
| `scripts/ac2_directlocal_owner_console.ps1` | Owner console for `BookCheck`, `PosSyncStatus`, `Process`, `Reconcile` |
| `scripts/directlocal_connector_hook.py` | `guard` (PreToolUse default-deny for MCP) and `capture` (PostToolUse inbox writer) |
| `config/directlocal_claude_settings.template.json` | Dedicated-session settings template (deny list + hooks) |
| `tests/test_member_directlocal*.py`, `tests/test_directlocal_*_ps.py` | Offline tests |

## Capture provenance and shapes

- A capture is the structured JSON Claude Code hands to the PostToolUse hook
  (`claude_code_posttooluse_hook_delivered_json`). It is never described as
  Shopify wire bytes.
- Pinned queries (SHA-256 of the exact text): `identity`, `baseline`
  (exhaustive, `sortKey: ID`), `recent` (`sortKey: UPDATED_AT`, one search
  `updated_at:>='<UTC>'`), `profile`. `first = 50` in tool input and
  variables; `after` must match; GID-shaped ids; exact keys; `shop` must equal
  the ledger's store.
- `connector` ledgers accept only `connector_graphql_json_text_v1`: a JSON
  text decoding to exactly `{"data": {...}}` (qualified 2026-10-14, re-observed
  2026-10-15). Objects, `errors` (expired authentication, throttling, partial
  results), `extensions`, plain-text failures (disconnected server) and any
  other wrapper are rejected; a rejected capture blocks `ingest-run` and
  `next-request` until `purge-inbox` removes it.
- Residual risk: the hook is not tamper-proof against an agent running as the
  same Windows user; acceptable only for attended operation.

## Registration event (G0, 2026-10-15, read-only)

Observed through the connector (identifiers and timestamps only):

- Registrations come from the **Shopify Forms** app (form `1178308`): the app
  creates or matches the customer and adds `member-mg` and
  `shopify-forms-1178308`; about three seconds later the customer metafields
  `membership.start_date`, `membership.expiry_date` and `membership.source`
  (`mg-qr`) are created. Expiry was exactly start + 2 years in the samples; the
  Core copies both dates unchanged and never recomputes them.
- Two of the four newest registrations were **existing customers** (created
  2024-07 and 2026-03, enrolled 2026-10-14/15). A `Customer.createdAt` cutover
  would wrongly send them to review.
- The Forms submission itself is not visible to this connector (no Forms-owned
  metaobject definition is exposed), and the Google Sheet `Members (from
  Shopify)` has only seven human headers (no GID, submission id or timestamp;
  #225). Neither is an event source.

Rule implemented: the registration event is `(customer GID,
membership.start_date metafield GID, its createdAt)`.

- Baseline and discovery partition `member-mg` GIDs by the registration
  instant (the metafield's `createdAt`; `Customer.createdAt` only while the
  metafield is not yet written). `>= C` is new; `< C` is historical.
- The profile decision runs the unchanged `evaluate_profile` on a view whose
  `createdAt` is the registration instant, so an existing customer who
  registers after `C` is admitted and a baseline member stays excluded.
- The first read binds the event (immutable). A later read naming another
  metafield or instant goes to review (`registration_event_changed`).
- One AutoCount member per customer GID, ever (unchanged fence rules).

## Discovery runs

- `begin-baseline` fixes `C` before any page; one non-abandoned baseline;
  exhaustive scan; sealed runs never change (unchanged).
- `begin-discovery` repeats the exhaustive scan (about 90 connector calls for
  the current 4,336 customers).
- `begin-discovery --recent` reads only customers updated since a watermark:
  the cutover for the first recent run, then the last sealed discovery start
  minus 15 minutes (search-index lag margin). The search string is stored with
  the run and every page must carry it. Periodic exhaustive discovery remains
  the backstop for index lag.

## Admission, duplicates, MemberNo and text

- `evaluate_profile` unchanged: `member-mg`, legacy tags, exact dates, bounded
  wait. Captures are single-use (hash and `tool_use_id`) and must be <= 900 s old.
- Text AutoCount cannot store exactly goes to `MANUAL_REVIEW` at admission,
  before any lookup: widths are counted in UTF-16 code units, as `nvarchar`
  counts them (Name 100, MobilePhone 25, EmailAddress 200), so a character
  outside the BMP counts twice (`autocount_field_width_exceeded`); C0 controls,
  DEL and unpaired surrogates are `autocount_text_invalid`. The payload crosses
  the process boundary as ASCII-escaped JSON, so CJK and astral characters
  round-trip exactly (tested through the real bridge).
- Duplicate precheck, symmetric `65xxxxxxxx` <-> `xxxxxxxx` phone keys, exact
  email, MemberNo allocation (<= 3 `GetNextMemberNo` per customer, ever) and
  re-probe are unchanged.

## Write authority

- Attended: `approve-write --gid <gid> --confirm-suffix <trailing digits>`
  (single use, <= 30 minutes), as before.
- Standing policy (#155:6195468717): `activate-policy --confirm-book <book>
  --max-creates N --ttl-hours H` (connector ledger, authorised environment
  only; N <= 500, H <= 168; one live policy; immutable except revocation).
  While live, `process --enable-autocount-write` mints one single-use approval
  (5 minutes) for an `ADMITTED` customer, at most once per customer and at
  most N per policy, then runs the unchanged write path. `revoke-policy` ends
  it. Duplicates, ambiguity, invalid fields, crashes and uncertain outcomes
  still go to review or HOLD; nothing ever starts a second `SaveMember`.
- Production activation of a policy is impossible in this source (E9).

## Write fence and recovery

| Step | Durable state |
|---|---|
| Approval consumed, fence inserted | `FENCED` (one per customer, one per MemberNo) |
| Before the child starts | `DISPATCHED` + deadline |
| Child: digest + fence check, real-session guard, exclusive `<fence>.claim`, `SaveMember`, read-back, queue re-check, `<fence>.outcome` | claim/outcome files |
| Parent classifies | `CREATED_VERIFIED`, `CREATED_READBACK_MISMATCH`, `CREATED_OUTBOUND_QUEUE_CHANGED`, `WRITE_OUTCOME_UNCERTAIN`, `NOT_WRITTEN_PROVEN` or `WRITER_TERMINATION_UNCONFIRMED` |

The writer outcome must carry `save_invocation_count = 1` and the booleans
`readback_found`, `readback_match` and `outbound_queue_changed`; anything else
is uncertain. Recovery, HOLD, `release-writer-hold` and read-only `reconcile`
are unchanged.

## State root custody and retention

- `prepare-state-root` creates a new root (refuses an existing path, UNC,
  `%LOCALAPPDATA%`, the repository): inheritance removed; only the current
  user, SYSTEM and Administrators granted; `inbox`, `claims`, `evidence`.
- A connector ledger re-verifies on every command (owner and DACL read
  through `GetNamedSecurityInfoW` as SDDL): the root must be protected and no
  ACE on the root, `inbox`, `claims`, `evidence` or the ledger may grant
  anyone else (`state_root_acl_inherited` / `_unsafe` / `_owner_unsafe`).
- `purge-inbox` removes consumed captures, rejected captures, profile
  captures past 900 s, pages no open run can use and stale hook temporaries;
  only counts are reported. Profile files are also deleted after use.
- The ledger keeps GIDs, MemberNos, registration ids/instants, counts, codes
  and salted digests only; events are hash-chained and append-only.
- Schema v3 ledgers only; a v2 (synthetic-only) ledger is refused, not migrated.
- Residual: profile responses pass through the Claude session (transcript and
  model). Owner consent #155:6195382795 covers the minimum fields.

## Keyed references and evidence

- Public output carries `c-` (customer) and `m-` (member) references:
  HMAC-SHA256 under a per-ledger random 256-bit key, 80 bits shown. They cannot
  be reversed by enumerating GIDs or MemberNos, and differ between ledgers.
  Only `pending` and `next-request` print GIDs (local operator identifiers).
- `uat-report`: states, codes, refs and latency seconds from the registration
  instant to discovery, fence, outcome and each recorded POS observation.
- `record-pos-visible --member-ref --outlet --observed-at`: operator-attested,
  append-only; only for a verified create; never a write trigger.
- `pos-sync-status` (Owner console): HQ-side outlet sync liveness and the
  `Member` change counter (no member data).

## POS synchronisation (G0, 2026-10-15, read-only)

- HQ: `AED_XBOUNDARIES` on this VM with AutoCount POS 5.2 Backend and the
  `AutoCount Pos Sync Server - XBOUNDARIES` service. Outlet front-end databases
  register in `SyncRegistration`: `XB01` (`AED_XBOUNDARIES_FE`, live) and `XB05`
  (`AED_XBOUNDARIES_JBM_FE`), program 5.2.16.33; terminals `T01`, `XB01`, `XB02`
  (outlet `XB03`), `XB05`; no member-type terminal filter.
- `Member` has an active `ChangeCount` counter (the incremental sync source).
  Outlet XB01 started sync cycles at 23:19:47, 23:34:47 and 23:49:47 SGT (each
  finished in under 0.1 s): a 15-minute
  interval set on the outlet (not stored at HQ), with liveness pings between.
  So a member saved at HQ becomes selectable at POS only after the outlet's next
  cycle (up to about 15 minutes) unless the outlet syncs on demand.
- `AED_TESTINGAPI` has no live POS registration (its rows predate the clone),
  so POS selectability cannot be proven on the test book.

## Synthetic command sequence

```powershell
python directlocal/xb_directlocal.py --state-root <root> init-ledger --mode synthetic --store-gid gid://shopify/Shop/900000001 --shop-domain xb-test.synthetic.invalid
python directlocal/xb_directlocal.py --state-root <root> begin-baseline
python directlocal/xb_directlocal.py --state-root <root> ingest-run
python directlocal/xb_directlocal.py --state-root <root> process --capture <root>\inbox\<id>.json
python directlocal/xb_directlocal.py --state-root <root> status
```

## E7 test-book path (one synthetic member on AED_TESTINGAPI)

Separate from the DirectLocal core. Consumed on 2026-10-15; its private intent, fence, claim and outcome records are retained unchanged and the script refuses any re-run.

| File | Role |
|---|---|
| `scripts/ac2_directlocal_e7_isolation_check.py` | Read-only, fail-closed outbound-isolation evidence (schema `xb155-e7-isolation-v2`) and a pure `classify` verdict (`ISOLATED` / `NOT_PROVEN`) |
| `scripts/ac2_directlocal_e7_testbook_create.ps1` | One-shot create through the unchanged adapter, pinned to `AED_TESTINGAPI`; re-validates the evidence itself |
| `scripts/ac2_directlocal_e7_owner_console.ps1` | Owner console procedure: fresh evidence, then the create script with locally typed credentials |
| `tests/test_directlocal_e7_testbook_ps.py` | Fake-session tests, evidence/ACL/custody negatives, collector fail-closed and classifier tests |

The previously accepted `ac2_member_create_uat_runner.ps1` is not reused: its
MemberNo comes from the approved Forms package (not `GetNextMemberNo`) and it
binds the book only by environment value.

### Outbound qualification (2026-10-14, read-only)

- AutoCountServerService (LocalSystem; port 19500) touches the book only to
  read `sys` metadata and poll `MailQueue` (it is the mail sender). The SQL
  plan cache for the book's context holds no statement reading Member,
  ChangeLog or ChangeCount. Accounting.exe polls `EInvoiceQueue`.
- Member triggers write only ChangeLog/ChangeCount. `UserScript`,
  `MailQueue`, `EInvoiceQueue` and `OneRewards_Member` are empty; POS sync
  registrations predate the clone; both plug-ins have `AllowSync = F`.
- An offline IL reachability scan from `MemberCommand.Create`,
  `GetNextMemberNo`, `GetMember`, `NewMember`, `SaveMember` and the
  `MemberEntity` accessors (529 methods, no truncation) found no MailQueue,
  EInvoiceQueue, Notification, SMTP or MailKit reference. It did find two
  vendor HTTPS paths that carry no member data: error telemetry to AutoCount HQ
  (gated by `ErrorLogClientHelper.SubmitErrorLog`, default `False`, set only by
  its static constructor in the loaded assemblies) and a country reference-list
  fetch from the AutoCount licence API when the profile's country information
  is missing. `MemberEntity` construction loads user scripts from `UserScript`.
- The proof holds only for the pinned `AutoCount.dll` and
  `AutoCount.Invoicing.dll` bytes; the collector and the create script both
  refuse other builds.

### Collector (fail closed)

Each inspection (`host`, `processes`, `sessions`, `sql_capabilities`,
`book_channels`, `scheduled_tasks`, `services`, `assemblies`) records
`ok`/`code`. Non-elevated task/service inventories (which omit entries the user
cannot read), tool exit failures, unparseable or implausibly small listings,
SQL failures, failed samples and sessions that cannot be attributed to a local
process all yield `NOT_PROVEN`. Evidence is written once (exclusive create) and
the printed SHA-256 is the custody link to the create script.

### Create script guards, in order

1. Host `DESKTOP-4I042L6`; an owned workspace whose marker names this
   RUN/LOCK/book; the workspace root has no inherited ACEs and the workspace,
   `state` and `evidence` directories grant access only to the current user,
   SYSTEM and Administrators (any other SID, including OWNER RIGHTS, refuses;
   ACLs are read through .NET, not `Get-Acl`, so a `pwsh` parent's
   PSModulePath cannot disable the check); no `XB_AC2_SESSION_FACTORY`; exclusive lock;
   one-shot (`e7.intent.json`, `e7.fence.json`, `e7.claim` or
   `e7.outcome.json` present refuses).
2. Evidence re-validated field by field: file inside the private `evidence`
   directory, SHA-256 equal to `-IsolationEvidenceSha256`, schema, host, book,
   `run_at`/`completed_at` within 30 minutes, every inspection `ok`, six clean
   samples, every negative finding recomputed, assembly pins, then the verdict.
   The live assemblies must also match the pins.
3. Reviewed vendor login with the database pinned to `AED_TESTINGAPI` for the
   process (the environment's `AC2_PROBE_DATABASE_NAME` is never used). The
   authenticated `DBSetting.DBName`, vendor `DB_NAME()` and vendor
   `SERVERPROPERTY('MachineName')` must all match. A production, missing,
   ambiguous or wrong identity is refused. Vendor error telemetry must be off.
4. Runtime outbound guard: `UserScript`, `MailQueue` and `EInvoiceQueue` empty.
   No earlier E7 synthetic member, by `COUNT` only (no member details loaded).
5. Write mode only (`-EnableTestBookWrite -ConfirmTargetAedTestingApi
   -ConfirmOwnerRiskAccepted`): durable intent, at most three
   `GetNextMemberNo` candidates each probed by `COUNT`, re-probe, queue
   re-check, fence with deadline, exclusive claim, one `SaveMember` inside
   `New-XbAutoCountMember` (`shopify_m1`, contact fields NULL), one `GetMember`
   readback, exact comparison (`OpeningPoints`, SQL `decimal(19,2)`, is
   compared at canonical scale so a reloaded `0.00` equals the assigned `0`;
   NULL, non-zero and missing values still mismatch), queue re-check (a new queue row is
   `CREATED_OUTBOUND_QUEUE_CHANGED_HOLD`), and an outcome file with
   before/after audit counters (Member change counter, ChangeLog, EventLog,
   Activity, queues). The intent, fence, claim and outcome records are
   hash-chained.
6. Any exception after the claim is `WRITE_OUTCOME_UNKNOWN_HOLD`. It is never
   retried, and the same MemberNo is only counted.

The real MemberNo stays in the private state records; stdout and `-JsonOut`
carry only `member_ref` (`e7m-` plus 12 hex of its SHA-256).

### Owner console procedure

`ac2_directlocal_e7_owner_console.ps1 -Mode DryRun|Write` runs in an
interactive, elevated console. It refuses redirected input/output and stale
credential variables, runs the collector, prompts for the user ID and (as a
`SecureString`) the password, and passes them only in the child process's
environment block. The BSTR is zeroed and freed and the `SecureString` disposed
in `finally`; the console's own environment never holds them. `Write` also
requires typing `AED_TESTINGAPI`. Limitation: the adapter reads the password
from an environment variable, so a managed copy exists briefly.
## Remaining effect gates (each needs separate explicit approval)

| Gate | Effect | Status 2026-10-15 |
|---|---|---|
| E0 | Create the private UAT/production `state_root` (`prepare-state-root`) | Source ready; tested offline |
| E1 | Materialise the dedicated session folder from the template | Open |
| E2 | Prove deny enforcement live (no write tool called) | Allow path observed live; deny path offline |
| E3 | Attended baseline capture (GIDs, `createdAt`, tags, registration metafield id/instant) | Owner read consent given (#155:6195382795); not run |
| E4 | Real profile reads (model-mediated PII), retention | Owner read consent given; not run |
| E5 | AutoCount login and `book-check` on the test book | Source ready (Owner console); not run |
| E6 | `GetNextMemberNo` side-effect qualification | Open |
| E7 | One synthetic `SaveMember` on the test book | Consumed 2026-10-15; never replayed |
| E7b | One attended real-registration create on the test book | Needs a new scoped approval |
| E8 | Unattended scheduling / standing policy activation | Needs approval and an unattended Shopify source (see runbook) |
| E9 | Production book (`AED_XBOUNDARIES`) source enablement, create and POS visibility | Needs a source change and approval; required for the POS acceptance |
