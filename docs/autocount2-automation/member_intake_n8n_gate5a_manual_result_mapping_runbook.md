# Member Intake n8n Gate 5A Manual Inactive Result-Mapping Runbook

Status: Gate 5A manual inactive n8n sanitized-result mapping UAT only. This runbook maps exactly one previously produced sanitized Gate 4 recovery result into controlled Google Sheet review/status fields and stops. It does not run any AC2 lookup, does not contact the AutoCount host, does not activate n8n, does not run any scheduler or webhook, does not approve any reviewer decision, and does not authorize member creation, member update, AutoCount writes, direct SQL, or final automation.

## Purpose

Gate 5A proves only this staged path:

```text
one existing sanitized Gate 4 recovery durable result (AutoCount host, unchanged)
  -> operator manual copy (copy only, never move) to an operator-PC staging file
  -> aggregate-only repo precheck on the staging copy
  -> operator manual copy into the approved n8n container file area /home/node/.n8n-files/
  -> manual inactive local operator PC non-AC2 n8n workflow
  -> strict fail-closed result validation
  -> reads exactly one approved source row from Google Sheets
  -> rebuilds the canonical Gate 4A identity and requires exactly one match
  -> updates controlled review/status columns only (reviewer_status stays UNREVIEWED)
  -> re-reads the approved row and verifies the exact persisted state
  -> emits aggregate-only evidence (success only after persisted-state verification) and stops
```

The AutoCount host receives no inbound network connection, webhook, callback, tunnel, or n8n command at any point. Every transfer is an operator-performed outbound manual copy. The original Gate 4 queue, results, markers, recovery claim, and recovery result artifacts remain byte-for-byte unchanged; the workflow and precheck open only the staging copies read-only.

## Differences From The Older n8n Lookup-Bridge UAT Plan

`docs/autocount2-automation/member_intake_n8n_lookup_bridge_uat_plan.md` predates the merged Gate 4 implementation. Gate 5A follows the merged Gate 4 durable result contract and documents these deliberate differences:

- The plan's Workflow B polled a `Lookup Results UAT` Sheet tab with a Schedule Trigger and a `ready_for_n8n` helper column. Gate 5A instead uses a Manual Trigger and a manual copy-only file handoff, because Gate 5A forbids schedulers and any network path from the AutoCount host.
- The plan's `Mark Result Applied` step set `result_applied_at` on the result row. Gate 5A never modifies the recovery result or any Gate 4 artifact; `result_applied_at` must remain `null` in the copied input, and idempotency lives in the controlled Sheet status fields instead.
- The plan marks several result fields optional. The merged Gate 4 durable result always contains all 25 allowed keys (some with `null` values), so Gate 5A requires exactly that key set.
- The plan's updatable field list includes `uat_lookup_queued_at` and `reviewer_decision_code`. The durable result carries no queue timestamp and Gate 5A makes no reviewer decision, so Gate 5A does not write either field.
- No fields are invented beyond the merged Gate 4 durable result contract; validation is a strict whitelist of that contract.

## n8n Template

Use this committed template as the local n8n starting point:

```text
n8n-workflows/member_intake_gate5a_sanitized_result_mapping.workflow.json
```

The template was checked against the live local n8n container node metadata on 2026-07-15 (n8n `2.29.8`). The live node metadata confirmed these node types and versions are available: `n8n-nodes-base.manualTrigger` version `1`, `n8n-nodes-base.readWriteFile` version `1.1`, `n8n-nodes-base.extractFromFile` version `1.1`, `n8n-nodes-base.code` version `2`, `n8n-nodes-base.googleSheets` version `4.7`, and `n8n-nodes-base.if` version `2.3`. The template was imported into the live local instance, re-exported, and verified to round-trip with identical node parameters and connections while staying inactive.

