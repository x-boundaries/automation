# Member DirectLocal Contract (#155, synthetic core)

Status: source-only synthetic core. No real Shopify read, AutoCount login,
`GetNextMemberNo` or `SaveMember` against any account book is authorised by
this document. The previous gateway/n8n/PostgreSQL path is not used and its
files are unchanged.

## Synthetic inertness (G3 correction)

The core cannot reach a real AutoCount account book. Two independent layers
enforce this; neither relies on Claude instructions, CLI flags, settings
templates or approval rows:

| Layer | Enforcement |
|---|---|
| Processor (`xb_directlocal.py`) | The only ledger mode is `synthetic`. `process --enable-autocount-write`, the internal write path and `reconcile` refuse any bridge whose `autocount_target` is not the test-only `SYNTHETIC_BRIDGE_TARGET` (`real_autocount_refused_synthetic_ledger`), before any capture is consumed or process started. The CLI only ever builds the real `BridgeClient`, so it is always refused. |
| Bridge entrypoint (`ac2_directlocal_member_bridge.ps1`) | Refuses (`autocount_real_bridge_disabled`) unless the caller passes `-SessionFactory` returning a binding marked `XbDirectLocalSyntheticFake`, whose `UserSession`, `DBSetting` and every `MemberCommand` are not AutoCount types. `-File` cannot pass a scriptblock, so a command-line invocation is refused before stdin is read, regardless of `-EnableProductionAdapter`, fence ids or `XB_AC2_*` environment values. The adapter's environment/assembly login path is unreachable from the bridge. |

Tests reach the fake MemberCommand only through that positively isolated
interface (test-only target label plus marked fake host). Enabling a real
account book requires a later, separately approved source change to both
layers (gates E5-E7).

## Data path

```text
Attended Claude session (dedicated folder, generated from the settings template)
  -> connector graphql_query, pinned queries only (PreToolUse guard)
  -> PostToolUse hook writes hook-delivered JSON to <state_root>\inbox
python directlocal/xb_directlocal.py  (one OS-locked owner process)
  -> strict capture checks -> SQLite ledger <state_root>\ledger.sqlite3
  -> reused shopify_admission.capture_member_mg_baseline / evaluate_profile
  -> powershell.exe -File scripts\ac2_directlocal_member_bridge.ps1 (one op per child)
       -> scripts\ac2_member_gateway_autocount_adapter.ps1 (shopify_m1; only change: OpeningPoints decimal scale)
```

| File | Role |
|---|---|
| `directlocal/xb_directlocal.py` | Capture parsing, phone keys, ledger, baseline/discovery, admission, allocation, fence, recovery, reconcile, CLI. Lives outside the hash-frozen `member_gateway/` tree and imports the reused stdlib-only admission functions from it read-only |
| `scripts/ac2_directlocal_member_bridge.ps1` | Child bridge: `legacy-precheck`, `next-member-no`, `probe`, `reconcile`, `write-once` |
| `scripts/directlocal_connector_hook.py` | `guard` (PreToolUse default-deny for MCP) and `capture` (PostToolUse inbox writer) |
| `config/directlocal_claude_settings.template.json` | Dedicated-session settings template (deny list + hooks); placeholders only |
| `tests/test_member_directlocal.py`, `tests/test_directlocal_bridge_ps.py` | Synthetic tests |

## Capture provenance and the fail-closed boundary

- A capture record is the structured JSON Claude Code hands to a PostToolUse
  hook (`provenance = claude_code_posttooluse_hook_delivered_json`). It is not
  the original Shopify wire bytes and is never described as such.
- Every capture must use a pinned query (`sha256` match), `first = 50` in both
  the tool input and variables, matching `after`, a GID-shaped profile `id`,
  exact response keys, and `shop { id myshopifyDomain }` equal to the ledger's
  immutable store identity.
- Only the `synthetic` response shape (`{"data": {...}}`) is qualified. A ledger
  can only be initialised in `synthetic` mode with a `*.synthetic.invalid`
  domain; any other mode or shape fails with `capture_response_shape_unqualified`.
  Gate E1 must qualify the real hook-delivered shape before this changes.
