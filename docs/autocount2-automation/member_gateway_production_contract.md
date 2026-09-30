# Member gateway production contract

Status: repository implementation of the member-write v2 contract (#155
W-G3-152, contract W-G2-149 with Web amendment #155:5890550314). Production
activation remains disabled; the first production member is the separate W-ACT
gate. Canonical architecture: [architecture.md](architecture.md) and the
[member intake automation blueprint](member_intake_automation_blueprint.md).

## Boundary

The member-first path is:

`Google Form -> n8n (SERVER_PC) -> protected member gateway API -> private PostgreSQL (SERVER_PC) <- one outbound-only AC2 worker (AC2_VM) -> narrow local AutoCount member primitive`

The gateway accepts one operation, `member.create`. It is not a generic
AutoCount proxy; a generic AutoCount API facade is deferred and not current.
The worker has no inbound HTTP, PowerShell remoting, SQL, RDP or batch-write
surface. AutoCount is changed only by the primitive, which calls `SaveMember`
at most once per invocation and reads the result back.

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
only through the versioned question-ID allowlist. Config v3 requires a closed
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

## Member semantics (XB-MN-1)

The source `create_time` is converted to `Asia/Singapore`. Its Singapore
calendar date becomes `RegisterDate`; `ExpiryDate` is that date plus two
calendar years minus one day. `MemberType` is `Default`, `OpeningPoints` is
zero, `IsActive` and `Individual` are true, and the existing repository DOB
representation is preserved: `Birthday Month -> 2000-MM-01`.

Ingest canonicalisation is unchanged (payload and `payload_hash` are
byte-identical to the v1 gateway). The canonical phone is the exact ASCII
digits the member supplied, `^[0-9]{6,15}$`, after presentation characters are
removed. A single leading `+` is accepted only as the first non-whitespace
character and is stripped; ASCII spaces, hyphens, parentheses and dots are
stripped without interpretation. Letters, extension syntax, Unicode digits
(including full-width digits), internal tabs/newlines, extra or mid-string `+`,
unsupported punctuation, and empty or separator-only input are rejected at
ingest. A country code is never required, validated, inferred, or prepended.
Leading zeroes are preserved and the value is never treated as an integer.

At `VALIDATED` the gateway computes, once and immutably:

- `base_member_no` = ASCII digits of NFKC(canonical phone), 6 to 15 digits,
  otherwise `REJECTED_VALIDATION(phone_digits_out_of_range)`. A base that starts
  `000` is an ordinary base.
- `name_component` = NFKD(name) with combining marks dropped, ASCII `a-z`
  upper-cased, `A-Z` kept, everything else dropped, truncated to
  `20 - len(base)` letters (so 5 to 14 letters of room; it may be empty).
- `member_no_rule = XB-MN-1`.

Field limits are checked, never truncated: `Name` at most 100 and `EmailAddress`
at most 200 UTF-16 code units (`name_exceeds_autocount_limit`,
`email_exceeds_autocount_limit`). Synthetic markers are a name starting
`ZZTEST ` or an email ending `@example.invalid`. In `member_book_mode=production`
any marker is `REJECTED_VALIDATION(synthetic_in_production)`; in
`member_book_mode=test` both markers are required
(`test_book_requires_synthetic`). There is no numeric-prefix synthetic rule.

The AutoCount MemberNo is chosen by the primitive and can only be `base` or
`base + name_component`; the gateway accepts no other value. Every create writes
`MobilePhone = base`. There is no X1/X2/X3 suffix allocation, no truncation of
input, and no update of an existing AutoCount member.

## Consent and admission predicates

PDPA acknowledgement must be true under the accepted acknowledgement contract.
Marketing consent must be recognised `Yes` or `No`; `No` does not block
membership. These source predicates (PDPA, consent, source system, allowlisted
form alias and mapping version, response and hash identity, exact payload field
set, `member.create`) are enforced when a job is validated
(`REJECTED_VALIDATION` with a bounded reason such as `pdpa_not_acknowledged`)
and again when it is claimed (a stored job that fails them is skipped and never
dispatched). The claim also re-derives the XB-MN-1 identity under the current
book mode and requires it to equal the stored identity.

Bearer credentials authenticate callers; they are not lease identities.
Production binds exactly five pairwise-distinct principals: source
(`source.ingest`), operator (`operator.status.read`,
`operator.reconciliation.read`), control (`control.kill_switch`,
`control.activate`, `control.resolve`), worker (`worker.claim`,
`worker.result`) and mailer (`welcome_email.claim`,
`welcome_email.send_intent`, `welcome_email.result`). The former recovery
principal and every v1 worker scope are removed from code; a recovery key in
configuration is a configuration error. Environment names, configured SHA-256
digests and resolved runtime values must each be pairwise distinct. There is no
role union. The reference-HMAC key is a separate runtime boundary and cannot
authenticate HTTP. Credential values remain outside Git, errors and logs.

## Production bootstrap and operator reads

The only production entry is `python -m xb_member_gateway --config
<reviewed-external-config>`. It loads closed config v3
(`xb.member.gateway.config.v3`, required `member_book_mode`), enforces safe
defaults, resolves only the named PostgreSQL, bind, reference-HMAC and five
bearer boundaries, validates separation, builds the authenticator and
repository, and performs read-only admission checks for migrations 0001 through
0006, control rows, the initialized watermark, exact production cutover and form
binding, the absence of any source response without an exact `createTime` or
handling receipt, and the absence of any `CREATED_VERIFIED` outcome (in either
the legacy `results` table or `member_outcomes`) without a welcome outbox row.
Only then may it listen. Bootstrap never migrates, initializes, repairs,
discovers identity, generates credentials, clears the kill switch, or activates
the gateway; failures expose bounded codes only.

Startup admits exactly one dark-bring-up exemption: a gateway whose
`autocount_adapter_ready` is false may compose and listen if every other check
passes; readiness stays false with `autocount_adapter_not_ready`, so nothing is
dispatched.

The resolved bind address must be a private IP literal (`bind_address_invalid`,
`bind_address_unspecified`, `bind_address_not_private`); no name resolution and
no wildcard fallback. The application does not terminate TLS.

Operator status, reconciliation and job (`xb.member.gateway.job.v3`) reads are
metadata-only projections: state, bounded reasons, attempt and budget counters,
timestamps and lineage. They never expose MemberNo, member Guid, worker/process
identity, source payloads, raw response IDs, credentials or page tokens.
Operator status (`xb.member.gateway.operator_status.v2`) also counts the
primitive's closed data-quality flags (`email_seen_on_other_member`,
`post_save_same_person_other_row`, `malformed_member_no_excluded`), recorded
once per accepted result (per attempt, not per job) as codes only.