Import round-trip fidelity does not extend to UI binding. The 2026-07-15 live UAT established (and the n8n editor implementation confirms) that when the sheet locator (`sheetName.value`) of a Google Sheets 4.7 node changes in the n8n editor, the editor resets that node's column resource mapper: the defined column values are cleared, the match column is re-defaulted, and hidden options (Cell Format, `returnFirstMatch`) can be lost. A template that ships an empty sheet locator therefore always loses its Update-node mappings the moment the operator picks the tab. To prevent this, all three Google Sheets nodes ship the sheet locator in `By Name` mode with the non-secret placeholder `REPLACE_WITH_SOURCE_TAB_NAME`. The operator replaces the placeholder with the source tab title in a local import copy before importing, and binds only the Google credential and the spreadsheet in the n8n UI. The column resource mapper depends only on the sheet locator, not the spreadsheet locator, so binding the spreadsheet does not reset the mapping. The Update node also ships the exact 18-column header schema fetched from the live instance (header names and mapper flags only, no values or locators): it lets the mapper render the committed mappings immediately, and at runtime the node refuses the update with a clear setup error if any documented UAT header is missing from the bound sheet.

The workflow is inactive/manual. It contains:

- `Manual Gate 5A Run`
- `Read Copied Sanitized Recovery Result`
- `Extract Result Text`
- `Validate Copied Sanitized Result Strictly`
- `Read One Approved Source Row`
- `Verify Identity And Decide Mapping`
- `Apply Only Fresh Mapping`
- `Update Approved Review Fields Only`
- `Re-Read Mapped Row For Verification`
- `Verify Persisted Mapping Strictly`
- `Emit Aggregate Evidence Only`

The Google Sheets credential and spreadsheet must be selected in the n8n UI on all three Google Sheets nodes, and all three must point at the same source tab. The source tab itself is never selected in the UI: it is pre-set by name in the local import copy (placeholder replacement above), because a UI tab selection resets the Update-node mapping. The committed template keeps the spreadsheet resource locator empty/unbound, carries only the non-secret tab-name placeholder, and ships a safe non-PII filter of `Gate5AApprovedForMapping = YES` on the read nodes. Do not commit or paste credential IDs, Sheet IDs, Sheet URLs, or other resource locator values, screenshots, node execution payloads, node raw input/output, or workflow exports containing credentials; the source tab title may appear only in the local import copy in the ignored staging area, never in the repository or in pasted evidence. Keep raw live exports outside the repository (`.tmp/` is git-ignored for this reason).

Workflow execution-data settings are the most restrictive available and must stay that way: saved manual executions off, saved success/error execution data off, saved execution progress off.

## Input Contract: The One Copied Sanitized Recovery Result

The only workflow input file is the operator-copied staging duplicate of the single sanitized Gate 4 recovery durable result row produced by
`scripts/member_lookup_gate4_failed_attempt_recovery.py` (the run recorded with `status = ok` and `lookup_ready_for_create_review_count = 1`).

The copied file must contain exactly one nonblank JSONL row with exactly these 25 fields (the `ac2_member_lookup_bridge_worker` durable result contract):

`job_id`, `intake_source`, `source_reference`, `source_row_ref`, `row_number`, `state`, `status`, `authentication_success`, `user_session_available`, `member_command_found`, `get_member_found`, `submitted_member_no_status`, `normalized_member_no_length`, `member_exists`, `member_found_by`, `manual_review_required`, `warning_count`, `error_code`, `consent_status`, `pdpa_status`, `attempt`, `dry_run_only`, `final_write_automation`, `result_created_at`, `result_applied_at`

Both the precheck and the workflow fail closed when the copied input is missing, empty or whitespace-only, more than one row, malformed JSON, not a single JSON object, missing required fields, carrying unexpected or forbidden fields, carrying invalid field types, in an unrecognised state, `dry_run_only != true`, `final_write_automation != false`, `result_applied_at != null`, or carrying an error/warning combination inconsistent with its recomputed review state.

