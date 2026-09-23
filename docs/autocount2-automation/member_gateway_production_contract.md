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
normalized line endings, stable whitespace/name handling, canonical opaque
phone digits, lower-case trimmed email, birthday month, explicit `Yes`/`No`
marketing consent, and explicit PDPA acknowledgement. Sorted-key JSON without
insignificant whitespace is hashed before ingest.

`response_id` is immutable. A same-ID/same-hash replay returns the original job;
a same-ID/different-hash observation is a conflict; a different ID remains a
different signup even when its customer fields match. The checked-in inactive
adapter consumes the Google Forms v1 `responses` shape and maps required values
only through the versioned question-ID allowlist. Config v2 requires a closed
set of distinct question IDs, the fixed `source_production_cutover_exact`, and
the closed `source_admission_mode` (`first_member` or `continuous`) before
admission.

### Exact source identity and the fixed cutover

Google's `createTime` is preserved byte-for-byte as `create_time_exact`. Only
the Google-issued UTC `Z` forms with 0, 3, 6 or 9 fractional digits are
accepted; any other width, any offset and any naive value is rejected at the
ingest API and again at durable admission. Ordering against the cutover uses
the lossless `(epoch_second, nanoseconds)` key; nothing on the identity path is
round-tripped through Python `datetime`, PostgreSQL `timestamptz`, or a
JavaScript `Date`. The same `responseId` with a byte-different exact
`createTime` is an immutable-source conflict even when both strings name the
same instant. `create_time_utc` (truncated, never rounded, to microseconds) is
a business derivative only: it feeds `RegisterDate` in `Asia/Singapore` and the
worker envelope, so a 9-digit fraction can never move `RegisterDate` forward.

`source_production_cutover_exact` is the only source exclusion lower bound.
Every scan epoch uses the verbatim inclusive filter
`timestamp >= <production_cutover_exact>`. It never auto-advances: no admitted
response, terminal page, highest observed timestamp, page token or completed
epoch moves it. The deprecated watermark, admitted tuple
(`last_admitted_create_time`, `last_admitted_response_id`), resume token and
0004 page receipts remain physically present as audit-only history; no code
path reads them as admission, filtering, checkpoint or skip authority.

### Receipt-driven admission

Admission is driven by one durable handling receipt per `responseId`:

- matching prior receipt (exact `createTime`, form binding, mapping version and
  payload fingerprint) replays the original job or rejection;
- any conflicting exact source identity or fingerprint fails closed;
- an unseen customer-valid response atomically records the source response,
  the member job and an `ACCEPTED` receipt;
- an unseen customer-invalid response with a valid source identity atomically
  records a PII-free rejection (`xb.member.source_rejection.v1`: fingerprint and
  a customer-validation code only) and a `REJECTED` receipt.

An unseen response is never compared with another response's position.
Mapping, identity, timestamp, token, schema and OAuth failures are never
rejections; they receive no receipt and block the page.

### Scan epochs and the two-phase page protocol

One `ACTIVE` epoch exists per source binding. Page tokens are private,
epoch-scoped hints; repetition is rejected only within the same epoch, and the
same opaque token in a successor epoch is not a conflict. Each page is
`OPEN` (request token, next token, terminal flag and item identities persisted
before the epoch token can move), then one `ACCEPTED`/`REJECTED` receipt per
item, then `COMMIT`, which verifies a matching receipt for every item (a
foreign key to the receipt makes this structural), appends page-item relations
and advances the epoch token under compare-and-set. An empty terminal page may
commit; only a committed terminal page completes an epoch. OPEN and COMMIT are
idempotent for an identical replay. Restart from the same fixed cutover is
allowed only for `token_invalidated` or `ambiguous_crashed_attempt`; any other
reason is `422 source_restart_reason_invalid`.

### Admission modes

`first_member` uses page size 1 and an atomic 0 -> 1 accepted-member guard.
A valid customer rejection may checkpoint without consuming the allowance. A
losing concurrent member admission gets no receipt, cannot checkpoint, and is
neither rejected nor skipped. `continuous` (only after separate Owner/Web
acceptance) begins a new mode-bound epoch from the same cutover, abandoning the
`first_member` epoch with `admission_mode_changed`; every response, rejection,
job and page record is retained and replays. Production activation no longer
changes source admission semantics.

