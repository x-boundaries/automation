# n8n Workflow Exports

This directory holds source-controlled n8n workflow export JSON for this repository. Agents must inspect this README before adding or modifying any workflow export here, per the n8n workflows playbook (`docs/agent-playbooks/n8n-workflows.md`).

Workflow JSON in this directory is source-controlled evidence of workflow design and UAT scope. It is not proof of deployment, import, activation, or execution on any n8n instance.

## Current Workflow Exports

### member_forms_gateway_ingest.workflow.json

- Purpose: inactive production-boundary design evidence for the configurable Google Forms source adapter (gateway source cursor v2). Each manual run begins or resumes one gateway scan epoch bound to the fixed `production_cutover_exact`, then runs a bounded manual page loop (at most `max_pages_per_run` pages): fetch one page with `pageSize=1` and the verbatim filter `timestamp >= <cutover>`, open the page durably, obtain one accepted (`/v1/source-events`) or PII-free rejected (`/v1/source-rejections`) handling receipt per item, and commit the page. `response.createTime` is forwarded verbatim; there is no sorting, ordering assertion or `Date` re-serialisation, and no progress is stored in `staticData`. A crashed open page restarts the epoch with `ambiguous_crashed_attempt`; only a positively classified invalid page token restarts with `token_invalidated`; every other failure halts. In `first_member` mode the run stops after the first committed member page. It does not create or update AutoCount members and has no continuous scheduling authority.
- Status: source-controlled, inactive, credential-free, and incapable of live execution merely by repository checkout. It contains no real Form ID, question ID, credential binding, customer data, pinned data, or execution data; its mapping IDs and endpoint values remain placeholders.
- Timeout policy: all eight HTTP nodes declare an explicit `options.timeout` of 30000 ms.
- Retry policy: bounded node-level retry only where a repeat is safe: epoch begin/resume 3 x 1000 ms, the Google Forms page read 3 x 2000 ms, and the receipt-idempotent ingest and rejection POSTs 2 x 2000 ms. The two restarts, page open and page commit are compare-and-set writes and declare no `retryOnFail`, `maxTries`, or `waitBetweenTries`; their failures surface for deliberate reconciliation rather than self-heal.
- MCP exposure is disabled in source control through `settings.availableInMCP: false`. MCP exposure and workflow activation are independent controls in n8n, and this export grants neither.
- Runbook: [Member gateway production runbook](../docs/autocount2-automation/member_gateway_production_runbook.md).
- The export stays `active: false` with its internal `activation_enabled` gate false and a manual trigger only. Import, credential binding, activation, execution, and any live re-alignment to this canonical definition each require separate explicit current-turn approval; this export is offline validation evidence only.
- Focused offline coverage: [tests/test_member_gateway_n8n.py](../tests/test_member_gateway_n8n.py).

### member_welcome_email_outbox.workflow.json

- Purpose: inactive, manual-only mailer for the durable gateway `welcome_v1` outbox. Each run claims at most one due row, verifies the gateway-built message against its durable hash, records send intent, sends it once with Send Email, and records either SMTP acceptance (`SENT`, not inbox-delivery proof) or, on the Send Email error output, `DELIVERY_OUTCOME_UNCERTAIN`, which is never resent automatically.
- Status: source-controlled, inactive, credential-free. Sender, subject and body come only from the gateway claim payload; the export contains no literal email address. Send Email automatic retry is disabled, n8n attribution is disabled and no Reply-To is set.
- Retry policy: the four idempotent gateway calls (claim, send intent, both results) retry 3 x 5000 ms; Send Email never retries.
- MCP exposure disabled (`settings.availableInMCP: false`); manual trigger only; `staticData: null`.
- Runbook: [Member gateway production runbook](../docs/autocount2-automation/member_gateway_production_runbook.md).
- Focused offline coverage: [tests/test_member_gateway_n8n.py](../tests/test_member_gateway_n8n.py).

### member_intake_gate4a_container_queue_write.workflow.json

