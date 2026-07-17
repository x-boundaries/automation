# Member Intake Shared-Folder Lookup Bridge Runbook

Status: shared-folder handoff design and VM-side manual runner only. This documents the private host-to-VM shared-folder handoff topology for the member lookup bridge and `scripts/member_lookup_shared_folder_handoff.py` with local tests. No live end-to-end execution was performed for this work. No n8n workflow was imported, executed, activated, or modified. No AutoCount member create, update, or delete path exists here, and no direct SQL is used. Both reused n8n workflows remain Manual Trigger only and inactive.

## Purpose

The current deployment runs n8n (Docker) on the main physical PC and AutoCount Accounting 2.x inside a Windows VM on that same physical PC. Because both runtimes share one physical machine, the n8n-to-bridge handoff does not need a tunnel, queue API, webhook, or any network service for UAT. A private host-to-VM shared folder is the entire transport.

This runbook reuses the existing contracts and machinery unchanged:

- Request contract and pending-queue file: Gate 4A, per [member_intake_n8n_node_contract.md](member_intake_n8n_node_contract.md) and [member_intake_n8n_gate4a_manual_queue_handoff_runbook.md](member_intake_n8n_gate4a_manual_queue_handoff_runbook.md).
- Sanitized result contract and result-copy file: Gate 5A, per [member_intake_n8n_gate5a_manual_result_mapping_runbook.md](member_intake_n8n_gate5a_manual_result_mapping_runbook.md).
- Validation, lookup, and state machine: the proven Gate 4 real-queue runner `scripts/member_lookup_gate4_real_queue_lookup.py`, per [member_intake_local_lookup_bridge_runbook.md](member_intake_local_lookup_bridge_runbook.md). The shared-folder runner delegates to it in-process and adds no lookup or validation logic of its own.

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
    -> acquires the VM-local exclusive execution claim (atomic, outside the share)
    -> delegates to scripts/member_lookup_gate4_real_queue_lookup.py:
       exact Gate 4A single-row precheck, canonical decoded numeric contract,
       canonical payload-hash/job-identity validation, canonical retry metadata,
       strict marker/result inspection, read-only PowerShell lookup, durable
       result-before-marker persistence -- all against VM-local staging paths
    -> atomically publishes the validated one-row staging result to
       <share-root>\outbox\member_lookup_bridge_gate5a_result_copy.jsonl
    -> releases the claim only after a clean terminal state

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
- The share holds only the contract files below (plus, transiently, the atomic publication temp file). It must not expose the AutoCount installation, AC2 runtime state, SQL Server data, credentials, `.env` values, `.n8n/` runtime state, or this repository.
- The pending-queue file contains `submitted_member_no_base64_utf8`, which is sensitive operational data. Do not open, print, paste, or commit it; delete it per the gate evidence-retention rules after the handoff closes.
- No tunnel, reverse proxy, queue API, webhook, scheduler, Windows service, or public inbound path to the VM is created, used, or approved by this topology.
- Atomic publication relies on same-directory atomic no-replace move semantics in the outbox. The supported expectation is an NTFS-backed hypervisor shared folder or SMB share, where a same-directory rename is atomic and fails when the destination already exists (the runner deliberately does not use replace-existing semantics). On POSIX-style filesystems the runner uses an atomic hard-link create-new instead. If the selected share cannot honour these semantics, the runner fails closed on the publication error and never leaves a partial final file; do not use such a share.

## Share Layout And VM-Local State

```text
<share-root>\inbox\member_lookup_bridge_gate4a_pending_queue.jsonl
<share-root>\outbox\member_lookup_bridge_gate5a_result_copy.jsonl
<share-root>\outbox\member_lookup_bridge_gate5a_result_copy.jsonl.tmp   (transient, atomic publication only)
```

The `inbox` and `outbox` directories must exist before the worker runs. The filenames are fixed by the reused Gate 4A and Gate 5A contracts and must not be renamed.

All VM-local state lives outside the share and ignored by Git. The runner refuses to run if any of these paths is inside the share root (or the share root is inside one of them):

```powershell
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$claim = "$root\member_lookup_bridge_shared_folder_claim.json"
$staging = "$root\member_lookup_bridge_shared_folder_staging_results.jsonl"
$processed = "$root\member_lookup_bridge_shared_folder_processed"
$failed = "$root\member_lookup_bridge_shared_folder_failed"
```

## Exactly One Row

