# Member gateway production contract

Status: bounded repository implementation for #155 G3. This document describes
the repository boundary only. Production activation remains disabled.

## Boundary

The member-first path is:

`Google Forms API -> n8n source adapter -> protected XB Gateway API -> private PostgreSQL -> one outbound-only AC2 worker -> official local AutoCount API`

The gateway accepts one operation, `member.create`. It is not a generic
AutoCount proxy. The worker has no inbound HTTP, PowerShell remoting, SQL,
RDP, scheduler, or batch-write surface.

## Source event

The closed `xb.member.source_event.v1` object requires `source_system` to be
`google_forms`, an allowlisted form alias and mapping version, an opaque
`response_id`, RFC3339 `create_time`, request identity, the canonical member
payload, and its SHA-256 payload hash. Canonicalization uses UTF-8, Unicode NFC,
normalized line endings, stable whitespace/name handling, canonical Singapore
phone, lower-case trimmed email, birthday month, explicit `Yes`/`No` marketing
consent, and explicit PDPA acknowledgement. Sorted-key JSON without
insignificant whitespace is hashed before ingest.

`response_id` is immutable. A same-ID/same-hash replay returns the original job;
a same-ID/different-hash observation is a conflict; a different ID remains a
different signup even when its customer fields match. The checked-in inactive
adapter consumes the Google Forms v1 `responses` shape and maps required values
only through the versioned question-ID allowlist; it follows `nextPageToken`
with a bounded `pageToken` request, rejects malformed or repeated tokens, and
de-duplicates overlapping `responseId` observations before ingest.

## Member semantics

The source `create_time` is converted to `Asia/Singapore`. Its Singapore
calendar date becomes `RegisterDate`; `ExpiryDate` is that date plus two
calendar years minus one day. `MemberType` is `Default`, `OpeningPoints` is
zero, and the existing repository DOB representation is preserved:
`Birthday Month -> 2000-MM-01`.

`MobilePhone` is the canonical normalized phone. `MemberNo` is independent,
but starts with the canonical phone-shaped base and progresses only as
`base`, `baseX1`, `baseX2`, and so on. A positive-free lookup is required before
durable binding. An existing binding always wins. Ambiguous or unavailable
lookups never advance the suffix. A bound candidate is rechecked immediately
before the dispatch fence; unexpected occupancy becomes manual review rather
than silent reallocation. No truncation, alternate suffix, or wraparound is
implemented.

The effective production `member_no_max_length` is required configuration and
must be an integer from 10 through 20. Repository/schema evidence does not
prove the installed account-book limit. Missing, invalid, or incompatible
configuration blocks readiness and dispatch.

## Consent and eligibility

PDPA acknowledgement must be true under the accepted acknowledgement contract.
Marketing consent must be recognized `Yes` or `No`; missing or unrecognized
values are invalid. `No` sets `marketing_allowed=false` conceptually and does
not block membership creation. No unattended dispatch occurs unless every
eligibility predicate is true, including activation, clear kill switch,
environment/source/mapping identity, valid hash and fields, exact operation,
lease ownership, positive-free evidence, bound allocation, production length
constraint, no prior fence/result/uncertain state, attempt/deadline limits,
gateway/adapter readiness, valid worker credential, immediate kill-switch
recheck, a repository-owned singleton rate/claim decision, and zero prior
SaveMember invocations. The rate predicate is deliberately not an owner-facing
throughput number: it is true only when the current durable lease is valid and
is the sole active non-expired worker lease. Missing or unavailable evidence
is ineligible.

Claim transactions also hold the existing `kill_switch_enabled` control row
lock for the complete claim transaction. That row is the durable singleton
mutex: separate PostgreSQL connections cannot both observe an empty active-lease
set and claim parallel jobs.

The bearer credential authenticates the caller; it is not the lease identity.
Production binds two distinct runtime-only credential sources: the normal worker
credential receives ordinary worker scopes, while the recovery credential
receives only `worker.writer_termination_recovery`. The normal worker credential
does not receive recovery scope, and the recovery credential is not a worker,
source-ingest, result, reconciliation, or control credential. Readiness and
environment authentication fail closed if either configured digest is missing,
the digests match, or the credential sources alias. Credential values and
digests remain outside Git and ordinary logs.