- Purpose: Gate 4A staged proof only. Reads exactly one approved real non-dummy source row from the Google Form / Google Sheet intake, validates and normalizes only allowed fields, and writes exactly one sanitized `PENDING_LOOKUP` queue row to JSONL inside the n8n container. No AC2 lookup, no bridge call, no result mapping.
- UAT status: manual, inactive. Never activated; runs only by explicit manual operator execution during approved UAT.
- Runbook: [Gate 4A manual queue handoff runbook](../docs/autocount2-automation/member_intake_n8n_gate4a_manual_queue_handoff_runbook.md).
- Committed selectors and credentials remain unbound. The export contains no bound credentials and no live selector values.
- Pinned data and execution data must not be committed with this export.
- This JSON is evidence, not proof that the workflow is deployed, imported, activated, or has executed anywhere.
- AutoCount writes, member creation, and final automation remain excluded unless separately reviewed and authorised.

### member_intake_gate5a_sanitized_result_mapping.workflow.json

- Purpose: Gate 5A staged proof only. Maps exactly one previously produced sanitized Gate 4 recovery result (operator copy-only handoff into the approved container file area) into controlled Google Sheet review/status fields via strict fail-closed validation, then stops. No AC2 lookup, no AutoCount host contact, no reviewer approval, no member creation or update.
- UAT status: manual, inactive. Never activated; runs only by explicit manual operator execution during approved UAT.
- Runbook: [Gate 5A manual result mapping runbook](../docs/autocount2-automation/member_intake_n8n_gate5a_manual_result_mapping_runbook.md).
- Committed selectors and credentials remain unbound. The export contains no bound credentials and no live selector values. The sheet tab locator ships in `By Name` mode with the non-secret placeholder `REPLACE_WITH_SOURCE_TAB_NAME`, replaced by the operator in a local import copy before import; this is required because the Google Sheets 4.7 editor resets the Update-node column mappings whenever the tab selection changes in the UI (see the runbook).
- Pinned data and execution data must not be committed with this export.
- This JSON is evidence, not proof that the workflow is deployed, imported, activated, or has executed anywhere.
- AutoCount writes, member creation, and final automation remain excluded unless separately reviewed and authorised.

### member_create_uat_result_mapping.workflow.json

- Purpose: Single-member creation UAT staged proof only. Maps exactly one previously produced sanitized create UAT terminal result (operator copy-only handoff into the approved container file area) into controlled Google Sheet review columns for the exact verified source record, then stops. It performs no reviewer approval, no package generation, no duplicate checking, no AC2 lookup, no AutoCount host or VM contact, and no member creation or update.
- The row is located by the single-use `uat_create_operation_id` key (never a shared marker and never a row number): the operator seeds `uat_create_operation_id`, `uat_create_source_record_id`, and `uat_create_source_fingerprint` on the approved row from the built package, and the workflow reads and updates that exact row by operation id. Identity is then revalidated by comparing the result `source_record_id` and `source_fingerprint` hashes (hash compare only; no raw PII enters the workflow), and the terminal code is recomputed from the canonical state table and rejected if it contradicts the flags.
- UAT status: manual, inactive. Never activated; runs only by explicit manual operator execution during approved UAT.
- Runbook: [Single-member create UAT runbook](../docs/autocount2-automation/member_create_uat_runbook.md).
- Committed selectors and credentials remain unbound. The export contains no bound credentials and no live selector values. The sheet tab locator ships in `By Name` mode with the non-secret placeholder `REPLACE_WITH_SOURCE_TAB_NAME`, replaced by the operator in a local import copy before import (same v4.7 editor binding caveat as Gate 5A).
- Operators must add the controlled columns listed in the workflow boundary sticky note to the source tab before running.
- Pinned data and execution data must not be committed with this export.
- This JSON is evidence, not proof that the workflow is deployed, imported, activated, or has executed anywhere.
- This is UAT scaffolding, not the permanent production member-intake workflow, which remains newly designed and unbuilt (see [member intake automation blueprint](../docs/autocount2-automation/member_intake_automation_blueprint.md)).

