# Member Intake Automation Blueprint

Canonical member-intake architecture surface (#155).

```text
ARCHITECTURE_AUTHORITY=#155 G1-147 (Web acceptance #155:5889718018)
MEMBER_WRITE_CONTRACT=W-G2-149 (+ Web amendment #155:5890550314)
MEMBER_WRITE_ARCHITECTURE_STATUS=ACCEPTED
MEMBER_WRITE_IMPLEMENTATION_STATUS=REPOSITORY_IMPLEMENTATION_PRODUCTION_DARK
FIRST_PRODUCTION_MEMBER=SEPARATE_W-ACT_GATE (not authorised)
GENERIC_AUTOCOUNT_API_FACADE=DEFERRED_NOT_CURRENT
```

The gateway, worker and AutoCount primitive for this flow are implemented in
this repository and are production-dark: the kill switch is on, activation is
off, the worker task is disabled and the n8n workflows are inactive. Every live
step (L1-L13 in the [member write v2 live runbook](member_write_v2_live_runbook.md))
and the first production member (W-ACT) need their own approval.

## Target Flow

```text
Google Form
  -> n8n (SERVER_PC)
  -> member gateway + Postgres (SERVER_PC): durable queue, state, idempotency, outbox
  <- AC2 worker (AC2_VM) pulls outbound: readyz, claim, result
  -> narrow local AutoCount member primitive (AC2_VM)
  -> probe -> at most one SaveMember -> readback (under a local mutex)
  -> bounded result back to the gateway
  -> replay-safe welcome-email outbox (sent by n8n)
```

## System Roles

| Component | Role | Source-of-truth status |
| --- | --- | --- |
| Google Form | Member registration intake surface. | Not source of truth |
| n8n (SERVER_PC) | Ingests form responses into the gateway; sends welcome emails from the outbox. | Not source of truth |
| Member gateway + Postgres (SERVER_PC) | Durable job queue and state machine, responseId + payload-hash idempotency, single active lease, retry budgets, kill switch and activation, manual-review queue, welcome outbox. | Source of truth for job state only |
| AC2 worker (AC2_VM) | Pulls one job at a time outbound, runs the primitive as a child process with a hard deadline, posts one bounded result. | Not source of truth |
| AutoCount member primitive (AC2_VM) | The only AutoCount write path: probe existing members, decide, at most one `SaveMember`, readback. | Not source of truth |
| AutoCount 2.0 | Final member record, member number, member type, bonus point state. | Source of truth |

There is no Google Sheets queue, no local lookup bridge and no dry-run approval
sheet in the current production flow. Those were earlier designs (see
[Superseded designs](#superseded-designs)).

## MemberNo And Duplicate Rules (XB-MN-1)

- **Base MemberNo** = the submitted phone after the existing ingest clean-up,
  digits only. No country inference, no E.164 conversion, no prefix added or
  stripped. Leading zeroes are kept; a base starting `000` is an ordinary base.
- **No X1/X2/X3 suffix allocation.**
- **Obvious same member -> `LINKED_EXISTING`.** If an existing member holding
  the number (or an unambiguous format variant of it) has the same email, or
  the same name ignoring word order, the job links to that member. Nothing is
  created and no existing member is updated.
- **Genuine shared number, different person -> base + bounded name
  component.** The component is the ASCII letters of the name, upper-cased,
  trimmed so the MemberNo is at most 20 characters. The gateway computes it
  once; the primitive only checks its shape.
- **Ambiguity -> `MANUAL_REVIEW`.** Inactive or multiple matching members, a
  holder with no name and no email, a format variant belonging to someone else,
  inconsistent evidence from a prior attempt, an empty name component, or a
  name-component collision all go to the manual-review queue. An operator can
  close or requeue a reviewed job.
- **Field limits** (Name at most 100, Email at most 200 UTF-16 units) are
  checked at validation; values are rejected, never truncated.
- Every created member records `MobilePhone` = the base MemberNo.

## Write Safety

- The primitive holds a machine-wide mutex across probe -> decision -> at most
  one `SaveMember` -> readback, so creates on `AC2_VM` are strictly one at a
  time. The gateway additionally allows only one active lease across all jobs.
- An uncertain outcome (crash after save, lost result, expired lease) is never
  guessed. The same job is re-run; the next probe under the mutex finds the
  real state and the job resolves to `CREATED_VERIFIED`, a link, or review.
- Result posts are compare-and-set on the lease: a stale or different result
  cannot create a second outcome.
- An AutoCount member can be credited as created by at most one job, so at most
  one welcome email is sent per member.

## Privacy And PDPA Considerations

Member intake contains personal data: name, mobile number (the base of the
AutoCount MemberNo), email, birthday month, and consent status. Treat every
form response as sensitive.

Google Form consent controls use these live export values (form contract:
[member_form_intake_contract.md](member_form_intake_contract.md); its
dry-run-validator rules about `MemberNo`/`MobilePhone` are historical, the
production rules are the XB-MN-1 rules above):

- `PDPA Acknowledged`: checkbox exporting `Yes`, accepted case-insensitively.
  Any other value, including blank, blocks admission.
- `Marketing Consent`: multiple choice with exact values `Yes` / `No`, accepted
  case-insensitively. `No` never blocks member registration; missing or
  unrecognized values reject the response.

Minimum controls:

- The gateway stores the canonical payload privately; operator views, logs and
  audit events carry metadata only (no name, phone, email, MemberNo or member
  Guid).
- The primitive's result carries no personal data except the MemberNo.
- n8n and worker logs must not dump payloads.
- Do not commit real form responses, form IDs, screenshots, n8n credentials,
  API keys, book names, host details or runtime outputs.

## Deferred / Not Current

- A generic X-Boundaries AutoCount API wrapper or external-agency API product
  is deferred by the Owner and is not a design requirement.
- Updating existing members, legacy member import and member merges are
  separate future projects with their own approval gates.

## Superseded Designs

The following earlier designs are historical and are not the current
architecture:

- Google Form -> Google Sheets -> n8n validation -> local bridge -> AutoCount
  Member API, with a Sheets approval queue and dry-run validator. See the
  historical [local bridge design](member_intake_local_bridge_design.md) and the
  review-only lookup/UAT runbooks.
- The gateway's earlier X1/X2 MemberNo allocator with probe/recheck, write
  intent, dispatch fence, writer registration/termination/quarantine, recovery
  principal and heartbeat. Removed in favour of the primitive + mutex design.
- The single-member creation UAT ([runbook](member_create_uat_runbook.md))
  remains bounded UAT scaffolding only; it is not the production workflow.
