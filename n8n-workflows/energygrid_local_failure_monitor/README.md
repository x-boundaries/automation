# EnergyGrid local n8n run monitor

**Status (16 October 2026, #226):** installed and active on the owner's same-PC n8n under owner authority. The source export in this repository stays import-safe (no credentials, Telegram destination or addresses, `ENABLE_OWNER_ALERTS = false`); the live copy binds those privately. It creates **no** Drive upload/download logic, no billing database migration, no external watchdog and no model usage, and it does not change the pinned EnergyGrid Core.

## Owner design: one sender per message

- **EnergyGrid Core (Claude task)** keeps sending every bill email it sends today: success emails and failure emails, deduplicated inside the Core. This is the UAT-accepted path and is unchanged.
- **n8n** alerts the owner (Telegram, existing bot) only for what the Core cannot report about itself:
  - no completed run report by **14:00 SGT** while n8n was already running before the **11:30** task (task missed, crashed, stopped early, or its report was refused);
  - a bill still **PENDING** after a finished run.
- A stream reported as **FAILURE** is recorded in n8n's incident state but never alerted, because the Core already emails it. There is therefore no second sender for any message and no old-sender cutover.
- PC shut down or n8n stopped across the check: no alert, by owner choice.

## Files

- `../energygrid_local_failure_monitor.workflow.json`: the n8n export (8 nodes): authenticated local webhook, strict report validation, PC-on-only missed-run check, send gate, `Remove Duplicates` (one attempt per incident key) and the Telegram sender (no retry, continue on fail).
- `energygrid_monitor_report.py`: standard-library reporter. With `--run-dir` it accepts only `acquire.json` + `status.json` from one fresh `egnative.cmd new-run` folder directly under the runs root (folder age at most 6 hours, files written after the folder and in acquire-then-status order, Core schema and command checked, acquire `ACQUIRED`). It writes `monitor-report.json` exclusively **before** the POST, so a run is reported at most once, then `monitor-post.json` with the outcome. No retries, redirects or non-local endpoints. Unknown Core states refuse to report, which surfaces as the missed-report alert.
- `egmonitor.cmd`: host wrapper with fixed paths: venv Python, installed reporter, runs root and the private token file. It is installed beside the EnergyGrid launcher in a `monitor` folder.
- `test_energygrid_monitor.py`, `test_n8n_workflow_code.js`: synthetic tests, with no credentials or provider effects.

## Host hook (installed)

The daily `energygrid-bill-check` task prompt gained three steps, and nothing else changed: save `acquire`'s JSON line as `acquire.json`, save the final `status` line as `status.json`, then run `egmonitor.cmd <run folder>` exactly once and never re-run it. The hook reads files only. It never runs Core commands, so a reporter or n8n failure cannot cause another upload or email. The task needs a narrow allow rule for the `egmonitor.cmd` path in the operator's local Claude settings.

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

## Qualification evidence (16 October 2026)

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

Deactivate only the `EnergyGrid - Local Run Monitor` workflow in n8n and remove the three monitor steps from the task prompt (a pre-change copy is kept beside the installed wrapper). Bill processing, Drive, Core emails, UAT state and PRs #242/#247 are untouched by both the monitor and its rollback. Never replay bill effects to "test" the monitor.
