# Daily health check (#325)

One email when any scheduled job fails silently. A read-only checker on the server PC runs at 07:30 SGT, judges every watched job OK, WARN or FAIL, and posts one codes-only event on loopback to the n8n workflow `XB Daily Health`. That workflow adds the n8n facts and mails the owner through the shared sender `XB Ops Alert Email`. No email when all is well, except the Monday heartbeat.

- Checker: [`scripts/xb_daily_health.py`](../scripts/xb_daily_health.py) (read-only).
- Settings: `config/daily_health.local.json` on the server PC (git-ignored), copied from [`config/daily_health.example.json`](../config/daily_health.example.json).
- Installer: [`scripts/install_daily_health_task.ps1`](../scripts/install_daily_health_task.ps1) (dry run by default).
- n8n workflow: [`n8n-workflows/xb_daily_health.workflow.json`](../n8n-workflows/xb_daily_health.workflow.json) (live name `XB Daily Health`).
- Sender: [`n8n-workflows/ops_alert_email.workflow.json`](../n8n-workflows/ops_alert_email.workflow.json), system `HEALTH`, key `daily_health`.
- Tests: [`tests/test_xb_daily_health.py`](../tests/test_xb_daily_health.py) and [`tests/test_xb_daily_health_workflow.py`](../tests/test_xb_daily_health_workflow.py) (offline, fake data).

Building this changes nothing live. Every live step (section "Live steps") needs its own owner OK.

## Owner rule (printed in every email)

**No `[XB health]` email on a Monday by 09:00 means the checker is down: tell Claude.**

The Gmail filter is `[XB health]`. The subject carries the date, so a FAIL never hides in an old OK thread:

- `[XB health] FAIL 3 jobs 2026-11-19`: 3 jobs need a look.
- `[XB health] WARN 1 jobs 2026-11-19`: 1 job needs a look, nothing has stopped.
- `[XB health] OK 10 jobs 2026-11-23`: Monday heartbeat, all 10 watched jobs OK.

## How it works

1. **07:30, server PC.** Task `\XB\XB-DailyHealth` runs `xb_daily_health.py run`. It reads:
   - Task Scheduler folder `\XB\` on the server PC (`Get-ScheduledTask` and `Get-ScheduledTaskInfo` only);
   - the AC2 VM's `last_run_status.json` and `latest_manifest.json` for the hourly stock export and the AR/AP aging export, which already reach this PC through Google Drive for desktop;
   - the Drive mirror's `last_run_status.json` in its log folder;
   - its own records of the last 14 days.
2. It judges each job (section "Rules"), writes `health_<date>.json` (codes, counts and dates only) into `record_dir`, and POSTs one `xb.daily_health.v1` event to `http://127.0.0.1:5678/webhook/<private path>` with the Header Auth secret from Windows Credential Manager. One POST, 10 s timeout, no retry, no proxy, no redirect. It always exits 0; only broken settings exit 2.
3. **n8n, `XB Daily Health`.** Header Auth on the webhook; a request carrying Cloudflare or proxy headers gets 404; the Code node checks the closed schema (anything else: 400 with a `HEALTH_*` code) and answers 200 `accepted`. Then it reads, with the n8n API credential, the workflow list and the error runs of the last 24 h; reads today's `MEMBER-SYNC` `daily_summary` row in `xb_alert_email_log`; reads recent `xb_health_runs` rows; judges the n8n-side jobs; writes every line from fixed English templates; calls `XB Ops Alert Email` when the result is not OK or it is Monday; and writes one `xb_health_runs` row.
4. **Email not sent** (`send_failed` when Gmail is down or the credential expired; also `rejected`, `error`, `call_failed` or an unknown answer from the sender): one fixed Telegram line through the existing Telegram credential, for example `XB health email could not be sent on 2026-11-19 (n8n email step: send_failed). Worst: FAIL 2 jobs. Check the n8n Gmail credential, then XB Daily Health.` Only `send_failed` names the Gmail credential; the other outcomes say `Check XB Daily Health and XB Ops Alert Email in n8n.` Nothing is sent to Telegram for `sent` or `not_due` (an OK weekday). The row in `xb_health_runs` records the outcome and the sender's code (for example `gmail_error` or `OPS_ALERT_SHORT`).
5. **08:15, n8n dead-man.** If `xb_health_runs` has no `check` row for today, n8n mails `[XB health] FAIL 1 jobs <date>`: "the health checker did not report today".

