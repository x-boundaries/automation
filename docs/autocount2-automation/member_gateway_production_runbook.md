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

The committed example config must remain activation-disabled, kill-switch-on,
without a MemberNo limit, and without worker or recovery credential digests.
Those values make readiness fail closed until an owner supplies deployment
configuration outside Git. The worker and recovery credential sources must also
remain distinct; equal digests or aliased sources are refused.

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
access, alerting, and manual reconciliation ownership. Bind the normal worker
credential to ordinary worker scopes only, and bind a separate recovery
credential to only `worker.writer_termination_recovery`; do not grant recovery
authority to the normal worker or ordinary worker/control authority to recovery.
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
