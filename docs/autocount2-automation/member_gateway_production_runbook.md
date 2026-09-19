# Member gateway production runbook

This is a preparation and review runbook for the bounded #155 G3 repository
implementation. It does not authorize deployment, activation, live imports, or
customer-data handling.

## Safe repository checks

Run the focused package tests, worker/static tests, schema and migration checks,
the offline n8n validator, relevant existing regression tests, structural
checks, `git diff --check`, and the final secret/PII/scope scan. Use synthetic
fixtures only. Do not set production credentials or enable the workflow while
reviewing the branch.

The committed config v2 example must remain activation-disabled,
kill-switch-on, adapter-not-ready, fixed to `member_no_max_length=20`, and
without real form/question IDs, cutover watermark, credential digests, database,
bind, TLS, host, or gateway bindings. Those values make readiness fail closed
until an owner supplies reviewed deployment configuration outside Git.

Before any listener starts, run only `python -m xb_member_gateway --config
<reviewed-external-config>`. Bootstrap must read and validate config, resolve
the named runtime boundaries, validate all five principal separations, build
the strict authenticator and repository, and read-only verify migrations
0001-0004, required control rows, and initialized cursor/watermark consistency.
Any bounded bootstrap error is a stop condition. Do not let bootstrap apply a
migration, initialize the cursor, repair state, generate a credential, clear
the kill switch, or activate the gateway.

Dark bring-up is the one composition exemption: `autocount_adapter_ready=false`
no longer blocks startup composition, provided production activation is false,
the kill switch is true, and every other admission check passes. Adapter-not-
ready still reports readiness false, still lists `autocount_adapter_not_ready`,
and still blocks dispatch, so a composed dark gateway is not an activated one.
The bind address must be a private IP literal; a wildcard, unspecified,
malformed, multicast, reserved, or public value is refused with a bounded code
that does not echo the value, and there is no fallback bind.

## Private HTTPS dark bring-up

This sequence prepares the dark gateway. It does not activate the gateway, start
a worker, or touch AutoCount or member data.

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
database connectivity working over the unchanged DSN; production activation
false, kill switch true, adapter not ready with readiness false and dispatch
ineligible; the five bearer principals and the reference-HMAC key separated by
environment name, configured digest, and resolved value, with the
reference-HMAC key unable to authenticate HTTP; cursor, watermark, control rows,
and business state unchanged, with source, jobs, results, allocations, attempts,
and write intents all zero; no n8n execution or activation; no worker start; and
no AutoCount, AC2, or member activity.

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

## Pre-activation review

An owner must independently verify the unsupported prerequisites in the
production contract. In particular, repository evidence of a 20-character
column is not sufficient evidence of effective account-book behaviour. Confirm
the official local API surface in the installed licensed environment without
using direct SQL or a real member write.

Review the source mapping against the current form contract. Preserve immutable
`responseId`, authoritative `createTime`, explicit marketing `Yes`/`No`, and
PDPA acknowledgement. Marketing `No` must remain eligible for membership
creation.

Confirm private transport, authentication scopes, database backups, operator
access, alerting, and manual reconciliation ownership. Bind exactly five
pairwise-distinct source, operator, control, normal-worker, and recovery bearer
principals. Keep their environment names, configured digests, and runtime
values pairwise distinct; do not combine roles. Bind recovery only to
`worker.writer_termination_recovery`, and never use the reference-HMAC key as a
bearer.

Apply migration 0004 and initialize its immutable production watermark/cursor
only in a later separately authorised deployment transaction. The configured
watermark must exactly match the persisted value. Review the closed question-ID
map, inclusive Forms query, `(create_time, response_id)` ordering, and one-new-
response initial window. A page-token checkpoint is valid only after all
eligible page responses have identical durable receipts. Terminal scans restart
inclusively; overlaps are expected and conflicting history is a stop condition.
Keep worker concurrency and claim size at one for this initial topology. The repository must enforce
one active non-expired worker lease across concurrent claim requests. A worker
run must generate one bounded `ws-` session identifier and send it in
`X-XB-Worker-Session`; it is an execution identity, not a credential or host
identity. Do not substitute a username, SID, hostname, or private path.
Claim transactions hold the existing `kill_switch_enabled` control row lock for
their full transaction, providing the durable singleton mutex across processes.