## Worker v2 wire contract

```text
GET  /readyz                            {ready, reasons[], dispatch_enabled, server_time_utc}
POST /v2/worker/claim                   scope worker.claim; schemas/member_gateway_worker_claim.v2.schema.json
POST /v2/jobs/{job_id}/result           scope worker.result; schemas/member_gateway_result.v2.schema.json
POST /v2/control/jobs/{job_id}/resolve  scope control.resolve; schemas/member_gateway_resolution.v1.schema.json
```

`dispatch_enabled` is `production_activation_enabled AND NOT kill_switch`.
Every worker request carries `X-XB-Worker-Session` (`ws-` plus 32 lower-case
hex characters, generated per worker run, not a secret or host identity).

The v1 worker surface is removed from code, not disabled by configuration:
`/v1/worker/claim`, `/v1/jobs/{id}/precheck`, `/lease` (heartbeat),
`/allocation*`, `/write-intent`, `/dispatch-fence`, `/writer/{register,
termination,quarantine,recover}`, the v1 `/result` and `/reconcile` routes, the
recovery principal and `allocation.py`/`reconciliation.py`. Removed routes
return `404 route_not_found`. `/v1/source*`, `/v1/welcome-emails/*`,
`/v1/operator/*`, `/v1/control/*`, `/livez` and `/readyz` remain.

A claim returns either `{claimed:false, reason: dispatch_disabled |
no_eligible_job | singleton_busy}` or one job with `attempt_no`, an opaque
`lease_id` (`lease-` plus 32 hex), `state_version`, `lease_expires_at`,
`first_claimed_at`, `server_time_utc` and the `request` object (XB-MN-1 rule,
base, name component, phone, name, email and the member record fields).

The worker cycle is exactly: readiness, claim, run the primitive as a child
process with a hard 300-second deadline, post one result (the identical body, at most three
posts in total, retried only on transport failure or HTTP 5xx, within the lease). There is no
heartbeat: the 600-second lease is at least twice the child deadline.

## AutoCount primitive contract

The primitive (`scripts/ac2_member_create_primitive.ps1`) runs on `AC2_VM`, one
process per job attempt, as the dedicated integration user:

1. Validate the request: shape, digits(phone) = base, name component shape and
   total length at most 20, field limits, explicit `-Book production|test`,
   synthetic rules; clock skew against the gateway at most 120 seconds.
2. Acquire the machine-wide mutex `Global\XB-AC2-MemberCreate` (30 s wait;
   an abandoned mutex counts as acquired; timeout is `MUTEX_BUSY`) and hold it
   through readback.
3. Open the reviewed AutoCount session; the connected book must be the
   configured book for the mode and the session user must be the integration
   user.
4. Probe every Member row (active and inactive): exact holders of the base (H)
   and suffix/prefix format variants of 8+ digits (V). Same person = same email,
   or same name tokens ignoring order. Decide, first match wins:

| Rule | Condition | Outcome |
| --- | --- | --- |
| R0 | attempt 2+ and a row in H or V created by the integration user since the first claim (minus 10 min) | exactly one exact match of all 11 fields -> `CREATED_VERIFIED_PRIOR_ATTEMPT`; else `MANUAL_REVIEW(prior_attempt_ambiguous)` |
| R1 | H and V empty | create `base` |
| R2a | two or more same-person rows | `MANUAL_REVIEW(multiple_same_person)` |
| R2b | one same-person row, inactive | `MANUAL_REVIEW(inactive_match)` |
| R2c | one same-person row, active | `LINKED_EXISTING` |
| R3 | no same person, V not empty | `MANUAL_REVIEW(format_variant_other_person)` |
| R3b | no same person, an H row with no name and no email | `MANUAL_REVIEW(holder_identity_unknown)` |
| R4 | every H row clearly another person | empty component -> `MANUAL_REVIEW(name_component_empty)`; `base+component` already used -> `MANUAL_REVIEW(name_candidate_collision)`; else create `base+component` |

5. On create only: `NewMember(false)`, assign the 11 fields, call
   `SaveMember` exactly once from the single call site, then `GetMember` with
   Guid, creator and creation time, and classify (verified, mismatch, foreign
   row, not created, not created on conflict, or uncertain).
6. Release the mutex and emit one bounded result (no personal data except the
   MemberNo).

## State machine and retries

```text
RECEIVED  -> VALIDATED | REJECTED_VALIDATION
VALIDATED -> QUEUED | REJECTED_VALIDATION
QUEUED | RETRY_WAIT(due) -> LEASED                      (claim)
LEASED    -> CREATED_VERIFIED | LINKED_EXISTING | REJECTED_VALIDATION | MANUAL_REVIEW | RETRY_WAIT
MANUAL_REVIEW -> RESOLVED (CLOSE) | QUEUED (REQUEUE: write budget +3, total cap 12)
```

REQUEUE resets the busy-attempt counter; a REQUEUE with no budget left is
refused (`409 write_budget_cap_reached`). Resolutions are append-only in
`job_resolutions` and never queue a welcome email.

Legacy v1 states remain readable for historical rows and are never entered.

A claim locks the `control_flags` row, requires `dispatch_enabled`, readiness
and no active lease on any job (one active lease across all jobs), reaps an
expired lease (the job goes to `RETRY_WAIT` as an uncertain attempt, eligible
5 minutes after expiry, or `MANUAL_REVIEW(uncertain_exhausted)`), picks the
oldest eligible job, records the attempt, sets `first_claimed_at` once, and
creates a 600-second lease with a fresh token. The kill switch blocks new
claims only; results for work in flight are always accepted.

A result is accepted only if the job is `LEASED`, the lease token is the active
one, and `attempt_no` and `state_version` match (compare-and-set). The same body
again returns the stored response; a different body for the same lease is `409`
and recorded; a stale or replaced lease is `409` with nothing changed. A
lease-matching body that breaks the per-outcome consistency rules (for example
a `CREATED_VERIFIED` whose MemberNo is not exactly `base` under R1 or
`base+component` under R4, or any save count above one) goes to
`MANUAL_REVIEW(result_contract_violation)`.