### Cursor-v2 endpoints

All carry scope `source.ingest`:

```text
GET  /v1/source/cursor?form_alias=&mapping_version=   cursor v2 + active epoch
POST /v1/source/epochs/begin                          begin or resume the ACTIVE epoch
POST /v1/source/epochs/{epoch_id}/restart             allowlisted restart reason
POST /v1/source/epochs/{epoch_id}/pages/open          CAS OPEN
POST /v1/source/pages/{page_id}/commit                CAS COMMIT (verifies receipts)
POST /v1/source-rejections                            PII-free REJECTED receipt
```

Closed error codes include `source_epoch_state_version_mismatch`,
`source_page_token_unexpected`, `source_page_token_repeated_in_epoch`,
`source_page_item_unreceipted`, `source_page_item_receipt_mismatch`,
`source_page_already_open`, `source_event_before_cutover`,
`source_identity_payload_conflict`, `initial_source_window_exhausted` (409) and
`source_restart_reason_invalid` (422). Cursor v2
(`schemas/member_gateway_source_cursor.v2.schema.json`) omits the deprecated
admitted tuple and resume token. `schemas/member_gateway_source_cursor.v1.schema.json`
is retained unchanged for compatibility; the v1 page-checkpoint route is removed.

## Member semantics

The source `create_time` is converted to `Asia/Singapore`. Its Singapore
calendar date becomes `RegisterDate`; `ExpiryDate` is that date plus two
calendar years minus one day. `MemberType` is `Default`, `OpeningPoints` is
zero, and the existing repository DOB representation is preserved:
`Birthday Month -> 2000-MM-01`.

`MobilePhone` is the canonical normalized phone: the exact ASCII digits the
member supplied, `^[0-9]{6,15}$`, after presentation characters are removed. A
single leading `+` is accepted only as the first non-whitespace character and is
stripped; ASCII spaces, hyphens, parentheses and dots are stripped without
interpretation. Letters, extension syntax, Unicode digits, internal
tabs/newlines, extra or mid-string `+`, unsupported punctuation, and empty or
separator-only input are rejected. A country code is optional and is never
required, validated, inferred, or prepended, so international and local numbers
are both valid and a local-looking number is never equivalent to the same
number carrying a country prefix. Leading zeroes are preserved and the value is
never treated as an integer. The 15-digit ceiling is what keeps the effective
production allocation horizon intact: 15 digits plus `X9999` is exactly the
20-character `member_no_max_length`.

`MemberNo` is independent, but starts with the canonical phone-shaped base and
progresses only as `base`, `baseX1`, `baseX2`, and so on. A positive-free
lookup is required before durable binding. An existing binding always wins.
Ambiguous or unavailable lookups never advance the suffix. A bound candidate is
rechecked immediately before the dispatch fence; unexpected occupancy becomes
manual review rather than silent reallocation. No truncation, alternate suffix,
or wraparound is implemented.

The effective production `member_no_max_length` is required configuration and
must be exactly 20. Repository/schema evidence does not
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

Bearer credentials authenticate callers; they are not lease identities.
Production binds exactly five pairwise-distinct principals: source
(`source.ingest`), operator (`operator.status.read` and
`operator.reconciliation.read`), control (`control.kill_switch` and
`control.activate`), the normal worker (the frozen worker scope set), and
recovery (`worker.writer_termination_recovery` only). Their environment names,
configured SHA-256 digests, and resolved runtime values must each be pairwise
distinct. There is no role union. The reference-HMAC key is a separate runtime
boundary and cannot authenticate HTTP. Missing, malformed, aliased, or
mismatched bindings fail before listening. Credential values remain outside
Git, errors, and logs.

## Production bootstrap and operator reads

