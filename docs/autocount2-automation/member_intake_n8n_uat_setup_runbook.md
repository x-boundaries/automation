# Member Intake n8n UAT Setup Runbook

Status: UAT setup and runbook only. Gate 3 local lookup preflight pass recorded. This document does not add an n8n workflow export, does not activate a live workflow, does not expose the AutoCount host, and does not authorize AutoCount writes.

## Purpose

This runbook defines the next safe setup path for AC2 member lookup UAT after the local bridge fixture/mock pass. It keeps n8n as a cloud/VPS/non-AC2 orchestrator, keeps the Windows AC2 lookup bridge as the only AC2-facing runtime, and keeps all live activation blocked.

The n8n-skills plugin is a Codex-side planning and build aid only. It is not the hosted n8n runtime and does not replace live n8n instance verification before a future build/export/activation PR.

## n8n-Skills Planning Basis

This runbook was written from the loaded n8n skills and local setup rules, with no live n8n instance tools exposed in this Codex thread.

| Source checked | Setup decision |
| --- | --- |
| `n8n-skills:using-n8n-skills` | Use skills first, then live n8n tools when available. Do not guess exact node parameters from memory. |
| `n8n-skills:n8n-workflow-lifecycle` | Plan, validate, verify saved connections, test with pinned/safe data, then publish only in a later approved step. This PR stops before publish. |
| `n8n-skills:n8n-node-configuration` | The node-level shape below names node families and operations only. Exact parameters and resource selectors remain future live-instance verification work. |
| `n8n-skills:n8n-credentials-and-security` | Credentials belong in n8n's credential system or local bridge runtime configuration, never in docs, queue rows, workflow text, or pasted evidence. |
| `ai-agent-toolkit:n8n-agent-rules` | Keep workflows inactive/unpublished by default, do not run live n8n actions without target-specific approval, and keep secrets out of repo files. |
| `ai-agent-toolkit:n8n-local-setup` | Local n8n is acceptable for local development only. The long-term and first queue UAT direction is hosted/VPS/non-AC2 n8n, with no n8n service hosted on the AutoCount host. |

Codex tool discovery did not expose live n8n tools such as `get_sdk_reference`, `search_nodes`, `get_node_types`, `validate_workflow`, `get_workflow_details`, `test_workflow`, or `search_data_tables`. Because of that, this PR remains documentation/runbook only and does not claim an import-ready workflow.

## Selected UAT Setup Path

The first n8n UAT should use hosted/VPS/non-AC2 n8n with a Google Sheets UAT queue tab and dummy fixture data first.

Decision:

- Do not use local n8n on the AutoCount host as the long-term path.
- Do not start with n8n Data Tables because bridge access and live API behavior were not verified in this session.
- Do not start with a custom hosted intake API because that is a later replacement for Google Forms/Sheets, not required for this first queue rehearsal.
- Use Google Sheets UAT tabs because they match the temporary Google Forms intake surface, are reviewable by operators, and keep the bridge contract source-agnostic through `intake_source`, `source_reference`, and `source_row_ref`.
- Run dummy fixture rows through n8n first, with no AC2 lookup, before any real queue UAT.

The Google Sheets queue is UAT-only. It must be disabled or replaced before production activation.

## Required Gate Order

### Gate 0: Documentation And Local Tests Only

This PR may update docs and tests only. It must not commit a production workflow export, real Sheet IDs, Sheet URLs, credentials, local AC2 target values, row-level runtime output, or an enabled automation.

### Gate 1: Fixture/Mock Bridge Pass

The already proven local bridge fixture/mock pass remains the baseline evidence:

```json
{
  "status": "ok",
  "queue_mode": "fixture",
  "lookup_mode": "mock",
  "processed_count": 5,
  "result_state_counts": {
    "READY_FOR_CREATE_REVIEW": 1,
    "EXISTING_MEMBER_REVIEW": 1,
    "MANUAL_REVIEW_REQUIRED": 1,
    "LOOKUP_ERROR_REVIEW": 2
  },
  "dry_run_only": true,
  "final_write_automation": false
}
```

Do not paste row-level fixture input, result rows, encoded submitted values, raw member values, normalized member values, local command transcripts, stderr/stdout, credentials, Sheet IDs, Sheet URLs, names, emails, or phone numbers.

### Recorded Local n8n Dummy Wiring Rehearsal

A local non-AC2 n8n dummy wiring rehearsal has passed on the operator PC. The runtime evidence label is:

```text
n8n_runtime_location = local_operator_pc_non_ac2_n8n_stack
```

This was not hosted/VPS n8n and must not be reported as `hosted_or_vps_non_ac2`.

Sanitized aggregate evidence recorded for this local development rehearsal:

```text
status = ok
n8n_runtime_location = local_operator_pc_non_ac2_n8n_stack
workflow_activation = inactive
execution_mode = manual_dummy_rehearsal
dummy_rows_read_count = 3
queue_rows_appended_count = 1
result_rows_read_count = 4
review_rows_updated_count = 4
result_state_counts = READY_FOR_CREATE_REVIEW=1, EXISTING_MEMBER_REVIEW=1, MANUAL_REVIEW_REQUIRED=1, LOOKUP_ERROR_REVIEW=1
ac2_touched = false
bridge_called = false
final_write_automation = false
sanitized_note = No real Sheet IDs/URLs, credentials, row-level output, raw/encoded/normalized values, names, emails, phone numbers, command transcripts, execution payloads, node raw input/output dumps, or PII are pasted.
```