Forbidden content that always fails closed includes raw member numbers, base64/encoded member values, decoded or normalized member values, names, phone numbers, emails, birthdays/DOB, addresses, AutoCount credentials or target values, command text, stdout/stderr, Sheet URLs or IDs, payload hashes, intake IDs, and arbitrary payload fields.

## Google Sheet Requirements

Do not rename the real Google Form questions or Sheet headers. The actual source headers remain those documented in the Gate 4A runbook.

Gate 5A helper/admin column maintained manually by the operator:

- `Gate5AApprovedForMapping` — set to `YES` on exactly one row: the same genuine consenting row that produced the Gate 4A queue row and the Gate 4 recovery result. Row selection by this marker alone never authorizes an update; the workflow refuses unless the canonical rebuilt identity matches the copied result exactly.

Required review/status columns that must already exist as headers on the source tab before the run (the workflow fails with a clear setup error rather than creating or remapping columns):

- `uat_lookup_job_id`
- `uat_lookup_state`
- `uat_lookup_status`
- `uat_lookup_error_code`
- `uat_lookup_warning_count`
- `uat_lookup_attempt`
- `uat_lookup_completed_at`
- `reviewer_status`

`uat_lookup_queued_at` and `reviewer_decision_code` remain part of the broader repository review-field contract but are not written by Gate 5A (no queue timestamp exists in the durable result, and Gate 5A makes no reviewer decision).

Do not create physical Sheet columns named `row_number`, `Gate4A Source Reference`, or `Gate4A Source Row Ref`; n8n's virtual `row_number` metadata must stay authoritative, exactly as in Gate 4A.

## Identity Matching

The workflow never updates a row based only on a row number. It rebuilds the canonical Gate 4A queue identity from the one approved source row using the exact merged Gate 4A derivation (field aliases, trimming, PDPA and member-number validation, non-dummy check, UTF-8 base64, FNV-1a payload hash over the eight canonical fields, `job_id = gate4a_<payload_hash>`) and requires the copied result to match every shared safe identity field:

- `job_id` (recomputed, which transitively verifies the submitted member value, row number, and derived references)
- `intake_source` and `source_reference` (canonical `google_sheets_uat_gate4a`)
- `source_row_ref` and `row_number`
- `consent_status` (canonical `marketing_consent_not_queued`)
- `pdpa_status = yes`
- `attempt = 0`

Zero matches, more than one filtered source row, or any field mismatch refuses with no Sheet write. Raw form values are used in n8n execution memory only for this recomputation; they are never logged, returned, printed, pinned, committed, or included in any evidence, and saved execution data stays disabled.

## Sheet Update Match Key

The actual Update Row operation locates the physical row with the stable non-PII operator marker, not the virtual row number:

- Match column: `Gate5AApprovedForMapping`, match value `YES` (the same marker the read filter uses, maintained on exactly one row).
- Virtual `row_number` remains a verified identity property (it is part of the recomputed canonical `job_id`) and internal evidence input, but it is never the physical update selector. Rows inserted, removed, or reordered between the read and the update therefore cannot silently redirect the write to a different physical row: the write follows the marker, and the post-write verification below re-proves the marked row's canonical identity.
- Verified against the live installed Google Sheets v4.7 implementation (n8n `2.29.8`): the update operation uses only the first `matchingColumns` entry (no atomic multiple-column match exists, so none is claimed or depended on), it locates the first row whose match-column cell equals the mapped match value, the match column itself is only used to find the row and is never rewritten, and zero matches produce zero cell updates and zero output items, so the evidence node never runs and no success evidence can be emitted.

Do not add any other helper column for matching. No PII-bearing column may ever be used as a match key.

## Post-Write Persisted-State Verification

On the fresh-apply branch the workflow never reports success from the update node alone. After `Update Approved Review Fields Only`:

1. `Re-Read Mapped Row For Verification` performs a second Google Sheets Get Row(s) with the same unbound document and sheet selectors, the same `Gate5AApprovedForMapping = YES` filter, and all-match behaviour explicitly retained (`returnFirstMatch` disabled).
2. `Verify Persisted Mapping Strictly` then requires, from the persisted Sheet state alone:
   - exactly one returned approved row (zero, duplicate, or drifted markers are terminal);
   - the full canonical Gate 4A identity rebuilt from that persisted row still matches the sanitized result on every shared safe identity field;
   - every controlled review/status field holds exactly the intended value: `uat_lookup_job_id`, `uat_lookup_state`, `uat_lookup_status`, `uat_lookup_error_code`, `uat_lookup_warning_count`, `uat_lookup_attempt`, `uat_lookup_completed_at`, and `reviewer_status = UNREVIEWED`;
   - no partial, missing, blank, mismatched, or conflicting value in any of those fields.
3. `Emit Aggregate Evidence Only` reports `mapping_success_count = 1` only after that persisted-state verification passes; the evidence node structurally requires the verification node's output on the fresh-apply branch, so successful aggregate evidence is unreachable without it.

The exact-rerun `already_applied` branch remains zero-write: it skips the update and the post-write read-back entirely, keeps its existing pre-read exact-state validation, and reports `mapping_success_count = 0` with `mapping_already_applied_count = 1`.

### If The Update Ran But Read-Back Verification Fails

This is a terminal stop condition requiring manual review, never automated recovery:

- The workflow fails closed with a sanitized error code only (no row values, IDs, hashes, timestamps, or PII in errors or output).
- No mapping success is claimed and no aggregate success evidence is emitted.
- No rollback, no automated repair, and no automatic second update is attempted.
- Original Gate 4 and recovery artifacts are never cleared, modified, or "fixed".
- The operator stops, records the sanitized error code only, and requests review before anything further happens.

## Mapping Rules

Strict precedence, recomputed from the stored lookup fields (the stored `state` string is verified against the recomputation and never trusted alone):

1. Invalid schema, unmatched identity, or conflicting prior mapping: fail closed, no update at all.
2. `status != ok` (with a sanitized `error_code`): `LOOKUP_ERROR_REVIEW`.
3. `manual_review_required = true` or `warning_count > 0`: `MANUAL_REVIEW_REQUIRED`.
4. `member_exists = true`: `EXISTING_MEMBER_REVIEW`.
5. `member_exists = false`, no warnings, no manual-review flag: `READY_FOR_CREATE_REVIEW`.

For the current recorded recovery evidence the expected mapped state is `READY_FOR_CREATE_REVIEW`.

No state authorizes member creation. The workflow always writes `reviewer_status = UNREVIEWED`, never any create-approved value, never reviewer free text, never raw or encoded member values, and never overwrites original form answers.

## Idempotency

- Fresh mapping requires every required review/status cell on the approved row to be blank.
- An exact manual rerun of the same valid result on the same already-mapped row returns an aggregate `already_applied` outcome with zero Sheet writes.
- Any other pre-existing review/status values (different job id, different state, partial fill, conflicting data) refuse with no write.
- The workflow never modifies the original or recovery result files to mark them applied; `result_applied_at` stays `null` in the copied input and no local receipt file is written (the controlled Sheet status fields are the single idempotency surface, so no extra local artifact can drift).

## Operator Steps

Do not perform these steps until this PR is reviewed and merged and the operator run is explicitly approved.

1. On the operator PC, confirm the repository is at the reviewed merge commit and clean: `git rev-parse HEAD` and `git status`.
2. On the AutoCount host, confirm the original Gate 4 and recovery evidence files are still present and unchanged (read-only directory listing only). Never edit, delete, truncate, rename, move, overwrite, reset, or "repair" any of them.
3. Copy — never move — the single sanitized recovery result from the AutoCount host to the operator-PC staging file using an operator-performed manual transfer (the AutoCount host receives no inbound connection):

   ```text
   source (AutoCount host, unchanged):
   C:\XB\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_gate4_recovery1_results.jsonl

   staging copy (operator PC):
   C:\Users\xPass\OneDrive\Desktop\X-Boundaries\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_gate5a_result_copy.jsonl
   ```