| Primitive outcome | Gateway state | Welcome email |
| --- | --- | --- |
| `CREATED_VERIFIED` | `CREATED_VERIFIED` + `member_outcomes` row | queued in the same transaction |
| `CREATED_VERIFIED_PRIOR_ATTEMPT` | `CREATED_VERIFIED`; if that Guid is already credited to another job, `LINKED_EXISTING(guid_already_verified)` | only when credited |
| `LINKED_EXISTING` | `LINKED_EXISTING` | no |
| `MANUAL_REVIEW`, `CREATED_READBACK_MISMATCH` | `MANUAL_REVIEW` | no |
| `REJECTED_VALIDATION` | `REJECTED_VALIDATION` | no |
| `FAILED_BEFORE_WRITE`, `NOT_CREATED`, `NOT_CREATED_CONFLICT`, `OUTCOME_UNCERTAIN`, lease expiry | `RETRY_WAIT` (+5 min, then +30 min); third write attempt -> `MANUAL_REVIEW(attempts_exhausted` or `uncertain_exhausted)` | no |
| `MUTEX_BUSY` | `RETRY_WAIT` +5 min; twelfth -> `MANUAL_REVIEW(mutex_busy_exhausted)` | no |

A fresh create whose Guid is already credited can only be a defect and goes to
`MANUAL_REVIEW(guid_conflict_on_fresh_create)`. Uncertainty is never guessed:
the next attempt's probe under the mutex finds the real state (R0 finds our own
row).

## Persistence and trust boundary

Migrations 0001-0005 are unchanged. The additive
`0006_member_write_v2.sql`:

1. refuses to run if any job is non-terminal outside `CREATED_VERIFIED`,
   `REJECTED_VALIDATION`, `CONFIRMED_NOT_CREATED`, `CREATED_READBACK_MISMATCH`,
   `MANUAL_REVIEW`, `DEAD_LETTER`, or if any `writer_execution_holds` row is not
   cleared;
2. widens the job state check with `LINKED_EXISTING` and `RESOLVED` and adds
   `member_no_rule`, `base_member_no`, `name_component`, `first_claimed_at`,
   `write_attempts`, `busy_attempts`, `outcome_reason`;
3. adds `lease_token`, `outcome`, `rule`, `save_invoked`, `result_hash` to
   attempts and a unique `lease_token` to leases;
4. adds the append-only `member_outcomes` table (one row per job) with
   `UNIQUE (member_guid) WHERE outcome = 'CREATED_VERIFIED'`, so an AutoCount
   member is credited as created by at most one job;
5. adds the append-only `job_resolutions` table for operator CLOSE/REQUEUE;
6. replaces the welcome-outbox trigger so an outbox row requires a
   `CREATED_VERIFIED` outcome in either the legacy `results` table or
   `member_outcomes`.

No table is dropped. The allocation, write-intent, dispatch-fence,
writer-hold and v1 result tables become unused and read-only; retiring them
needs a separately approved migration. No database transaction remains open
across an AutoCount call.

Normal logs contain only run/request/job/attempt metadata, state, safe reason
and error codes, timing/counts and keyed HMAC references. Names, phones, email,
DOB, MemberNo, member Guid, raw response IDs, payloads, credentials, SQL and
private server/database identity are not log fields.

The committed n8n exports are inactive, credential-free and placeholder-only;
they call only the source and welcome-email routes and are unchanged by v2. CI
is offline-only and does not contact Google, n8n, AutoCount, PostgreSQL,
Docker or a deployment target.

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
inserted inside the same result transaction that first records a
`CREATED_VERIFIED` outcome for the job. A database trigger refuses any outbox
insert without a `CREATED_VERIFIED` outcome (legacy `results` or
`member_outcomes`). A duplicate positive acknowledgement verifies the
existing outbox identity and fails closed if it is missing; historical success
is never backfilled. Linked, demoted, uncertain, mismatch, rejected, manual-review, resolved and
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
proof that SMTP did not accept. Email failure never touches the member job or AutoCount.

The `configured-mailer` principal holds only `welcome_email.claim`,
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

Before any separately controlled activation, an owner must positively verify
(live steps L1-L13 and the test-book matrix T-1..T-17 in the
[member write v2 live runbook](member_write_v2_live_runbook.md)):

- a 20-character alphanumeric MemberNo is accepted and is POS-searchable (T-1, T-6);
- member Guid, creator and creation time are readable and the creator is the integration user (T-2, T-14);
- the dedicated integration user exists per book with create/view rights only (T-16);
- zero malformed legacy MemberNos before W-ACT;
- the private HTTPS gateway/auth deployment, the five credential bindings and digests;
- PostgreSQL provisioning, migration 0006 application after a pre-migration dump, backup and restore rehearsal;
- Google Forms API authentication, form alias/question mapping and pagination policy;
- kill-switch ownership, manual-review handling and rollback/runbook ownership.
