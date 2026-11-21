# EnergyGrid local n8n run monitor

**Status (20 November 2026, #288 T1):** the source export now sends alerts by **email** through the shared `XB Ops Alert Email` workflow (system `ENERGYGRID`), by the owner's 19 Nov 2026 choice "stick purely to email notifications". The live copy on the owner's same-PC n8n (installed 16 October 2026, #226) still sends by Telegram until the separately approved live step E1 updates it from this export, and E2 sends one synthetic test email. The source export stays import-safe (no credentials or addresses, `ENABLE_OWNER_ALERTS = false`, the sender workflow ID as `REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID`). It creates **no** Drive upload/download logic, no billing database migration, no external watchdog and no model usage, and it does not change the EnergyGrid Core.

## Owner design: one sender per message

- **EnergyGrid Core (daily native task, 11:30 SGT)** keeps sending every bill email it sends today: success emails and failure emails, deduplicated inside the Core, through `EnergyGrid - Owner Email`. This path is unchanged.
- **n8n** emails the owner through `XB Ops Alert Email` only for what the Core cannot report about itself:
  - no completed run report by **14:00 SGT** while n8n was already running before the **11:30** task (task missed, crashed, stopped early, report withheld or refused): `[XB alert] ENERGYGRID ERROR run_report_missing`;
  - a bill still **PENDING** after a finished run: `[XB alert] ENERGYGRID WARN bill_pending:EB_BILL` (or `:TENANT_BILL`).
- A stream reported as **FAILURE** is recorded in n8n's incident state but never alerted, because the Core already emails it. There is therefore no second sender for any message and no old-sender cutover.
- PC shut down or n8n stopped across the check: no alert, by owner choice.

## Files

- `../energygrid_local_failure_monitor.workflow.json`: the n8n export (8 nodes): authenticated local webhook, strict report validation, PC-on-only missed-run check, send gate, `Remove Duplicates` (one attempt per incident key) and `Send Owner Alert Email`, an Execute Workflow call to `XB Ops Alert Email` (one call per alert, no retry, continue on its regular output). The Code nodes emit `{severity, key, short, lines}` built only from fixed text and validated closed fields; the sender's own rules (fold the same key within 60 minutes, at most 10 alert emails per system per hour, `send_failed` logged, never thrown) apply unchanged.
- `../energygrid_monitor_error_log.workflow.json`: the monitor's error workflow (#288 option (c)): `Error Trigger` -> `Reduce To Closed Codes` -> one Data Table row of closed codes per failed run. Test: `tests/test_energygrid_monitor_error_log_workflow.py`. See "Failed runs: the error log" below.
- `energygrid_monitor_report.py`: standard-library reporter. With `--run-dir` it accepts only `acquire.json` + `status.json` from one fresh `egnative.cmd new-run` folder directly under the runs root (folder age at most 6 hours, files written after the folder and in acquire-then-status order, Core schema and command checked, acquire `ACQUIRED`). It writes `monitor-report.json` exclusively **before** the POST, so a run is reported at most once, then `monitor-post.json` with the outcome. No retries, redirects or non-local endpoints. Unknown Core states refuse to report, which surfaces as the missed-report alert.
- `egmonitor.cmd`: host wrapper. It is installed in a `monitor` folder of an EnergyGrid home and derives that home (`EG_HOME`) from its own location, so the same file serves every install (the original `C:\XB\_ScheduledTask\EnergyGrid` and the protected `C:\Program Files\EnergyGrid`). It runs that home's venv Python on that home's reporter with that home's `data\runs` as the runs root, and the private token file.
- `test_energygrid_monitor.py`, `test_n8n_workflow_code.js`: synthetic tests, with no credentials or provider effects. CI runs both (`energygrid-bill-downloader-tests`, step "EnergyGrid n8n run monitor focused offline tests"). The Node test also runs every alert the monitor can emit through the sender's own `Validate Alert Request` code: an alert line that named another system would be refused there and no email would go out.

## Host hook (installed)

Since the Claude-free cutover (19 October 2026), the daily native task's `egdaily.ps1` runs `egmonitor.cmd <run folder>` once, and only when the run's result line says `monitor_report: SUBMIT` ([native_daily_claude_free.md](../../energygrid-bill-downloader/docs/native_daily_claude_free.md) section 5). The reporter reads files only. It never runs Core commands, so a reporter or n8n failure cannot cause another upload or email. (The original hook was three steps in the retired Claude task prompt.)

## Report contract

Exact schema `xb.energygrid.monitor.v1`:

```json
{
  "schema": "xb.energygrid.monitor.v1",
  "run_id": "12345678-1234-1234-1234-123456789abc",
  "observed_at_utc": "2026-10-16T03:45:00Z",
  "streams": {
    "EB_BILL": {"result": "OK", "support_ref": null, "incident_ref": null},
    "TENANT_BILL": {"result": "PENDING", "support_ref": "EG_MONITOR_PENDING_AFTER_RUN", "incident_ref": "bill-0123456789abcdef01234567"}
  }
}
```

Every key is strictly validated in n8n and reports older than 30 minutes are refused. Result values: `OK`, `FAILURE`, `PENDING`. `incident_ref` is `null` or `bill-` plus 24 lowercase hex characters hashed from the local presentation name; the name itself is never sent.

**Precedence:** an acquisition failure is a FAILURE even when Core Drive `status` is still `DRAINED` from earlier bills; Core `BLOCKED` is a FAILURE; `PENDING` after a finished run is PENDING; only a good acquisition with `DRAINED` is OK.

## Failed runs: the error log (#288 option (c), owner 20 Nov 2026)

**Why.** A rejected report fails `Validate Both Streams and Classify` with a closed `EG_MONITOR_*` code. Header Auth has already accepted the request, so a saved failed execution would keep the valid `X-EnergyGrid-Monitor-Key` token in n8n's execution data. The export therefore saves **no** execution (`saveDataErrorExecution: none`, `saveDataSuccessExecution: none`) and names an error workflow instead (`settings.errorWorkflow`, export placeholder `REPLACE_WITH_ENERGYGRID_MONITOR_ERROR_LOG_WORKFLOW_ID`).

**How.** For every failed production run (webhook path and the 5-minute schedule path; manual runs never), n8n starts `EnergyGrid - Monitor Error Log` ([export](../energygrid_monitor_error_log.workflow.json)). It writes one row to the Data Table `energygrid_monitor_error_log`:

| Column | Value |
|---|---|
| `workflow_id` | the failed workflow's ID (letters and digits only, else `unknown`) |
| `execution_id` | the failed run's ID (digits only, else empty; that run itself is not kept) |
| `failed_node` | one of the monitor's 7 working node names, `other`, or empty |
| `code` | one of the monitor's 11 `EG_MONITOR_*` codes when the error message is that code, alone or followed by exactly n8n's line suffix ` [line N]` or ` [line N, for item M]` (n8n 2.48.5 adds it to every Code node error, so the live message is for example `EG_MONITOR_REPORT_SHAPE [line 6]`); `EG_MONITOR_TRIGGER_FAILED` when the trigger itself failed; otherwise `EG_MONITOR_UNCLASSIFIED` (also for a code followed by any other text) |
| `mode` | n8n's run mode (`webhook`, `trigger`, ...), else `other` |
| `failed_at` | when the row was written (UTC ISO) |

No message text, description, stack, context, node parameter, header or report body reaches the row: a runtime error message can quote its input (a JSON parse error does), so the reducer keeps only exact matches against closed lists (the line suffix is matched exactly and dropped). The error workflow saves none of its own runs either.

**Who reads it.** `XB Daily Health` counts every row of the last 24 h (the table is the monitor's alone; `WARN n8n workflows: N failed run(s) in the last 24 h`), checks that the error log workflow is active, and checks that the live monitor still has `errorWorkflow` = the error log and `saveDataErrorExecution: none` (an ERROR otherwise) ([daily health check](../../docs/daily_health_check.md)). The monitor's own `run_report_missing` alert after 14:00 still covers a rejected report on the same day.

**Proof that `none` still calls the error workflow (n8n 2.48.5, tag `n8n@2.48.5`):** `packages/cli/src/execution-lifecycle/execution-lifecycle-hooks.ts`, `hookFunctionsSave`: when a failed run is not to be saved (`shouldNotSave`), n8n calls `executeErrorWorkflow(...)` first and only then deletes the in-flight execution; the queue-mode worker hook calls it for every failed non-manual run regardless of the save setting. `executeErrorWorkflow` (`execute-error-workflow.ts`) passes `execution.id`, `execution.error`, `execution.lastNodeExecuted`, `execution.mode` and `workflow.id`/`name`. The target must be published and its caller policy must allow the monitor (`workflowsFromSameOwner`: `subworkflow-policy-checker.ts` passes only when both workflows are in the **same n8n project**).

**Proof of the message shape (same tag):** the JavaScript Code node always runs in the task runner (`packages/nodes-base/nodes/Code/Code.node.ts`, `JsTaskRunnerSandbox`). In secure mode `packages/@n8n/task-runner/src/js-task-runner/js-task-runner.ts` runs the code in a `vm` context wrapped in `VmCodeWrapper`; an error made there is not a host `Error`, so `toExecutionErrorIfNeeded` builds an `ExecutionError`, and `packages/@n8n/task-runner/src/js-task-runner/errors/execution-error.ts` (`populateFromStack`, `toLineNumberDisplay`) sets the message to `<details> [line N]` from the `evalmachine.<anonymous>:N` stack frame, or `[line N, for item M]` when an item index is known. That message reaches the Error Trigger as `execution.error.message`. The offline test runs the monitor's own validator that way (a port of `populateFromStack`) and checks the reducer records the closed code.

**Rows are kept** until removed by hand. A normal day writes none; a broken schedule path writes up to 288 a day, which the health check shows the next morning. After the cause is fixed, the owner may delete that day's rows in the Data Table view.

### Live steps (later, each with its own owner OK)

Server PC n8n only, local API `http://127.0.0.1:5678/api/v1`. Nothing here was done by the PR that added it. Each step needs the owner's OK words for that step, given in the chat that runs it.

**Same project:** the Data Table, `EnergyGrid - Monitor Error Log` and `EnergyGrid - Local Run Monitor` must sit in the **same n8n project**. The caller policy `workflowsFromSameOwner` only lets the monitor call an error workflow in its own project, and the Data Table node reads tables of its own project. M1 and M2 create the table and the workflow in the monitor's project and check it on read-back.

| Step | What | Undo |
|---|---|---|
| M1 | OK words **"OK create EG error log table"**. Create the Data Table `energygrid_monitor_error_log` in the monitor's project with six `string` columns: `workflow_id`, `execution_id`, `failed_node`, `code`, `mode`, `failed_at`; read back its project and columns. | Delete the empty table. |
| M2 | OK words **"OK import EG error log workflow"**. Import `EnergyGrid - Monitor Error Log` from the merged export (inactive) into the monitor's project, set `Record Failure Row` to the table ID, read it back (project, table ID, `callerPolicy`), then publish (activate) it: n8n only runs a published error workflow. | Deactivate and archive it. |
| M3 | OK words **"OK test EG error log once"**. Synthetic failure test on a copy, kept short: import a second copy of the monitor export as `EnergyGrid - Local Run Monitor (error log test)` with a one-off test webhook path, the same Header Auth credential, `ENABLE_OWNER_ALERTS` false and `errorWorkflow` = the M2 workflow; activate it; POST `{}` once on loopback with the header (the operator reads the token file; nobody prints it). Expect: one row with `code` `EG_MONITOR_REPORT_SHAPE` (n8n's message is `EG_MONITOR_REPORT_SHAPE [line 6]`), `failed_node` `Validate Both Streams and Classify`, `mode` `webhook`; **0** saved executions for the copy; no email. While active, the copy also runs the 5-minute schedule tick, so keep the window short: POST right after activating, read the table, then deactivate at once (within 5 minutes if possible). Then archive the copy and delete the test row **and any other row the copy wrote** (every row with the copy's `workflow_id`, for example a `trigger` row from a tick). | Same as the last two sentences. |
| M4 | OK words **"OK switch EG monitor to error workflow"**. At n8n 2.48.5 a workflow's `settings` are kept on the workflow row and are **not part of a version** (a version stores nodes, connections and node groups only), so this step changes settings only and publishes no new version. (1) Back up the live monitor (one GET; keep the answer). (2) **Before the PUT**, read back that `versionId` equals `activeVersionId` (no unpublished draft); if they differ, **stop** and report, and send nothing. (3) Update only its settings with `PUT /workflows/{id}?publishIfActive=false`: `saveDataErrorExecution: none` (it is `all` live since the E1 ruling) and `errorWorkflow` = the M2 workflow; keep `staticData`, nodes and every other setting. With `publishIfActive=false`, n8n re-activates the version that is already published, now with the new settings, and the API key needs only `workflow:update`; without it, a PUT whose draft differs from the published version also needs `workflow:activate`. (4) **On a 403:** n8n saves first and then refuses to publish ("refused rather than rolled back"), so the new settings are **already on the row**. Re-apply the backup's settings at once (Undo), read them back, report, and do not go on to M5. (5) Read back: the PUT answered **HTTP 200**; a GET shows `settings.errorWorkflow` = the M2 workflow and `settings.saveDataErrorExecution` = `none`; and `activeVersionId` is **unchanged** from the backup. Source (tag `n8n@2.48.5`): `packages/@n8n/db/src/entities/workflow-history.ts`; `packages/cli/src/workflows/workflow.service.ts` (`update`: `versionIdToPublish`, `assertMayPublishOnSave`, the settings-only re-activation); `packages/cli/src/public-api/v1/controllers/workflows.public.controller.ts` (`PUT /workflows/{workflowId}`, `publishIfActive`). | Re-apply the backup's settings; read them back. |
| M5 | Update `XB Daily Health` from its merged export (new `Read Monitor Error Log` node and the `WATCH` entries for the monitor and the error log), keeping its private values; read back. See [daily_health_check.md](../../docs/daily_health_check.md). | Re-apply its backup. |

## Qualification evidence (16 October 2026, Telegram build; historical)

- Offline: 20 Python reporter tests and the Node Code-node suite pass.
- Live, synthetic data, through the installed reporter and authenticated webhook, checked on n8n execution node outputs (not HTTP status alone):
  - healthy reports: silent;
  - EB failure, Tenant blocked, and a failed acquisition with old `DRAINED`: recorded, not alerted;
  - both streams PENDING: two independent incident keys;
  - identical repeat: same keys;
  - recovery then a new PENDING: a new key;
  - incident state persisted across executions;
  - missing or wrong header: HTTP 403.
- One controlled synthetic owner alert sent: Telegram accepted it. The identical repeat was dropped by `Remove Duplicates`, and a healthy report closed the incident.
- Missed-run timing and PC-off suppression are covered by the time-simulated Node tests. The live 5-minute tick and heartbeat state were read back.

## Rollback

Re-apply the live backup taken before step E1 (or before M4 for the error-log settings), or deactivate only the `EnergyGrid - Local Run Monitor` workflow in n8n. Bill processing, Drive and Core emails are untouched by both the monitor and its rollback. Never replay bill effects to "test" the monitor.