4. Run the aggregate precheck on the staging copy and continue only on `status = ok`:

   ```powershell
   cd "C:\Users\xPass\GitHub Projects\automation"

   python scripts\member_lookup_gate5a_result_precheck.py `
     --result-jsonl "C:\Users\xPass\OneDrive\Desktop\X-Boundaries\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_gate5a_result_copy.jsonl"
   ```

5. Ensure the approved container staging directory exists, then copy the staging file into the n8n container:

   ```powershell
   docker compose -p n8n-local exec -u node n8n sh -lc `
     'mkdir -p /home/node/.n8n-files && chmod 700 /home/node/.n8n-files'

   docker compose -p n8n-local cp `
     "C:\Users\xPass\OneDrive\Desktop\X-Boundaries\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_gate5a_result_copy.jsonl" `
     n8n:/home/node/.n8n-files/member_lookup_bridge_gate5a_result_copy.jsonl
   ```

6. Copy `n8n-workflows/member_intake_gate5a_sanitized_result_mapping.workflow.json` to a local import copy in the ignored staging area (for example `C:\Users\xPass\OneDrive\Desktop\X-Boundaries\autocount_outputs\review\member_lookup_bridge\member_intake_gate5a_sanitized_result_mapping.corrected.workflow.json`) and, in a text editor, replace every `REPLACE_WITH_SOURCE_TAB_NAME` placeholder (all three Google Sheets nodes) with the exact source tab title before importing. Do not add spreadsheet IDs, URLs, or credential values to the file. Open local n8n at `http://localhost:5678` and import the edited local copy. Keep the workflow inactive.
7. In all three Google Sheets nodes, select the UAT Google credential and the real source spreadsheet in the n8n UI only. Do not paste those values anywhere. Never change the source tab locator in the n8n UI: it is pre-set by name from the import copy, and a UI tab change resets the Update-node column mappings, match column, and hidden options. If the tab locator is wrong, delete the imported workflow, fix the local import copy, and re-import. After binding, verify on `Update Approved Review Fields Only` that the match column is `Gate5AApprovedForMapping`, all nine mapped fields are present, and Cell Format is `RAW`; verify the read node filter `Gate5AApprovedForMapping = YES` is still present on both read nodes; verify `Re-Read Mapped Row For Verification` still returns all matches (`returnFirstMatch` disabled). If any of these were dropped, stop and re-import rather than reconfiguring by hand.
8. Verify the source tab: exactly one row has `Gate5AApprovedForMapping = YES` (the same genuine consenting Gate 4A/Gate 4 row), all required review/status columns above exist as headers, the review cells on that row are blank, and no physical `row_number` column exists.
9. Confirm the workflow has no scheduler, webhook, wait, HTTP request, SSH, execute-command, SQL, AC2, bridge, tunnel, member create/update/delete, or AutoCount write node, and that it remains inactive with saved execution data disabled.
10. Manually execute the workflow exactly once in n8n. Paste only the aggregate evidence shape below. Do not paste row data, node input/output, IDs, hashes, timestamps, or the file contents.
11. Stop. Gate 5A ends before any human review decision. `READY_FOR_CREATE_REVIEW` plus `reviewer_status = UNREVIEWED` is review-routing only and never member-creation approval. After recording evidence, clear the `Gate5AApprovedForMapping = YES` marker so the row cannot be selected accidentally again (operator cleanup only; the repository cannot clear Sheet values).

## Required Aggregate Evidence Shape

Precheck (step 4):