Before the irreversible call, refresh the job lease with the current
`state_version`. The worker keeps one `ws-` session and starts the reviewed
AutoCount writer in one supervised child process. It renews the lease on the
configured heartbeat cadence and uses `attempt_started_at` plus the configured
execution deadline as the hard boundary. A heartbeat failure or deadline causes
the child to be terminated and its exit to be positively observed before lease
protection can lapse. Failure to confirm exit is fail closed into durable
quarantine; it never starts a second writer or retries `SaveMember`.

The dispatch-fence transaction creates a `PENDING` writer hold before the child
starts. The child receives no payload while pending, so it cannot create the
AutoCount session or reach `SaveMember`. The parent records the exact PID and
process-start timestamp, registers them with the existing fence/attempt/session/
host/execution bindings, waits for the `REGISTERED` acknowledgement, and only
then releases stdin. The registered child is the single writer process and may
not detach or spawn a SaveMember-capable descendant.

## Operating sequence after a separately approved activation

The kill switch defaults to ON. To engage it, use the separately scoped
`POST /v1/control/kill-switch/enable` operation with the `control.kill_switch`
scope and an empty body. Engagement fails closed for new claims and dispatch.
Clearing is separate: use `POST /v1/control/kill-switch/disable` only after the
owner has verified the remaining predicates and is observing the approved
window.

1. Keep the kill switch engaged until readiness, source mapping, and adapter
   checks are green.
2. Enable only the approved private source/gateway transport and verify that
   the workflow remains inactive until the explicit activation procedure.
3. Enable gateway activation through the scoped control endpoint with an
   external approval reference and a verified effective MemberNo limit.
4. Clear the kill switch only when the owner is observing the first bounded
   synthetic or approved operational window.
5. Observe job states and safe counts. Never treat a timeout as proof of
   absence or success.

## Uncertain write handling

After a dispatch fence, do not replay the job, call SaveMember again, or select
another suffix. First verify that the writer hold is
`TERMINATION_CONFIRMED`. A normal child result is posted only after that
durable confirmation. If confirmation is unavailable, preserve the fence,
allocation, and write-intent lineage in `WRITER_TERMINATION_UNCONFIRMED` with a
`QUARANTINED` hold; do not infer safety from elapsed time, a Kill() return,
watchdog callback, or a restart. A quarantined job cannot be claimed,
reallocated, written, expired into ordinary uncertainty, or reconciled.

Only the distinct runtime recovery principal, with a fresh host-bound session,
exact recorded process identity, and positive exit evidence, may resolve the
termination side of a quarantined fence. The normal worker principal is denied
this route even if it supplies a newly created worker session. Recovery does not
retry SaveMember or allocate a new MemberNo. When termination is confirmed but the business outcome is unknown,
the repository atomically creates exactly one immutable
`WRITE_OUTCOME_UNCERTAIN` event and current projection, moves the job to that
existing result state, clears the hold, and releases the stale lease. The
existing reconciliation path then applies only to that ordinary uncertainty:
the hold must be cleared, no lease may remain active, and the existing case and
read-only check evidence must be present. Positive absence does not authorize
automatic recreation; mismatch or ambiguity remains manual review.

## Stop and disable

Use the scoped `POST /v1/control/kill-switch/enable` operation on any source,
mapping, readiness, lease, allocation, adapter, database, or readback anomaly.
The worker must stop claiming and no new dispatch fence may be recorded while
the switch is set. Do not delete history or reset a job to make a retry appear
clean; preserve the source, allocation, intent, fence, result, and
reconciliation lineage. Only the separately scoped `.../disable` operation may
clear the switch after controlled review.

