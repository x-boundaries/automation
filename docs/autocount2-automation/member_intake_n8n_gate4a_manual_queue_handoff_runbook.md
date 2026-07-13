# Member Intake n8n Gate 4A Container Queue-Write Runbook

Status: Gate 4A real n8n one-row queue-write proof only. This runbook does not run Gate 4A lookup, does not run Gate 4, does not activate n8n, does not call AC2, does not run PowerShell, does not run the local bridge, does not call any bridge endpoint, does not map results, and does not authorize AutoCount writes.

## Purpose

Gate 4A proves only this staged path:

```text
Google Form / Google Sheet source row
  -> manual inactive local operator PC non-AC2 n8n workflow
  -> reads exactly one approved real non-dummy source row
  -> validates and normalizes only allowed fields
  -> writes exactly one sanitized PENDING_LOOKUP queue row to JSONL inside the n8n container
operator copy-out
  -> docker compose cp from the n8n container to the local ignored evidence root
local queue precheck
  -> aggregate-only counters
operator stop
```

Gate 4A does not claim AC2 lookup, bridge execution, result routing, member creation, production activation, scheduler/webhook activation, public inbound exposure, direct SQL access, RDP access, tunnel exposure, or final automation.

## n8n Template

Use this committed template as the local n8n starting point:

```text
n8n-workflows/member_intake_gate4a_container_queue_write.workflow.json
```

The template was checked against the live local n8n container node metadata on 2026-07-09. The live node metadata confirmed these node types are available: `n8n-nodes-base.manualTrigger`, `n8n-nodes-base.googleSheets` version `4.7`, `n8n-nodes-base.code` version `2`, and `n8n-nodes-base.readWriteFile` version `1.1`.

The workflow is inactive/manual. It contains:

- `Manual Gate 4A Run`
- `Read One Approved Real Source Row`
- `Validate And Build One Sanitized Queue Row`
- `Write Queue JSONL Inside n8n Container`

The Google Sheets credential, document, and sheet/tab must be selected in the n8n UI. The committed template keeps the document and sheet resource locators empty/unbound and ships a safe non-PII filter of `Gate4AApprovedForLookup = YES` on the Google Sheets node. Do not commit or paste credential IDs, Sheet IDs, Sheet URLs, resource locator values, screenshots, node execution payloads, node raw input/output, or workflow exports containing credentials. Do not paste the raw exported live workflow or its configured Google selectors anywhere; keep raw live exports outside the repository.

After selecting the Google credential, spreadsheet document, and sheet in the n8n UI, verify the Google Sheets filter is still present. The imported node may not visibly retain the filter after document/sheet binding. If the filter is absent, manually add:

- Column: `Gate4AApprovedForLookup`
- Value: `YES`

Exactly one row may match the filter.

## Queue Target

The n8n workflow writes the staged queue file inside the n8n container at:

```text
/home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl
```

`/home/node/.n8n-files` is the n8n-approved local file-access staging directory. `/tmp` may be shell-writable inside the container, but the n8n Read/Write Files node rejects `/tmp` because of n8n application-level (node-level) file-access restrictions. No Docker Compose edit is required for this path; the directory only needs to exist before the node writes to it.

The writer node overwrites the file on each manual run and Append must remain disabled.

### Pre-Run Directory Preparation

Before the first manual run, create the n8n-approved staging directory inside the container:

```powershell
docker compose -p n8n-local exec -u node n8n sh -lc `
  'mkdir -p /home/node/.n8n-files && chmod 700 /home/node/.n8n-files'
```

Optional writability check:

```powershell
docker compose -p n8n-local exec -u node n8n sh -lc `
  'touch /home/node/.n8n-files/gate4a-write-test && rm /home/node/.n8n-files/gate4a-write-test && echo writable'
```

The operator copies it out to this Windows path:

```text
C:\Users\xPass\OneDrive\Desktop\X-Boundaries\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_gate4a_pending_queue.jsonl
```

Do not assume Windows paths are mounted inside the n8n container. The workflow writes only the container path above. The copy-out step is manual and uses `docker compose cp`.

The workflow overwrites this staged container JSONL file on each manual run. This is intentional: Gate 4A requires exactly one queue row, and stale rows from prior manual tests must not silently remain in `/home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl`.

## Source Row Rules

The first Gate 4A run must match exactly one approved real non-dummy Google Form / Google Sheet row.

Do not rename the real Google Form questions or Google Sheet headers. They are member-facing labels. Backend and n8n handling adapt to the real headers.

The actual source headers are:

- `Date & Time`
- `Full Name`
- `AutoCount MemberNo`
- `Email Address`
- `Birthday Month`
- `Marketing Consent`
- `PDPA Acknowledged`

