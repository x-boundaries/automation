# X-Boundaries Automation

This page lists the automations that are running today, what is built but waiting for a live step, what is still being built, and what has been retired. Status as of **22 Nov 2026**. Day-to-day progress is tracked on [issue #117](https://github.com/x-boundaries/automation/issues/117) and its child issues.

**Keep this page current: every PR that ships or changes a live automation updates it.**

No runtime below uses an AI model. Every live change (installing, importing, switching on, scheduling) needs the owner's OK for that step, as each runbook says.

## Live automations

| Automation | Where it runs | When | Issue |
| --- | --- | --- | --- |
| [EnergyGrid bills](#energygrid-bills) | Server PC (Task Scheduler + local n8n) | Daily 11:30 SGT; log cleanup 19:30 SGT | #226, #257 |
| [AC2 stock balance, daily close and buying analysis](#ac2-stock-balance-daily-close-and-buying-analysis) | AutoCount 2 (AC2) server, Task Scheduler `\XB\` | Hourly at :00; close and analysis just after midnight | #245, #292 |
| [AC2 AR/AP aging](#ac2-arap-aging) | AC2 server, Task Scheduler `\XB\` | Hourly at :30 | #321 |
| [AC2 member sync](#ac2-member-sync-and-member-core) | AC2 server, Task Scheduler `\XB\` | Every 15 minutes; summary email 06:40 SGT | #155 |
| [Member core (S$300 upgrade), observe only](#ac2-member-sync-and-member-core) | AC2 server, Task Scheduler `\XB\` | Every 15 minutes | #268 |
| [Shopify kit re-tagger](#shopify-kit-re-tagger) | Server PC n8n | Daily 03:00 SGT | #269 |
| [Error alerts by email](#error-alerts-and-daily-health-check) | Server PC n8n (`XB Ops Alert Email`) | When a job reports an error | #288 |
| [Daily health check](#error-alerts-and-daily-health-check) | Server PC, Task Scheduler `\XB\` + n8n | Daily 07:30 SGT | #325 |
| [Drive mirror of `main`](#drive-mirror-of-main) | Server PC, Task Scheduler `\XB\` | Sunday 03:00 SGT | #273 |

### EnergyGrid bills

Gets every new EnergyGrid EB Bill and Tenant Bill into Google Drive, checked against Google's copy, and tells the owner when a new bill arrives or something fails. No AI model runs.

- **Where it runs:** one Windows PC with Task Scheduler (the user must be signed in), Google Drive for desktop, and a local n8n.
- **Daily bills (11:30 SGT):** downloads the newest bills, saves each PDF to Drive, checks Google's copy, and emails the owner. Runbook: [Claude-free daily run](energygrid-bill-downloader/docs/native_daily_claude_free.md).
- **Run monitor:** an email alert (through the shared XB Ops Alert Email workflow) if the daily run has not reported by 14:00 SGT or a bill is still pending. If the monitor itself fails, the failure is written to a small n8n error log (no failed runs are saved) and the daily health check reports it (#288, live 21 Nov 2026). Runbook: [run monitor](n8n-workflows/energygrid_local_failure_monitor/README.md).
- **Log cleanup (19:30 SGT):** keeps 90 days of clean run logs and archives every run that had a problem. Runbook: [run-log retention](energygrid-bill-downloader/docs/run_log_retention.md).

<details>
<summary>Claude setup prompt</summary>

```text
Set up EnergyGrid bills from the x-boundaries/automation repo on this Windows PC.

Read first: energygrid-bill-downloader/docs/native_daily_claude_free.md (sections 2, 6, 7, 9),
n8n-workflows/energygrid_local_failure_monitor/README.md, and
energygrid-bill-downloader/docs/run_log_retention.md (sections 1 to 6).

I will give you the private values when you ask: the EnergyGrid portal sign-in, the Google Drive
folder and its account, the owner email address, the n8n header token, and the ID of the live
XB Ops Alert Email workflow. Never write them into the repo, a commit, or a log.

Ask me separately before each live action: installing or copying files, changing ACLs, importing
into n8n, binding credentials, creating the Data Table, activating a workflow, daily-preflight,
any live or test run, registering a task, turning on -Apply, and enabling a task.

Before any live run, confirm no other EnergyGrid daily task or Claude routine is enabled on any
PC, so bills are never filed or emailed twice.

Do these steps in order:
1. As administrator, install one locked EG_HOME (for example C:\Program Files\EnergyGrid) as
   section 7 says: its own Python, a hash-locked venv, Playwright Chromium, and the monitor
   folder (egmonitor.cmd and energygrid_monitor_report.py). Only EG_HOME\data may be writable
   by the task user.
2. Write the private native.daily.json from
   energygrid-bill-downloader/config/energygrid.native_daily.example.json.
3. Import into the local n8n as inactive, then bind credentials:
   n8n-workflows/energygrid_drive_verify.workflow.json,
   n8n-workflows/energygrid_owner_email.workflow.json, and
   n8n-workflows/energygrid_local_failure_monitor.workflow.json (in the live copy only, set
   ENABLE_OWNER_ALERTS to true and the XB Ops Alert Email workflow ID, which must already be
   published). Create the email claim Data Table.
4. Prove the Drive Verify and Owner Email webhooks are not reachable from the internet
   (section 6), activate the three workflows, then prove it again.
5. Run daily-preflight as the signed-in task user.
6. Register energygrid-bill-downloader/task-scheduler/energygrid_daily_native.task.example.xml
   disabled for that user, read it back, run it once by hand, show me the result, then enable it.
7. Copy energygrid-bill-downloader/runtime/maintenance/energygrid_retention.py and render
   egretention.ps1.template from the same folder into EG_HOME\maintenance. Lock both, and
   create EG_HOME\data\retention with the ACLs in its section 6. Show the negative ACL proof,
   then one dry run (no -Apply) as the task user after 14:00 SGT.
8. Register energygrid-bill-downloader/task-scheduler/energygrid_retention.task.example.xml,
   set the trigger to 19:30 SGT (the file ships 19:00), read it back, and only then add -Apply
   and enable it.

Never re-run a bill download or email just to test the monitor. After each step, tell me what
you checked and what changed.
```

</details>

### AC2 stock balance, daily close and buying analysis

Publishes AutoCount 2 stock to Google Drive for the owner's dashboard and for staff (and their AI agents) to read. Read-only: nothing writes to AutoCount. Units only, no prices or costs.

- **Where it runs:** the AC2 server, as a Task Scheduler task in `\XB\`, writing into a Drive for desktop folder.
- **Hourly (:00):** checks AutoCount's stored stock balance against the stock ledger, then replaces one verified latest set in Drive: the stock balance CSV, an item index and a dashboard feed. If the two disagree, nothing new is published that hour.
- **Just after midnight:** the first run of the day also writes the previous day's stock close (kept from 1 Aug 2026) and rebuilds the nightly buying analysis files (sales units by item, outlet, day and channel; stock-outs; placement; size mix) and the purchasing file (open PO units, goods received, vendor lead days; switched on 22 Nov 2026, first nightly confirmation 23 Nov).
- **Errors:** a failed or flagged run sends one email to the operations inbox through `XB Ops Alert Email` (system `AC2-STOCK`).
- **Runbooks:** [hourly stock balance export](docs/autocount2-automation/stock_balance_hourly_runbook.md) (set-up, settings, alert steps) and the [AC2 data guide](docs/autocount2-automation/AC2_DATA_GUIDE.md) (what each file is and how to read it).

### AC2 AR/AP aging

What customers owe us and what we owe suppliers, as a latest-only picture in a private Drive folder. Read-only.

- **Where it runs:** the AC2 server, Task Scheduler `\XB\`.
- **Hourly (:30):** replaces one verified latest set (open items, parties, a tracker feed, manifest and status). No history. Checked against AutoCount's own Debtor and Creditor Aging reports on 18 Nov 2026.
- **Errors:** one email through `XB Ops Alert Email` (system `AC2-ARAP`).
- **Runbook:** [AR/AP aging](docs/autocount2-automation/arap_aging_runbook.md); reading the files: [AC2 data guide](docs/autocount2-automation/AC2_DATA_GUIDE.md).

### AC2 member sync and member core

Keeps Shopify members and AutoCount members in step. Shopify is the source of truth for sign-ups.

- **Member sync (#155), every 15 minutes on the AC2 server:** links each Shopify sign-up to the AC2 member that staff keyed (phone rule), and when none appears by the next morning creates it in AutoCount as type `GUEST` (capped per run). Shopify creates from AutoCount are off. Runbook: [member sync phase 1](docs/autocount2-automation/member_sync_phase1.md).
- **Daily summary (06:40 SGT):** one email to the operations inbox (`Member Sync - Daily Summary`, through `XB Ops Alert Email`). It replaces the manual daily check.
- **Member core (#268), every 15 minutes, about 7 minutes after the sync, observe only:** reads paid Shopify orders and records which customers would move to `MEMBER` (one order of S$300 or more, 2-year expiry). AutoCount writes are switched off until the owner approves them. Runbook: [member core](docs/autocount2-automation/member_benefits_runbook.md).

### Shopify kit re-tagger

Keeps the "Pack for this trip" gear-kit tags on the Shopify store in line with the kit rules, so new products join the right kit collections.

- **Where it runs:** n8n on the server PC (`XB Kit Re-tagger`).
- **Daily (03:00 SGT):** reads every active product and adds or removes only its own kit tags. Staff overrides (`kit-no`, `kit-pin-<kit>`) and every other tag are left alone. A safety brake stops the run and emails the owner if it would remove far more than usual.
- **Errors:** one email through `XB Ops Alert Email` (system `KIT-RETAG`).
- **Runbook:** [kit re-tagger](docs/xb_kit_retagger.md) (live since 21 Nov 2026).

### Error alerts and daily health check

- **Error alerts by email (#288):** every job sends its errors through one n8n sub-workflow on the server PC, `XB Ops Alert Email`, which holds the only Gmail node for alerts, folds repeats and logs each alert. Errors only, no "all is well" messages. Callers: `AC2-STOCK`, `AC2-ARAP`, `ENERGYGRID`, `MEMBER-SYNC`, `HEALTH` and `KIT-RETAG`. Telegram is retired. Docs: [workflow exports](n8n-workflows/README.md) and the [alert log table](docs/ops_alert_email_log.md).
- **Daily health check (#325, 07:30 SGT):** a read-only checker on the server PC judges every scheduled job OK, WARN or FAIL and emails the owner only when something needs a look, plus a heartbeat every Monday. **No `[XB health]` email on a Monday by 09:00 means the checker is down.** Runbook: [daily health check](docs/daily_health_check.md).

### Drive mirror of `main`

Once a week (Sunday 03:00 SGT) the server PC copies this repository's `main` branch into one company Drive folder, read-only, so people without GitHub can read it. Runbook: [Drive mirror](docs/drive_mirror_runbook.md).

## Built, waiting for a live step

| What | Issue | Waiting for |
| --- | --- | --- |
| Member core upgrades (S$300 order to `MEMBER`) | [#268](https://github.com/x-boundaries/automation/issues/268) | Owner OK to switch AutoCount writes on; runs in observe mode now. |
| Member tags `member-qualified` and `member-bday-month` | [#268](https://github.com/x-boundaries/automation/issues/268) | Scheduled observe runs, then the owner's OK for the write switches ([member tags runbook](docs/autocount2-automation/member_tags_runbook.md)). |
| Member backfill and Shopify creates from AutoCount | [#155](https://github.com/x-boundaries/automation/issues/155) | The backfill and Shopify-create live steps in the [member sync runbook](docs/autocount2-automation/member_sync_phase1.md), each with its own owner OK. |
| SIMBA invoice intake (email to Drive) | [#244](https://github.com/x-boundaries/automation/issues/244) | On hold until the intake mailbox is set up; redesign in [PR #265](https://github.com/x-boundaries/automation/pull/265). Runbook: [SIMBA intake](docs/simba_invoice_intake.md). |

## In progress

- [#268](https://github.com/x-boundaries/automation/issues/268) member journey: member types, discounts and tags (the discount codes go live only on the partner's say).
- [#155](https://github.com/x-boundaries/automation/issues/155) member sync: forms watch (default off) and the two-way backfill.
- [#292](https://github.com/x-boundaries/automation/issues/292) analysis dashboard: an "Ask Claude" panel in owner testing.
- [#269](https://github.com/x-boundaries/automation/issues/269) gear kits Part B: the trip discount code (waiting on partner input).
- [#342](https://github.com/x-boundaries/automation/issues/342) courier monthly statement to the accounts Drive (design).
- [#332](https://github.com/x-boundaries/automation/issues/332) n8n 3.0 readiness.
- Paused or scoping only: [#246](https://github.com/x-boundaries/automation/issues/246) buying-intelligence money data, #195-#198.

## Retired

| What | Replaced by |
| --- | --- |
| Telegram alerts (credential deleted and bot revoked, 21 Nov 2026) | Email through `XB Ops Alert Email` (#288). |
| EnergyGrid Claude routine | The Claude-free native daily task (#226, 19 Oct 2026). |
| EnergyGrid alert ingress and download error handler workflows (never live / archived) | The email run monitor (#288). |
| Member gateway worker and its Google Forms gateway workflow (never activated) | AC2 member sync phase 1 (#155). |
| Member intake lookup bridge (Gates 3-5) and the n8n member intake UAT workflows (earlier member intake design, not in use) | AC2 member sync phase 1 (#155). The scripts and runbooks stay below as reference. |

## Developers and CI

The full repository reference (scripts, config templates, output folders, runbooks, and data rules) is in the section below. Many member-intake scripts listed there belong to the earlier member intake design and are kept as reference only (see [Retired](#retired)). CI workflows are in [.github/workflows/](.github/workflows/) and run offline tests only.

<details>
<summary>Repository reference</summary>

This repository contains AutoCount 2 automation work: local extraction scripts, SQL/API investigation notes, configuration templates, and runbooks for safe read-only automation around the AutoCount 2 environment. It also contains the isolated Energy@Grid bill downloader documented below.

Task tracking, pending-work dashboards, completed-task logs, and personal planning notes are intentionally kept outside this repo. Programme status lives on parent issue #117 and its child issues; this repository uses the canonical Toolkit Controller and keeps no local Controller copy.

## XB Automation Operating Model

Automation XB defaults to a **Google Workspace-first, Claude Code-first** operating model.

- **Google Workspace is the corporate working surface.** Prefer approved Google Drive / Google Workspace connectors for corporate documents, operational files, shared working data, and business context that already live there. Do not duplicate those materials into Git merely to make them available to an agent.
- **Claude Code is the preferred executor.** Prefer approved plugins/connectors, then supported APIs, then browser/UI interaction only when required. Production-facing work should not be forced into manual Owner clicks when the current child authority permits Claude to perform the exact operation safely.
- **Prefer connector-native SaaS access over local sync clients.** When Claude Code can reach Google Workspace, Shopify, n8n, or another SaaS surface through an approved connector/API, do not install a local sync/mount client merely to expose the same data unless the child contract proves a concrete runtime need.
- **Authority remains operation-scoped.** This preference does not grant blanket production mutation, credentials, deployment, customer/accounting writes, destructive operations, merge, or finality.
- **Runtime hosts stay thin.** Production hosts and VMs are execution targets, not default development workstations. Repository development, broad diagnostics, n8n workflow work, Toolkit work, and agent-skill work normally belong on the laptop, server PC, or controller environment. For Windows-hosted recurring automation, prefer Windows Task Scheduler as the deterministic cadence/launch boundary and a tightly scoped Claude Code CLI invocation when connector-aware orchestration is required.
- **Do not install Toolkit or general-purpose agent skills on runtime VMs by default.** Add runtime-side tooling only when the active delivery contract proves it is genuinely required there and Owner/Web explicitly approves it.
- **Use deterministic runtime seams.** AutoCount remains behind its reviewed local official-API worker/adapter; Claude may supervise an already-authorised closed command envelope without turning the VM into a development environment.
- **Sensitive operational detail stays private.** Public Git/GitHub surfaces contain sanitised policy, architecture, identities/digests, and secret names only. Durable sensitive operational mappings use the existing private `x-boundaries/TRANSPORT-automation` custody workflow.
- **Never put secrets into repo `.env` files.** Use approved connector credential stores, Windows/private-host environment bindings, or other authorised private configuration outside Git.

Preferred topology:

```text
Google Workspace / SaaS source
    -> approved connector or supported API
    -> Claude Code / controller
    -> reviewed deterministic automation
    -> thin production runtime
```

This is the default direction for new Automation XB work unless a child-specific contract records a concrete reason to deviate.

The isolated Energy@Grid utility surface is documented in [energygrid-bill-downloader/](energygrid-bill-downloader/README.md) and does not change the AutoCount integration surfaces above.

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
- `scripts/ac2_member_gateway_worker.ps1` (with `scripts/ac2_member_gateway_worker_lib.ps1` and `scripts/ac2_member_gateway_autocount_adapter.ps1`): the bounded production member-first worker seam. **Retired, never activated:** the AC2 member sync (#155, below) replaced it; the member sync still reuses helpers from the adapter. It is outbound-only, disabled by default, uses only the reviewed `member.create` AutoCount API surface, and never contacts AutoCount from repository tests.
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
- `scripts/member_create_uat_approval.py` and `scripts/ac2_member_create_uat_runner.ps1`: the bounded single-member creation UAT path. The laptop-side approval tool records a controlled reviewer decision for one `READY_FOR_CREATE_REVIEW` row and builds an immutable, single-use create package (validated against `schemas/member_create_uat_package.schema.json`); the AutoCount VM runner validates that package in-process, performs an existing-member duplicate check, assigns only whitelisted fields, reads every assigned field back, and gates a single member-save call behind five explicit confirmation switches plus the fail-closed business confirmations in `config/member_create_uat_business_confirmation.json`. `ExpiryDate=2028-06-30` is now an active assignable field, proven persistent by the synthetic capability probe (durable result SHA-256 `48CC0185EFF59C3A21AC087BC0C120A00B70801599C3F5513675F7950CD1541B`); all four business values are explicitly confirmed. Because the payload shape changed, the package schema is now `member_create_uat_package/v2` and any package built under the previous `v1` contract is refused fail-closed, so a new reviewer decision and a fresh package are required. It is UAT scaffolding, not the production workflow, supports exactly one member, performs no live write here, and performs no member update, deletion, or rollback; a real write still requires the separate explicit operator step. The separate bounded production member gateway is documented in the [member gateway production contract](docs/autocount2-automation/member_gateway_production_contract.md) and remains activation-disabled. See [Single-member creation UAT runbook](docs/autocount2-automation/member_create_uat_runbook.md).
- `scripts/autocount_migration_preflight.py` (with pure contract library `scripts/autocount_migration_preflight_contract.py`): repository-only migration preflight for the AutoCount 2.0 cutover and import data contract. It binds a frozen source dataset to a manifest by cryptographic hash, validates the explicit bounded paste range, checks contract-qualified source-to-destination field mappings, and validates the two external evidence documents (effective import-time field routing, and the actual import execution context across both account-book/company and import surface/entity). It prepares and validates migration evidence only: it never contacts AutoCount, never opens the vendor `.xls` templates, never reads live AutoCount settings or a live session, never writes to production SQL, and never performs, triggers or approves an import. Unknown, partial, stale or unverifiable authority fails closed as `UNKNOWN` and `NOT_IMPORT_READY`, and unrecognised input columns block rather than being silently dropped. The strongest outcome is `REPOSITORY_PREFLIGHT_SATISFIED`, which is not production import approval; production import remains a human/vendor-controlled AutoCount action. Manifest and evidence contracts live in `schemas/autocount_migration_preflight_manifest.schema.json`, `schemas/autocount_migration_routing_evidence.schema.json`, and `schemas/autocount_migration_execution_context_evidence.schema.json`. See [AutoCount migration preflight and manifest runbook](docs/autocount2-automation/migration_preflight_manifest_runbook.md).
- `scripts/autocount_stock_extract.py`: stock extraction/archive workflow for AutoCount stock master, stock balance, and stock movement datasets.
- `scripts/install_autocount_stock_extract_task.ps1`: Windows Task Scheduler installer for the stock extraction job.
- `scripts/autocount_stock_balance_hourly.py`: hourly read-only AC2 stock balance export (#245, **live** on the AC2 server): every run checks the stock ledger against the stored balance and replaces one verified latest pair, a gzipped dashboard feed, an item index and `last_run_status.json` in a Drive for desktop sync folder (no hourly folders in Drive); the first run after midnight also publishes the previous day's ledger close (`--backfill` from 1 Aug 2026, 7-day restate window) and, when analysis is on, the nightly buying analysis set built by `scripts/autocount_stock_analysis.py` (#292; the purchasing file is on since 22 Nov 2026); local hourly folders and daily files are pruned behind a setting. Errors go by email through `XB Ops Alert Email` (`n8n-workflows/ac2_stock_alert.workflow.json`).
- `scripts/install_autocount_stock_balance_hourly_task.ps1`: Task Scheduler installer for the hourly stock balance export (#245): one hourly trigger with no daily trigger, `\XB\` folder, `-DryRun` by default; each registration needs a separate owner OK.
- `scripts/ac1_sales_history_build.py`: one-off, units-only AC1 monthly sales history for the Buying view (#292); republishing needs the owner's OK. See [AC1 sales history build runbook](docs/autocount2-automation/ac1_sales_history_runbook.md).
- `scripts/autocount_arap_aging.py`: hourly read-only AC2 AR/AP aging export (#321, **live** on the AC2 server): replaces one verified latest set (open items, parties, tracker feed, manifest, `last_run_status.json`) in the archive folder and the private Drive folder; no history. Errors go by email (`n8n-workflows/ac2_arap_alert.workflow.json`). See [AR/AP aging runbook](docs/autocount2-automation/arap_aging_runbook.md).
- `scripts/install_autocount_arap_aging_task.ps1`: Task Scheduler installer for the AR/AP aging export (#321 slice A2): task `\XB\AC2 ARAP Aging`, one hourly trigger at `schedule.run_minute` (:30), `-DryRun` by default; each registration needs a separate owner OK.
- `directlocal/xb_member_sync.py` (with `scripts/ac2_directlocal_member_bridge.ps1`, `scripts/ac2_member_sync_member.ps1` and the installer `scripts/install_member_sync_task.ps1`): the #155 two-way member create sync, **live** on the AC2 server every 15 minutes (AutoCount `GUEST` creates on, Shopify creates off). The daily summary `directlocal/xb_member_sync_daily_summary.py` (installer `scripts/install_member_sync_summary_task.ps1`) runs at 06:40 SGT and, once the #288 errors-only change is live, sends nothing on a clean morning unless it carries an action alert (an alert from the runbook's "Hypercare alerts that need action" list, FIRST CREATE or the forms watch's `forms_flow_silent`); a clean morning is logged for the daily health check instead. See [member sync phase 1](docs/autocount2-automation/member_sync_phase1.md).
- `directlocal/xb_member_benefits.py` (with `scripts/ac2_member_benefits_bridge.ps1`, `scripts/ac2_member_benefits.ps1` and `scripts/install_member_benefits_task.ps1`): the #268 member core (S$300 online order to `MEMBER`, plus claim links), scheduled on the AC2 server in observe mode; AutoCount writes stay off until the owner approves them. See [member core runbook](docs/autocount2-automation/member_benefits_runbook.md).
- `directlocal/xb_member_tags.py` (installer `scripts/install_member_tags_task.ps1`): the #268 Shopify tags `member-qualified` and `member-bday-month`; observe by default, both write switches off. See [member tags runbook](docs/autocount2-automation/member_tags_runbook.md).
- `directlocal/xb_member_type_move.py` (with `scripts/ac2_member_type_move_bridge.ps1` and `scripts/ac2_member_type_move.ps1`): the attended one-off move of existing members from type `Default` to `MEMBER` / `GUEST` (#268); no standing schedule. See [member type move runbook](docs/autocount2-automation/member_type_move_runbook.md).
- `scripts/xb_daily_health.py` (installer `scripts/install_daily_health_task.ps1`, workflow `n8n-workflows/xb_daily_health.workflow.json`): the read-only daily health check (#325, **live**, 07:30 SGT on the server PC). See [daily health check](docs/daily_health_check.md).
- `n8n-workflows/xb_kit_retagger.workflow.json`: the daily Shopify kit re-tagger (#269 Part A, **live**, 03:00 SGT in n8n on the server PC). See [kit re-tagger runbook](docs/xb_kit_retagger.md).
- `n8n-workflows/ops_alert_email.workflow.json`: the shared alert email sub-workflow `XB Ops Alert Email` (#288, **live**) that every job above sends its errors through. See [n8n-workflows/README.md](n8n-workflows/README.md) and [alert log table](docs/ops_alert_email_log.md).
- `scripts/mirror_main_to_drive.py`: weekly read-only copy of GitHub `main` into one marked company Drive folder (#273, **live**); no-op when `main` has not moved, never writes outside the marked folder.
- `scripts/install_mirror_main_to_drive_task.ps1`: registers the Sunday 03:00 mirror task for the signed-in user; registering needs a separate owner OK.
- `scripts/pii_guard.py`: CI guard that blocks personal data in added lines (#211, #270).

## Configuration Templates

- `config/autocount_sql_probe.example.json`
- `config/autocount_readonly_login_validate.example.json`
- `config/autocount_phase1_reconcile.example.json`
- `config/autocount_broader_surface_discovery.example.json`
- `config/autocount_inventory_operation_discovery.example.json`
- `config/autocount_stock_extract.ac2_smoke.example.json`
- `config/autocount_stock_extract.example.json`
- `config/autocount_stock_extract.from_probe.example.json`
- `config/autocount_stock_balance_hourly.example.json` (#245 hourly stock balance export; fake values only, the real file is the gitignored `config/autocount_stock_balance_hourly.local.json`)
- `config/member_gateway.production.example.json` (closed `xb.member.gateway.config.v2` example; MemberNo is fixed at 20, activation is disabled, the kill switch is on, the adapter is not ready, and all real source, database, network, watermark, and credential bindings remain external)
- `config/mirror_main_to_drive.example.json` (Drive mirror settings with placeholder folders; copy to the gitignored `config/mirror_main_to_drive.local.json`)
- `config/autocount_arap_aging.example.json` (#321 AR/AP aging export)
- `config/member_sync.example.json`, `config/member_sync.live.example.json` and `config/member_sync_daily_summary.example.json` (#155 member sync and its daily summary)
- `config/member_benefits.example.json` and `config/member_benefits.live.example.json` (#268 member core)
- `config/member_tags.example.json` and `config/member_tags.live.example.json` (#268 member tags)
- `config/member_type_move.example.json` and `config/member_type_move.live.example.json` (#268 member type move)
- `config/daily_health.example.json` (#325 daily health check)

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
- [AC2 data guide for people and AI agents](docs/autocount2-automation/AC2_DATA_GUIDE.md) (what each AC2 data file is, where, how fresh, traps; #292, #321)
- [AC2 hourly stock balance export runbook](docs/autocount2-automation/stock_balance_hourly_runbook.md) (#245)
- [AC1 sales history build runbook](docs/autocount2-automation/ac1_sales_history_runbook.md) (#292)
- [AC2 AR/AP aging runbook](docs/autocount2-automation/arap_aging_runbook.md) (#321)
- [Member sync phase 1](docs/autocount2-automation/member_sync_phase1.md) (#155)
- [Member core runbook](docs/autocount2-automation/member_benefits_runbook.md) (#268)
- [Member tags runbook](docs/autocount2-automation/member_tags_runbook.md) (#268)
- [Member type move runbook](docs/autocount2-automation/member_type_move_runbook.md) (#268)
- [Shopify kit re-tagger runbook](docs/xb_kit_retagger.md) (#269)
- [Daily health check](docs/daily_health_check.md) (#325)
- [Ops alert email log table](docs/ops_alert_email_log.md) (#288)
- [n8n workflow exports](n8n-workflows/README.md)
- [SIMBA invoice intake](docs/simba_invoice_intake.md) (#244, on hold)
- [Drive mirror of main runbook](docs/drive_mirror_runbook.md)
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
- [Member gateway production contract](docs/autocount2-automation/member_gateway_production_contract.md) (retired, never activated)
- [Member gateway production runbook](docs/autocount2-automation/member_gateway_production_runbook.md) (retired, never activated)
- [Single-member creation UAT runbook](docs/autocount2-automation/member_create_uat_runbook.md)
- [AutoCount migration preflight and manifest runbook](docs/autocount2-automation/migration_preflight_manifest_runbook.md)
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

</details>
