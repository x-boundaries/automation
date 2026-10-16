# EnergyGrid local n8n failure monitor

**Status:** Source-controlled package, inactive, not imported, no live email or host changes. Based on `x-boundaries/automation` #226, the accepted v4 EB + Tenant UAT and owner direction of 16 October 2026. It creates **no** new Drive upload/download logic, no billing database migration, no external watchdog and no model usage.

## Files

- `../energygrid_local_failure_monitor.workflow.json`: n8n importable workflow, inactive by default, with local authenticated webhook + a PC-on-only missed-run check + at-most-once incident email filtering.
- `energygrid_monitor_report.py`: Python standard-library reporter that converts EnergyGrid's actual `acquire` and `status` command JSON outputs into a *strictly sanitized* two-stream webhook payload, then posts it to localhost only. It does **not** directly open or modify billing SQLite, send Gmail, or fetch/republish bills.
- `test_energygrid_monitor.py`, `test_n8n_workflow_code.js`: synthetic tests, with no network connections, credentials or provider effects.
- The version-controlled JSON export is the import source of truth; the original build generator is retained in the earlier downloadable offline package, not required for n8n runtime or local tests.

## Assumptions and limits

- Existing accepted EnergyGrid daily Claude task runs around **11:30 SGT**. The monitor checks n8n was ticking before that task, then alerts if no completed report has arrived by **14:00 SGT**. Adjust these values in `Check Missing Run (PC-On Only)` after confirming the installed schedule.
- n8n runs on the **same Windows PC** and is accessible locally at `http://127.0.0.1:5678`. If it is inside Docker, publish **only** local host port 5678 to loopback (or keep a suitable firewall/credential boundary); the Python reporter runs on Windows and posts to the published port, *not* inside the container.
- n8n 2.x disables Execute Command by default. **Do not enable arbitrary shell execution.** This design uses native Webhook/Code/Schedule/Remove Duplicates/Send Email nodes instead.
- Machine shutdown: **no missed-run alert**, by owner choice. If n8n stops for more than 11 minutes between monitor ticks, its missed-run check is disarmed. A very short shutdown or sleep may be indistinguishable from a missed Claude run without reading host boot/sleep events. It may produce a false alert.
- A heartbeat from a legitimate completed task is necessary to detect a *missing* task. The current installed task must be given a separately qualified local-only emitter step, as described below. Merely importing the workflow **will not** monitor anything.
- Deduplication uses n8n's built-in `Remove Duplicates` history (10,000 keys), plus the workflow's incident lifecycle state. It limits normal repeated failure alerts; it is **not** a transactional, cross-app exactly-once email guarantee during an n8n database crash/restore. Automatic email retries are disabled. Do not manually retry uncertain email executions.
- If the original Claude task's failure-email sending is still enabled, activating n8n alerts may produce two alerts. The old sender must be disabled or made inert **after** n8n's safe delivery test and immediately before enabling n8n alerts. Never disable core failure logging or Drive processing.

## Safe setup, in order

1. **Import** `../energygrid_local_failure_monitor.workflow.json` into the local n8n instance. Leave it **Inactive**. Its `Alerts Disabled Until Qualified` Code node also contains `ENABLE_OWNER_ALERTS = false`, a second no-send safety gate. No credentials, customer data or email addresses are embedded in the source file.
2. **Bind webhook Header Auth** credential in `Local Authenticated Report`, using a new long random key and the header name `X-EnergyGrid-Monitor-Key`. Store the same token in a private, access-controlled UTF-8 file *outside Git* on the Windows PC. The token itself must never enter the workflow export, logs, ChatGPT or GitHub.
3. **Bind SMTP** credentials in `Send Owner Failure Email`; replace `REPLACE_WITH_VERIFIED_SENDER` and `REPLACE_WITH_OWNER_EMAIL` with the owner's approved sender and only private recipient. Keep retries **off**, attribution off and `continueOnFail` on. For Gmail/Outlook, configure the supported provider SMTP credential as usual; no passwords are to be committed.
4. **Use a local-only, host-approved post-run hook**, outside Claude's restricted launcher, to generate the two JSON inputs and invoke the reporter *once* after a completed run. The existing Claude task's output from the `acquire` and **final** `status` commands must be saved verbatim as `acquire.json` and `status.json` under that run's private data/runs directory. Use a Write-only-to-runs mechanism already allowed by the host or a separately authorised host integration. Do **not** let n8n execute shell commands, mutate EnergyGrid SQLite or replay `acquire/drive-seal/Gmail` effects merely to create a report.
5. **Offline dry-run:** Use the runner's latest synthetic or known-safe status JSON files:

   ```powershell
   py -3 energygrid_monitor_report.py --acquire-file "C:\\path\\to\\private\\run\\acquire.json" --status-file "C:\\path\\to\\private\\run\\status.json" --dry-run
   ```

   This prints only `schema`, `run_id`, timestamp, EB/Tenant result, closed `EG_` reference and an opaque invoice identity hash; no filename, token or address.