Backend mapping:

- `Full Name` -> required source name.
- `AutoCount MemberNo` -> submitted member number and AutoCount `MemberNo` lookup value.
- `Email Address` -> required source email.
- `Birthday Month` -> required birthday-month source field.
- `Marketing Consent` -> optional marketing/consent source metadata only.
- `PDPA Acknowledged` -> PDPA source field; must equal `Yes`.

Gate 4A queue helper fields are derived by the workflow. The workflow derives queue `source_reference` from the fixed safe source label and queue `source_row_ref` from n8n Google Sheets row metadata.

The only Gate 4A helper/admin column the operator manually maintains is:

- `Gate4AApprovedForLookup`

Remove any physical Sheet columns with these headers before running Gate 4A:

- `row_number`
- `Gate4A Source Reference`
- `Gate4A Source Row Ref`

n8n's Google Sheets Get Row(s) operation automatically supplies virtual `row_number` metadata for each selected row. A physical Sheet column named `row_number` overwrites that generated metadata, and a blank cell in that column blanks the row number entirely. Members and operators must not create or manually populate a physical `row_number` column. `row_number` is not a member-facing form question. If a physical `row_number` column already exists from earlier setup instructions, delete the whole column so n8n's virtual `row_number` is used. Do not add or maintain `Gate4A Source Reference` or `Gate4A Source Row Ref` sheet columns; the workflow derives those queue fields.

Symptom of a stale physical `row_number` column: the Code node fails with `gate4a_safe_row_number_required` because the blank physical cell overrode the virtual metadata.

The one source row must contain:

- `Full Name`
- `AutoCount MemberNo`
- `Email Address`
- `Birthday Month`
- `PDPA Acknowledged = Yes`
- `Gate4AApprovedForLookup = YES`
- `row_number`

Exactly one source row must have `Gate4AApprovedForLookup = YES`. The row must be real and non-dummy. Do not queue dummy, fixture, placeholder, rehearsal, sample, or synthetic source rows.

`AutoCount MemberNo` maps to AutoCount `MemberNo`. AutoCount `MobilePhone` is intentionally unused. `AutoCount MemberNo` must be numeric only and match `^[0-9]{6,20}$`.

`Birthday Month` is required and must be one of the 12 month names, trimmed and case-insensitive. The backend/default DOB policy is user-facing `01/FORM_INPUT_MONTH/2000`; internally this is expressed as ISO `2000-MM-01`, for example `June` maps to `2000-06-01`. Gate 4A derives this value only as validation/policy proof. It does not write `Birthday Month`, DOB, derived DOB, raw month, or any birthday value to the Gate 4A queue row.

`PDPA Acknowledged = Imported` is blocked. Missing or invalid PDPA is blocked. `Marketing Consent` and `consent_status` cannot rescue, override, or reinterpret missing, invalid, or imported PDPA.

## Queue Row Contract

The workflow may write exactly one JSONL row with these fields only:

- `job_id`
- `intake_source`
- `source_reference`
- `source_row_ref`
- `row_number`
- `intake_id`
- `state = PENDING_LOOKUP`
- `submitted_member_no_base64_utf8`
- `consent_status`
- `pdpa_status = yes`
- `payload_hash`
- `attempt`
- `max_attempts`
- `created_at`
- `updated_at`
- `timeout_at`

The queue row must not contain names, emails, raw `AutoCount MemberNo`, raw phone/member values, normalized member values, decoded member values, `Birthday Month`, DOB, derived DOB, raw month, raw `Marketing Consent`, Sheet IDs/URLs, credential IDs, tokens, local target details, command output, stderr/stdout, node raw input/output dumps, screenshots, or PII.

The encoded lookup value must decode to the submitted phone/member number, but raw, encoded, decoded, and normalized values must never be committed, pasted, logged, screenshotted, or added to PR evidence.

## Operator Steps