The repository check is authoritative at the mutation boundary. In memory, the
kill-switch read and claim/fence mutation share one repository lock. In
PostgreSQL, the mutation transaction locks the `kill_switch_enabled` control row
with `FOR UPDATE` before creating a lease or fence; a missing row blocks closed.
The API readiness check is only defence in depth.

## Recovery boundaries

Before a dispatch fence, lease expiry may move a job through bounded retry or
dead-letter handling while retaining the durable allocation and intent. After a
fence, expiry while the writer hold lacks positive termination proof preserves
quarantine and global exclusion. If termination proof was already durable,
expiry may atomically settle the existing ordinary
`WRITE_OUTCOME_UNCERTAIN` event/projection and clear the hold. There is no
automatic suffix advance or second SaveMember attempt. Manual review is the
safe terminal path when lookup or readback evidence is not positive and exact.

The candidate `FREE` probe is not the final dispatch evidence. Immediately
before write intent/fence, the worker rechecks the already bound MemberNo using
a distinct public-safe probe reference. The repository durably binds that
marker to the job, attempt, worker session, bound MemberNo, and `FREE` result;
stale, missing, conflicting, or non-FREE evidence blocks the write boundary.
Reconciliation is permitted only from exact `WRITE_OUTCOME_UNCERTAIN` after
the original writer lease has expired and been reclaimed, then an existing case
and read-only check must be recorded. A `WRITING` job or live writer cannot be
reconciled.

The published fence ID remains the `fence-...` representation. PostgreSQL's
existing UUID storage is internal only and is converted at the repository
boundary; the A1 migration adds the writer hold without changing that public
representation. The repository rate gate is fail closed and derives eligibility only from the
current sole active lease/attempt evidence; no new owner-facing numeric rate is
defined here.

The public job/status contract is versioned as
schemas/member_gateway_job.v2.schema.json. It exposes only the safe
writer-termination state and proof-required/hold-active indicators; it does
not expose PID, process-start timestamp, host identity, nonce, or raw evidence.
The result-status vocabulary remains the existing v1 vocabulary.

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
resolved credential IDs and names, cursor watermark, and source token stay in
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
replace it with the actual approved origin. All three gateway endpoints must
match that approved scheme, host, and port while retaining their reviewed
path relationships. The Forms request must be exactly
`https://forms.googleapis.com:443/v1/forms/<form-id>/responses`. Alternate
hosts, ports, versions, form IDs, queries, encoded/case/trailing-dot host
variants, and other canonicalisation tricks are fail-closed before any token,
credential, or Docker access.

### Required sequence

1. Reconcile the protected canonical main revision, the parent/child issue
   authority, the frozen predecessor PR, and the exact canonical workflow blob.
   A changed authority packet requires a new reviewed plan.
2. Prepare the private manifest and acquire the authoritative source cursor,
   watermark, exact project/workflow metadata, and either the exact existing
   workflow export or complete absence evidence. A case-distinct target is a
   collision, not an absent target.
3. Run `CapturePlan` read-only. The plan binds the repository H/tree/parent,
   canonical workflow blob, project/workflow identity, prepared workflow,
   resolved credential-binding digest, cursor digest and state version,
   domain-separated watermark digest, and existing/absent preimage digest.
4. Review the immutable files under
   `.n8n-local/member-gateway-bounded-import/operations/<operation-id>/`.
   They must contain only the plan, binding, cursor state, prepared workflow,
   mutation intent, exactly one preimage/absence receipt, and later receipts.
   A containerised apply additionally records one private
   `container-custody.json` receipt before import. It binds the container and
   image IDs, non-root import UID/GID, random nonce, sticky `/tmp` proof,
   private staging ownership/modes, exact prepared bytes/hash, and cleanup
   state.
5. Immediately before `Apply`, reacquire and compare the cursor, watermark,
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
