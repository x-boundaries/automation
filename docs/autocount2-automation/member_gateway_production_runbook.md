# Member gateway production runbook

This is a preparation and review runbook for the bounded #155 G3 repository
implementation. It does not authorize deployment, activation, live imports, or
customer-data handling.

## AC2 worker package and production launcher

The committed worker example at
`config/ac2_member_gateway_worker.production.example.json` is a shape-only
template. Its `autocount_server_name`, `autocount_database_name`, and
`autocount_user_id` fields are intentionally `null`; an operator must provide
the reviewed values in the external runtime config at
`config\worker.config.json`.

In `Production` mode, the launcher requires each of those three properties to
exist, be a JSON string, be non-null, and be non-empty and non-whitespace. It
preserves each accepted value exactly as supplied: there is no trimming,
coercion, or inference. Invalid values fail closed with the corresponding
internal code: `launcher_autocount_server_name_invalid`,
`launcher_autocount_database_name_invalid`, or
`launcher_autocount_user_id_invalid`, before the worker child starts.

The launcher binds the values unchanged to `XB_AC2_SERVER_NAME`,
`XB_AC2_DATABASE_NAME`, and `XB_AC2_USER_ID`. The child environment removes
`AC2_PROBE_SERVER_NAME`, `AC2_PROBE_DATABASE_NAME`, `AC2_PROBE_USER_ID`,
`AC2_PROBE_PASSWORD`, and `XB_AC2_SESSION_FACTORY`. The existing DPAPI-backed
password artifact remains mapped to `XB_AC2_PASSWORD` with
`XB_AC2_PASSWORD_ENV_VAR=XB_AC2_PASSWORD`. `DisabledProof` remains the
default, does not read production config or secret artifacts, and does not
start the production worker.

The dependency probe remains Windows PowerShell 5.1 Desktop and 64-bit. It
loads exactly these five assemblies in order using
`ReflectionOnlyLoadFrom`: `AutoCount.dll`, `AutoCount.Accounting.dll`,
`AutoCount.Invoicing.dll`, `AutoCount.ImportExport.dll`, and
`AutoCount.Tools.dll`. `AutoCount.BonusPoint.Member.MemberCommand` is resolved
only from `AutoCount.Invoicing.dll`, with `Create`, `GetMember`, `NewMember`,
and `SaveMember` required. No assembly-directory search, resolver, or broad
fallback is permitted.

Do not run licensed AutoCount or a production worker on the development
laptop. Use synthetic/offline validation only; deployment and activation
remain separately authorised operations.

## Safe repository checks

Run the focused package tests, worker/static tests, schema and migration checks,
the offline n8n validator, relevant existing regression tests, structural
checks, `git diff --check`, and the final secret/PII/scope scan. Use synthetic
fixtures only. Do not set production credentials or enable the workflow while
reviewing the branch.

The committed config v3 example (`config/member_gateway.production.example.json`,
`schema_version` `xb.member.gateway.config.v3`) must remain
activation-disabled, kill-switch-on, adapter-not-ready, bound to
`member_book_mode=production`, and without real form/question IDs, cutover
watermark, credential digests, database, bind, TLS, host, or gateway bindings.
Those values make readiness fail closed until an owner supplies reviewed
deployment configuration outside Git. Config v3 has exactly five principals
(source, operator, control, worker, mailer). The removed keys
`recovery_token_sha256`, `recovery_token_env`, `heartbeat_seconds` and
`member_no_max_length` are configuration errors, not ignored fields. The lease
is fixed at 600 s, the worker child deadline at 300 s, and the write budget at
3.

Before any listener starts, run only `python -m xb_member_gateway --config
<reviewed-external-config>`. Bootstrap must read and validate config, resolve
the named runtime boundaries, validate all five principal separations, build
the strict authenticator and repository, and read-only verify migrations
0001-0006, required control rows, initialized watermark, exact production
cutover and form binding, that every source response has an exact
`createTime` and a handling receipt, and that no `CREATED_VERIFIED` in either
the legacy `results` table or `member_outcomes` lacks a welcome outbox row.
Any bounded bootstrap error is a stop condition. Do not let bootstrap apply a
migration, initialize the cursor, repair state, generate a credential, clear
the kill switch, or activate the gateway.

