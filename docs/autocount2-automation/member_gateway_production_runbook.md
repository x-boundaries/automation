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
without a MemberNo limit, and without a worker-token digest. Those values make
readiness fail closed until an owner supplies deployment configuration outside
Git.

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
access, alerting, and manual reconciliation ownership. Keep worker concurrency
and claim size at one for this initial topology. The repository must enforce
one active non-expired worker lease across concurrent claim requests. A worker
run must generate one bounded `ws-` session identifier and send it in
`X-XB-Worker-Session`; it is an execution identity, not a credential or host
identity. Do not substitute a username, SID, hostname, or private path.
Claim transactions hold the existing `kill_switch_enabled` control row lock for
their full transaction, providing the durable singleton mutex across processes.

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
another suffix. Use the same bound MemberNo and the read-only `GetMember`
reconciliation path. An exact readback can close as verified. Positive absence
closes as `CONFIRMED_NOT_CREATED` for manual follow-up and does not authorize
automatic recreation. Any mismatch or ambiguous lookup stays in manual review.

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
fence, lease expiry becomes uncertainty. There is no automatic suffix advance
or second SaveMember attempt. Manual review is the safe terminal path when
lookup or readback evidence is not positive and exact.

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
boundary; the existing migration is therefore retained unchanged. The
repository rate gate is fail closed and derives eligibility only from the
current sole active lease/attempt evidence; no new owner-facing numeric rate is
defined here.

## Not performed by this run

No live Google Forms/Sheets, n8n instance, PostgreSQL server, AutoCount account
book, Docker/service, VM, network, credential, scheduler, production
activation, deployment, or existing UAT package execution is part of this
runbook execution.