- Qualified 2026-10-14 (one attended read of the pinned `IDENTITY_QUERY`,
  `shop { id myshopifyDomain }` only, through the live PreToolUse guard and
  PostToolUse capture hook): the hook-delivered `tool_response` is a JSON
  **text** that decodes to exactly `{"data": {...}}`
  (`connector_graphql_json_text_v1`). `inspect-identity --capture <file>`
  verifies such a capture and returns only the store identity. Errors,
  extensions or any other wrapper are rejected. Ledger ingestion of real
  connector data remains disabled (the ledger accepts only the synthetic shape).
- The hook is not tamper-proof against an agent running as the same Windows
  user; this residual risk is acceptable only for attended operation.

## Cutover and baseline

- `begin-baseline` fixes `C` (whole-second UTC) before any page is requested.
  At most one non-abandoned baseline run exists per ledger; `cutover_at` is
  immutable and a sealed run can never change.
- An incomplete (`CAPTURING`) run can be abandoned with
  `abandon-run --run-id <id> --confirm-suffix <last 8 hex>`. It becomes
  `ABANDONED` (immutable), its unconsumed page captures are retired, and a new
  baseline must take a strictly later cutover (`cutover_not_advanced`).
- `ingest-run` chains unconsumed page captures from `after = null` and runs the
  reused exhaustive `capture_member_mg_baseline` (all customers, `sortKey: ID`,
  no tag search). A missing, conflicting, repeated, duplicated or unchained page,
  or a capture taken before the run started, never seals.
- Sealed: `member-mg` GIDs with `createdAt < C` become the immutable historical
  baseline; `createdAt >= C` become `DISCOVERED` customers. Discovery runs repeat
  the same full scan with the same `C`; pre-cutover GIDs missing from the
  baseline are surfaced and `evaluate_profile` sends them to review.

## Admission, duplicates and MemberNo

- `process --capture <profile>` consumes a fresh (<= 900 s) unconsumed profile
  capture for a discovered GID and applies `evaluate_profile` unchanged
  (`member-mg`, legacy tags, exact Shopify dates, bounded wait).
  Captures are recorded by hash and `tool_use_id`, so a replay is refused.
  The profile file is deleted after use (best-effort, not a secure erase).
- Writing requires `--enable-autocount-write` **and** an unexpired, unused
  approval from `approve-write --gid <gid> --confirm-suffix <trailing digits>`.
- Duplicate precheck: keys from `duplicate_phone_keys` (`65xxxxxxxx` <->
  `xxxxxxxx`), one unchanged adapter lookup per key plus exact email. Any hit,
  failed lookup or ambiguous result is `MANUAL_REVIEW`. The stored MobilePhone
  stays the unchanged `canonical_phone` value.
- AutoCount column widths (test-book catalog, 2026-10-14): Name
  `nvarchar(100)`, MobilePhone `nvarchar(25)`, EmailAddress `nvarchar(200)`,
  MemberNo `nvarchar(20)`. Wider values go to `MANUAL_REVIEW`
  (`autocount_field_width_exceeded`) before any lookup or write.
- MemberNo: at most 3 `GetNextMemberNo` attempts per customer, ever; each
  attempt is committed before the call. Occupied or ledger-held candidates
  advance; a repeated, invalid or unprobeable candidate goes to review. The bound
  candidate is re-probed and never replaced. `GetNextMemberNo` is treated as
  possibly consuming a number until gate E6 proves otherwise.

## Write fence and recovery

| Step | Durable state |
|---|---|
| Approval consumed, fence inserted | `FENCED` (one per customer, one per MemberNo) |
| Before the child starts | `DISPATCHED` + deadline |
| Child: digest + fence check, exclusive `<fence>.claim`, `SaveMember`, read-back, `<fence>.outcome` | claim/outcome files |
| Parent classifies | `CREATED_VERIFIED`, `CREATED_READBACK_MISMATCH`, `WRITE_OUTCOME_UNCERTAIN`, `NOT_WRITTEN_PROVEN` or `WRITER_TERMINATION_UNCONFIRMED` |