### energygrid_download_error_handler.workflow.json

- Purpose: dedicated internal error notification handler for the Energy@Grid bill downloader. It is selected as the n8n Error Workflow for EnergyGrid workflows and sends one bounded operations alert to the owner/operator. It is not a customer-facing notice.
- Architecture: `Error Trigger` -> `Build Error Row` (bounded normalisation) -> `Build Safe Error Alert Context` (HTML-safe conversion) -> `Send EnergyGrid Failure Alert` (Telegram).
- Status: source-controlled, inactive. This file is design evidence only; it is not proof of deployment, import, activation, or execution on any n8n instance.
- The Telegram destination and credential are deliberately unbound in source control. The export carries no credential binding, no webhook id, and no instance id, and the `chatId` ships as the non-secret placeholder `REPLACE_WITH_TELEGRAM_CHAT_ID`.
- The operator binds the approved Telegram credential and the real destination only in a separately authorised local import copy, never in this repository.
- Supported events: ordinary EnergyGrid execution failures (`event_type: execution_failure`), and missing Scheduler heartbeat (`event_type: scheduler_heartbeat_missing`), which renders its own event label instead of the generic download-failure label.
- Only bounded operational metadata is retained: `source`, `event_type`, `failure_stage`, `support_ref`, `run_id`, `exit_code`, alongside the existing n8n error fields. The raw Error Trigger payload is deliberately not retained, and no replacement whole-payload field may be added.
- The Telegram node carries no `onError`, `continueOnFail`, or `alwaysOutputData`: a failed alert delivery must fail the handler execution and stay visible in n8n rather than being reported as a success. For the same reason this export deliberately does not adopt the sibling exports' `saveDataErrorExecution: none` setting, which would hide that failed execution.
- This workflow must never be configured as its own error workflow.
- The future Windows ingress workflow and the missing-heartbeat watchdog workflow are separate workflows and are not included here.
- Committing this file performs no live n8n action. Import, credential binding, activation, and execution each require separate explicit current-turn approval.
- Focused offline coverage: [tests/test_energygrid_n8n_error_handler.py](../tests/test_energygrid_n8n_error_handler.py).

## Directory Rules

- Do not rename existing workflow files during unrelated work. Existing filenames are canonical and are referenced by tests, README links, and runbooks.
- New exports follow this repository's established naming convention: lowercase snake_case with the suffix `*.workflow.json`.
- A naming migration requires its own reviewed PR, because filenames are referenced by tests, README links, and runbooks.
- Live import, export, execution, activation, credential binding, deployment, or sync requires explicit current-turn approval naming the target instance and the permitted operation.
- Real credentials, credential IDs, Sheet IDs, cached selectors, private execution data, and live exports remain local and uncommitted. Local live import/export staging lives under `.tmp/`, which is git-ignored and must never be committed.
- The approved local container file area is `/home/node/.n8n-files/`, where the relevant runbook permits it.
- Original Gate 4 and recovery evidence must not be modified, moved, deleted, renamed, truncated, or overwritten.
- `.n8n/` and `.n8n-local/` are local n8n runtime state (databases, credentials, configuration, execution history). They are git-ignored, must never be committed or copied into workflow exports, and are not source-controlled workflow definitions. Agents must not inspect or print their contents without a separate explicit current-turn request authorising a bounded local diagnostic. Source-controlled workflow definitions belong only in this directory.

## Helper Scripts

### Bounded member-gateway import successor

`scripts/import-member-forms-gateway-bounded.ps1` is a dedicated, fresh
split-successor helper for the inactive member gateway source-adapter
workflow. It is the sole bounded production path for this workflow. It reads
the authoritative cursor v2 (keeping only a public-safe projection: no page
token or response ID is persisted) and exact target metadata, captures an immutable
operation plan, and can import at most the one reviewed workflow after an
explicit apply confirmation. It does not call the generic live importer,
export all workflows, execute or activate a workflow, enable MCP, or contact
Google Forms, AutoCount, or member endpoints.