Dark bring-up is the one composition exemption: `autocount_adapter_ready=false`
does not block startup composition, provided production activation is false,
the kill switch is true, and every other admission check passes. Adapter-not-
ready still reports readiness false, still lists `autocount_adapter_not_ready`,
and every claim answers `claimed:false, reason:dispatch_disabled`, so a composed
dark gateway is not an activated one. The bind address must be a private IP
literal; a wildcard, unspecified, malformed, multicast, reserved, or public
value is refused with a bounded code that does not echo the value, and there is
no fallback bind.

## Private HTTPS dark bring-up

This sequence prepares the dark gateway. It does not activate the gateway, start
a worker, or touch AutoCount or member data. The compose placeholders and the
database backup procedure are in `member_gateway/deploy/` (placeholders only;
nothing there is run by the repository or CI).

1. Re-verify the repository head and a clean worktree. Confirm the accepted
   PostgreSQL network is still `internal=true`, that no host database port is
   published, and that the application DSN is unchanged.
2. Capture the current n8n container baseline: image identity, mount set,
   environment variable count, loopback-only user-interface publication, and
   restart policy. Capture the unrelated tunnel container identity so it can be
   proven untouched afterwards.
3. Prove the Hyper-V topology with elevated read-only metadata: the target
   switch type is `Internal`, the accepted AC2 VM is attached, and the
   host-endpoint subnet is not LAN routable. Any ambiguity or mismatch is a stop
   condition. Do not continue to any host binding on a partial proof, and never
   substitute a wildcard, LAN, or public bind.
4. Build the gateway image and pull the pinned nginx ingress image.
5. Create the reviewed external deployment state, the five pairwise-distinct
   bearer principals, and the separate reference-HMAC key. Keep every raw value
   outside Git, chat, and logs.
6. Create the two container networks: the internal backend bridge and the
   n8n/ingress bridge.
7. Generate the private CA and the leaf certificate. Issue the leaf only after
   step 3, because its subject alternative names must cover the proven private
   endpoint as well as the ingress container name.
8. Start the gateway and then the ingress with a no-restart policy and manual
   start. Do not publish a host port yet.
9. Add the read-only CA mount and the `NODE_EXTRA_CA_CERTS` binding to n8n and
   recreate only the n8n service. Preserve its image, volumes, environment
   bindings, loopback-only publication, and restart posture. The unrelated
   tunnel container and its configuration must remain untouched.
10. Import the private CA into the Windows AC2 machine trusted-root store as a
    separately approved mutation.
11. Re-verify the step 3 proof immediately before binding, then publish exactly
    one host HTTPS port bound only to the proven Hyper-V Internal endpoint.

## Dark proof boundaries

Prove, without activating anything: no database host port and the database
network still internal; no LAN or public path to the gateway or the ingress; the
gateway listening socket equal to its fixed private backend address and never a
wildcard; private HTTPS only, with trusted certificates from both the n8n and
AC2 caller classes and no verification disabled anywhere; least-privilege
database connectivity working over the unchanged DSN; `GET /readyz` showing
`dispatch_enabled:false` (activation false, kill switch true); a manual idle
worker cycle answering `claimed:false`; the five bearer principals and the
reference-HMAC key separated by environment name, configured digest, and
resolved value, with the reference-HMAC key unable to authenticate HTTP;
migrations 0001-0006 applied with zero non-terminal jobs; cursor, watermark,
control rows, and business state unchanged; no n8n execution or activation; no
worker task enabled; and no AutoCount, AC2, or member activity.

## Dark rollback boundaries

Roll back in reverse order: remove the host publication, remove the AC2
trusted-root entry, remove the n8n CA mount and environment binding and recreate
only n8n back to the captured baseline, stop and remove the ingress and gateway
containers, remove the two new networks, then destroy the leaf and CA keys and
the external deployment state. Verify afterwards that the n8n baseline matches
what was captured and that the tunnel container was never recreated. The
accepted database, its DSN, the source cursor and watermark, the control rows,
and the unrelated tunnel stack are never mutated by this bring-up, so none of
them requires rollback.

## Migration 0006 (member write v2)

