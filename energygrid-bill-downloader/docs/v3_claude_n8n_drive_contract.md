# EnergyGrid v3: Claude orchestrator, n8n Google Drive, deterministic core

Authority: #226 G2 contract as amended by Web (#226 comment 6016300178).
Run: `2026-10-06-xb-226-energygrid-v3-claude-n8n-drive-g3-001`. This document is
the repository contract for the v3 successor on PR #229. It authorises no live
action; every private setup step below needs its own explicit authority.

## 1. Production chain

```text
Windows Task Scheduler (08:00 +08:00, disabled template)
  -> runtime/claude_supervisor.ps1            (hash, version and token preflight)
  -> pinned standalone Claude Code CLI        (bounded orchestrator only)
  -> runtime/bin/egcore.cmd                   (eleven exact commands)
  -> claude_supervisor.ps1 -CoreDispatch      (allowlist, scrubbed environment)
  -> installed runtime/launcher.ps1           (unchanged preflight, -Command/-Stream)
  -> python -m energygrid_bill_downloader     (deterministic core, SQLite authority)
  -> n8n "EnergyGrid - Drive Upload"          (exact Drive upload and read-back)
  -> n8n "EnergyGrid - Invoice Delivery"      (accepted email workflow, unchanged)
```

Google Drive for desktop and the filesystem `DriveStager` are retired. The Claude
Google Drive connector is not used for binary transfer.

## 2. Schema v3

`PRAGMA user_version=3`, additive over v2 (STRICT tables, journal `DELETE`,
`synchronous FULL`, foreign keys on, no deletes). Daily commands only open an
existing `COMPLETE_V3` / `RESUMABLE_V3` database whose schema manifest equals the
reference exactly.

| Object | Purpose |
|---|---|
| `energygrid_drive_binding_v3` | One `ACTIVE` binding per stream: `binding_id = 'egdb3-' + sha256("energygrid.drive_binding.v3\n" + stream + "\n" + account_ref + "\n" + root_folder_id + "\n" + folder_id)[:32]`. Frozen; only `ACTIVE -> RETIRED` with no open operation. |
| `energygrid_drive_operation_v3` | One row per invoice per binding: frozen intent, `reserved_remote_file_id`, attempt count (0..2), reconcile evidence and the verified receipt. |
| `energygrid_drive_dispatch_v3` | One write-once marker per attempt, carrying the reserved ID. |
| `energygrid_run_v3` | Insert-only run row with per-stream acquire results. |
| `energygrid_delivery_invoice_guard_v3` | Replaces the v1 guard: an email intent needs a `DRIVE_VERIFIED` receipt for the same bytes under the `ACTIVE` binding. |
| Legacy freeze triggers | Historical `drive_*` columns are frozen; new invoices are `NOT_STAGED`; new `DRIVE_STAGE` operations are refused. |

Drive states: `NOT_UPLOADED` (no row), `DRIVE_UPLOAD_INTENT`,
`DRIVE_UPLOAD_UNCERTAIN`, `DRIVE_VERIFIED`, `DRIVE_CONFLICT`, `HOLD`. Allowed
transitions: INTENT -> {VERIFIED, UNCERTAIN, CONFLICT, HOLD}; UNCERTAIN ->
{VERIFIED, CONFLICT, HOLD, INTENT (core retry authority only)}; HOLD
(`EG_DRIVE_VERIFICATION_UNAVAILABLE`) -> VERIFIED. VERIFIED, CONFLICT and every
other HOLD are final.

The seven private appProperties (canonical sorted JSON, rebuilt by a SQLite
CHECK from the frozen identity columns): `egApp="xb-energygrid"`,
`egSchema="eg-drive-v3"`, `egStream`, `egInv`, `egBnd`, `egOp`, `egSha`.

## 3. Pre-generated Drive file ID (Web amendment A)

1. `drive-intent` creates the INTENT row locally; no network.
2. `drive-upload`, before its first dispatch, calls the n8n `RESERVE_ID` mode,
   which performs exactly `GET /drive/v3/files/generateIds?count=1&space=drive&type=files`
   and creates no file.