```text
status = <ok/needs_fix>
gate = gate5a_manual_inactive_n8n_result_mapping
runtime_location = local_operator_pc_non_ac2_n8n_stack
execution_mode = manual_inactive_single_result_copy_precheck
input_result_row_count = <aggregate-count-only>
result_schema_valid_count = <aggregate-count-only>
result_state_consistent_count = <aggregate-count-only>
forbidden_field_count = <aggregate-count-only>
unexpected_shape_count = <aggregate-count-only>
mapped_lookup_error_review_count = <aggregate-count-only>
mapped_manual_review_required_count = <aggregate-count-only>
mapped_existing_member_review_count = <aggregate-count-only>
mapped_ready_for_create_review_count = <aggregate-count-only>
original_gate4_artifacts_modified = false
autocount_lookup_invoked = false
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
n8n_result_mapping_run = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
final_write_automation = false
no_row_values_printed = true
sanitized_note = <fixed sanitized note printed by the script>
```

Workflow run (step 10, from `Emit Aggregate Evidence Only`):

```text
status = ok
gate = gate5a_manual_inactive_n8n_result_mapping
runtime_location = local_operator_pc_non_ac2_n8n_stack
execution_mode = manual_inactive_single_result_review_mapping_only
input_result_row_count = 1
result_schema_valid_count = 1
source_rows_read_count = 1
source_row_match_count = 1
mapping_attempt_count = 1
mapping_success_count = <1 fresh apply / 0 already applied>
mapping_already_applied_count = <0 fresh apply / 1 already applied>
mapping_error_count = 0
mapped_lookup_error_review_count = <aggregate-count-only>
mapped_manual_review_required_count = <aggregate-count-only>
mapped_existing_member_review_count = <aggregate-count-only>
mapped_ready_for_create_review_count = <aggregate-count-only>
reviewer_status_unreviewed_count = 1
original_gate4_artifacts_modified = false
autocount_lookup_invoked = false
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
final_write_automation = false
no_row_values_printed = true
```

For the current recorded recovery evidence the expected successful first run shows `mapping_success_count = 1`, `mapped_ready_for_create_review_count = 1`, and `reviewer_status_unreviewed_count = 1`. `mapping_success_count = 1` is emitted only after the post-write persisted-state verification confirms the exact intended values on the identity-verified row.

## Stop Conditions

Stop immediately if any of these occur:

- The workflow is activated, or a scheduler, webhook, wait, tunnel, or service appears.
- Any inbound connection, callback, or n8n command reaches the AutoCount host.
- The original Gate 4 or recovery artifacts are edited, deleted, renamed, moved, truncated, overwritten, or "repaired" — including to mark a result applied.
- The staging copy fails the precheck, contains more than one row, or contains any forbidden field.
- Zero or more than one source row carries `Gate5AApprovedForMapping = YES`.
- Any required review/status column is missing (add the missing headers manually, then rerun; never let the workflow create columns).
- The identity match fails, or a conflicting prior mapping is present.
- The update node ran but the post-write read-back verification disagrees with the intended persisted state (zero, duplicate, drifted, partial, mismatched, or conflicting read-back). Stop, record the sanitized error code only, and request manual review; never rerun the update automatically, never roll back, and never edit any artifact or Sheet value to "repair" the state.
- Any state would be written other than the four review states, or any reviewer value other than `UNREVIEWED`.
- Raw values, encoded/decoded/normalized member values, names, emails, birthdays, phone/member numbers, job IDs, hashes, timestamps, Sheet IDs/URLs, credential IDs, tokens, command output, screenshots, node raw input/output, execution payloads, secrets, connection strings, or PII appear in logs, evidence, docs, screenshots, or PR text.
- Any member create/update/delete, AutoCount write, direct SQL, or reviewer auto-approval path appears.

Gate 5A completion authorizes nothing further. Member creation, reviewer approval flows, scheduling, webhooks, production activation, and any AutoCount write remain separate explicitly gated future work.
