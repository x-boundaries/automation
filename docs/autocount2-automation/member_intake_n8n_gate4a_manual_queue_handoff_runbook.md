# Member Intake n8n Gate 4A Manual Queue Handoff Runbook

Status: Gate 4A queue-write preparation package only. This runbook does not run Gate 4A lookup, does not run Gate 4, does not activate n8n, does not touch real Google Sheets queue data from repo work, does not call AC2 from Codex, does not run PowerShell from Codex, and does not authorize AutoCount writes. This PR stops before bridge handoff.

## Purpose

Gate 4A is the manual handoff path for AC2 lookup-only review routing when no real Google Sheets poller exists in the local bridge.

The current lookup queue tab contains only dummy Gate 2 rehearsal rows. Those rows proved TSV-to-JSONL handoff mechanics only. They are not real Gate 4A queue rows, must not be passed to AC2 lookup, and must not be recorded as Gate 4A pass evidence.

The next safe path in this PR is:

```text
inactive/manual local operator PC non-AC2 n8n
  -> reads exactly one approved real Google Form/Sheet source row
  -> validates required fields and PDPA Acknowledged = Yes
  -> writes exactly one sanitized non-dummy PENDING_LOOKUP row to Google Sheets UAT queue tab
operator manual handoff
  -> exports/copies only that approved sanitized queue row to local ignored JSONL
local queue precheck
  -> prints aggregate-only pre-bridge queue evidence
operator stop
  -> no bridge handoff unless explicitly approved later
```

Gate 4A does not claim a real Google Sheets poller, production queue poller, n8n result mapping run, scheduler, webhook, tunnel, public inbound AC2 exposure, member create/update/delete path, AutoCount write path, direct SQL write path, local bridge handoff approval, AC2 lookup execution, or final write automation.

## Real Queue-Write Preparation Gate

Gate 4A lookup may not start until the operator has first produced exactly one real, non-dummy, sanitized `PENDING_LOOKUP` queue row from a tiny approved source batch.

This preparation gate is queue-write-only. It does not run Gate 4A lookup, does not call AC2, does not run PowerShell, does not run the local bridge, does not run n8n result mapping, and does not authorize any AutoCount write path.

Stop before bridge handoff if the lookup queue tab contains only dummy Gate 2 rehearsal rows, if the row count is not exactly one, if the encoded lookup value is blank or invalid, or if the decoded value only matches a dummy/rehearsal marker. Do not paste or commit the encoded or decoded value while checking this.

The required pre-bridge aggregate checks are:

- `queue_row_count = 1`
- `queue_base64_decode_ok_count = 1`
- `queue_base64_decode_fail_count = 0`
- `queue_decoded_blank_count = 0`
- `queue_decoded_looks_dummy_count = 0`

The preparation gate may be recorded only as queue-write precheck evidence, not as Gate 4A lookup evidence or Gate 4A pass evidence. Stop before bridge handoff after the aggregate pre-bridge check. This successful precheck does not approve local bridge handoff, AC2 lookup, result mapping, or final write automation.

## Local Ignored File Paths

Use the existing local output root on the Windows AC2 lookup bridge host:

```text
C:\XB\autocount_outputs\review\member_lookup_bridge
```

Use these local file names:

```text
member_lookup_bridge_gate4a_pending_queue.jsonl
member_lookup_bridge_gate4a_results.jsonl
```

These files are local handoff artifacts only. They must not be committed, pasted, uploaded, or attached to PR evidence.

## Pre-Run Checklist

Before running Gate 4A, the operator confirms:

1. Repo `main` is at the expected reviewed Gate 4 plan commit.
2. Gate 1 fixture/mock bridge pass has passed.
3. Gate 2A local operator PC non-AC2 n8n dummy wiring rehearsal has passed.
4. Gate 3 local Windows PowerShell lookup preflight has passed.
5. Gate 4 plan-only PR has merged.
6. Gate 4A has not already been run for this batch.
7. Runtime label for evidence is exactly `local_operator_pc_non_ac2_n8n_stack`.
8. n8n workflow is inactive and manual.
9. Scheduler is disabled.
10. No public inbound webhook, callback, tunnel, or reverse proxy reaches the AC2 host.
11. No Execute Command node is used for AC2 lookup on the non-AC2 n8n host.
12. Google Sheets UAT queue/review tabs are used only as temporary UAT surfaces.
13. The lookup queue tab does not contain only dummy Gate 2 rehearsal rows.
14. The real queue-write preparation gate produced exactly one non-dummy sanitized `PENDING_LOOKUP` row.
15. Sheet URLs, Sheet IDs, credential IDs, OAuth details, service account JSON, execution payloads, node raw input/output dumps, row-level data, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, secrets, connection strings, and PII will stay out of pasted evidence.
16. The operator will stop before bridge handoff unless an explicit later approval names the next bridge step.

Stop if any item cannot be confirmed.

## Batch Control