3. The returned ID is validated and written once into `reserved_remote_file_id`
   (`UPDATE ... WHERE reserved_remote_file_id IS NULL AND upload_attempt_count=0`).
   A SQLite trigger refuses any replacement, any reservation after a dispatch,
   and a dispatch without a reservation. A second concurrent reservation cannot
   freeze a second ID; the run lock also serialises commands.
4. A lost or invalid reservation means zero upload (`exit 10`,
   `EG_DRIVE_RESERVATION_UNAVAILABLE`); a later call may reserve again because no
   create was attempted.
5. Only after the reservation is durable does the core commit the dispatch marker
   (which records the same ID) and send exactly one `UPLOAD_IF_ABSENT` request.
6. The n8n workflow creates only when `files.get(reserved_id)` is 404 and both
   complete conflict searches are empty, with a resumable create whose metadata
   carries `id=<reserved>`. A create answered 409 ("already exists") is read back,
   never re-minted.
7. Reconciliation (`drive-reconcile`, `RECONCILE` mode, no PDF, no path to any
   create node) reads `files.get(reserved_id)` first, then the identity and name
   searches.
8. A retry is authorised only by the core: state UNCERTAIN, attempt count 1, a
   complete positive NOT_FOUND in this run, a run different from attempt 1, the
   binding unchanged and the archive re-checked. Attempt 2 uses the same ID. A
   further NOT_FOUND after attempt 2 holds `EG_DRIVE_RETRY_EXHAUSTED`. The earlier
   60-minute delay is not a correctness predicate.