`member_gateway/migrations/0006_member_write_v2.sql` is additive: it widens the
job state check (every old value kept, `LINKED_EXISTING` and `RESOLVED` added),
adds the XB-MN-1 identity, retry-budget and lease-token columns, creates the
append-only `member_outcomes` and `job_resolutions` tables with the partial
unique index `UNIQUE(member_guid) WHERE outcome='CREATED_VERIFIED'`, and
replaces the body of `require_created_verified_for_welcome()` so an outbox row
needs `CREATED_VERIFIED` in either `results` or `member_outcomes`. No table,
column, row, index, trigger or function is dropped; the v1 allocation, fence,
writer, hold and result tables stay present and read-only until a separately
approved retirement migration.

Its guard refuses (and the whole transaction rolls back) while any job is in a
state outside `CREATED_VERIFIED`, `REJECTED_VALIDATION`,
`CONFIRMED_NOT_CREATED`, `CREATED_READBACK_MISMATCH`, `MANUAL_REVIEW`,
`DEAD_LETTER`, or while any `writer_execution_holds` row is not `CLEARED`.
Before applying it, as a separately approved step: take and verify a
`pg_dump` (see `member_gateway/deploy/BACKUP_RESTORE.md`), confirm zero
non-terminal jobs and zero uncleared holds, and keep the kill switch on. Once
recorded, a re-run is a no-op for the guard. Rollback is restoring the
pre-migration dump and the previous image; there is no down-migration.

## Pre-activation review

An owner must independently verify the unsupported prerequisites in the
production contract and the live test matrix (T-1 20-character MemberNo, T-2
`CreatedUserID`, T-6 POS lookup, T-14 `CreatedTime` time zone). Confirm the
official local API surface in the installed licensed environment without using
direct SQL or a real member write.

Review the source mapping against the current form contract. Preserve immutable
`responseId`, authoritative `createTime`, explicit marketing `Yes`/`No`, and
PDPA acknowledgement. Marketing `No` must remain eligible for membership
creation. The gateway applies the former dispatch predicates at `VALIDATED`:
a response without PDPA acknowledgement, with unrecognised marketing consent,
a wrong field set, a non-`member.create` operation, or a non-allowlisted
source/form/mapping becomes `REJECTED_VALIDATION` with a bounded reason and is
never queued. The same predicates, plus dispatch enabled, environment match
and readiness, are rechecked at claim; a stored job failing them is never
handed to the worker.

Confirm private transport, authentication scopes, database backups, operator
access, alerting, and manual-review ownership. Bind exactly five
pairwise-distinct source, operator, control, worker, and mailer bearer
principals. The worker holds only `worker.claim` and `worker.result`; the
control principal holds `control.kill_switch`, `control.activate` and
`control.resolve`; the mailer holds only `welcome_email.claim`,
`welcome_email.send_intent`, and `welcome_email.result`. Keep their environment
names, configured digests, and runtime values pairwise distinct; do not combine
roles, and never use the reference-HMAC key as a bearer. There is no recovery
principal.

Apply migrations and initialize the cursor row (watermark, immutable
`production_cutover_exact`, and private form ID) only in a later separately
authorised deployment transaction. The configured watermark,
`source_production_cutover_exact` (the verbatim Google `createTime` form), and
`source_form_id` must exactly match the persisted values; the cutover never
advances. Keep `source_admission_mode` at `first_member` until first-member
evidence is accepted. Every scan epoch repeats the inclusive fixed-cutover
filter; overlaps replay their receipts, and any immutable-source conflict,
unreceipted page item, mapping drift, or token repetition within an epoch is a
stop condition. Restart an epoch only for `token_invalidated` or
`ambiguous_crashed_attempt`. If readiness reports
`source_exact_time_backfill_missing` or `created_verified_without_welcome_outbox`,
stop: exact values are never guessed and historical success is never made
email-eligible without separate reviewed private no-send work. In
`first_member` mode only a member accepted by the gateway consumes the single
member admission. A `REJECTED_VALIDATION` response is recorded and receipted
without consuming that allowance.

Welcome email: bind exactly one authorised SMTP credential for the sending
identity `noreply@x-boundaries.com` (provider SPF/DKIM as required) and the
mailer bearer before first-member activation. Keep the mailer workflow inactive
and manual. Send Email retry stays disabled. A `DELIVERY_OUTCOME_UNCERTAIN` row
is never resent automatically; resend needs private positive proof that SMTP
did not accept. Rollback after any attempt re-engages the kill switch and
disables schedules; it never deletes or rewinds outbox evidence, resends
uncertain email, or down-migrates PostgreSQL.