1. Choose a tiny approved batch only. Initially this must be exactly one source row.
2. Record only the aggregate approved batch count for later evidence.
3. Mark only those approved rows for lookup in the UAT source tab.
4. Do not paste row data, screenshots with row data, names, emails, phone numbers, raw member values, encoded member values, normalized member values, Sheet URLs, or Sheet IDs.
5. Stop if more rows than expected are selected, queued, exported, loaded, or written.

The one approved source row must include the required source fields:

- `Name`
- phone/member number field submitted by the user
- `Email`
- birthday field only when the current source includes birthday
- `PDPA Acknowledged = Yes`

`PDPA Acknowledged = Imported` remains blocked and must not be treated as consent. For the current live/form path, normalize only `PDPA Acknowledged = Yes` to queue value `pdpa_status = yes`.

## n8n Manual Queue-Write Step

Run n8n manually while inactive:

1. Open the local operator PC non-AC2 n8n workflow.
2. Confirm the workflow is inactive.
3. Confirm no scheduler is enabled.
4. Confirm no webhook/tunnel/public inbound AC2 callback is configured.
5. Manually run only the queue-write path.
6. n8n manually reads exactly one approved real Google Form/Sheet source row.
7. n8n validates required fields and `PDPA Acknowledged = Yes`: `Name`, the submitted phone/member number, `Email`, birthday when applicable to the source, and `PDPA Acknowledged = Yes`.
8. Normalize only what is needed for queue routing.
9. Encode the submitted phone/member number into `submitted_member_no_base64_utf8`.
10. Write only one sanitized non-dummy `PENDING_LOOKUP` queue row to the Google Sheets UAT queue tab.
11. Confirm the queue row contains only the approved queue request fields from the UAT plan and queue contract.
12. Confirm AutoCount `MobilePhone` is intentionally unused; the submitted phone/member number maps to AutoCount `MemberNo`.
13. Do not run n8n result mapping in Gate 4A.
14. Do not paste raw node input/output, execution payloads, Sheet URLs, Sheet IDs, credential IDs, row-level output, command output, raw member values, encoded member values, decoded member values, normalized member values, or PII.

Stop if the n8n run reads or writes more rows than the approved batch, reads a row without `PDPA Acknowledged = Yes`, writes a dummy queue row, writes a blank encoded lookup value, writes unexpected columns, activates the workflow, enables a scheduler, exposes inbound AC2 access, or references any write/create/update/delete path.

The appended queue row must contain:

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

The encoded lookup value must decode to the submitted phone/member number, but the raw, encoded, decoded, and normalized values must never be committed, pasted, logged, or added to PR evidence.

## Manual Local Queue Handoff Step

The operator manually exports or copies only the approved sanitized queue rows from the UAT queue tab into:

```text
C:\XB\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_gate4a_pending_queue.jsonl
```

Rules:

- Include only rows from the tiny approved batch.
- Include only allowed queue request fields.
- Include exactly one row for the initial real queue-write preparation.
- Do not include dummy Gate 2 rehearsal rows.
- Do not continue unless the pre-bridge aggregate checks show one row, one successful base64 decode, no decode failures, no blank decoded value, and no dummy-looking decoded value.
- Keep the file on the Windows AC2 lookup bridge host.
- Do not paste or commit the file.
- Do not include Sheet URLs, Sheet IDs, credential IDs, OAuth details, service account JSON, execution payloads, node raw input/output dumps, names, emails, phone numbers, raw member values, normalized member values, command transcripts, stderr/stdout, secrets, connection strings, or PII.
- The encoded lookup field is operationally sensitive. Keep it only in the local ignored handoff file and never paste it.

Stop if the local JSONL contains unexpected fields, more rows than approved, raw member values, normalized member values, names, emails, phone numbers, Sheet IDs/URLs, credentials, local target details, command output, or PII.

## Pre-Bridge Aggregate Queue Check

Before any bridge handoff, run only the aggregate queue precheck. This command decodes the queued lookup value only in memory and prints counters only:

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$queue = Join-Path $root 'member_lookup_bridge_gate4a_pending_queue.jsonl'

python scripts\member_lookup_gate4a_queue_precheck.py `
  --queue-jsonl "$queue"
```

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

Continue only if the aggregate counters are exactly `queue_row_count = 1`, `queue_base64_decode_ok_count = 1`, `queue_base64_decode_fail_count = 0`, `queue_decoded_blank_count = 0`, `queue_decoded_looks_dummy_count = 0`, and `unexpected_queue_shape_count = 0`. This PR still stops here. This successful precheck does not approve local bridge handoff, AC2 lookup, result mapping, scheduler activation, webhook activation, member create/update/delete, AutoCount writes, direct SQL writes, or final write automation.

## Deferred Local Bridge Lookup Step

The local bridge lookup step is deferred unless the operator explicitly approves the next step after reviewing the aggregate pre-bridge queue evidence. Do not run the command below as part of this queue-write preparation PR.

Run this only on the approved local Windows AC2 lookup bridge host. This command is for the operator, not Codex.

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$queue = Join-Path $root 'member_lookup_bridge_gate4a_pending_queue.jsonl'
$results = Join-Path $root 'member_lookup_bridge_gate4a_results.jsonl'