The binding shape is
`../config/member_forms_gateway_bounded_import.v2.template.json`. Real target
IDs, form/question IDs, resolved credential IDs and names, the production cutover, source
tokens, and operation material stay in ignored private custody under
`.n8n-local/member-gateway-bounded-import/operations/`. These files are
rejected if they are tracked, outside the canonical private root, linked, or
not protected by the required Windows ACL. Persisted JSON is canonical UTF-8
without BOM with LF line endings.

`scripts/import-member-welcome-email-bounded.ps1` is the equivalent bounded
path for `member_welcome_email_outbox.workflow.json`, with binding shape
`../config/member_welcome_email_bounded_import.v1.template.json` and private
custody under `.n8n-local/member-welcome-email-bounded-import/operations/`. It
binds the mailer bearer to exactly the four gateway nodes and one SMTP
credential to the Send Email node, never calls the gateway, and refuses Send
Email retry, attribution, Reply-To, or literal addresses.

The private manifest must bind each credential role's exact resolved
`credential_id`, `credential_name`, `credential_type`, and node role. Prepared
node references retain both ID and name; readback rejects a different object
even when its name and type are unchanged. The manifest's
`security.approved_gateway_origin` is the reviewed private production
authority and must be a canonical `https://<host>:443` origin with no
userinfo, query, fragment, or non-root path. The
`https://gateway.example.com:443` value in the committed template is an
illustrative placeholder only; the private reviewed production binding must
replace it with the actual approved origin. All three gateway endpoints must
match that origin's scheme, host, and port. The Forms endpoint is separately fixed to
`https://forms.googleapis.com:443` with the exact
`/v1/forms/<form-id>/responses` path and no alternate host, port, version,
query, or canonicalisation form.

When the target is containerised, the operation records immutable container
and image identities, the non-root n8n import UID/GID, a random operation
nonce, sticky `/tmp` proof, private 0700/0600 staging ownership and modes, and
the exact prepared-file hash before import. Root-assisted staging,
verification, and exact recursive cleanup are allowed; the n8n import itself
is never run as root. A cleanup failure after dispatch leaves the operation in
terminal no-replay custody. No completion receipt or success/no-op status may
be emitted unless persisted custody proves `cleanup_state=cleaned` and
`cleanup_verified=true`; recovery may only perform readback or separately
authorised exact cleanup.

The helper's CapturePlan and Apply recovery contract is covered by the exact
offline command `python -m unittest tests.test_member_gateway_bounded_import_security -v`.
An incomplete or ambiguous operation is retained and reconciled by exact
identity/readback evidence; it is never silently reinitialised or replayed.

The approved n8n import/export helper-script package from the Toolkit source (`ai-agent-toolkit:n8n-workflow-helper-scripts`, project `n8n.workflow-toolkit`) is installed under `scripts/` in this directory, as required by the n8n workflows playbook (`docs/agent-playbooks/n8n-workflows.md`) and the package's own consumer-repo layout.

- Entry points: `scripts/_import-n8n-workflows-live.cmd` and `scripts/_export-n8n-workflows-live.cmd` (manual, review-required; they never run automatically).
- The helper files are generated from Toolkit curated output. Do not edit them directly except for reviewed local deviations; report fixes upstream so the next Toolkit sync carries them.
- Local deviation from the generic Toolkit template: new `AllLive` export filenames follow this repository's `*.workflow.json` snake_case convention (see Directory Rules) in `export-n8n-workflows-live.ps1` and `sync-n8n-live-exports.cjs`. This deviation is reported upstream for adoption.
- Local deviation from the generic Toolkit template: import `-DryRun` in `import-n8n-workflows-live.ps1` plans into an isolated temporary `.tmp/n8n-live-import-dryrun-<random>` directory that is removed before exit, and never clears, regenerates, or creates the configured persistent prepared dir (see `scripts/README.md`, Import Dry-Run Isolation). This deviation is reported upstream for adoption.
- Live import/export/sync through these helpers still requires explicit current-turn approval naming the target instance and operation.
