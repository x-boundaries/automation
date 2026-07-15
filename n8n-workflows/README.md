# n8n Workflow Exports

This directory holds source-controlled n8n workflow export JSON for this repository. Agents must inspect this README before adding or modifying any workflow export here, per the n8n workflows playbook (`docs/agent-playbooks/n8n-workflows.md`).

Workflow JSON in this directory is source-controlled evidence of workflow design and UAT scope. It is not proof of deployment, import, activation, or execution on any n8n instance.

## Current Workflow Exports

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
- Committed selectors and credentials remain unbound. The export contains no bound credentials and no live selector values.
- Pinned data and execution data must not be committed with this export.
- This JSON is evidence, not proof that the workflow is deployed, imported, activated, or has executed anywhere.
- AutoCount writes, member creation, and final automation remain excluded unless separately reviewed and authorised.

## Directory Rules

- Do not rename existing workflow files during unrelated work. Existing filenames are canonical and are referenced by tests, README links, and runbooks.
- New exports follow this repository's established naming convention: lowercase snake_case with the suffix `*.workflow.json`.
- A naming migration requires its own reviewed PR, because filenames are referenced by tests, README links, and runbooks.
- Live import, export, execution, activation, credential binding, deployment, or sync requires explicit current-turn approval naming the target instance and the permitted operation.
- Real credentials, credential IDs, Sheet IDs, cached selectors, private execution data, and live exports remain local and uncommitted.
- The approved local container file area is `/home/node/.n8n-files/`, where the relevant runbook permits it.
- Original Gate 4 and recovery evidence must not be modified, moved, deleted, renamed, truncated, or overwritten.

## Helper Scripts

No approved n8n import/export helper-script package was included in the current Toolkit healing output, and none is installed in this directory. Do not install improvised helper scripts. Helpers must only be installed by a future Toolkit refresh or a separately reviewed PR using the approved Toolkit source (`ai-agent-toolkit:n8n-workflow-helper-scripts`).