1. Open local n8n at `http://localhost:5678`.
2. Import `n8n-workflows/member_intake_gate4a_container_queue_write.workflow.json`.
3. Keep the workflow inactive.
4. Run the pre-run directory preparation command above so `/home/node/.n8n-files` exists inside the container.
5. In `Read One Approved Real Source Row`, select the UAT Google credential in n8n UI.
6. In `Read One Approved Real Source Row`, select the real source spreadsheet and source tab in n8n UI. Do not paste those values into chat, docs, PRs, or evidence.
7. After selecting the document and sheet, verify the Google Sheets filter is still present. If the filter is absent after import or binding, manually add Column `Gate4AApprovedForLookup` with Value `YES`.
8. Confirm the source tab has exactly one row with the non-PII approval marker `Gate4AApprovedForLookup = YES`.
9. Confirm that matching row is real, non-dummy, and contains the required actual source headers and approval marker above. Do not rename the form questions or sheet headers. Confirm the sheet has no physical `row_number` column and no `Gate4A Source Reference` or `Gate4A Source Row Ref` column; delete them if present so n8n's virtual `row_number` metadata is used. The only manually maintained Gate 4A helper column is `Gate4AApprovedForLookup`.
10. Confirm the workflow has no scheduler, webhook, HTTP Request, Execute Command, AC2, SQL, RDP, tunnel, bridge endpoint, result-mapping, member create/update/delete, or AutoCount write node.
11. Confirm the writer node is configured to overwrite `/home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl` with Append disabled, preventing stale rows from previous manual tests.
12. Manually execute the workflow once in n8n.
13. Stop if n8n reads zero rows, more than one row, a dummy/rehearsal row, invalid PDPA, invalid `AutoCount MemberNo`, invalid `Birthday Month`, blank required fields, or any unexpected field/output.

Do not paste raw n8n execution output. Do not paste row data. Do not paste the generated JSONL row or encoded value.

## Copy The Container Queue File Out

Run from PowerShell after the n8n manual execution:

```powershell
cd "C:\Users\xPass\GitHub\automation"

$dest = "C:\Users\xPass\OneDrive\Desktop\X-Boundaries\autocount_outputs\review\member_lookup_bridge"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

docker compose -p n8n-local cp `
  n8n:/home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl `
  "$dest\member_lookup_bridge_gate4a_pending_queue.jsonl"
```

Do not paste the copied file contents.

## Pre-Bridge Aggregate Queue Check

Run only the aggregate queue precheck:

```powershell
cd "C:\Users\xPass\GitHub\automation"

python scripts\member_lookup_gate4a_queue_precheck.py `
  --queue-jsonl "C:\Users\xPass\OneDrive\Desktop\X-Boundaries\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_gate4a_pending_queue.jsonl"