6. **Authenticated local POST:** With the webhook temporarily available on the private local n8n instance, run:

   ```powershell
   py -3 energygrid_monitor_report.py --acquire-file "C:\\path\\to\\private\\run\\acquire.json" --status-file "C:\\path\\to\\private\\run\\status.json" --token-file "C:\\private\\energygrid-monitor-token.txt"
   ```

   The reporter refuses off-device HTTP hosts, HTTPS redirects, arbitrary endpoint paths and extra query strings. No automatic retry is performed. If a local port other than 5678 is used, specify an explicit `--endpoint http://127.0.0.1:PORT/webhook/xb-energygrid-monitor-v1`.
7. **Test** both successful bills (silence), EB failed and Tenant healthy (one alert candidate), repeated failure (no second send), recovery then fresh failure (new alert), incomplete bill (alert), a missed run while PC/n8n remain on (one alert), and n8n stopped across the check (no missing-run alert). Run with the send gate `false` first; inspect execution output only in the local instance. The first real email must be a deliberately approved test to the owner's verified address.
8. **Cut over failure notification ownership:** Reconcile the old Claude failure-mail queue. Disable only its *future failure email sending* without disabling failure evidence or bill delivery. Then set `ENABLE_OWNER_ALERTS = true` in the n8n gate and activate the workflow after verifying the local webhook, schedule, credentials and permissions. This is a live host/credential/SMTP change: it requires actual PC access and on-host validation. Do not infer installation from this code package or from GitHub.

## Report contract

Exact schema `xb.energygrid.monitor.v1`:

```json
{
  "schema": "xb.energygrid.monitor.v1",
  "run_id": "12345678-1234-1234-1234-123456789abc",
  "observed_at_utc": "2026-10-16T03:45:00Z",
  "streams": {
    "EB_BILL": {"result": "OK", "support_ref": null, "incident_ref": null},
    "TENANT_BILL": {"result": "FAILURE", "support_ref": "EG_MONITOR_ACQUISITION_FAILED", "incident_ref": null}
  }
}
```

Every key is strictly validated in n8n. Result values: `OK`, `FAILURE`, `PENDING`. `PENDING` means the daily task ended with a bill still awaiting completion. `incident_ref` is `null` or `bill-` plus 24 lowercase hex characters derived from the *local* presentation name. It does not contain the name itself.

**Failure detection precedence:** An acquisition/LIST/FETCH error is always a failure even when Core Drive `status` remains `DRAINED` from prior verified bills. A Core `BLOCKED` stream is a failure; `PENDING` after a finished run needs attention; only a successful acquisition and `DRAINED` state together count as `OK`. This prevents yesterday's valid PDF from masquerading as today's successful run.

## Safety and rollback

- Rollback the monitoring change by deactivating **only** this n8n workflow and restoring the previous notification sender if explicitly needed. Do not touch accepted UAT, live invoices, Core SQLite, PRs #242/#247, scheduled bill processing or Drive data.
- No n8n credentials or personal data belong in public source or test fixtures. Do not reuse older dormant n8n EnergyGrid exports or grant the Claude scheduled task arbitrary shell/database access.
- The transport report's local HTTP success means n8n received the payload, **not** that Gmail delivered an email.
- Until the local instance, scheduled-host hook, credentials and sender cutover have been verified, the correct status is **BUILT / TESTED OFFLINE / NOT INSTALLED**.

## Local executor handoff

`LOCAL_EXECUTOR_ONE_SHOT.md` is the single bounded instruction for a trusted executor operating on the actual PC. It explicitly protects the current Drive reconciliation sequence during the old-Claude-notification to n8n handover. Nothing in this package claims that local n8n credentials, webhook or scheduler are currently bound.