Every worker request carries `X-XB-Worker-Session` with a generated `ws-` plus
32 lower-case hexadecimal characters. The value is generated locally for one
worker process/run, is validated against that closed pattern, and is not a
secret, hostname, Windows/account identity, SID, or private path. The same
bearer used with another session cannot inherit or operate the first session's
lease. The normal worker session is bound to claim, lease/heartbeat, allocation,
write-intent, dispatch-fence, and result operations. Recovery uses a fresh
host-bound session and the exact recorded execution bindings, independently of
the normal worker credential.

The worker refreshes the same lease, with the current `state_version`, immediately
before starting the irreversible AutoCount call. The call runs in one supervised
child process while the singleton worker retains the gateway session. The parent
renews the lease on the configured heartbeat cadence and requires the returned
state version to advance and the lease to remain beyond the absolute execution
deadline. A heartbeat failure, deadline, or protection cutoff terminates the child
and waits until process exit is positively observed before the lease protection can
lapse. If exit cannot be confirmed, the worker fails closed into the durable
writer-termination quarantine described below. An already-dispatched ambiguous
outcome has no SaveMember retry.

## Writer liveness and quarantine (A1)

The dispatch-fence transaction creates one durable writer-execution hold in
`PENDING` state and locks the singleton writer-termination gate. The hold is
bound to the job, fence, attempt, worker session, host binding, opaque execution
identity, and bound MemberNo. PID and process-start identity are intentionally
unset until the process exists. While any hold is active, the repository blocks
another claim, fence, allocation, SaveMember-capable path, ordinary lease
reclamation, and reconciliation. PostgreSQL serializes these transitions by
transactionally locking the gate row; the in-memory repository applies the same
rule under its repository lock.

The parent starts the child with stdin withheld. The child cannot create the
AutoCount session or reach SaveMember until the parent has obtained the exact PID
and process-start timestamp, registered both through the worker-only
`writer/register` CAS operation, and received a durable `REGISTERED`
acknowledgement. Only then does the parent release the payload. The registered
child is the sole SaveMember-capable process for this fence; it does not detach
or spawn a writer descendant.

Normal completion first establishes positive process exit and durably moves the
hold to `TERMINATION_CONFIRMED`, with exact fence/attempt/session/host/execution
and PID/start bindings plus bounded process-exit evidence. Only after that
confirmation does the worker post the business result. Result acknowledgement
does not release the hold early. If exit or proof cannot be established, the
hold becomes `QUARANTINED` and the job becomes
`WRITER_TERMINATION_UNCONFIRMED`; elapsed time, Kill(), a watchdog callback,
restart, or stale session is never proof.

If termination is positively known but the business result is unknown, the
repository atomically creates the single immutable
`WRITE_OUTCOME_UNCERTAIN` event and projection, moves the job to that existing
result state, clears the hold, and releases the stale lease. A recovery operation
is narrower than ordinary writer authority, must be fresh host-bound evidence,
and can only resolve the termination side of a quarantined fence. The recovery
principal has no `job.read` scope because the recovery operation does not require
it. Legacy post-fence uncertainty is materialized as legacy-unproven quarantine;
it is not automatically cleared or made reconcilable.

## Kill-switch control

The repository control plane exposes only the scoped member-gateway kill-switch
operations: `POST /v1/control/kill-switch/enable` engages
`kill_switch_enabled=true` and `POST /v1/control/kill-switch/disable` clears it.
The default is ON. Engaging the switch fails closed for new claims and for the
dispatch fence; clearing it is a separately authorised operation and does not
bypass any other eligibility predicate. These endpoints are not generic database
or administrator controls. The repository owns the final safety boundary:
the in-memory implementation checks the flag and mutates under one
repository lock; PostgreSQL locks the `control_flags` row with `FOR UPDATE`
in the same transaction that creates a lease or dispatch fence. A missing
control row also fails closed.

## State and irreversible boundary

The validated state machine includes `RECEIVED`, `VALIDATED`, `QUEUED`,
`LEASED`, `PRECHECKING`, `ALLOCATION_BOUND`, `WRITE_INTENT_RECORDED`,
`WRITING`, `READBACK`, and `CREATED_VERIFIED`, plus explicit validation,
ambiguity, retry, uncertain, mismatch, manual-review, and dead-letter states.

The durable dispatch fence is the irreversible boundary. It is unique per job,
requires the bound allocation, write intent, and a successful fresh recheck of
that same bound MemberNo, and records zero SaveMember invocations before the
worker call. Allocation probing and the fresh pre-dispatch recheck are distinct
durable observations. The recheck marker records only public-safe lineage for
the job, attempt, worker session, operation reference, and `FREE` status; it
does not log MemberNo. A missing, stale, occupied, ambiguous, unavailable, or
conflicting recheck fails closed without allocating another suffix.