python scripts\ac2_member_lookup_bridge_worker.py `
  --enable-local-lookup-bridge-review `
  --queue-mode fixture `
  --fixture-jobs "$queue" `
  --results-jsonl "$results" `
  --lookup-mode powershell `
  --enable-powershell-lookup `
  --allow-root-login
```

Bridge boundaries:

- The bridge reads only the local ignored JSONL handoff file.
- The bridge uses fixture queue mode because no real Google Sheets poller exists.
- The bridge calls only `scripts/ac2_member_lookup_review.ps1` in read-only lookup mode.
- `--allow-root-login` carries forward the Gate 3 local lookup auth setting and is still read-only; it does not authorize member create/update/delete, AutoCount writes, direct SQL writes, or final write automation.
- The bridge writes only local sanitized result JSONL.
- The bridge must not create, update, delete, or write AutoCount members.
- The bridge must not perform direct SQL writes.
- The bridge must not invoke final write automation.
- Do not paste the command transcript, stdout, stderr, result rows, queue rows, encoded member values, local target details, credentials, connection strings, or PII.

Stop if the bridge reports unexpected fields, unknown states, command errors that cannot be reduced to aggregate evidence, lookup error spike, any AutoCount write indication, member create/update/delete indication, direct SQL write indication, or final write automation indication.

## Aggregate Evidence Summary Step

After the local result JSONL exists, run the local summarizer. This command reads only the local result JSONL path and prints aggregate-only evidence.

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$results = Join-Path $root 'member_lookup_bridge_gate4a_results.jsonl'

python scripts\member_lookup_gate4a_evidence_summary.py `
  --results-jsonl "$results" `
  --approved-batch-size <aggregate-count-only> `
  --n8n-queue-rows-written-count <aggregate-count-only> `
  --local-queue-rows-loaded-count <aggregate-count-only>
```

The summarizer must not print individual rows, `job_id`, `source_reference`, `source_row_ref`, `row_number`, raw/encoded/normalized member values, names, emails, phones, Sheet IDs/URLs, credentials, command output, local target details, secrets, connection strings, or PII.

If `lookup_error_count > 0`, if required safe result fields are missing, or if `approved_batch_size`, `n8n_queue_rows_written_count`, `local_queue_rows_loaded_count`, and `lookup_attempt_count` do not all match, the summarizer prints `status = needs_fix`.

## Result Mapping Boundary

Gate 4A stops after aggregate evidence. n8n result mapping is deferred unless separately planned and approved.

Do not map result rows back to Google Sheets in Gate 4A. Do not mark form/source rows from this Gate 4A package. Do not treat `READY_FOR_CREATE_REVIEW` as create approval.

## Stop Conditions

Stop Gate 4A immediately if any of these occur:

- Gate prerequisites are not confirmed.
- The lookup queue contains only dummy Gate 2 rehearsal rows.
- The real queue-write preparation pre-bridge checks are not exactly `queue_row_count = 1`, `queue_base64_decode_ok_count = 1`, `queue_base64_decode_fail_count = 0`, `queue_decoded_blank_count = 0`, and `queue_decoded_looks_dummy_count = 0`.
- More rows than the tiny approved batch are marked, queued, exported, loaded, processed, or written.
- A queue row is dummy or has a blank encoded lookup value.
- Any unexpected field appears in queue, result, or evidence.
- Raw member values, encoded member values, normalized member values, names, emails, phone numbers, row-level data, Sheet IDs/URLs, credential IDs, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, screenshots with row data, secrets, connection strings, or PII appear in evidence.
- Unknown state appears.
- Lookup error spike occurs.
- n8n workflow activation occurs.
- Scheduler is enabled.
- Webhook, tunnel, callback, reverse proxy, or other public inbound AC2 exposure appears.
- Any member create/update/delete path is referenced or invoked.
- Any AutoCount write is attempted.
- Any direct SQL write is attempted.
- Any final write automation is referenced or invoked.

## Deferred Lookup Paste-Back Evidence

This shape is for the later explicitly approved bridge lookup step only. It is not produced by the queue-write preparation PR.

Paste back only the summarizer output in exactly this aggregate-only shape:

```text
status = <ok/needs_fix>
gate = gate4a_manual_queue_handoff_ac2_lookup_only
runtime_location = local_operator_pc_non_ac2_n8n_stack
execution_mode = manual_inactive_review_only_handoff
approved_batch_size = <aggregate-count-only>
n8n_queue_rows_written_count = <aggregate-count-only>
local_queue_rows_loaded_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_existing_member_review_count = <aggregate-count-only>
lookup_manual_review_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
local_review_result_rows_written_count = <aggregate-count-only>
n8n_result_mapping_run = false
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
final_write_automation = false
sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, or PII are pasted.
```

Gate 4A passing still does not authorize member create/update, AutoCount writes, direct SQL writes, production activation, scheduler activation, n8n result mapping, or final write automation. Those require a separate reviewed PR and explicit business approval.