The only production entry is `python -m xb_member_gateway --config
<reviewed-external-config>`. It loads closed config v2, enforces safe defaults,
resolves only named PostgreSQL, bind, reference-HMAC, and six bearer
boundaries (source, operator, control, worker, recovery, mailer), validates
separation, builds the authenticator and repository, and performs read-only
admission checks for migrations 0001 through 0005, control rows, the
initialized watermark, exact production cutover and form binding, the absence
of any source response without an exact `createTime` or handling receipt, and
the absence of any `CREATED_VERIFIED` result without a welcome outbox row. Only then may it construct
the service/application and listen. Bootstrap never migrates, initializes,
repairs, discovers identity, generates credentials, clears the kill switch, or
activates the gateway; failures expose bounded codes only.

Startup composition admits exactly one dark-bring-up exemption. A gateway whose
`autocount_adapter_ready` is false may compose and listen provided every other
config, readiness, separation, and repository admission check passes.
`GatewayConfig.readiness_reasons()` is unchanged, so adapter-not-ready still
returns readiness false, still publishes `autocount_adapter_not_ready` in the
readiness reasons, still fails the dispatch eligibility predicate, and still
leaves AutoCount execution unavailable. The activation-false and
kill-switch-true startup fence is unchanged and is evaluated before the
exemption. Every unrelated readiness or config failure remains fail closed.

The resolved bind address must be a private IP literal. Bootstrap rejects a
malformed or non-literal value as `bind_address_invalid`, an IPv4 or IPv6
unspecified or wildcard value as `bind_address_unspecified`, and a multicast,
reserved, or public value as `bind_address_not_private`. IPv4-mapped IPv6 forms
are unwrapped before classification, so a mapped wildcard is rejected as a
wildcard. The supplied value is never echoed in the bounded code, no name
resolution is performed, and there is no fallback to `0.0.0.0` or `::`. The
application does not terminate TLS; deployment places the gateway on its fixed
private backend address.

The operator status and reconciliation GET endpoints are read-only safe
projections. They expose readiness/control state, bounded lease/hold/proof and
uncertainty summaries, and public reconciliation/result lineage. They never
expose worker/process/host identity, MemberNo, source payloads, raw response
IDs, credentials, or page tokens. Operators have no mutation route; existing
control and worker mutation scopes remain separate.

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
The separate 0004 migration adds only the durable Forms cursor and append-only
page receipts. It does not modify migrations 0001-0003 and does not seed a real
watermark or cursor; one-time production initialization is a later authorised
deployment transaction.
The additive 0005 migration (`0005_member_vertical_slice.sql`) adds exact source
time columns, unified handling receipts, PII-free rejections, scan epochs,
pages and page items (token uniqueness scoped to the epoch), append-only and
immutability triggers, the `welcome_v1` outbox and its append-only events. It
modifies no earlier migration, deletes nothing and seeds no production cutover
or form binding. Rows admitted before 0005 cannot prove their exact Google
string, so the column stays nullable and bootstrap readiness fails closed with
`source_exact_time_backfill_missing` until a reviewed backfill; exact values are
never guessed.

The first `results` insert on both the leased worker path and the confirmed
termination path previously named 11 columns but supplied 12 placeholders for
11 parameters, so psycopg rejected it and no positive result could be recorded
against a real database. Both statements now share one 11/11 statement, and the
offline PostgreSQL test cursor runs every statement through psycopg's real query
adaptation so a placeholder/parameter mismatch cannot pass silently again.
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

## Private HTTPS deployment topology

The production member gateway is reached only over private HTTPS. The gateway
application serves plain HTTP on a fixed private backend address and is never
the TLS terminator and never host or publicly exposed.

A dedicated pinned nginx reverse-proxy container is the sole TLS terminator and
the sole holder of the leaf private key. It reaches the gateway only across a
dedicated internal backend bridge. Two container networks are added: one
internal backend bridge carrying only the gateway and the ingress, and one
separate n8n/ingress bridge. n8n reaches the ingress by container DNS over
private HTTPS and uses no host port. The accepted PostgreSQL deployment is
unchanged apart from the gateway attaching to its network: it remains
`internal=true`, publishes no host database port, and keeps its existing
least-privilege application DSN.