After the fence exists, there is no automatic retry, no new suffix, and no
return to the normal create queue. A missing termination proof remains
`WRITER_TERMINATION_UNCONFIRMED` with an active quarantine and is never
reconcilable. Reconciliation may begin only for exactly
`WRITE_OUTCOME_UNCERTAIN`, with positively confirmed writer termination, a
cleared hold, no active lease, the same bound MemberNo and fence, and the
existing reconciliation case/check evidence. Positive absence does not
authorize recreation; mismatch or ambiguous lookup remains manual/reconciliation
work, and a late writer result cannot overwrite a completed reconciliation.

The public dispatch-fence identifier is always `fence-` followed by the
published safe identifier shape. Both repositories and result construction
expose that representation. PostgreSQL retains its existing internal UUID
columns and converts them at the repository boundary, so a plain UUID is never
emitted as a versioned result fence ID. No additional migration is needed for
this boundary-only representation.

## AutoCount adapter contract

The adapter is shaped around an authenticated local official session,
`MemberCommand.Create(...)`, `GetMember(MemberNo)`, `NewMember(false)`, and
exactly one `SaveMember(MemberEntity)`. It assigns and verifies:

`MemberNo`, `MemberType`, `Name`, `MobilePhone`, `EmailAddress`, `DOB`,
`RegisterDate`, `ExpiryDate`, `OpeningPoints`, `IsActive`, and `Individual`.

`IsActive` and `Individual` are adapter-managed defaults. Update, delete, direct
SQL, batch writes, and fallback creation are outside this contract. The adapter
has an executable reviewed session boundary: it loads the established AutoCount
assemblies, creates `DBSetting`, runs the established `UserSession`
authentication/login sequence, and accepts a private deployment-bound session
factory when supplied. Server, database, user, password-environment name, and
assembly path are process-scoped deployment values; secrets remain outside Git
and are never printed. No repository credential or new credential topology is
introduced.

## Persistence and trust boundary

The append-only 0003_writer_termination_quarantine.sql migration adds the
WRITER_TERMINATION_UNCONFIRMED job state, a singleton writer_termination_gate,
and one versioned writer-execution hold per dispatch fence. It preserves exact
job/fence/attempt/session/host/execution/member bindings, nullable pending
process identity, registered/confirmed/quarantined/cleared lifecycle, bounded
public-safe evidence metadata, and legacy post-fence quarantine materialization.
The versioned schemas/member_gateway_job.v2.schema.json publishes the safe
job/status shape; the result-status vocabulary remains unchanged. The migration
stores these surfaces alongside source responses and observations, ingest
receipts, jobs, attempts, leases, allocation probes and bindings, write intents,
dispatch fences, append-only result events plus a current result projection,
reconciliation cases/checks, rejections, dead letters, control flags, audit
events, and schema versions. Foreign keys use restrictive delete behaviour.
The current projection is replaced by a conditional update only after the
immutable result event is recorded in the same transaction; the unique `job_id`
projection is never delete/reinserted. No database transaction remains open
across an AutoCount call.

Normal logs contain only run/request/job/attempt metadata, state, operation,
safe error codes, timing/counts, and versioned keyed HMAC references. Names,
phones, email, DOB, MemberNo, raw response IDs, payloads, credentials, SQL,
and private server/database identity are not normal log fields.

The committed n8n export is inactive, credential-free, placeholder-only, and
has a false activation gate. It is not imported, activated, or executed by this
run. CI is offline-only and does not contact Google, n8n, AutoCount,
PostgreSQL, Docker, or a deployment target. Private process identity,
host-binding, nonce, and raw evidence never appear in ordinary public job
status or result responses.

## Unsupported production prerequisites

Before any separately controlled activation, an owner must positively verify:

- the effective account-book MemberNo length and accepted field types;
- the licensed official AutoCount assembly/version and session factory;
- the private HTTPS gateway/auth deployment, distinct normal-worker and
  recovery credential issuance, source binding, and digest configuration;
- PostgreSQL provisioning, migration application, backup, monitoring, and access policy;
- Google Forms API authentication, form alias/question mapping, and pagination policy;
- operator approval, kill-switch ownership, reconciliation handling, and rollback/runbook ownership.

None of those prerequisites is claimed as proven by this repository change.
