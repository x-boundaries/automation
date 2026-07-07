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
- `scripts/ac2_member_browse_extract_review.ps1`: explicit opt-in read-only local member browse extract for migration reconciliation review; output contains PII and must not be committed.
- `scripts/ac2_member_lookup_review.ps1`: explicit opt-in read-only local member lookup for future duplicate checking; the Windows AC2 lookup bridge should pass form values with `MemberNoBase64Utf8`, AC2 is source of truth, Google Form mobile/member number maps to AutoCount `MemberNo`, AutoCount `MobilePhone` is intentionally unused, and it does not create/update/delete members.
- `scripts/ac2_member_lookup_bridge_worker.py`: disabled-by-default local polling worker skeleton for review-only AC2 member lookup bridge design; fixture/mock mode models the first Google Sheets UAT queue in fixture-only mode with source-agnostic fields for a future custom intake surface, without real Google API calls, PowerShell lookup mode is separately opt-in, and it never writes to AutoCount.
- `scripts/member_intake_validate.py`: dry-run-only validator/normalizer for Google Form member intake CSV rows, with optional matching against a private local AC2 member extract; it never creates, updates, or deletes AutoCount members, and its row-level outputs contain PII and must stay local.
- `scripts/member_intake_decision_review.py`: dry-run-only decision review layer that combines validated Google Form rows with sanitized AC2 lookup JSONL or planned lookup review; AC2 remains source of truth, it does not create/update/delete members, and row-level outputs contain only row numbers, status codes, and issue codes.
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
- Member lookup bridge worker fixture outputs: `C:\XB\autocount_outputs\review\member_lookup_bridge`
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
- [AC2 member browse extract review runbook](docs/autocount2-automation/member_browse_extract_review_runbook.md)
- [AC2 member lookup review runbook](docs/autocount2-automation/member_lookup_review_runbook.md)
- [Member intake local lookup bridge runbook](docs/autocount2-automation/member_intake_local_lookup_bridge_runbook.md)
- [Member intake n8n direct lookup runbook](docs/autocount2-automation/member_intake_n8n_direct_lookup_runbook.md)
- [Member intake n8n dry-run workflow](docs/autocount2-automation/member_intake_n8n_dry_run_workflow.md)
- [Member intake n8n UAT setup runbook](docs/autocount2-automation/member_intake_n8n_uat_setup_runbook.md)
- [Member intake n8n Gate 2 dummy rehearsal runbook](docs/autocount2-automation/member_intake_n8n_gate2_dummy_rehearsal_runbook.md)
- [Member intake n8n lookup bridge UAT plan](docs/autocount2-automation/member_intake_n8n_lookup_bridge_uat_plan.md)
- [Member intake n8n node contract](docs/autocount2-automation/member_intake_n8n_node_contract.md)
- [Member form intake contract (dry-run validator)](docs/autocount2-automation/member_form_intake_contract.md)
- [Member intake decision review runbook](docs/autocount2-automation/member_intake_decision_review_runbook.md)
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