Python never sends Telegram or Gmail itself: that would copy a credential out of n8n.

## Jobs

`expected` in the settings file says, per job, `on` (checked), `off` (on purpose, not checked) or `not_installed` (not live yet). Jobs that are not `on` are listed under "Not watched" in every email, so a forgotten switch-on shows up on the next Monday at the latest.

| Job id | Line label | Judged by | Starts as |
| --- | --- | --- | --- |
| `member_sync_summary` | Member sync daily summary | n8n: today's `daily_summary` row in `xb_alert_email_log` | `not_installed` |
| `member_tags` | Member tags | the #288 summary (its own email); OK when `on` | `not_installed` |
| `ac2_stock_export` | AC2 hourly stock export | checker: stock status pair on Drive | `on` |
| `stock_alert_route` | Stock alert route | checker: `alert_status` in the stock status file | `on` |
| `arap_export` | AR/AP export | checker: AR/AP status pair on Drive | `on` |
| `energygrid_daily_bills` | EnergyGrid daily bills | checker: its server task (liveness only) | `on` |
| `energygrid_retention` | EnergyGrid retention | checker: its server task (liveness only) | `on` |
| `drive_mirror` | Drive mirror | checker: its server task and status file | `on` |
| `n8n_workflows` | n8n workflows | n8n: expected workflows active, error runs | `on` |
| `simba_intake` | SIMBA intake | n8n: the SIMBA workflows active, error runs | `not_installed` |
| `server_tasks` | Server task list | checker: tasks in `\XB\` that the settings do not name | `on` |
| `checker` | Health checker | checker: yesterday's POST; n8n dead-man | `on` |
| `drive_folder` | Drive folder | checker: the Drive report folder is reachable | `on` |

The member jobs (sync, member core, tags) stay judged by the #288 member-sync daily summary. The health check only confirms that the summary went out.

**Go-live line.** Every go-live checklist gets one more line: "set `expected: on` for `<job id>` in the daily health settings". When a new server task is registered in `\XB\`, either give it a job or add its name to `known_tasks`; otherwise it shows as WARN `unknown_task`.

## Rules

The check runs at 07:30 SGT; ages are measured at check time. Thresholds are in the settings file (`thresholds`, minutes); these are the defaults. Each job gets the worst level of its reasons; the subject word is the worst of all jobs.

| Job | WARN | FAIL |
| --- | --- | --- |
| AC2 hourly stock export | last run over 70 min ago (`last_run_old`); last run failed but the last good `latest_manifest.json` is under 3 h old (`run_failed`); `delivery_status` `verify_failed` or `copy_failed` (`delivery_failed`); `daily_close_status` `failed` (`daily_close_failed`); `analysis_failure_class` set (`analysis_failed`); run over 7.5 min (`run_slow`) | last run over 3 h ago (`last_run_too_old`); failed and the good manifest over 3 h old or missing (`manifest_too_old`, `manifest_missing`); run over 12 min, 80 % of `PT15M` (`run_too_slow`); status file missing or unreadable (`status_missing`) |
| Stock alert route | `alert_status` `send_failed`, `secret_unavailable` or `error` | none |
| AR/AP export | last run over 90 min ago; failed with a good manifest under 3 h old; delivery `verify_failed` or `copy_failed` | last run over 3 h ago; good manifest over 3 h old or missing; status file missing |
| EnergyGrid daily bills | last result not 0 (`task_result_nonzero`); last run over 26 h ago | task Disabled without a pause (`task_disabled`); last run over 50 h ago; task missing |
| EnergyGrid retention | exit 20 (`retention_held`) or 64 (`retention_refused`); other non-zero result; last run over 26 h ago | Disabled without a pause; last run over 50 h ago; task missing |
| Drive mirror | exit 4 `FETCH_FAILED` (`mirror_fetch_failed`) or 7 `LOCKED` (`mirror_locked`); status file stuck at `running` while the task is not running (`mirror_stuck_running`); last run over 8 days ago | exit 1, 2, 3, 5 or 6 (`mirror_failed`); Disabled without a pause; last run over 15 days ago; task missing |
| Server task list | a task in `\XB\` that is neither a job's task nor in `known_tasks` (`unknown_task`, with a count) | none |
| Health checker | yesterday's own record says its POST failed (`previous_send_failed`) | the 08:15 dead-man found no report today (`checker_missing`) |
| Drive folder | none | the Drive report folder is not reachable on the server PC (`folder_missing`) |
| Member sync daily summary | `xb_alert_email_log` could not be read (`log_read_failed`) | no `daily_summary` row for today (`summary_missing`); only `send_failed` rows today (`summary_send_failed`) |
| n8n workflows, SIMBA intake | error runs in the last 24 h for workflows that keep them (`error_runs`); the n8n API could not be read (`n8n_api_failed`); a workflow ID still a placeholder (`not_configured`) | an expected workflow inactive (`workflow_inactive`) or not found (`workflow_missing`) |

- Task Scheduler results 0x41300-0x41303 (ready, running, disabled, not run yet) are states, not failed runs. A task that has never run counts as "last run too old".
- If Task Scheduler cannot be read at all, the task jobs and the server task list get WARN `task_query_failed`.
- **VM or Drive down:** when the stock and AR/AP exports are both stale by more than 3 h with ages within 90 min of each other, the email adds one line: "Together: the AC2 VM or its Google Drive sync looks down". It is a note, not an extra job.
- **Repeats:** a problem that lasts is mailed every morning until it clears. Each line says "since <date>": the checker works it out from its own last 14 records, n8n from `xb_health_runs` for the n8n-side jobs.
- The existing real-time alerts (the stock alert, the EnergyGrid monitor) are not replaced and still fire as before.

## Pauses

A planned stop that lasts past 07:30 goes into `pauses` in the settings file:

```json
"pauses": [
  { "job": "ac2_stock_export", "until": "2026-11-20T18:00", "reason_code": "sitting" }
]
```

- `until` is SGT, `YYYY-MM-DDTHH:MM`, at most 7 days after the check (a longer one makes the settings invalid). `reason_code` is one of `sitting`, `maintenance`, `vm_work`, `owner_request`.
- While a pause runs, the job is listed once as "Paused: <job> until <date> <time> (<reason>)". No WARN or FAIL.
- When a pause has run out (within the last 7 days) and the job is still not OK: FAIL `pause_expired`, with the job's own reasons after it.
- An n8n-judged job (member sync summary, n8n workflows, SIMBA intake) can be paused the same way; after the pause it is judged normally (no `pause_expired`).
- **Who adds a pause:** the Controller (Work Claude on the server PC). It adds one whenever it hands the owner a VM or server sitting prompt that disables a task past 07:30, and adds the matching removal to that prompt's rollback.
- Short pauses need nothing: the check runs once, at 07:30, so an evening sitting that re-enables its task before morning never alarms.

## Who watches the watcher

1. **Monday heartbeat.** Every Monday the 07:30 run sends an email even when all is well. It lists the jobs watched, the jobs not watched, and how many of the last 7 daily checks ran (from `xb_health_runs`). If Monday already has a WARN or FAIL, that email is the heartbeat and carries the same week line.
2. **Dead-man, 08:15.** No `check` row for today in `xb_health_runs`: `[XB health] FAIL 1 jobs <date>`. It covers a dead checker task, a sleeping PC or a broken webhook while n8n is up.
3. **Gmail down:** the Telegram fallback line (above).
4. **n8n or the server PC down:** nothing can send, and the dead-man cannot run either. The checker records `send_status: failed` and reports `previous_send_failed` the next day. Detection is the missing Monday email: at worst 7 days. This is the accepted limit (owner, 18 Nov 2026, Q4).
5. **VM down:** the stock and AR/AP exports go stale and the member-sync summary is missing: FAIL lines plus the "AC2 VM or Google Drive sync looks down" line, all sent from the server.

## Event (checker to n8n)

`xb.daily_health.v1`, closed. Keys exactly `schema`, `date` (SGT), `checked_at` (`YYYY-MM-DDTHH:MM+08:00`, at most 60 min from n8n's clock), `jobs`, `notes`.

- `jobs`: all 13 jobs, in the order of the Jobs table, each with exactly `job`, `state` (`on`, `off`, `not_installed`, `paused`), `verdict` (`OK`, `WARN`, `FAIL`; `N8N` for an n8n-judged job that is `on`; `NA` when not `on`), `reasons` (0-6 closed codes of that job; the verdict must equal their worst level), `since` (a date for WARN and FAIL, else `null`), `until` and `pause_reason` (paused jobs only), `age_min`, `exit`, `count` (whole numbers or `null`).
- `notes`: `[]` or `["vm_or_drive_down"]`.

No task name, path, host name, message, address or business value is in the event. n8n refuses any other key or value.

## Settings file

`config/daily_health.local.json` on the server PC, copied from the example. Every `REPLACE-...` value is set privately and never committed; the checker refuses a settings file that still holds one.

| Key | Value |
| --- | --- |
| `record_dir` | Private local folder for `health_<date>.json` records. |
| `drive_root` | The Drive report folder on the server PC (the `drive_folder` job). |
| `stock_folder`, `arap_folder` | The Drive folders that hold each export's `last_run_status.json` and `latest_manifest.json`. |
| `mirror_log_dir` | The Drive mirror's `log_dir` (its `last_run_status.json`). |
| `task_path` | `\XB\`. |
| `tasks` | The Task Scheduler names of `energygrid_daily_bills`, `energygrid_retention`, `drive_mirror` and `checker`. |
| `known_tasks` | Other task names in `\XB\` that are known and not watched (not `unknown_task`). |
| `expected` | `on`, `off` or `not_installed` for each of the 13 jobs. |
| `thresholds` | Optional overrides, minutes (see the example for names and defaults). |
| `pauses` | Planned pauses (above). |
| `send` | `enabled`; `webhook_url` (`http://127.0.0.1:<port>/webhook/<path>` only: the request carries the secret, so it never leaves the PC); `auth_header_name` (the Header Auth credential's header name); `secret_credential_target` (the Windows Credential Manager generic credential that holds the secret); `timeout_seconds` (5-10). |