```

Continue only if the precheck prints:

- `queue_row_count = 1`
- `queue_base64_decode_ok_count = 1`
- `queue_base64_decode_fail_count = 0`
- `queue_decoded_blank_count = 0`
- `queue_decoded_looks_dummy_count = 0`
- `unexpected_queue_shape_count = 0`

Required pre-bridge paste-back shape:

```text
status = <ok/needs_fix>
gate = gate4a_real_queue_write_pre_bridge_check
runtime_location = local_operator_pc_non_ac2_n8n_stack
execution_mode = manual_inactive_queue_write_pre_bridge_check
queue_row_count = <aggregate-count-only>
queue_base64_decode_ok_count = <aggregate-count-only>
queue_base64_decode_fail_count = <aggregate-count-only>
queue_decoded_blank_count = <aggregate-count-only>
queue_decoded_looks_dummy_count = <aggregate-count-only>
unexpected_queue_shape_count = <aggregate-count-only>
bridge_handoff_approved = false
ac2_lookup_invoked = false
n8n_result_mapping_run = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
no_row_values_printed = true
sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/decoded/normalized member values, names, emails, phone numbers, birthdays, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, screenshots, or PII are pasted.
```

This successful precheck is queue-write evidence only. It does not approve local bridge handoff, AC2 lookup, result mapping, scheduler activation, webhook activation, member create/update/delete, AutoCount writes, direct SQL writes, or final write automation.

## Stop Conditions

Stop immediately if any of these occur:

- The workflow is activated.
- A scheduler or webhook is enabled.
- More than one source row is read or written.
- The source sheet has a physical `row_number` column, a duplicate `row_number` header, or any `Gate4A Source Reference` or `Gate4A Source Row Ref` column. A physical `row_number` header overrides n8n's virtual `row_number` metadata and a blank cell triggers `gate4a_safe_row_number_required`; delete the physical column instead of typing a value into it.
- The source row is dummy, fixture, placeholder, rehearsal, sample, or synthetic.
- PDPA is missing, invalid, or imported.
- `Marketing Consent` or `consent_status` is used to rescue, override, or reinterpret missing, invalid, or imported PDPA.
- `AutoCount MemberNo` is not numeric only, 6-20 digits.
- `Birthday Month` is missing or is not one of the 12 month names.
- The queue file contains anything other than exactly one `PENDING_LOOKUP` row.
- The queue row contains unexpected fields.
- Raw values, encoded values, decoded values, normalized values, names, emails, birthday months, DOB values, derived DOB values, phone/member numbers, Sheet IDs/URLs, credential IDs, tokens, command output, screenshots, node raw input/output, execution payloads, secrets, connection strings, or PII appear in logs, evidence, docs, screenshots, or PR text.
- Any AC2, bridge endpoint, PowerShell, SQL, RDP, tunnel, AutoCount write, member create/update/delete, result-mapping, or final automation path appears.

Gate 4A is ready for operator run only when the n8n workflow remains inactive/manual, the template has been bound to the operator-owned UAT Google credential and source tab in n8n UI, exactly one approved real row is marked, and the operator is prepared to stop after the aggregate precheck.

## Recorded Technical UAT Evidence (2026-07-13)

Gate 4A technical n8n-to-container queue-write UAT: PASS.

The operator manually executed the full inactive workflow once in local n8n with all four nodes green (`Manual Gate 4A Run`, `Read One Approved Real Source Row`, `Validate And Build One Sanitized Queue Row`, `Write Queue JSONL Inside n8n Container`), copied the container file out with `docker compose -p n8n-local cp`, and ran the aggregate precheck. Aggregate-only result:

```text
status = ok
gate = gate4a_real_queue_write_pre_bridge_check
runtime_location = local_operator_pc_non_ac2_n8n_stack
execution_mode = manual_inactive_queue_write_pre_bridge_check
queue_row_count = 1
queue_base64_decode_ok_count = 1
queue_base64_decode_fail_count = 0
queue_decoded_blank_count = 0
queue_decoded_looks_dummy_count = 0
unexpected_queue_shape_count = 0
bridge_handoff_approved = false
ac2_lookup_invoked = false
n8n_result_mapping_run = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
no_row_values_printed = true
```

Scope of this earlier run. The source row used test-style form values, so this run proved the technical path only: Google Sheets read, exactly-one-row validation, actual header handling, `Birthday Month` validation, sanitized queue construction, n8n-approved container file write, file copy-out, and aggregate precheck. By itself it did not yet satisfy the final real consenting member-row evidence requirement; that requirement is now separately satisfied and recorded in the next section.

This technical UAT record is retained as historical evidence of the earlier test-style run. The final real consenting source-row evidence is recorded separately below.

## Final Real Consenting Source-Row Evidence (2026-07-13)

Final real non-dummy consenting source-row Gate 4A evidence: PASS.

The operator completed the final Gate 4A manual run using exactly one genuine, non-dummy, consenting UAT Google Form response. The run:

- used one genuine consented UAT Form response as the source;
- selected exactly one row through `Gate4AApprovedForLookup = YES`;
- kept the workflow manual and inactive;
- wrote exactly one sanitized queue row;
- decoded that one queue row's Base64 lookup value exactly once, with no decode failure;
- produced no blank decoded value;
- produced no dummy-looking decoded value;
- produced no unexpected queue shape;
- stopped after the aggregate precheck, before any bridge or AutoCount call.

Aggregate-only result:

```text
status = ok
gate = gate4a_real_queue_write_pre_bridge_check
runtime_location = local_operator_pc_non_ac2_n8n_stack
execution_mode = manual_inactive_queue_write_pre_bridge_check
queue_row_count = 1
queue_base64_decode_ok_count = 1
queue_base64_decode_fail_count = 0
queue_decoded_blank_count = 0
queue_decoded_looks_dummy_count = 0
unexpected_queue_shape_count = 0
bridge_handoff_approved = false
ac2_lookup_invoked = false
n8n_result_mapping_run = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
final_write_automation = false
no_row_values_printed = true
```

Sanitized evidence note: No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/decoded/normalized member values, names, emails, phone numbers, birthdays, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, screenshots, or PII are included.

### Final Gate 4A Status

- Gate 4A queue-write evidence: PASS.
- Final real non-dummy consenting source-row evidence: PASS.
- Bridge handoff remains unapproved.
- AC2 lookup remains uninvoked.
- n8n result mapping remains unrun.
- Workflow remains inactive.
- Scheduler remains disabled.
- No public inbound access to the AC2 host exists.
- No member create/update occurred.
- No AutoCount write occurred.
- No direct SQL write occurred.
- Final write automation remains false.

Gate 4A completion does not by itself approve the next bridge/AC2 lookup-only step. That next step requires a separate explicit gate, review, and operator approval before any bridge handoff or AC2 lookup runs.

### Operator Cleanup After This Run

- `Gate4AApprovedForLookup` is the only manually maintained Gate 4A helper/admin column. It is not a Google Form question.
- It is set to `YES` for exactly one approved row before the manual run.
- After this evidence run, the operator should clear the `Gate4AApprovedForLookup = YES` value so the same row is not selected accidentally again.
- This is an operator cleanup instruction only; this repository did not and cannot clear the Sheet value.
- Do not create or maintain physical Sheet columns named `row_number`, `Gate4A Source Reference`, or `Gate4A Source Row Ref`.
