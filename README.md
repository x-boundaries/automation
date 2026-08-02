# X-Boundaries AutoCount 2 Automation

This repository is for AutoCount 2 automation work only: local extraction scripts, SQL/API investigation notes, configuration templates, and runbooks for safe read-only automation around the AutoCount 2 environment.

Task tracking, pending-work dashboards, completed-task logs, and personal planning notes are intentionally kept outside this repo.

## Current Automation Surfaces

- `scripts/autocount_sql_probe.py`: read-only SQL metadata probe for discovering AutoCount database objects, columns, role memberships, and risky permissions.
- `scripts/autocount_phase1_reconcile.py`: safe aggregate Phase 1 stock reconciliation summaries for validating candidate SQL surfaces against AutoCount UI/report outputs before scheduling extraction.
- `scripts/autocount_broader_surface_discovery.py`: metadata-only broader AC2 surface discovery for customer, supplier, GL/accounting, AR/AP, locations, payment, PO, and stock-in-transit planning.
- `scripts/autocount_inventory_operation_discovery.py`: metadata-only inventory-operation surface discovery for GRN, stock transfer, location, richer item attributes, movement semantics, supplier context, and outstanding PO support.
- `scripts/ac2_member_api_probe.ps1`: local-only metadata/reflection probe for the installed AutoCount 2.2 member API surface.
- `scripts/ac2_bootstrap_api_probe.ps1`: local-only metadata/reflection probe for AutoCount bootstrap/session types needed before any member command factory use.
- `scripts/ac2_session_auth_probe.ps1`: explicit opt-in local authentication probe for DBSetting/UserSession only, with no member reads or writes.
- `scripts/ac2_member_type_browse_probe.ps1`: explicit opt-in read-only member type browse probe for confirmed member type values.
- `scripts/ac2_member_no_save_schema_probe.ps1`: explicit opt-in local MemberCommand no-save schema probe for an in-memory MemberEntity only.
- `scripts/ac2_member_no_save_assignment_probe.ps1`: explicit opt-in local MemberCommand fake-data no-save assignment probe for an in-memory MemberEntity row only.
- `scripts/ac2_member_fake_create_probe.ps1`: explicitly gated local write probe that creates exactly one synthetic fake AutoCount member only after all write confirmations are supplied.
- `scripts/ac2_member_expiry_capability_probe.ps1` (with pure helper library `scripts/member_expiry_capability_probe_lib.ps1`): explicitly gated, inactive-by-default synthetic capability probe that creates exactly one synthetic member with `ExpiryDate=2028-06-30`, verifies the value after a `GetMember` read-back, and never updates, deletes, rolls back, or cleans up. Its permanent single-use attempt claim and its authoritative durable result both live in the fixed canonical machine-local state root `C:\XB\create_uat\expiry_probe_state`, which is fixed in reviewed code and not operator-selectable; the root must already exist and is validated before any AutoCount contact, and the probe never creates, repairs, redirects, migrates or cleans it. Before the irreversible save it atomically creates the claim there exclusively and durably (no-clobber, never overwritten or deleted, so crashes and concurrent launches fail closed), which is the concurrency and no-retry boundary. The result is durably written to a no-clobber `*.incomplete` staging artefact and then published by an atomic no-replace move, with authority bound to the exact final path, so a failed publication is nonzero and leaves only a non-authoritative staging artefact. Evidence is bound to the run via `operation_id`, `-ApprovalReference`, and a non-secret target fingerprint; terminal outcomes are honest (a confirmed save with a failed read-back is `WRITE_CONFIRMED_READBACK_FAILED`, never a pre-write failure), and it returns exit `0` only for an authoritative `EXPIRY_VERIFIED`. It exists only to prove ExpiryDate persistence before the main creation UAT enables it; it requires current-turn owner approval naming the AutoCount target and one synthetic record, and any uncertain save is terminal and never retried. See [Synthetic ExpiryDate capability probe runbook](docs/autocount2-automation/member_expiry_capability_probe_runbook.md).
- `scripts/ac2_member_browse_extract_review.ps1`: explicit opt-in read-only local member browse extract for migration reconciliation review; output contains PII and must not be committed.
- `scripts/ac2_member_lookup_review.ps1`: explicit opt-in read-only local member lookup for future duplicate checking; the Windows AC2 lookup bridge should pass form values with `MemberNoBase64Utf8`, AC2 is source of truth, Google Form mobile/member number maps to AutoCount `MemberNo`, AutoCount `MobilePhone` is intentionally unused, and it does not create/update/delete members.
- `scripts/ac2_member_lookup_bridge_worker.py`: disabled-by-default local polling worker skeleton for review-only AC2 member lookup bridge design; fixture/mock mode models the first Google Sheets UAT queue in fixture-only mode with source-agnostic fields for a future custom intake surface, without real Google API calls, PowerShell lookup mode is separately opt-in, and it never writes to AutoCount.
- `scripts/member_lookup_gate3b_prepare_local_queue.py`: local-only Gate 3B helper that writes exactly one ignored `PENDING_LOOKUP` queue JSONL row from an operator-supplied test member/member-number value at runtime and prints only aggregate queue-prep metadata.
- `scripts/member_lookup_gate3b_evidence_summary.py`: local aggregate-only Gate 3B evidence summarizer for sanitized AC2 bridge result JSONL; it proves AC2-side local bridge readiness only and does not require n8n, Google Sheets, hosted services, scheduler, webhook, or result mapping.
- `scripts/member_lookup_gate4a_queue_precheck.py`: local aggregate-only Gate 4A pre-bridge queue checker for exactly one sanitized non-dummy `PENDING_LOOKUP` queue JSONL row; it decodes lookup values only in memory and prints only aggregate counters before the operator stops for explicit bridge-handoff approval.
- `scripts/member_lookup_gate3c_local_bridge_runtime.py`: local-only Gate 3C runtime harness for repeated AC2 bridge-host runs against ignored pending/results/processed/failed filesystem paths, with aggregate-only evidence, idempotency markers, read-only lookup mode, and no n8n, Google Sheets, hosted service, scheduler, queue API, AutoCount write, or direct SQL write path.
- `scripts/member_lookup_gate3d_prepare_small_batch.py`: local-only Gate 3D helper that writes ignored duplicate-seed or mixed-batch pending JSONL rows from an operator-supplied synthetic value and prints only aggregate queue-prep metadata.
- `scripts/member_lookup_gate3d_local_bridge_small_batch.py`: local-only Gate 3D small-batch wrapper around the Gate 3C runtime harness, proving fresh lookup, duplicate idempotency, and expected malformed-job dead-letter routing with aggregate-only evidence and no n8n, Google Sheets, hosted service, scheduler, queue API, AutoCount write, or direct SQL write path.
- `scripts/member_lookup_gate3e_service_readiness.py`: local-only Gate 3E service-readiness discipline wrapper around the Gate 3C runtime harness, proving explicit opt-in, single-instance lock, operator stop switch, and bounded non-daemonized cycles with aggregate-only evidence, and no n8n, Google Sheets, hosted service, scheduler, Windows service installation, queue API, AutoCount write, or direct SQL write path.
- `scripts/member_lookup_gate4a_evidence_summary.py`: local aggregate-only Gate 4A evidence summarizer for sanitized bridge result JSONL; it prints counts and fixed false write/activation flags only, never row-level data.
- `scripts/member_lookup_gate4_real_queue_lookup.py`: local-only Gate 4 lookup-only handoff wrapper that takes the single approved Gate 4A `PENDING_LOOKUP` queue row placed on the Windows AC2 bridge host, reuses the Gate 4A precheck for exact one-row/dummy validation and the Gate 3C runtime harness for the read-only PowerShell lookup and idempotency markers, and prints aggregate-only Gate 4 evidence; it never decodes/echoes the member value, never maps results to n8n, and has no AutoCount write, direct SQL, scheduler, service, webhook, or tunnel path.
- `scripts/member_lookup_gate4_failed_attempt_recovery.py`: local-only fail-closed Gate 4 failed-attempt recovery wrapper that authorizes exactly one explicitly approved read-only retry of the failed Gate 4 lookup; it strictly validates the complete original failed attempt against the exact diagnosed failure signature (originals preserved byte-for-byte and never modified), requires the four `AC2_PROBE_*` runtime values plus a passing authentication-only preflight via `scripts/ac2_session_auth_probe.ps1` (never with root login), enforces at-most-one-lookup mechanically with a permanent atomic exclusive `recovery1` attempt claim (concurrency- and crash-safe, never auto-reset), writes only to isolated ignored `recovery1` claim/result/marker paths, blocks any second recovery generation, and prints aggregate-only evidence with no AutoCount write, direct SQL, n8n mapping, scheduler, service, webhook, or tunnel path.
- `scripts/member_lookup_shared_folder_handoff.py`: VM-side fail-closed manual runner for the private host-to-VM shared-folder lookup handoff; it reads the exactly-one-row Gate 4A pending-queue file from the share inbox, delegates the full canonical validation, read-only PowerShell lookup, and marker/result state machine in-process to the proven Gate 4 real-queue runner against VM-local staging paths, guards execution with an atomic VM-local exclusive claim, atomically publishes the validated sanitized Gate 5A result copy to the share outbox (never appending to or overwriting a nonempty final file), and has no mock route, AutoCount write, direct SQL, member create/update/delete, queue API, tunnel, webhook, scheduler, or service path.
- `scripts/member_lookup_gate5a_result_precheck.py`: local aggregate-only Gate 5A precheck for the operator-copied sanitized Gate 4 recovery result staging file; it accepts exactly one durable result row matching the merged Gate 4 contract, fails closed on missing/extra/forbidden fields, invalid types, or an inconsistent recomputed review state, opens the copy read-only, and prints only aggregate counters and fixed false write/activation flags before the manual inactive n8n mapping run.
- `scripts/member_intake_validate.py`: dry-run-only validator/normalizer for Google Form member intake CSV rows, with optional matching against a private local AC2 member extract; it never creates, updates, or deletes AutoCount members, and its row-level outputs contain PII and must stay local.
- `scripts/member_intake_decision_review.py`: dry-run-only decision review layer that combines validated Google Form rows with sanitized AC2 lookup JSONL or planned lookup review; AC2 remains source of truth, it does not create/update/delete members, and row-level outputs contain only row numbers, status codes, and issue codes.
- `scripts/member_create_uat_approval.py` and `scripts/ac2_member_create_uat_runner.ps1`: the bounded single-member creation UAT path. The laptop-side approval tool records a controlled reviewer decision for one `READY_FOR_CREATE_REVIEW` row and builds an immutable, single-use create package (validated against `schemas/member_create_uat_package.schema.json`); the AutoCount VM runner validates that package in-process, performs an existing-member duplicate check, assigns only whitelisted fields, reads every assigned field back, and gates a single member-save call behind five explicit confirmation switches plus the fail-closed business confirmations in `config/member_create_uat_business_confirmation.json`. `ExpiryDate=2028-06-30` is now an active assignable field, proven persistent by the synthetic capability probe (durable result SHA-256 `48CC0185EFF59C3A21AC087BC0C120A00B70801599C3F5513675F7950CD1541B`); all four business values are explicitly confirmed. Because the payload shape changed, the package schema is now `member_create_uat_package/v2` and any package built under the previous `v1` contract is refused fail-closed, so a new reviewer decision and a fresh package are required. It is UAT scaffolding, not the production workflow, supports exactly one member, performs no live write here, and performs no member update, deletion, or rollback; a real write still requires the separate explicit operator step. See [Single-member creation UAT runbook](docs/autocount2-automation/member_create_uat_runbook.md).
- `scripts/autocount_stock_extract.py`: stock extraction/archive workflow for AutoCount stock master, stock balance, and stock movement datasets.
- `scripts/install_autocount_stock_extract_task.ps1`: Windows Task Scheduler installer for the stock extraction job.