## Worker v2 wire contract

Keep worker concurrency and claim size at one. The worker makes exactly three
gateway calls per cycle: `GET /readyz`
(`{ready, reasons[], dispatch_enabled, server_time_utc}`),
`POST /v2/worker/claim` and `POST /v2/jobs/{job_id}/result`, each with one
bounded `ws-` session in `X-XB-Worker-Session` (an execution identity, not a
credential or host identity).

The claim transaction locks both control rows, requires activation on and the
kill switch off, reaps any expired lease (the job goes to `RETRY_WAIT` as an
uncertain write attempt, eligible 5 minutes after expiry, or to
`MANUAL_REVIEW(uncertain_exhausted)` on the third), refuses while any lease is
active anywhere (`singleton_busy`), and leases the oldest eligible job for
600 s with a fresh `lease-<32hex>` token. `first_claimed_at` is set once. The
claim response (`xb.member.gateway.worker_claim.v2`) carries the member record
computed once at `VALIDATED`, including the immutable XB-MN-1
`base_member_no` and `name_component`. There is no heartbeat, precheck,
allocation, write-intent, dispatch fence, writer registration, quarantine or
recovery route; those v1 routes answer `404 route_not_found`.

A result (`xb.member.gateway.result.v2`) is accepted only while the job is
`LEASED` with the same active token, session, `attempt_no` and
`state_version`; the kill switch never blocks it. The identical body again
returns the stored response; a different body for a recorded lease is
`409 result_conflict` and is recorded; a body for an expired or replaced lease
is `409 result_stale` and changes nothing. A lease-matching body that breaks
the section 4.4 consistency rules sends the job to
`MANUAL_REVIEW(result_contract_violation)`. `FAILED_BEFORE_WRITE`,
`NOT_CREATED`, `NOT_CREATED_CONFLICT`, `OUTCOME_UNCERTAIN` and lease expiry
retry after 5 then 30 minutes within a 3-attempt write budget; `MUTEX_BUSY`
retries every 5 minutes within its own 12-attempt budget.

`CREATED_VERIFIED` writes one `member_outcomes` row and the welcome outbox row
in the same transaction. A prior-attempt create whose member Guid is already
credited to another job is demoted to `LINKED_EXISTING(guid_already_verified)`
without email; a fresh create hitting a credited Guid is
`MANUAL_REVIEW(guid_conflict_on_fresh_create)`. MemberNo and Guid are stored
but never logged, audited, or shown in operator views.

## Operating sequence after a separately approved activation

The kill switch defaults to ON. To engage it, use the separately scoped
`POST /v1/control/kill-switch/enable` operation with the `control.kill_switch`
scope and an empty body. Engagement blocks new claims; a result for a job
already in flight is still accepted. Clearing is separate: use
`POST /v1/control/kill-switch/disable` only after the owner has verified the
remaining predicates and is observing the approved window.

1. Keep the kill switch engaged until readiness, source mapping, and adapter
   checks are green.
2. Enable only the approved private source/gateway transport and verify that
   the workflow remains inactive until the explicit activation procedure.
3. Enable gateway activation through the scoped control endpoint with an
   external approval reference (activation also requires a valid
   `member_book_mode`).
4. Clear the kill switch only when the owner is observing the first bounded
   synthetic or approved operational window.
5. Observe `GET /v1/operator/status` (`xb.member.gateway.operator_status.v2`:
   control flags, `dispatch_enabled`, per-state counts and manual-review reason
   counts) and `GET /v1/jobs/{job_id}` (`xb.member.gateway.job.v3`, operator
   scope, metadata only). Never treat a timeout as proof of absence or success.

## Manual review and resolution

Every unclear case is settled by running the same job again; the next
attempt's probe runs under the AC2 mutex and sees the real state. Nothing in
the gateway retries `SaveMember` itself. A job in `MANUAL_REVIEW` is resolved
only by the control principal with
`POST /v2/control/jobs/{job_id}/resolve`
(`xb.member.gateway.resolution.v1`):

- `{"action":"CLOSE","resolution_code":"<code>"}` moves it to `RESOLVED`.
- `{"action":"REQUEUE","resolution_code":"<code>"}` moves it to `QUEUED` with
  the write budget raised by 3 (capped at 12; a spent capped budget is
  `409 write_budget_cap_reached`) and the busy counter reset.