Commands (read-only):

```powershell
python scripts\xb_daily_health.py check-settings --config config\daily_health.local.json
python scripts\xb_daily_health.py run --config config\daily_health.local.json --no-send
```

`--no-send` judges and writes today's record (send status `not_sent`) and prints one codes-only summary line; it posts nothing. `run` exits 0 in every case except broken settings (2): a record that cannot be written prints `{"ok": false, "code": "record_write_failed"}`, and any unexpected error prints `{"ok": false, "code": "internal_error"}` with no detail. The 08:15 dead-man then reports the missing check.

## n8n Data Table `xb_health_runs`

One row per check and per dead-man email. n8n adds `id`, `createdAt` and `updatedAt` itself.

| Column | Type | Value |
| --- | --- | --- |
| `date` | string | The check date, SGT, `YYYY-MM-DD`. |
| `kind` | string | `check` (the 07:30 event) or `deadman` (the 08:15 email). |
| `verdict` | string | `OK`, `WARN` or `FAIL`. |
| `watched` | number | Jobs `on` or paused. |
| `problems` | number | Jobs WARN or FAIL. |
| `fail_jobs`, `warn_jobs` | string | Comma-separated job ids (used for "since" on the n8n-side jobs). |
| `email` | string | `sent`, `not_due` (OK and not Monday), `send_failed`, `rejected`, `error`, `call_failed` (the sender call itself failed) or `unknown`. |
| `code` | string | The sender's closed code (for example `gmail_error`), or empty. |
| `logged_at` | string | ISO-8601 UTC. |

