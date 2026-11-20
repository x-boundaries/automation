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

Re-apply the live backup taken before step E1, or deactivate only the `EnergyGrid - Local Run Monitor` workflow in n8n. Bill processing, Drive and Core emails are untouched by both the monitor and its rollback. Never replay bill effects to "test" the monitor.