- Optional `member_no` / `member_guid` record what staff found. They are stored
  only in the append-only `job_resolutions` table and are never echoed.

A resolved or linked job never gets a welcome email. Resolution is allowed only
from `MANUAL_REVIEW` (`409 resolution_state_invalid` otherwise).

## Stop and disable

Use the scoped `POST /v1/control/kill-switch/enable` operation on any source,
mapping, readiness, lease, adapter, database, or readback anomaly. The worker
must stop claiming while the switch is set. Do not delete history or reset a
job to make a retry appear clean; preserve the source, attempt, outcome,
resolution and outbox lineage. Only the separately scoped `.../disable`
operation may clear the switch after controlled review.

The repository check is authoritative at the mutation boundary. In memory, the
control read and claim mutation share one repository lock. In PostgreSQL, the
claim transaction locks the control rows with `FOR UPDATE` before reaping or
creating a lease; a missing row blocks closed. The API readiness check is only
defence in depth.

## Not performed by this run

No live Google Forms/Sheets, n8n instance, PostgreSQL server, AutoCount account
book, Docker/service, VM, network, credential, scheduler, production
activation, deployment, or existing UAT package execution is part of this
runbook execution.

## Bounded member-gateway workflow import successor

The bounded importer is a fresh split-successor path for the inactive member
gateway source-adapter workflow. Its only production mutation is one explicit,
reviewed n8n workflow import. It does not call the generic repository importer,
export all workflows, execute a workflow, activate a workflow, expose MCP, or
contact Forms, AutoCount, or the member API. The canonical source remains
`n8n-workflows/member_forms_gateway_ingest.workflow.json` and must remain
inactive, manual-triggered, credential-free, and unchanged.

The bounded entry point is
`n8n-workflows/scripts/import-member-forms-gateway-bounded.ps1`. Its reviewed
binding shape is documented by
`config/member_forms_gateway_bounded_import.v2.template.json`. The committed
file is a shape template only: real project/workflow IDs, form/question IDs,
resolved credential IDs and names, production cutover, and source token stay in
a private, ignored operator manifest. Do not put those values in a workflow
export, ordinary temporary files, or this repository.

The reviewed manifest must carry the exact resolved credential object for each
role: ID, name, type, and node-role membership. The prepared workflow retains
the ID and name, and exact readback rejects a different credential object even
when the replacement has the same name and type. The manifest must also carry
`security.approved_gateway_origin` as the reviewed private production
authority: HTTPS, explicit port 443, no userinfo, query, fragment, or
non-root path, and the exact `https://<host>:443` representation. The
`https://gateway.example.com:443` value in the committed template is an
illustrative placeholder only; the private reviewed production binding must
replace it with the actual approved origin. `endpoints.gateway_origin` must be
exactly that approved origin and `endpoints.source_cursor` exactly
`<gateway_origin>/v1/source/cursor`; every gateway call in the export is that
origin plus a fixed route. The gateway-bearer role binds exactly the seven
gateway nodes (begin/resume, both restarts, open, ingest, rejection, commit). The Forms request must be exactly
`https://forms.googleapis.com:443/v1/forms/<form-id>/responses`. Alternate
hosts, ports, versions, form IDs, queries, encoded/case/trailing-dot host
variants, and other canonicalisation tricks are fail-closed before any token,
credential, or Docker access.

### Required sequence

1. Reconcile the protected canonical main revision, the parent/child issue
   authority, the frozen predecessor PR, and the exact canonical workflow blob.
   A changed authority packet requires a new reviewed plan.
2. Prepare the private manifest (`cursor_expectation` = exact
   `production_cutover_exact`, its domain-separated digest, and
   `admission_mode`) and acquire the authoritative cursor v2, exact
   project/workflow metadata, and either the exact existing
   workflow export or complete absence evidence. A case-distinct target is a
   collision, not an absent target.
3. Run `CapturePlan` read-only. The plan binds the repository H/tree/parent,
   canonical workflow blob, project/workflow identity, prepared workflow,
   resolved credential-binding digest, public cursor-projection digest and
   state version, domain-separated production cutover digest, and
   existing/absent preimage digest. Page tokens and raw response IDs from the
   cursor's active epoch are validated but never persisted.