This evidence proves only the local n8n and Google Sheets queue/review wiring with dummy rows. It does not prove hosted/VPS runtime readiness, does not activate or publish a workflow, does not touch AC2, does not call the bridge, and does not authorize Gate 4.

The next gate is Gate 3: local Windows PowerShell lookup preflight. Gate 3 must remain read-only, fixture-based or dummy-only, aggregate-evidence-only, with no member create/update, no AutoCount writes, and no raw, encoded, or normalized member values or PII pasted.

### Gate 2: Hosted n8n Dummy Queue Rehearsal

Gate 2 may proceed before the PowerShell lookup preflight because it uses dummy fixture rows only and does not touch AC2.

Run the first n8n UAT rehearsal with dummy fixture rows only:

- hosted/VPS/non-AC2 n8n,
- workflow left inactive/unpublished,
- manual execution only,
- UAT Google credential selected in the n8n UI,
- placeholder tab names resolved only in n8n resource selectors,
- no bridge call to AC2,
- no public inbound webhook, tunnel, callback, or reverse proxy on the AC2 host,
- execution data minimized and pruned before running.

This proves n8n can read a marked UAT row, build an allowed queue job shape, write only allowed queue columns, read a dummy sanitized result row, and update only review/status fields.

The recorded local operator PC pass above is useful wiring evidence, but it is not hosted/VPS runtime evidence. Do not use it to claim hosted/VPS readiness.

Use [member_intake_n8n_gate2_dummy_rehearsal_runbook.md](member_intake_n8n_gate2_dummy_rehearsal_runbook.md) for the exact Gate 2 operator setup steps, placeholder UAT tabs, allowed columns, dummy row shapes, manual inactive n8n node shape, safe paste-back evidence, and stop conditions.

### Gate 3: Required Local PowerShell Lookup Preflight

Before any real n8n queue UAT is allowed to touch AC2 lookup, the bridge worker PowerShell lookup mode must be run locally once on the approved Windows AC2 lookup bridge host. Gate 3 does not block Gate 2, but Gate 3 must pass before Gate 4.

Requirements:

- Run only on the local Windows AC2 lookup environment or approved AC2-capable Windows bridge host.
- Use `--queue-mode fixture`.
- Use `--lookup-mode powershell` and `--enable-powershell-lookup`.
- Use only safe synthetic input or one manually approved lookup input that is dummy-only.
- Keep the fixture file and result file under the local ignored output directory.
- The bridge may call only the proven read-only lookup script with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8`.
- `AC2_PROBE_PASSWORD` and local AC2 target settings must come from local runtime configuration.
- Verify only that the local Windows preflight environment is available, the AutoCount session/auth bootstrap path is available, `MemberCommand` is available, and `MemberCommand.GetMember` is available.
- Keep the lookup read-only. Do not call member create, update, delete, number-generation, member browse export, direct SQL write, or final write automation paths.
- Do not involve n8n in Gate 3. No n8n workflow activation, hosted/VPS real queue UAT, webhook, tunnel, callback, or n8n-triggered bridge call is part of Gate 3.
- Paste back aggregate sanitized evidence only. Do not paste the bridge worker stdout, result JSONL rows, command transcript, stderr/stdout, local target values, raw member values, encoded member values, normalized member values, names, emails, phone numbers, or PII.

Required paste-back shape:

```text
status = ok
gate = gate3_local_powershell_lookup_preflight
runtime_location = local_windows_ac2_lookup_environment
execution_mode = manual_read_only_preflight
autocount_session_bootstrap_available = <true/false>
member_command_found = <true/false>
get_member_found = <true/false>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_manual_review_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
n8n_involved = false
bridge_called_by_n8n = false
final_write_automation = false
sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, or PII are pasted.
```

All count fields are aggregate-only. A successful lookup, missing lookup, manual-review lookup, or lookup error must be reduced to booleans and counts only. No state authorizes member creation.

#### Recorded Gate 3 Sanitized Evidence

Gate 3 local Windows PowerShell lookup preflight has passed with aggregate-only sanitized evidence:

```text
status = ok
gate = gate3_local_powershell_lookup_preflight
runtime_location = local_windows_ac2_lookup_environment
execution_mode = manual_read_only_preflight
autocount_session_bootstrap_available = true
member_command_found = true
get_member_found = true
lookup_attempt_count = 1
lookup_success_count = 1
lookup_manual_review_count = 1
lookup_error_count = 0
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
n8n_involved = false
bridge_called_by_n8n = false
final_write_automation = false
sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, or PII are pasted.
```

The operator output included local PowerShell prompt wrapper noise, but no sensitive values were recorded here. Only the sanitized evidence body above is retained.

This Gate 3 pass proves only that the local Windows AC2 lookup environment was available, the AutoCount session/auth bootstrap was available, `MemberCommand` was found, `MemberCommand.GetMember` was found, one lookup attempt succeeded, the lookup result was manual-review rather than an error, and no member create/update/write/direct SQL/n8n/final automation path was invoked.

This Gate 3 pass does not prove production automation, does not authorize member create/update, does not authorize AutoCount writes, and does not by itself prove hosted/VPS n8n runtime readiness.

### Gate 4: Real Queue UAT Touching AC2 Lookup

Only after Gates 1, 2/2A, and 3 pass may an operator consider a real queue UAT where the Windows bridge polls outbound and touches AC2 lookup. Gate 4 is still review-only, dry-run-only, and inactive by default. It cannot create or update AutoCount members. The local operator PC n8n dummy wiring pass does not prove hosted/VPS readiness; hosted/VPS runtime readiness must still be proven before any hosted/VPS real queue UAT.

Gate 4 remains blocked until a separate reviewed PR defines the exact real queue UAT plan, including the queue surface, dummy-to-real transition boundary, hosted/VPS runtime readiness proof, operator evidence shape, rollback/stop conditions, and review-only status handling.

## Minimum Next Runnable n8n UAT Step

The minimum next runnable n8n step is a manual, inactive hosted/VPS/non-AC2 workflow rehearsal against dummy Google Sheets UAT tabs.

The exact operator runbook for this step is [member_intake_n8n_gate2_dummy_rehearsal_runbook.md](member_intake_n8n_gate2_dummy_rehearsal_runbook.md).

It should exercise only:

1. Schedule/manual start disabled for activation, run manually by the operator.
2. Read dummy rows marked for lookup from `Form Responses UAT`.
3. Apply intake and PDPA guards.
4. Build the allowed queue object without logging the submitted value.
5. Append one dummy queue row to `Lookup Queue UAT`.
6. Read one dummy sanitized result row from `Lookup Results UAT`.
7. Map the sanitized result to a review state.
8. Update only the approved review/status fields on the dummy form row.

This step does not touch AC2 and does not require the local bridge to poll a real queue.

## Reference Node-Level Shape

No workflow export is committed. The exact node parameters must be verified later with live n8n tooling.

Workflow A, queue dummy lookup jobs:

1. `Manual Trigger` or disabled `Schedule Trigger` named `Start UAT Queue Rehearsal`.
2. `Google Sheets` named `Read Dummy UAT Rows Marked For Lookup`.
3. `If` named `Block Already Queued Rows`.
4. `If` named `Apply Intake And PDPA Guards`.
5. `Edit Fields` named `Build Allowed Queue Job`.
6. `If` named `Validate Encoded Member Value`.
7. `Google Sheets` named `Append Dummy UAT Queue Job`.
8. `Google Sheets` named `Mark Dummy Form Row Queued`.

Workflow B, apply dummy sanitized results:

1. `Manual Trigger` or disabled `Schedule Trigger` named `Start UAT Result Rehearsal`.
2. `Google Sheets` named `Read Dummy Bridge Results Ready For n8n`.
3. `If` or `Switch` named `Validate Result Schema`.
4. `Switch` named `Map Result To Review State`.
5. `Google Sheets` named `Update Dummy Form Review Fields`.
6. `Google Sheets` named `Mark Dummy Result Applied`.

Workflow C, timeout/retry rehearsal:

1. `Manual Trigger` or disabled `Schedule Trigger` named `Start UAT Timeout Sweep`.
2. `Google Sheets` named `Read Dummy Expired Lookup Jobs`.
3. `If` named `Retry Or Error Review`.
4. `Google Sheets` named `Update Dummy Timeout Review Fields`.

Do not use an n8n Execute Command node for AC2 lookup in hosted/cloud/VPS n8n. Execute Command would run on the n8n host/container, not on the Windows AC2 lookup bridge host.

## Operator Setup Checklist

- Create or select an isolated UAT project/folder in hosted/VPS/non-AC2 n8n.
- Create Google credentials in the n8n UI with least privilege for the UAT spreadsheet only.
- Create a separate least-privilege local bridge credential outside n8n if a later real queue poll needs it.
- Create placeholder UAT tabs with allowed columns only.
- Protect UAT tabs so only the operators who need them can inspect or edit them.
- Configure n8n execution-data saving to the most restrictive available setting before any run.
- Keep workflow inactive/unpublished unless a future PR explicitly approves activation.
- Verify node schemas, resource selectors, credential bindings, and connections in the live n8n instance before any build/export/activation PR.

## Blocked Until A Future PR

- Import-ready workflow export.
- Live scheduled activation.
- Production n8n workflow.
- n8n hosted on the AutoCount host as the long-term path.
- AC2 host public inbound webhook, tunnel, callback, or reverse proxy.
- Real Sheet IDs or Sheet URLs in repo files.
- Credentials, tokens, service account JSON, local AC2 target values, or runtime secrets in repo files.
- Row-level runtime output or command transcripts in repo files or PR evidence.
- AutoCount member writes or final write automation.
- `READY_FOR_CREATE_REVIEW` being treated as approval to create.
- `PDPA Acknowledged = Imported` being treated as valid consent.
