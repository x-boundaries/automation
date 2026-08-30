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
different signup even when its customer fields match. Poll pagination rejects
repeated or malformed page tokens and the overlap helper removes duplicate
observations before ingest.

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
recheck, and zero prior SaveMember invocations.

## State and irreversible boundary

The validated state machine includes `RECEIVED`, `VALIDATED`, `QUEUED`,
`LEASED`, `PRECHECKING`, `ALLOCATION_BOUND`, `WRITE_INTENT_RECORDED`,
`WRITING`, `READBACK`, and `CREATED_VERIFIED`, plus explicit validation,
ambiguity, retry, uncertain, mismatch, manual-review, and dead-letter states.

The durable dispatch fence is the irreversible boundary. It is unique per job,
requires the bound allocation and write intent, and records zero SaveMember
invocations before the worker call. After it exists, there is no automatic
retry, no new suffix, and no return to the normal create queue. A timeout,
exception, or crash is uncertain. Reconciliation calls only `GetMember` for
the same bound MemberNo. Positive absence does not authorize recreation;
mismatch or ambiguous lookup remains manual/reconciliation work.

## AutoCount adapter contract

The adapter is shaped around an authenticated local official session,
`MemberCommand.Create(...)`, `GetMember(MemberNo)`, `NewMember(false)`, and
exactly one `SaveMember(MemberEntity)`. It assigns and verifies:

`MemberNo`, `MemberType`, `Name`, `MobilePhone`, `EmailAddress`, `DOB`,
`RegisterDate`, `ExpiryDate`, `OpeningPoints`, `IsActive`, and `Individual`.

`IsActive` and `Individual` are adapter-managed defaults. Update, delete, direct
SQL, batch writes, and fallback creation are outside this contract. Runtime
session construction is intentionally an external deployment prerequisite and
is not represented by a repository credential.

## Persistence and trust boundary

The migration stores source responses and observations, ingest receipts, jobs,
attempts, leases, allocation probes and bindings, write intents, dispatch
fences, append-only result events plus a current result projection,
reconciliation cases/checks, rejections, dead letters, control flags, audit
events, and schema versions. Foreign keys use restrictive delete behaviour.
No database transaction remains open across an AutoCount call.

Normal logs contain only run/request/job/attempt metadata, state, operation,
safe error codes, timing/counts, and versioned keyed HMAC references. Names,
phones, email, DOB, MemberNo, raw response IDs, payloads, credentials, SQL,
and private server/database identity are not normal log fields.

The committed n8n export is inactive, credential-free, placeholder-only, and
has a false activation gate. It is not imported, activated, or executed by this
run. CI is offline-only and does not contact Google, n8n, AutoCount,
PostgreSQL, Docker, or a deployment target.

## Unsupported production prerequisites

Before any separately controlled activation, an owner must positively verify:

- the effective account-book MemberNo length and accepted field types;
- the licensed official AutoCount assembly/version and session factory;
- the private HTTPS gateway/auth deployment and worker credential issuance;
- PostgreSQL provisioning, migration application, backup, monitoring, and access policy;
- Google Forms API authentication, form alias/question mapping, and pagination policy;
- operator approval, kill-switch ownership, reconciliation handling, and rollback/runbook ownership.

None of those prerequisites is claimed as proven by this repository change.