9. A verified receipt's `remote_file_id` must equal `reserved_remote_file_id`
   (SQLite CHECK). Any candidate with a different ID is a conflict
   (`EG_DRIVE_REMOTE_ID_MISMATCH` when it carries this operation's identity).

## 4. Verification (core only)

`DRIVE_VERIFIED` requires all of: exactly one candidate equal to the reserved
ID; `parents == [folder_id]`; canonical name; `application/pdf`; not trashed;
appProperties equal to the frozen seven-key map; size equal to the local and
invoice size; every present checksum equal (`sha256Checksum` against SHA-256,
`md5Checksum` against MD5) and at least one present; the binding still `ACTIVE`
and matching the private config; and the canonical local archive re-checked in
the same command. SHA-256 is preferred; MD5 + size is the fallback
(`verification_method=MD5_SIZE`). Neither checksum: HOLD
`EG_DRIVE_VERIFICATION_UNAVAILABLE`, no email, only a later read-only reconcile
can verify. HTTP 200 or an n8n "success" alone never counts, and an n8n outcome
that disagrees with the raw facts is treated as no valid result.

Conflict codes: `EG_DRIVE_NAME_OCCUPIED`, `EG_DRIVE_MULTIPLE_CANDIDATES`,
`EG_DRIVE_IDENTITY_BYTES_MISMATCH`, `EG_DRIVE_IDENTITY_MOVED`,
`EG_DRIVE_IDENTITY_TRASHED`, `EG_DRIVE_IDENTITY_OP_MISMATCH`,
`EG_DRIVE_NAME_MISMATCH`, `EG_DRIVE_MIME_MISMATCH`, `EG_DRIVE_REMOTE_ID_MISMATCH`.
Never overwrite, suffix, create a variant or delete.

## 5. n8n workflows and Google credential (Web amendment B)

- `n8n-workflows/energygrid_drive_upload.workflow.json`: inactive,
  `availableInMCP=false`, no saved success/error/manual executions,
  `binaryMode=separate`, no webhook ID, no credential ID, placeholder path,
  loopback Header Auth webhook separate from the email workflow. Modes:
  `RESERVE_ID`, `RECONCILE`, `UPLOAD_IF_ABSENT`, `RESOLVE_DESTINATION`. Every
  Google call is an HTTP Request node with
  `authentication=predefinedCredentialType`,
  `nodeCredentialType=googleDriveOAuth2Api`, `retryOnFail=false`, redirects
  disabled, full response with `neverError` so the classifier sees every status,
  and an error output routed to a bounded 503 response. It contains no SMTP node.
- `n8n-workflows/energygrid_drive_destination_setup.workflow.json`: Manual
  Trigger only; resolves `My Drive/Automation/_MandarinGallery/Utilities/EnergyGrid`
  one level at a time without creating chain levels; finds or creates once the
  `EB Bill` and `Tenant Bill` child folders; never deletes; never called by the
  core or Claude.
- Credential (private setup, later): n8n credential type `googleDriveOAuth2Api`
  with `customScopes=true` and `enabledScopes` exactly
  `https://www.googleapis.com/auth/drive`; never the default multi-scope set and
  never a generic `googleOAuth2Api` substitute without returning to Web. The
  OAuth consent screen must be Internal. The refresh token lives only in n8n's
  credential store.
- The email workflow `energygrid_invoice_delivery.workflow.json` is unchanged and
  has no Drive path; the Drive workflow never reaches SMTP. Email waits for
  `DRIVE_VERIFIED` through the core plan and the SQLite delivery guard.
- The live `get_node_types` / `validate_workflow` MCP tools were unavailable during
  G3, so the node shapes follow the accepted n8n 2.39.10 export conventions in
  this directory and are validated offline. Validate in the target instance
  before any authorised import.

## 6. Deterministic core commands

`egcore.cmd <command> [--stream EB_BILL|TENANT_BILL]` -> launcher -> `python -m
energygrid_bill_downloader <command> --config <private> [--stream S]`. No ID is
ever accepted from the caller. Every command takes the run lock without waiting
(held: exit 10 `RUN_IN_PROGRESS`), re-computes the plan and refuses an unplanned
command (exit 64 `EG_CORE_ACTION_NOT_PLANNED`, no mutation), and prints one line
of strict JSON with fixed words only. Exits: 0 done or nothing to do, 10
retryable, 20 HOLD/conflict/uncertainty recorded, 64 refused.

| Command | Effect |
|---|---|
| `plan` | Read-only. `next {action, stream, argv}` plus per-stream `action, archive, drive, email, support_ref`. |
| `status` | Read-only. Per-stream outcome and `fully_handled`; `terminal`, `uncertainty_outstanding`, `business_outcome`. |
| `acquire` | Once per run: inventory -> latest -> canonical archive (zero FETCH when already archived). Records the run row. Per-stream holds surface through plan/status; exit 0 once recorded. The only command that contacts EnergyGrid. |
| `drive-intent --stream S` | Re-checks the archive, computes MD5, inserts the frozen INTENT. No network. |
| `drive-upload --stream S` | Reserves the file ID if needed, commits the dispatch marker, sends one `UPLOAD_IF_ABSENT`, verifies, records exactly one state change. |
| `drive-reconcile --stream S` | Recovers an open marker as UNCERTAIN, reads back by reserved ID, verifies or applies the retry rule. At most once per operation per run. |
| `deliver --stream S` | Only after `DRIVE_VERIFIED` under the ACTIVE binding: the unchanged `DeliveryClient` (one marker, one POST, never resend). |

Operator only (never on the Claude allowlist): `migrate-state`, `drive-bind`.
`drive-bind` verifies the configured folder IDs read-only through
`RESOLVE_DESTINATION` and inserts `ACTIVE` rows only if identical; `--rebind`
retires the old binding only when no INTENT/UNCERTAIN operation uses it. The
combined v2 `run`/`list` is retired (exit 64 `EG_DUAL_RUN_RETIRED_USE_CORE`).

`TENANT_BILL` remains production `UNBOUND` until accepted #227 evidence. An
unbound stream is a per-stream HOLD, so the daily business outcome is `HOLD`
(supervisor 81) while it stays unbound, even when EB Bill completes.

## 7. Claude envelope

- Pinned versioned standalone binary (never the `.local\bin` shim or
  `WindowsApps`); SHA-256 and exact `--version` checked before every run;
  `DISABLE_AUTOUPDATER=1`; model pinned.
- Token: `claude setup-token` output stored as a DPAPI CurrentUser SecureString
  CLIXML outside Git; placed only in the Claude process environment; removed,
  with every `ANTHROPIC_*` variable, from the core's environment.
- Arguments: `-p --output-format stream-json --verbose --model <pinned>
  --permission-mode dontAsk --tools Bash --allowedTools <11 exact rules>
  --disallowedTools mcp__* --strict-mcp-config --mcp-config <empty>
  --setting-sources project --settings <reviewed> --no-session-persistence
  --max-turns 40 --max-budget-usd 1.00`; the reviewed prompt on stdin; working
  directory holding only `.claude/settings.json` and no `CLAUDE.md`; PATH starts
  with `runtime\bin`, which holds only `egcore.cmd`.
- Layers: (1) Claude's exact permission rules; (2) the reviewed project settings
  deny every other tool and disable hooks and MCP; (3) the supervisor audits the
  stream-json transcript and fails 87 on any tool other than Bash, any command
  not exactly one of the eleven strings (chained, piped, redirected, path-prefixed,
  extra-argument, case or whitespace variants), any permission denial, a missing
  or error result, or the turn/budget limits; (4) `egcore.cmd` forwards four
  fixed slots plus an overflow sentinel to `-CoreDispatch`, which accepts only the
  eleven forms; (5) the core refuses any command that is not the planned action.
  `egcore.cmd` is a batch file: it is defence in depth, not a boundary against a
  hostile argument that the permission layer already refuses.
- Claude's part is accepted only with exit 0 within the timeout, one success
  result, no denials, `num_turns <= 40` and `total_cost_usd <= 1.00`. Its final
  text is advisory only.

## 8. Scheduler

Inert template only: daily 08:00 +08:00, `IgnoreNew`, `StartWhenAvailable`,
`ExecutionTimeLimit PT30M` (Claude killed at 20 minutes in a kill-on-close Job
Object), `LeastPrivilege`, `LogonType Password`, never LocalSystem,
`Enabled=false`. The action is `powershell.exe -File claude_supervisor.ps1
-SettingsPath <private>`; it never calls `launcher.ps1` directly.

Supervisor exit codes (most severe wins: 88 > 87 > 89 > 85 > 86 > 84 > 83 > 81 > 82):
0 `NO_WORK`/`COMPLETED`; 81 HOLD; 82 source failure retryable; 83 source failure;
84 Drive uncertain; 85 Drive conflict or Drive HOLD; 86 email uncertain; 87
Claude/runtime failure or timeout; 88 preflight failed (no Claude start, no core
call); 89 final status invalid or incomplete (including Claude exit 0 without a
valid terminal status). On a non-zero exit it sends the existing
`energygrid.alert.v1` alert (stage `run`, status `ACTION_REQUIRED`, support
reference `EG_SUPERVISOR_EXIT_<code>`), which the unchanged alert-ingress
validator accepts.

## 9. Controlled E2E (each step needs its own authority)

1. Internal Google OAuth client; n8n `googleDriveOAuth2Api` credential with
   `customScopes=true` and only the `drive` scope; run the destination setup
   workflow manually; write the private v3 config; `drive-bind --apply`.
2. Drive and email Header Auth credentials with matching host tokens; webhook
   paths; import the Drive workflow inactive, then activate it on loopback; the
   email workflow stays as accepted.
3. Install the pinned Claude CLI, token custody, supervisor and runtime; record
   hashes in the private supervisor settings; run the supervisor preflight.
4. `migrate-state` v1 -> v3: plan, then apply with backup.
5. One supervisor run started by hand (Scheduler still disabled): expect per bound
   stream `DRIVE_VERIFIED` (record the checksum method) and exactly one email.
6. An immediate second run must give `NO_WORK`: 0 fetches, 0 uploads, 0 emails.
7. Register the Scheduler task disabled and read it back; enable only after Owner
   UAT and a separate enable authority.

## 10. Proven offline, still to prove live

Offline (G3): schema/migration/triggers, reserved-ID idempotency, 409 read-back,
verification hierarchy, Drive-before-email, no resend, retry authority, command
contract, workflow Code-node JavaScript under Node, supervisor transcript audit
with a compiled fake Claude, Scheduler template.

Live, before Scheduler enablement: the n8n HTTP Request node binds
`googleDriveOAuth2Api` with the custom single scope for these raw calls on
n8n 2.39.10 (if not, return to Web; never widen scopes); `sha256Checksum`
availability (MD5 fallback is real); Claude's Bash tool on Windows resolves
`egcore.cmd` through Git Bash and matches the exact rules; the setup token works
with the pinned flags.