This handoff feeds the single fixed Gate 5A result-copy file, and the downstream Gate 5A precheck accepts exactly one result row. The batch size is therefore hard-fixed at exactly one row and is not configurable. The delegated Gate 4A precheck rejects a pending file with zero rows, more than one row, blank rows, or any schema deviation before any lookup. Multi-row splitting is out of scope.

## Canonical Gate 4A Validation

The runner delegates the entire pre-lookup validation to the Gate 4 real-queue runner. Before any PowerShell lookup can occur, the pending row must pass exactly the same checks as Gate 4:

- exactly one nonblank queue row with the exact Gate 4A schema;
- `state = PENDING_LOOKUP` and `pdpa_status = yes`;
- non-dummy/non-rehearsal decoded value;
- valid base64 whose decoded value matches `^[0-9]{6,20}$` (decoded only in memory, never printed);
- canonical FNV-1a `payload_hash` over the canonical field order;
- canonical `job_id = gate4a_<payload_hash>`;
- canonical `source_row_ref = row_<row_number>` and `intake_id = gate4a_row_<row_number>`;
- `attempt = 0`, `max_attempts = 1`, and `consent_status = marketing_consent_not_queued` exactly.

An arbitrary file that merely satisfies the generic bridge request contract can never reach PowerShell; every deviation is `needs_fix` with zero lookups.

## Exclusive VM-Local Claim

Before any marker/result inspection and before any lookup, the runner atomically creates the VM-local exclusive execution claim file (`O_CREAT | O_EXCL`). Exactly one process can win; a concurrent second invocation fails closed with `needs_fix`, `preexisting_claim_detected = true`, and zero lookups. The claim is held through staging, publication, and marker completion, and is removed only when the run reaches a clean deterministic terminal state (including clean rejections and idempotent reruns).

A claim left behind by an interrupted run blocks every subsequent run with `needs_fix` and is never removed, inspected, repaired, or retried automatically. This is not a distributed lock; it protects a single VM host only.

## VM-Local Staging And Atomic Publication

The lookup never writes into the share directly. The delegated Gate 4 runner writes its durable sanitized result and idempotency markers only to the VM-local staging paths. After Gate 4 reports a fully validated fresh success, the runner:

1. re-verifies that staging holds exactly one complete sanitized result row;
2. refuses if the final outbox file already exists nonempty (see below);
3. copies the staging content to `member_lookup_bridge_gate5a_result_copy.jsonl.tmp` inside the outbox, flushes and closes it;
4. moves the temp file to the fixed Gate 5A filename with an atomic no-replace primitive that fails when the destination already exists (Windows same-directory rename without replace-existing; POSIX hard-link create-new).

The final outbox filename is never visible with partial content, is never appended to, and is never overwritten. The VM-local claim alone cannot stop the other share participant from creating the fixed filename during the lookup, so the publication primitive itself refuses an existing destination: a file that appears in the outbox after the pre-lookup inspection stays byte-for-byte unchanged, the temp file is left for operator diagnosis, and the run ends `needs_fix`. A nonempty final outbox file is acceptable only when it is the exact idempotent copy of the validated staged result for the current job; any stale, malformed, truncated, late-written, or other-job content is `needs_fix` and the file is left untouched.

## Incomplete-State Handling And Operator Recovery

The runner treats every incomplete or inconsistent state as `needs_fix` with zero lookups. It never deletes, repairs, or regenerates questionable evidence, and it never relaunches a lookup automatically. In particular:

| Observed state | Outcome |
| --- | --- |
| Preexisting claim file (interrupted or concurrent run) | `needs_fix`, zero lookups |
| Processed marker without a staged result | `needs_fix` |
| Staged result without a processed marker | `needs_fix` |
| Failed/dead-letter marker present in any combination | `needs_fix` |
| Malformed, duplicate, or unrelated marker artifact | `needs_fix` |
| Corrupted or multi-row staging result | `needs_fix` |
| Staging/marker pair complete but outbox missing or empty (interrupted publication) | `needs_fix` |
| Outbox nonempty with no staged evidence, or not the exact staged row | `needs_fix`, outbox untouched |
| Final outbox filename created by another share participant during the lookup | `needs_fix`, destination byte-for-byte unchanged, temp file retained for diagnosis |
| Exclusive claim cannot be released after a completed run | `needs_fix` (never `ok`/`already_processed`), `claim_release_failed = true`, claim retained for operator recovery |

