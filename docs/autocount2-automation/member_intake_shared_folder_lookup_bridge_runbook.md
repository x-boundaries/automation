# Member Intake Shared-Folder Lookup Bridge Runbook

Status: shared-folder handoff design and VM-side manual runner only. This documents the private host-to-VM shared-folder handoff topology for the member lookup bridge and adds `scripts/member_lookup_shared_folder_handoff.py` with local tests. No live end-to-end execution was performed for this work. No n8n workflow was imported, executed, activated, or modified. No AutoCount member create, update, or delete path exists here, and no direct SQL is used. Both reused n8n workflows remain Manual Trigger only and inactive.

## Purpose

The current deployment runs n8n (Docker) on the main physical PC and AutoCount Accounting 2.x inside a Windows VM on that same physical PC. Because both runtimes share one physical machine, the n8n-to-bridge handoff does not need a tunnel, queue API, webhook, or any network service for UAT. A private host-to-VM shared folder is the entire transport.

This runbook reuses the existing contracts unchanged:

- Request contract and pending-queue file: Gate 4A, per [member_intake_n8n_node_contract.md](member_intake_n8n_node_contract.md) and [member_intake_n8n_gate4a_manual_queue_handoff_runbook.md](member_intake_n8n_gate4a_manual_queue_handoff_runbook.md).
- Sanitized result contract and result-copy file: Gate 5A, per [member_intake_n8n_gate5a_manual_result_mapping_runbook.md](member_intake_n8n_gate5a_manual_result_mapping_runbook.md).
- Read-only lookup engine: the Gate 3C filesystem runtime harness and `scripts/ac2_member_lookup_review.ps1`, per [member_intake_local_lookup_bridge_runbook.md](member_intake_local_lookup_bridge_runbook.md).

No new queue or result fields are introduced. The reused n8n workflow exports (`n8n-workflows/member_intake_gate4a_container_queue_write.workflow.json` and `n8n-workflows/member_intake_gate5a_sanitized_result_mapping.workflow.json`) are not modified by this topology.

## Topology

```text
main physical PC
  n8n (Docker, Manual Trigger only, inactive)
    Gate 4A workflow writes one sanitized PENDING_LOOKUP row to
    /home/node/.n8n-files/member_lookup_bridge_gate4a_pending_queue.jsonl
  operator copies the staged file out of the n8n container
    into <share-root>\inbox\

private host-to-VM shared folder (the only handoff surface)

Windows VM (AutoCount Accounting 2.x runtime)
  scripts/member_lookup_shared_folder_handoff.py (manual, explicit opt-in)
    -> reads <share-root>\inbox\member_lookup_bridge_gate4a_pending_queue.jsonl
    -> read-only lookup via the Gate 3C harness and ac2_member_lookup_review.ps1
    -> writes sanitized result to
       <share-root>\outbox\member_lookup_bridge_gate5a_result_copy.jsonl
    -> idempotency markers stay VM-local, outside the share

main physical PC
  operator copies the outbox result file into the approved n8n container
    file area /home/node/.n8n-files/
  Gate 5A workflow (manual, inactive) maps the sanitized result
```

Bind-mounting `/home/node/.n8n-files` onto the shared folder would remove both operator copy steps, but that is a Docker Compose change to the live n8n stack. It is not performed here and requires its own explicit current-turn approval before any future use.

## Shared Folder Boundary

- The share must be private between the physical host and the VM only: a hypervisor shared folder, or an authenticated SMB share bound to a host-only/private interface. Never a guest-accessible, public, workgroup-open, or internet-reachable share.
- Restrict share permissions to the operator account and the VM worker account. No Everyone/anonymous access.
- The share must not sit inside a cloud-synced path (OneDrive, Dropbox, Google Drive) on either side.
- The share holds only the two contract files below. It must not expose the AutoCount installation, AC2 runtime state, SQL Server data, credentials, `.env` values, `.n8n/` runtime state, or this repository.
- The pending-queue file contains `submitted_member_no_base64_utf8`, which is sensitive operational data. Do not open, print, paste, or commit it; delete it per the gate evidence-retention rules after the handoff closes.
- No tunnel, reverse proxy, queue API, webhook, scheduler, Windows service, or public inbound path to the VM is created, used, or approved by this topology.

## Share Layout

```text
<share-root>\inbox\member_lookup_bridge_gate4a_pending_queue.jsonl
<share-root>\outbox\member_lookup_bridge_gate5a_result_copy.jsonl
```

The `inbox` and `outbox` directories must exist before the worker runs. The filenames are fixed by the reused Gate 4A and Gate 5A contracts and must not be renamed.

Idempotency markers never live in the share. They stay VM-local and ignored:

```powershell
$processed = 'C:\XB\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_shared_folder_processed'
$failed = 'C:\XB\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_shared_folder_failed'
```

The worker refuses to run if either marker directory is inside the share root (or the share root is inside a marker directory).

## VM Worker Behavior

`scripts/member_lookup_shared_folder_handoff.py` is a thin fail-closed wrapper over the Gate 3C runtime harness:

- Default invocation refuses to run; `--enable-shared-folder-handoff-review` is mandatory.
- The real read-only lookup additionally requires `--enable-powershell-lookup`. Mock mode exists only for local harness validation and automated tests; mock evidence is not handoff pass evidence.
- `--approved-batch-size` defaults to 1 and is hard-capped at 10. A pending file with more rows than the approved batch size is rejected before any lookup.
- A missing share root, missing inbox/outbox directory, or missing pending file is `needs_fix` with zero lookups.
- A non-empty outbox result file blocks fresh work: if every pending row already has a matching VM-local marker with no payload conflict, the run reports `already_processed` with zero lookups; any other non-empty outbox state is `needs_fix` with zero lookups. The worker never deletes or overwrites an existing outbox result file.
- Malformed pending rows, contract violations, forbidden fields, invalid PDPA status, retry exhaustion, and payload-hash conflicts follow the existing Gate 3C failed/dead-letter discipline into the VM-local failed directory.
- The worker never writes to AutoCount, never runs direct SQL, and has no member create, update, or delete path. `READY_FOR_CREATE_REVIEW` remains review-only and is not approval to create.

## Exact Operator Command

Run only inside the AutoCount VM, from the repository root, after setting the four `AC2_PROBE_*` values as runtime-only process environment values (never in repo files, machine/user persistence, command arguments, or pasted evidence):

```powershell
$share = 'X:\xb_member_lookup_handoff'
$processed = 'C:\XB\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_shared_folder_processed'
$failed = 'C:\XB\autocount_outputs\review\member_lookup_bridge\member_lookup_bridge_shared_folder_failed'

python scripts\member_lookup_shared_folder_handoff.py `
  --enable-shared-folder-handoff-review `
  --share-root "$share" `
  --processed-dir "$processed" `
  --failed-dir "$failed" `
  --enable-powershell-lookup `
  --allow-root-login
```

`$share` is an example VM-visible path; use the actual private share path. Do not paste the real share UNC path, hostnames, account names, or any credential into evidence.

## Expected Aggregate Evidence Shape

The operator may paste back only this sanitized aggregate shape:

```text
status = <ok/needs_fix/no_work/dry_run_only/already_processed/refused>
gate = shared_folder_lookup_bridge_handoff
runtime_location = autocount_vm_bridge_worker
n8n_runtime_location = main_physical_pc
transport = private_host_vm_shared_folder
execution_mode = manual_shared_folder_lookup_handoff
lookup_mode = <mock/powershell>
powershell_lookup_enabled = <true/false>
approved_batch_size = <aggregate-count-only>
pending_rows_loaded_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
processed_or_archived_count = <aggregate-count-only>
failed_or_dead_letter_count = <aggregate-count-only>
duplicate_or_already_processed_count = <aggregate-count-only>
idempotency_markers_outside_share = true
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
n8n_result_mapping_run = false
workflow_activation = inactive
queue_api_used = false
tunnel_or_reverse_proxy_used = false
webhook_used = false
scheduler_enabled = false
windows_service_installed = false
public_inbound_to_ac2_host = false
final_write_automation = false
no_row_values_printed = true
```

Handoff pass evidence requires `status = ok`, `lookup_mode = powershell`, `powershell_lookup_enabled = true`, `pending_rows_loaded_count = 1`, `lookup_attempt_count = 1`, `lookup_success_count = 1`, `lookup_error_count = 0`, and `failed_or_dead_letter_count = 0` for the default one-row batch. `status = dry_run_only` means mock mode ran and is not pass evidence. `status = already_processed` proves idempotency only. `status = no_work` means the pending file was empty.

Do not paste pending rows, result rows, processed or failed markers, raw/encoded/decoded/normalized member values, names, emails, phone numbers, birthday values, `AC2_PROBE_*` values, credentials, share paths/hostnames/accounts, Sheet IDs/URLs, command transcripts, stdout/stderr transcripts, screenshots, secrets, or PII.

## Manual UAT Procedure

Every live step below requires explicit current-turn operator approval naming the target and operation before it runs. Nothing in this runbook pre-approves them, and none of them were executed for this PR.

1. Create the private share between the physical host and the VM with least-privilege permissions, plus the `inbox` and `outbox` directories.
2. On the main physical PC, manually execute the inactive Gate 4A workflow (Manual Trigger) per its runbook to stage exactly one sanitized `PENDING_LOOKUP` row, then run the Gate 4A aggregate precheck.
3. Copy the staged pending file from the n8n container into `<share-root>\inbox\` without opening or printing it.
4. Inside the VM, run the exact operator command above and record only the aggregate evidence shape.
5. Copy `<share-root>\outbox\member_lookup_bridge_gate5a_result_copy.jsonl` into the approved n8n container file area, then run the Gate 5A result precheck.
6. Manually execute the inactive Gate 5A workflow (Manual Trigger) per its runbook to map the sanitized result, and record its evidence.
7. Apply the gate cleanup and evidence-retention rules to the share contents; the pending and result files are sensitive operational data and must not persist beyond retention.

## Out Of Scope

- AutoCount member create, update, or delete in any form; `READY_FOR_CREATE_REVIEW` is review-only.
- AutoCount writes and direct SQL of any kind.
- Workflow import, activation, publication, scheduling, or any non-manual trigger; both reused workflows stay Manual Trigger only and inactive.
- Docker Compose changes, including bind-mounting the n8n file area onto the share.
- Queue API, Cloudflare Tunnel / reverse proxy, webhooks, Windows services, schedulers, hosted/VPS runtimes, and any public inbound path.
- Live end-to-end execution during implementation; the manual UAT procedure runs only under separate explicit approvals.
- Real credentials, share paths, hostnames, or secrets in repo files or pasted evidence.