The outbound-only AC2 worker reaches one host HTTPS publication. That
publication is bound exclusively to a Hyper-V Internal switch host endpoint.
Before any host binding, elevated read-only metadata must positively prove that
the target switch type is `Internal`, that the accepted AC2 VM is attached to
it, and that the host-endpoint subnet is not LAN routable. Any ambiguity or
mismatch halts the deployment. There is no wildcard, LAN, or public fallback,
and no public tunnel, NAT, or public DNS is used for the member gateway. The
unrelated public tunnel stack is not modified or used.

Trust is supplied by a private CA and a leaf certificate whose subject
alternative names cover both caller classes: the ingress container name used by
n8n, and the private endpoint address used by the AC2 worker. No public DNS name
is required, and certificate verification is never disabled on either caller.
Keys and certificates live outside Git under a restrictive ACL.

For n8n the selected mechanism is a read-only CA certificate mount plus the
`NODE_EXTRA_CA_CERTS` environment binding. It was selected against the observed
n8n runtime, which is Node on Alpine, where Node does not consult the operating
system trust store by default. This is the mechanism this deployment uses; it is
not asserted to be the only CA mechanism available to a Node 24 runtime. For the
Windows AC2 worker, the private CA is imported into the machine trusted-root
store as a separately approved mutation. The AC2 worker already refuses any base
URL that is not HTTPS.

Gateway and ingress containers are created with a no-restart policy and are
started manually for dark bring-up, so no unattended activation authority is
created. Dark bring-up keeps production activation false, the kill switch true,
the adapter not ready, and the initialized source cursor and watermark
unchanged.

## Welcome email outbox

Owner-approved `welcome_v1`: From `X-Boundaries <noreply@x-boundaries.com>`, no
Reply-To, subject and plain-text body `Welcome to X-Boundaries!`. The gateway,
never form input or the n8n export, constructs the message and records its
hash; the mailer sends it verbatim after verifying that hash.

A unique outbox row (`UNIQUE (response_id, template_id)`, `UNIQUE (job_id)`) is
inserted inside the same `acknowledge_result` transaction that first records
`CREATED_VERIFIED`, on both the leased worker path and the exact-match
reconciliation projection. A database trigger refuses any outbox insert without
a `CREATED_VERIFIED` result. A duplicate positive acknowledgement verifies the
existing outbox identity and fails closed if it is missing; historical success
is never backfilled. Uncertain, absent, mismatch, rejected, manual-review and
dead-letter outcomes create no outbox row.

State machine:

```text
PENDING -> LEASED -> RETRY_WAIT              (failure before any send intent)
                  -> DEAD_LETTER             (third definitively safe failure)
                  -> SEND_INTENT_RECORDED -> SENT
                                          -> DELIVERY_OUTCOME_UNCERTAIN
RETRY_WAIT -> LEASED | DEAD_LETTER
```

A row becomes claimable one minute after `CREATED_VERIFIED`, then +5 and +30
minutes after the first and second safe failures; the third safe failure
dead-letters. The claim lease is 120 seconds and only one row may be leased at
a time. Send Email automatic retry is disabled. Positive Send Email completion
records `SENT`, meaning SMTP accepted the message, not inbox delivery. After a
send intent, any timeout, crash, lost acknowledgement or unclassified outcome
(including lease expiry) is `DELIVERY_OUTCOME_UNCERTAIN`, which is never
claimable, so no blind resend can happen; a future resend needs private positive
proof that SMTP did not accept. Email failure never touches the member job,
allocation or AutoCount.

The sixth `configured-mailer` principal holds only `welcome_email.claim`,
`welcome_email.send_intent` and `welcome_email.result`; it is denied every
member, source, control and operator route, and no other principal can reach
the welcome-email routes:

```text
POST /v1/welcome-emails/claim
POST /v1/welcome-emails/{outbox_id}/send-intent
POST /v1/welcome-emails/{outbox_id}/result
```

The mailer claim envelope is `xb.member.welcome_email.job.v1`; recipients,
response IDs and message payloads stay private. Public-safe evidence is limited
to outbox/job IDs, the HMAC source reference, hashes, state, version, attempt,
template and bounded error codes.

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