4. Review the immutable files under
   `.n8n-local/member-gateway-bounded-import/operations/<operation-id>/`.
   They must contain only the plan, binding, cursor-projection state, prepared workflow,
   mutation intent, exactly one preimage/absence receipt, and later receipts.
   A containerised apply additionally records one private
   `container-custody.json` receipt before import. It binds the container and
   image IDs, non-root import UID/GID, random nonce, sticky `/tmp` proof,
   private staging ownership/modes, exact prepared bytes/hash, and cleanup
   state.
5. Immediately before `Apply`, reacquire and compare the cursor projection, production cutover,
   project/workflow metadata, and preimage. Every retry repeats this check.
   Any mismatch blocks the operation and requires a new reviewed plan.
   Container staging is root-assisted only for mkdir/chown/chmod/stat/hash/
   cleanup; the n8n import runs as the recorded non-root UID/GID. Cleanup is
   unconditional and exact. No completion receipt or success status is valid
   unless persisted custody proves `cleanup_state=cleaned` and
   `cleanup_verified=true`. A cleanup failure after possible mutation is
   terminal no-replay state; subsequent recovery may read back the target or
   clean the exact recorded path, but may not re-import or silently clean it.
6. Run `Apply` only with the separately authorised target, source token, and
   explicit `-ConfirmBoundedApply`. Read back the exact inactive/manual target
   projection before writing completion evidence.

Example planning command (read-only; use a private manifest path):

```powershell
pwsh -NoProfile -File n8n-workflows/scripts/import-member-forms-gateway-bounded.ps1 `
  -Mode CapturePlan `
  -OperationId member-gateway-<reviewed-operation-id> `
  -BindingManifestFile <private-manifest-path>
```

The live apply command is intentionally not part of automated CI and is not a
deployment command. It requires current-turn authority naming the target and
operation, plus `-ConfirmBoundedApply`; this repository change alone is never
proof of import, activation, execution, or production deployment.

### Recovery and custody rules

The operation directory is immutable once populated. Missing, corrupt, extra,
or reinitialised material fails closed. A pre-dispatch failure may be retried
only after fresh evidence. A dispatch with no completion is ambiguous: if the
exact target already matches, write only completion evidence; if it does not,
stop without replay. A partial create with a complete target result is also
completed without a second mutation. A completed operation is a no-op only
when the target projection and operation identity still match exactly.

Before the first private byte is written, the helper resolves the repository
and canonical private root, verifies ignored/untracked custody, rejects path
escape and reparse/link components, creates restrictive Windows ACLs for the
current operator/SYSTEM/Administrators, then writes flushed UTF-8 LF-only
files by create-new and same-root staged rename. Incomplete operations remain
for reconciliation; they are never silently deleted or reinitialised.

Offline validation for this path is the dedicated regression command:

```text
python -m unittest tests.test_member_gateway_bounded_import_security -v
```

The hosted `bounded-import-security` job runs that exact command on Windows.
It uses synthetic fixtures only and has no live credentials, Docker target,
generic importer hook, or n8n mutation authority.

## Bounded welcome-email mailer import

`n8n-workflows/scripts/import-member-welcome-email-bounded.ps1` imports the
inactive `n8n-workflows/member_welcome_email_outbox.workflow.json` with the
same custody, ACL, canonical JSON, CapturePlan/Apply/Inspect identity chain,
dispatch ownership and receipts as the forms importer. Its reviewed binding
shape is `config/member_welcome_email_bounded_import.v1.template.json`: one
gateway origin, the `gateway_mailer_bearer` role on exactly the four mailer
gateway nodes, and the `smtp` role on exactly the Send Email node. It never
calls the gateway (a claim would consume a lease) and never sends mail. It
refuses an active or MCP-exposed workflow, any non-manual trigger or webhook,
runtime state, Send Email retry, n8n attribution, any Reply-To, and any literal
address in the Send Email fields; the gateway supplies sender, subject and body
at claim time. Private operations live under
`.n8n-local/member-welcome-email-bounded-import/operations/<operation-id>/`.

```text
python -m unittest tests.test_member_welcome_email_bounded_import_security -v
```

The hosted `bounded-import-security` job runs that command as a separate step.