## Configuration Templates

- `config/autocount_sql_probe.example.json`
- `config/autocount_readonly_login_validate.example.json`
- `config/autocount_phase1_reconcile.example.json`
- `config/autocount_broader_surface_discovery.example.json`
- `config/autocount_inventory_operation_discovery.example.json`
- `config/autocount_stock_extract.ac2_smoke.example.json`
- `config/autocount_stock_extract.example.json`
- `config/autocount_stock_extract.from_probe.example.json`

Copy example files to local ignored config paths before use. Do not commit credentials, connection strings, production exports, or real operational data.

## Local Output Convention

Keep all generated AutoCount outputs under `C:\XB\autocount_outputs`:

- SQL probe: `C:\XB\autocount_outputs\probe`
- Broader surface discovery: `C:\XB\autocount_outputs\probe\broader_surfaces`
- Inventory operation discovery: `C:\XB\autocount_outputs\probe\inventory_operations`
- Member intake API discovery review: `C:\XB\autocount_outputs\review\member_intake_discovery`
- Member browse extract review: `C:\XB\autocount_outputs\review\member_browse_extract`
- Member lookup review: sanitized console JSON only; no local member data output file is required. The Windows AC2 lookup bridge may parse this JSON for duplicate-check routing, but it must not be used as final write automation.
- Member lookup bridge worker fixture, Gate 3B local bridge readiness, Gate 3C local runtime hardening, Gate 3D local small-batch proof, Gate 3E service-readiness discipline, and Gate 4A manual handoff outputs: `C:\XB\autocount_outputs\review\member_lookup_bridge`
- Gate 5A sanitized recovery-result staging copy (operator PC): `member_lookup_bridge_gate5a_result_copy.jsonl` under the local ignored review root; the original recovery result on the AutoCount host stays unchanged
- Shared-folder handoff VM-local state: `member_lookup_bridge_shared_folder_claim.json`, `member_lookup_bridge_shared_folder_staging_results.jsonl`, `member_lookup_bridge_shared_folder_processed`, and `member_lookup_bridge_shared_folder_failed` under `C:\XB\autocount_outputs\review\member_lookup_bridge`; the private share itself holds only the contract files and stays outside the repository
- Member intake dry-run validation: `C:\XB\autocount_outputs\review\member_intake_validation`
- Member intake dry-run decision review: `C:\XB\autocount_outputs\review\member_intake_decision`
- Phase 1 reconciliation: `C:\XB\autocount_outputs\reconcile`
- Stock extraction: `C:\XB\autocount_outputs\extract\stock`