- No transition leads back to `FENCED`/`DISPATCHED` (SQLite triggers), so no
  crash, replay, timeout or retry can dispatch a second write.
- `NOT_WRITTEN_PROVEN` requires the claim file to be absent (the claim always
  precedes `SaveMember`). It is still terminal; there is no automatic retry.
- Every command first runs recovery: an orphaned `FENCED` fence becomes
  `MANUAL_REVIEW`; a `DISPATCHED` or `WRITER_TERMINATION_UNCONFIRMED` fence
  holds (exit 75) until its deadline plus grace has passed **and** the writer's
  exit is proven against its recorded identity (PID plus process creation
  instant). A vanished PID, or one now owned by a process with a different
  creation instant (PID reuse), proves exit. A running child, a missing
  identity or any indeterminate probe stays on HOLD (`writer_may_be_live`,
  `writer_identity_unrecorded`, `writer_exit_unproven`). Once exit is proven
  the fence becomes uncertain, or not-written only if no claim exists.
- `release-writer-hold --fence-id <id> --confirm-suffix <last 8 hex>` is the
  attended exit from a HOLD that cannot be proven: after the deadline, the
  operator attests no bridge process remains and the fence becomes
  `WRITE_OUTCOME_UNCERTAIN` (`operator_attested_writer_absent`), never
  not-written.
- `reconcile --capture <fresh profile>` re-derives the payload, requires the
  salted digest to match the fence, and compares read-only with the same bound
  MemberNo. It never calls `SaveMember`.
- The ledger holds GIDs, MemberNos, counts, codes and salted digests only.
  Events are hash-chained and append-only; a second process cannot open the
  ledger (OS lock) and a stale owner epoch cannot commit.

## State root

`--state-root` must be an existing absolute local path that is not UNC, not
under `%LOCALAPPDATA%` (MSIX redirection) and not inside the repository. It is
chosen and its permissions are qualified at gate E0; nothing is hardcoded.

## Synthetic command sequence

```powershell
python directlocal/xb_directlocal.py --state-root <root> init-ledger --mode synthetic --store-gid gid://shopify/Shop/900000001 --shop-domain xb-test.synthetic.invalid
python directlocal/xb_directlocal.py --state-root <root> begin-baseline
python directlocal/xb_directlocal.py --state-root <root> ingest-run
python directlocal/xb_directlocal.py --state-root <root> process --capture <root>\inbox\<id>.json
python directlocal/xb_directlocal.py --state-root <root> status
python directlocal/xb_directlocal.py --state-root <root> abandon-run --run-id <run-id> --confirm-suffix <last-8-hex>
```

## E7 test-book path (one synthetic member on AED_TESTINGAPI)

Separate from the DirectLocal core, whose synthetic hard-disarm is unchanged.

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

| Gate | Effect |
|---|---|
| E0 | Production `state_root` with hardened permissions. (UAT used an owned workspace under `C:\XB`, which inherits Authenticated Users: Modify.) |
| E1 | Done for identity: shape qualified and store identity observed. Still open: materialise the dedicated session folder from the template |
| E2 | Prove deny enforcement live without calling any write tool. (Allow-path of the guard and capture hook observed live; deny-path proven offline only) |
| E3 | Attended baseline capture of customer GIDs, `createdAt`, tags; clock check |
| E4 | Real profile reads (model-mediated PII), retention and consent |
| E5 | AutoCount login and read-only bridge ops on the test book (catalog-level read-only SQL qualification done; the bridge remains synthetic-only) |
| E6 | `GetNextMemberNo` side-effect qualification on the test book |
| E7 | One synthetic `SaveMember` on the test book. Owner risk accepted (#155 comment 6166747236) for possible data loss and irreversible audit effects (the Member table carries active `MemberChangeLog` / `UpdateMemberChangeCount` triggers, the book uses SIMPLE recovery, and the test book is a live-configured clone). Runs only through the Owner console procedure above |
| E8 / E9 | Scheduling; production book |