The table grows by one or two rows a day. No clean-up is built.

## Off switch

```powershell
Disable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-DailyHealth'
```

Then deactivate `XB Daily Health` in n8n (or the dead-man mails a FAIL at 08:15 every day). Nothing else is touched. To remove the task for a re-install: `Unregister-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-DailyHealth'` (owner OK).

## Install

1. Copy `config\daily_health.example.json` to `config\daily_health.local.json` and set the private values.
2. Create the Windows Credential Manager generic credential named in `secret_credential_target`, holding the same secret as the n8n Header Auth credential (by hand on the server PC; never in a file, a prompt or a log).
3. `check-settings`, then `run --no-send` once (read-only).
4. Dry run of the installer (the default; nothing is registered):

   ```powershell
   powershell -NoProfile -File scripts\install_daily_health_task.ps1 -PythonExe "<full path to python.exe>" -UserId "<the EnergyGrid task user>"
   ```

5. With the owner's OK, the same command with `-Register`. Settings: daily 07:30, `PT5M`, `IgnoreNew`, `StartWhenAvailable`, Interactive logon, `RunLevel Limited`.

## Live steps (H1)

Each step needs its own owner OK; server PC and n8n only, nothing on the VM. All n8n calls use the local API `http://127.0.0.1:5678/api/v1`. Prerequisite: `XB Ops Alert Email` and `xb_alert_email_log` are live (#288 slice 1).

| # | Step | OK words | Rollback |
| --- | --- | --- | --- |
| L1 | Re-import the updated `XB Ops Alert Email` (system `HEALTH`) from the merged export, keeping its private values (local `.tmp` copy, read back). Before the update, back up the live workflow and diff it node by node against the pre-PR export: any drift beyond the known private values (recipient, table ID, Gmail credential binding) stops the step. | "HEALTH SENDER UPDATE" | Re-apply the backup taken from the live read; read back. |
| L2 | Create Data Table `xb_health_runs` (columns above); create a new n8n API key and its `n8nApi` credential; create a new Header Auth credential (and the matching Credential Manager entry). | "HEALTH CREDENTIALS" | Delete the empty `xb_health_runs` table; delete the new `n8nApi` and Header Auth credentials; revoke the new API key; the owner deletes the Credential Manager entry. |
| L3 | Import `XB Daily Health` inactive; set the webhook path, the `WATCH` workflow IDs, both table IDs, the sender workflow ID and the Telegram chat; bind Header Auth, `n8nApi` and the existing Telegram credential; read the workflow back, including `List Workflows` and `List Error Runs` in the editor. Also one read-only GET of `/api/v1/workflows` on the local API: published workflows must report `active: true` on this n8n version (the judge reads that field; otherwise every watched workflow shows FAIL `workflow_inactive`). | "HEALTH N8N IMPORT" | Archive or delete the inactive `XB Daily Health` (never active; nothing calls it). |
| L4 | Server: write the private settings, then `check-settings` and `run --no-send` once. | none (read-only) | Delete `config\daily_health.local.json` and the one `health_<date>.json` record if wanted. |
| L5 | One test send: activate `XB Daily Health`, then run the checker once by hand. An all-OK weekday sends no email by design, so run it on a Monday (heartbeat) or expect only an `xb_health_runs` row. Read back through the local API that `XB Daily Health` now reports `active: true`. Activation also arms the 08:15 dead-man: do L6 in the same sitting, or deactivate again. | "HEALTH TEST EMAIL" | Deactivate `XB Daily Health`. The one test email cannot be recalled. |
| L6 | Register `\XB\XB-DailyHealth` (installer dry run first). | "HEALTH TASK" | `Disable-ScheduledTask` (off switch), or `Unregister-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-DailyHealth'` with the owner's OK. |
| L7 | Observe 7 mornings and the first Monday heartbeat; then retire the desktop reminder for the manual #155 check. | none (records) | None (records only). |

## Privacy

- Event and records: job ids, closed codes, whole numbers and dates only. No names, phones, emails, member numbers, customer ids, paths, host names, item codes or amounts.
- Email lines are written in n8n from fixed templates; the only other text is the fixed workflow labels in the `WATCH` settings of `Judge And Build Email`.
- Credentials: Gmail stays in `XB Ops Alert Email` (reused, never copied; the health workflow has no Gmail node). Telegram is the existing credential, bound in n8n only. The n8n API key lives only in an n8n `n8nApi` credential. The webhook secret is a Header Auth credential in n8n and a Credential Manager entry on the server PC.
- `XB Daily Health` saves no executions (the webhook item holds the Header Auth secret).
- The recipient is set only in `XB Ops Alert Email`, privately, at #288 slice 1.