Raw CSVs and member browse extracts stay local and must not be committed. Member browse output contains PII and personal data; paste back only reviewed sanitized status fields or safe summaries. Decision-review outputs are designed to be PII-free, but they are still local review artifacts and must not be used as final write automation.

## Runbooks And Design Notes

- [SQL Server local probe runbook](docs/autocount_sql_probe.md)
- [AutoCount stock extraction runbook](docs/autocount_stock_extraction.md)
- [AutoCount 2 automation architecture](docs/autocount2-automation/architecture.md)
- [AutoCount 2 API research](docs/autocount2-automation/api_research.md)
- [Inventory intelligence scope](docs/autocount2-automation/inventory_intelligence_scope.md)
- [Inventory operation surface discovery runbook](docs/autocount2-automation/inventory_operation_surface_discovery_runbook.md)
- [Member intake API research](docs/autocount2-automation/member_intake_api_research.md)
- [Member intake field mapping](docs/autocount2-automation/member_intake_field_mapping.md)
- [Member intake local probe runbook](docs/autocount2-automation/member_intake_local_probe_runbook.md)
- [Member intake bootstrap probe runbook](docs/autocount2-automation/member_intake_bootstrap_probe_runbook.md)
- [Member intake session auth probe runbook](docs/autocount2-automation/member_intake_session_auth_probe_runbook.md)
- [Member type browse probe runbook](docs/autocount2-automation/member_type_browse_probe_runbook.md)
- [AC2 member no-save schema probe runbook](docs/autocount2-automation/member_no_save_schema_probe_runbook.md)
- [AC2 member no-save assignment probe runbook](docs/autocount2-automation/member_no_save_assignment_probe_runbook.md)
- [AC2 member fake create probe runbook](docs/autocount2-automation/member_fake_create_probe_runbook.md)
- [Synthetic ExpiryDate capability probe runbook](docs/autocount2-automation/member_expiry_capability_probe_runbook.md)
- [AC2 member browse extract review runbook](docs/autocount2-automation/member_browse_extract_review_runbook.md)
- [AC2 member lookup review runbook](docs/autocount2-automation/member_lookup_review_runbook.md)
- [Member intake local lookup bridge runbook](docs/autocount2-automation/member_intake_local_lookup_bridge_runbook.md)
- [Member intake shared-folder lookup bridge runbook](docs/autocount2-automation/member_intake_shared_folder_lookup_bridge_runbook.md)
- [Member intake n8n direct lookup runbook](docs/autocount2-automation/member_intake_n8n_direct_lookup_runbook.md)
- [Member intake n8n dry-run workflow](docs/autocount2-automation/member_intake_n8n_dry_run_workflow.md)
- [Member intake n8n UAT setup runbook](docs/autocount2-automation/member_intake_n8n_uat_setup_runbook.md)
- [Member intake n8n Gate 2 dummy rehearsal runbook](docs/autocount2-automation/member_intake_n8n_gate2_dummy_rehearsal_runbook.md)
- [Member intake n8n lookup bridge UAT plan](docs/autocount2-automation/member_intake_n8n_lookup_bridge_uat_plan.md)
- [Member intake n8n Gate 4A manual queue handoff runbook](docs/autocount2-automation/member_intake_n8n_gate4a_manual_queue_handoff_runbook.md)
- [Member intake n8n Gate 4A container queue-write workflow](n8n-workflows/member_intake_gate4a_container_queue_write.workflow.json)
- [Member intake n8n Gate 5A manual result-mapping runbook](docs/autocount2-automation/member_intake_n8n_gate5a_manual_result_mapping_runbook.md)
- [Member intake n8n Gate 5A sanitized result-mapping workflow](n8n-workflows/member_intake_gate5a_sanitized_result_mapping.workflow.json)
- [Member intake n8n node contract](docs/autocount2-automation/member_intake_n8n_node_contract.md)
- [Member form intake contract (dry-run validator)](docs/autocount2-automation/member_form_intake_contract.md)
- [Member intake decision review runbook](docs/autocount2-automation/member_intake_decision_review_runbook.md)
- [Single-member creation UAT runbook](docs/autocount2-automation/member_create_uat_runbook.md)
- [AutoCount 2 MVP plan](docs/autocount2-automation/mvp_plan.md)
- [Extraction surface decision pack](docs/autocount2-automation/extraction_surface_decision.md)
- [AC2 read-only SQL login validation runbook](docs/autocount2-automation/readonly_sql_login_runbook.md)
- [Broader AC2 surface discovery runbook](docs/autocount2-automation/broader_surface_discovery_runbook.md)
- [Phase 1 extraction mapping](docs/autocount2-automation/phase1_extraction_mapping.md)
- [Phase 1 AC2 stock extraction smoke runbook](docs/autocount2-automation/phase1_stock_extract_smoke_runbook.md)
- [Phase 1 reconciliation runbook](docs/autocount2-automation/phase1_reconciliation_runbook.md)
- [Stock reconciliation checklist](docs/autocount2-automation/reconciliation_checklist.md)
- [Security guardrails](docs/autocount2-automation/security_guardrails.md)
- [Data policy](docs/data_policy.md)

## AutoCount Template References

The `migration/` folder contains vendor AutoCount Excel import templates. These are kept as AutoCount reference artifacts, not as migration planning notes or task tracking.

## Data Policy Warning

Do not commit real company operational data to this repository.

This repository must not store:

- Real stock master or product master files.
- Real customer or supplier lists.
- AP/AR records or invoices.
- Bank or payment exports.
- AutoCount database backups.
- Passwords, API keys, or `.env` files.

Real data should stay in approved secure storage outside GitHub. AutoCount workflow outputs should go under `C:\XB\autocount_outputs` on the VM; generic local test outputs should go into ignored folders such as `data/`, `exports/`, `outputs/`, or `exceptions/`.