`already_processed` (exit 0, zero lookups) is reported only when the delegated Gate 4 state machine confirms exactly one clean processed marker plus exactly one fully valid matching staged result for the current job, and the final outbox file contains exactly that same single row.

Operator recovery for a stale claim or an incomplete marker/result state is manual and review-first:

1. Confirm no other handoff process is running on the VM.
2. Record the aggregate evidence of the blocked run.
3. Review the VM-local claim, staging, and marker artifacts without pasting their content anywhere.
4. Only after review, remove the stale claim file by hand. Do not delete or edit staging results or markers to force a rerun; an incomplete staging/marker/publication state remains `needs_fix` by design and requires its own reviewed recovery decision, consistent with the Gate 4 recovery discipline.
5. Rerun the handoff. A completed-but-unpublished or otherwise inconsistent state will still report `needs_fix` and will not run another lookup.

## Exact Operator Command

Run only inside the AutoCount VM, from the repository root, after setting the four `AC2_PROBE_*` values as runtime-only process environment values (never in repo files, machine/user persistence, command arguments, or pasted evidence):

```powershell
$share = 'X:\xb_member_lookup_handoff'
$root = 'C:\XB\autocount_outputs\review\member_lookup_bridge'
$claim = "$root\member_lookup_bridge_shared_folder_claim.json"
$staging = "$root\member_lookup_bridge_shared_folder_staging_results.jsonl"
$processed = "$root\member_lookup_bridge_shared_folder_processed"
$failed = "$root\member_lookup_bridge_shared_folder_failed"

python scripts\member_lookup_shared_folder_handoff.py `
  --enable-shared-folder-handoff-review `
  --enable-powershell-lookup `
  --share-root "$share" `
  --claim-json "$claim" `
  --staging-results-jsonl "$staging" `
  --processed-dir "$processed" `
  --failed-dir "$failed"
```

`$share` is an example VM-visible path; use the actual private share path. Do not paste the real share UNC path, hostnames, account names, or any credential into evidence. Like Gate 4, this runner is PowerShell-only: there is no mock route, and only a genuine read-only PowerShell lookup can produce `status = ok`. Automated tests exercise the runner with a local fake lookup executable; that fixture is tests-only and is not an operator mode.

## Expected Aggregate Evidence Shape

The operator may paste back only this sanitized aggregate shape:

```text
status = <ok/needs_fix/already_processed/refused>
gate = shared_folder_lookup_bridge_handoff
runtime_location = autocount_vm_bridge_worker
n8n_runtime_location = main_physical_pc
transport = private_host_vm_shared_folder
execution_mode = manual_shared_folder_lookup_handoff
lookup_mode = powershell
powershell_lookup_enabled = <true/false>
ac2_lookup_invoked = <true/false>
approved_batch_size = 1
queue_rows_read_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_existing_member_review_count = <aggregate-count-only>
lookup_manual_review_count = <aggregate-count-only>
lookup_ready_for_create_review_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
review_rows_written_count = <aggregate-count-only>
claim_acquired = <true/false>
preexisting_claim_detected = <true/false>
claim_release_failed = <true/false>
outbox_published = <true/false>
vm_local_state_outside_share = true
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

Handoff pass evidence requires `status = ok`, `powershell_lookup_enabled = true`, `ac2_lookup_invoked = true`, `queue_rows_read_count = 1`, `lookup_attempt_count = 1`, `lookup_success_count = 1`, exactly one of the three review routing counts equal to `1`, `lookup_error_count = 0`, `review_rows_written_count = 1`, `claim_acquired = true`, `claim_release_failed = false`, and `outbox_published = true`. `status = already_processed` proves idempotency only. `READY_FOR_CREATE_REVIEW` remains review-only and is not approval to create.

Do not paste pending rows, staged or published result rows, processed or failed markers, claim file content, raw/encoded/decoded/normalized member values, names, emails, phone numbers, birthday values, `AC2_PROBE_*` values, credentials, share paths/hostnames/accounts, Sheet IDs/URLs, command transcripts, stdout/stderr transcripts, screenshots, secrets, or PII.

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
- Multi-row batches, result splitting, and per-row mapping.
- Automatic deletion, repair, or regeneration of claims, markers, staging results, or outbox evidence.
- Live end-to-end execution during implementation; the manual UAT procedure runs only under separate explicit approvals.
- Real credentials, share paths, hostnames, or secrets in repo files or pasted evidence.
