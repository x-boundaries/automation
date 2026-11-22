# Daily health check (#325)

One email when any scheduled job fails silently. A read-only checker on the server PC runs at 07:30 SGT, judges every watched job OK, WARN or FAIL, and posts one codes-only event on loopback to the n8n workflow `XB Daily Health`. That workflow adds the n8n facts and mails the owner through the shared sender `XB Ops Alert Email`. No email when all is well, except the Monday heartbeat and the morning after a health email that could not be sent.

**Email only** (#288, owner 19 Nov 2026: "remove all telegram notifications"). Nothing in the health check uses Telegram.

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

## When the owner forwards a health email

Owner rule (21 Nov 2026): the owner is emailed only about errors to fix, and the owner's only action is to tell Claude. So every WARN and FAIL line ends with the same fixed `Next: forward this email to Claude.` (#288, 22 Nov 2026). The line keeps its job label, reason text, codes and counts. No line sends the owner to n8n, the server PC, the VM or a runbook. The 08:15 dead-man FAIL and the `Build Broken-Check Email` FAIL end the same way.

Claude's first look per job (these were the email's old `Next:` texts):

| Job id | First look |
| --- | --- |
| `member_sync_summary` | Task `XB-MemberSync-DailySummary` on the VM ([member_sync_phase1.md](autocount2-automation/member_sync_phase1.md), "Daily summary email"). |
| `member_tags` | The member sync daily summary. |
| `ac2_stock_export` | The AC2 VM is on and signed in, then Task Scheduler "XB AC2 Hourly Stock Balance" ([stock_balance_hourly_runbook.md](autocount2-automation/stock_balance_hourly_runbook.md), "Failure classes"). |
| `stock_alert_route` | The stock alert workflow in n8n and its webhook secret on the VM ([stock_balance_hourly_runbook.md](autocount2-automation/stock_balance_hourly_runbook.md), "Error alerts"). |
| `arap_export` | The AC2 VM is on, then its AR/AP aging task ([arap_aging_runbook.md](autocount2-automation/arap_aging_runbook.md)). |
| `arap_alert_route` | The AR/AP alert workflow in n8n and its webhook secret on the VM ([arap_aging_runbook.md](autocount2-automation/arap_aging_runbook.md), "A3: error alerts"). |
| `energygrid_daily_bills` | Its task in Task Scheduler on the server PC (EnergyGrid runbook; its own email monitor still runs). |
| `energygrid_retention` | Its task and retention report on the server PC (EnergyGrid runbook). |
| `drive_mirror` | Nothing if the next Sunday run passes; otherwise [drive_mirror_runbook.md](drive_mirror_runbook.md). |
| `n8n_workflows` | The workflows named, through the local n8n API (this runbook, section "Rules"). For `error_runs`, read the `energygrid_monitor_error_log` rows first: a row with workflow `1` and execution `231` is n8n's editor test sample, not a real failure (the #288 editor-sample fix stops writing it once it is live). |
| `simba_intake` | The SIMBA invoice workflows in n8n ([simba_invoice_intake.md](simba_invoice_intake.md)). |
| `server_tasks` | Add the task to the daily health settings or remove it (section "Jobs"). |
| `checker` | The server PC is on, and Task Scheduler "XB-DailyHealth" (section "How it works"). |
| `drive_folder` | Google Drive for desktop is running and signed in on the server PC. |

## How it works

1. **07:30, server PC.** Task `\XB\XB-DailyHealth` runs `xb_daily_health.py run`. It reads:
   - Task Scheduler folder `\XB\` on the server PC (`Get-ScheduledTask` and `Get-ScheduledTaskInfo` only);
   - the AC2 VM's `last_run_status.json` and `latest_manifest.json` for the hourly stock export and the AR/AP aging export, which already reach this PC through Google Drive for desktop;
   - the Drive mirror's `last_run_status.json` in its log folder;
   - its own records of the last 14 days.
2. It judges each job (section "Rules"), writes `health_<date>.json` (codes, counts and dates only) into `record_dir`, and POSTs one `xb.daily_health.v1` event to `http://127.0.0.1:5678/webhook/<private path>` with the Header Auth secret from Windows Credential Manager. One POST, 10 s timeout, no retry, no proxy, no redirect. It always exits 0; only broken settings exit 2.
3. **n8n, `XB Daily Health`.** Header Auth on the webhook; a request carrying Cloudflare or proxy headers gets 404; the Code node checks the closed schema (anything else: 400 with a `HEALTH_*` code) and answers 200 `accepted`. Then it reads, with the n8n API credential, the workflow list and the error runs of the last 24 h; reads today's `MEMBER-SYNC` `daily_summary` row and the `send_failed` rows of every system from the last 24 h in `xb_alert_email_log`; reads the run monitor's failure rows in `energygrid_monitor_error_log`; reads the `KIT-RETAG` `ERROR` rows from the last 24 h in `xb_alert_email_log`; reads recent `xb_health_runs` rows; judges the n8n-side jobs; writes every line from fixed English templates; calls `XB Ops Alert Email` when the result is not OK, on a Monday, or when yesterday's health email could not be sent; and writes one `xb_health_runs` row.
4. **Email not sent** (`send_failed` when Gmail is down or the credential expired; also `rejected`, `error`, `call_failed` or an unknown answer from the sender): the `xb_health_runs` row records the outcome and the sender's code (for example `gmail_error` or `OPS_ALERT_SHORT`). Nothing else happens at 07:30.
5. **08:15, n8n dead-man and retry.**
   - No `check` row for today in `xb_health_runs`: n8n mails `[XB health] FAIL 1 jobs <date>`: "the health checker did not report today".
   - Today's due email was not sent (every `check` row today has one of the failed outcomes above) and there is no `retry` row yet: n8n sends it **once** more, `[XB health] <verdict> <n> jobs <date>`, rebuilt from the row: `The 07:30 health email could not be sent (send_failed). This is the 08:15 retry.`, the FAIL and WARN job labels (each line ending `Next: forward this email to Claude.`), and `Full detail: health_<date>.json on the server PC.` It logs a `retry` row. Never for `sent` or `not_due` (an OK weekday), and never twice.
6. **Next morning.** If yesterday's email went out neither at 07:30 nor at the 08:15 retry, today's email is sent even on an OK weekday, with the line `Yesterday's health email could not be sent (send_failed at 07:30 and 08:15).` (or `... at 07:30; the 08:15 retry did not run).`). It is not retried again.
7. **Health check broken.** If `Judge And Build Email` or `Decide Dead-Man` itself fails, `Build Broken-Check Email` (fixed values) sends `[XB health] FAIL 1 jobs <date>`: "the health check could not be built today" and "n8n XB Daily Health failed before the email". Its row has `kind: check` from the 07:30 path (so the dead-man does not mail twice) and `kind: deadman` from the 08:15 path. Only a Code step is broken, not Gmail, so this email normally goes out.

Python never sends email itself: that would copy a credential out of n8n.

## Jobs

`expected` in the settings file says, per job, `on` (checked), `off` (on purpose, not checked) or `not_installed` (not live yet). Jobs that are not `on` are listed under "Not watched" in every email, so a forgotten switch-on shows up on the next Monday at the latest.

| Job id | Line label | Judged by | Starts as |
| --- | --- | --- | --- |
| `member_sync_summary` | Member sync daily summary | n8n: today's `daily_summary` row in `xb_alert_email_log` (`sent`, or `clean_not_sent` for a CLEAN morning that was not emailed, #288 slice 3 errors only) | `not_installed` |
| `member_tags` | Member tags | the #288 summary (its own email); OK when `on` | `not_installed` |
| `ac2_stock_export` | AC2 hourly stock export | checker: stock status pair on Drive | `on` |
| `stock_alert_route` | Stock alert route | checker: `alert_status` in the stock status file | `on` |
| `arap_export` | AR/AP export | checker: AR/AP status pair on Drive | `on` |
| `arap_alert_route` | AR/AP alert route | checker: `alert_status` in the AR/AP status file (#321 L3) | `on` |
| `energygrid_daily_bills` | EnergyGrid daily bills | checker: its server task (liveness only) | `on` |
| `energygrid_retention` | EnergyGrid retention | checker: its server task (liveness only) | `on` |
| `drive_mirror` | Drive mirror | checker: its server task and status file | `on` |
| `n8n_workflows` | n8n workflows | n8n: expected workflows active, error runs, alert emails not sent | `on` |
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
| AC2 hourly stock export | last run over 70 min ago (`last_run_old`); last run failed but the last good `latest_manifest.json` is under 3 h old (`run_failed`); `delivery_status` `verify_failed` or `copy_failed` (`delivery_failed`); `daily_close_status` `failed` (`daily_close_failed`); `analysis_failure_class` set or `analysis_purchasing_status` `failed` (`analysis_failed`; a purchasing-only failure still publishes the buying files and leaves `analysis_failure_class` empty); run over 7.5 min (`run_slow`) | last run over 3 h ago (`last_run_too_old`); failed and the good manifest over 3 h old or missing (`manifest_too_old`, `manifest_missing`); run over 12 min, 80 % of `PT15M` (`run_too_slow`); status file missing or unreadable (`status_missing`) |
| Stock alert route | `alert_status` `send_failed`, `secret_unavailable` or `error` | none |
| AR/AP alert route | `alert_status` `send_failed`, `secret_unavailable` or `error` (the same values; `suppressed`, the steady state after the one-time AP notice, is fine) | none |
| AR/AP export | last run over 90 min ago; failed with a good manifest under 3 h old; delivery `verify_failed` or `copy_failed` | last run over 3 h ago; good manifest over 3 h old or missing; status file missing |
| EnergyGrid daily bills | last result not 0 (`task_result_nonzero`); last run over 26 h ago | task Disabled without a pause (`task_disabled`); last run over 50 h ago; task missing |
| EnergyGrid retention | exit 20 (`retention_held`) or 64 (`retention_refused`); other non-zero result; last run over 26 h ago | Disabled without a pause; last run over 50 h ago; task missing |
| Drive mirror | exit 4 `FETCH_FAILED` (`mirror_fetch_failed`) or 7 `LOCKED` (`mirror_locked`); status file stuck at `running` while the task is not running (`mirror_stuck_running`); last run over 8 days ago | exit 1, 2, 3, 5 or 6 (`mirror_failed`); Disabled without a pause; last run over 15 days ago; task missing |
| Server task list | a task in `\XB\` that is neither a job's task nor in `known_tasks` (`unknown_task`, with a count) | none |
| Health checker | yesterday's own record says its POST failed (`previous_send_failed`) | the 08:15 dead-man found no report today (`checker_missing`) |
| Drive folder | none | the Drive report folder is not reachable on the server PC (`folder_missing`) |
| Member sync daily summary | `xb_alert_email_log` could not be read (`log_read_failed`) | no `sent` or `clean_not_sent` `daily_summary` row for today (`summary_missing`: the summary did not report, or its row could not be written); only `send_failed` rows today (`summary_send_failed`; a `sent` or `clean_not_sent` row the same day wins: a later POST got through) |
| n8n workflows, SIMBA intake | failed runs in the last 24 h (`error_runs`, with a count): saved error runs for workflows that keep them (SIMBA), and for the EnergyGrid run monitor, which keeps none, every row of `energygrid_monitor_error_log` (at most 250 are read, so `250` means 250 or more); that table could not be read (`error_log_read_failed`); the n8n API could not be read (`n8n_api_failed`); a workflow ID still a placeholder (`not_configured`; not for the kit re-tagger before its live step) | an expected workflow inactive (`workflow_inactive`) or not found (`workflow_missing`), including `EnergyGrid - Monitor Error Log` and, once bound, the kit re-tagger; the run monitor no longer linked to its error log (`error_log_unlinked`: its `errorWorkflow` is not the error log or `saveDataErrorExecution` is not `none`; checked only once the error log's `WATCH` ID is set) |
| n8n workflows only | `send_failed` rows of any system in `xb_alert_email_log` in the 24 h before the check (`alert_email_failed`, with a count): a real-time alert (stock, AR/AP, EnergyGrid) or a health email that could not be sent shows the next morning; `KIT-RETAG` `ERROR` rows in the same window (`kit_retag_errors`, with a count; each kit re-tagger error path sends exactly one); the log could not be read (`alert_log_read_failed`) | none |

- Task Scheduler results 0x41300-0x41303 (ready, running, disabled, not run yet) are states, not failed runs. A task that has never run counts as "last run too old".
- If Task Scheduler cannot be read at all, the task jobs and the server task list get WARN `task_query_failed`.
- **VM or Drive down:** when the stock and AR/AP exports are both stale by more than 3 h with ages within 90 min of each other, the email adds one line: "Together: the AC2 VM or its Google Drive sync looks down". It is a note, not an extra job.
- **Repeats:** a problem that lasts is mailed every morning until it clears. Each line says "since <date>": the checker works it out from its own last 14 records, n8n from `xb_health_runs` for the n8n-side jobs.
- The existing real-time alerts (the stock alert, the AR/AP alert, the EnergyGrid monitor) are not replaced and still fire as before, by email.

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
3. **Gmail or the sender down for a short while:** the 08:15 retry, then the next-morning line ("How it works", items 5 and 6). A failed real-time alert shows the next morning as `alert_email_failed`.
4. **Blind spot (email only, accepted):** every email leaves through one n8n workflow (`XB Ops Alert Email`), one Gmail credential, n8n and the server PC. If any of these **stays** broken (OAuth consent revoked, token expired, the sender broken, a Google outage longer than a day, n8n down, the PC off), **nothing arrives at all**: no health email, no stock, AR/AP, EnergyGrid or member alert, and no retry. The only signal is the **missing Monday `[XB health]` email**: up to 7 days (about 8 if the failure starts just after a Monday email). The owner kept the Monday-only heartbeat (#288 Q3 = (i), 20 Nov 2026; first set 18 Nov 2026, #325 Q4): the OK email is not sent daily. Before #288, Telegram covered the Gmail-only case; that cover is gone by the owner's choice.
5. **VM down:** the stock and AR/AP exports go stale and the member-sync summary is missing: FAIL lines plus the "AC2 VM or Google Drive sync looks down" line, all sent from the server.

## Event (checker to n8n)

`xb.daily_health.v1`, closed. Keys exactly `schema`, `date` (SGT), `checked_at` (`YYYY-MM-DDTHH:MM+08:00`, at most 60 min from n8n's clock), `jobs`, `notes`.

- `jobs`: all 14 jobs, in the order of the Jobs table, each with exactly `job`, `state` (`on`, `off`, `not_installed`, `paused`), `verdict` (`OK`, `WARN`, `FAIL`; `N8N` for an n8n-judged job that is `on`; `NA` when not `on`), `reasons` (0-6 closed codes of that job; the verdict must equal their worst level), `since` (a date for WARN and FAIL, else `null`), `until` and `pause_reason` (paused jobs only), `age_min`, `exit`, `count` (whole numbers or `null`).
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
| `expected` | `on`, `off` or `not_installed` for each of the 14 jobs. |
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

One row per check, per dead-man email and per 08:15 retry. n8n adds `id`, `createdAt` and `updatedAt` itself.

| Column | Type | Value |
| --- | --- | --- |
| `date` | string | The check date, SGT, `YYYY-MM-DD`. |
| `kind` | string | `check` (the 07:30 event, or the broken-check email from that path), `deadman` (the 08:15 email) or `retry` (the 08:15 retry of a failed 07:30 email). |
| `verdict` | string | `OK`, `WARN` or `FAIL`. |
| `watched` | number | Jobs `on` or paused. |
| `problems` | number | Jobs WARN or FAIL. |
| `fail_jobs`, `warn_jobs` | string | Comma-separated job ids (used for "since" on the n8n-side jobs). |
| `email` | string | `sent`, `not_due` (OK and not Monday), `send_failed`, `rejected`, `error`, `call_failed` (the sender call itself failed) or `unknown`. |
| `code` | string | The sender's closed code (for example `gmail_error`), or empty. |
| `logged_at` | string | ISO-8601 UTC. |

The table grows by one to three rows a day. No clean-up is built.

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
| L3 | Import `XB Daily Health` inactive; set the webhook path, the `WATCH` workflow IDs, both table IDs and the sender workflow ID; bind Header Auth and `n8nApi`; read the workflow back, including `List Workflows` and `List Error Runs` in the editor. Also one read-only GET of `/api/v1/workflows` on the local API: published workflows must report `active: true` on this n8n version (the judge reads that field; otherwise every watched workflow shows FAIL `workflow_inactive`). | "HEALTH N8N IMPORT" | Archive or delete the inactive `XB Daily Health` (never active; nothing calls it). |
| L4 | Server: write the private settings, then `check-settings` and `run --no-send` once. | none (read-only) | Delete `config\daily_health.local.json` and the one `health_<date>.json` record if wanted. |
| L5 | One test send: activate `XB Daily Health`, then run the checker once by hand. An all-OK weekday sends no email by design, so run it on a Monday (heartbeat) or expect only an `xb_health_runs` row. Read back through the local API that `XB Daily Health` now reports `active: true`. Activation also arms the 08:15 dead-man: do L6 in the same sitting, or deactivate again. | "HEALTH TEST EMAIL" | Deactivate `XB Daily Health`. The one test email cannot be recalled. |
| L6 | Register `\XB\XB-DailyHealth` (installer dry run first). | "HEALTH TASK" | `Disable-ScheduledTask` (off switch), or `Unregister-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-DailyHealth'` with the owner's OK. |
| L7 | Observe 7 mornings and the first Monday heartbeat; then retire the desktop reminder for the manual #155 check. | none (records) | None (records only). |

### Email-only update of the live workflow (#288 step H1)

The live `XB Daily Health` was installed with two Telegram nodes; this export (#288 T2 and #321 L3) replaces them. It also changes the event: the checker now sends **14** jobs (the new `arap_alert_route`), and `Validate Health Event` accepts only 14. So the server checker, its settings and the n8n workflow change **in one sitting**, between 08:15 and the next 07:30. Otherwise the old workflow refuses the new event (`HEALTH_JOBS`, 400) or the new checker refuses the old settings (`settings_expected_keys`, exit 2); either way no `check` row is written and the 08:15 dead-man mails a FAIL.

| # | Step | OK words | Rollback |
| --- | --- | --- | --- |
| H1 | Back up the live `XB Daily Health` and diff it node by node against the pre-PR export: drift beyond the known private values stops the step. Update it in place from the merged export through the local API, keeping every private value (local `.tmp` copy): the webhook path, both table IDs (`Read Failed Alert Emails` uses the alert log table), the sender workflow ID and the `WATCH` IDs, including the new `REPLACE_WITH_ARAP_ALERT_WORKFLOW_ID` (`AC2 ARAP Export - Error Alert`) and the member sync summary. The Telegram nodes and their binding go. Publish and read back. In the same sitting: bring the clone the task runs from to the merged main, add `"arap_alert_route": "on"` to `expected` in `config\daily_health.local.json`, then `check-settings` and `run --no-send`. | "HEALTH NO TELEGRAM" | Re-apply the backup and return the clone and the settings to the previous version, together. |
| H2 | Observe the next Monday heartbeat, and a `retry` row only if a failure happens. | none (records) | None. |

### Run monitor error log (#288 option (c), owner 20 Nov 2026)

The EnergyGrid run monitor saves no failed executions any more (a rejected report's saved execution would keep its webhook token). Its error workflow `EnergyGrid - Monitor Error Log` writes one closed-code row per failed run to the Data Table `energygrid_monitor_error_log` (`workflow_id`, `execution_id`, `failed_node`, `code`, `mode`, `failed_at`; design and source proof in the [monitor package README](../n8n-workflows/energygrid_local_failure_monitor/README.md#failed-runs-the-error-log-288-option-c-owner-20-nov-2026)). This export reads that table in `Read Monitor Error Log` (newest 250 rows, read only) and counts **every** row of the 24 h before the check as `error_runs` (fix1 L1: the table is the monitor's alone, so a row whose `workflow_id` fell back to `unknown` counts too). The read returns at most 250 rows, so a count of **250 means 250 or more** (fix1 L3): a broken 5-minute path writes up to 288 a day; it is still the same WARN. In `WATCH` the monitor is now `errors: false, error_log: true`, and the error log workflow has its own entry (`REPLACE_WITH_ENERGYGRID_MONITOR_ERROR_LOG_WORKFLOW_ID`, `is_error_log: true`), so an inactive or missing error log workflow is a FAIL. Only the count reaches the email, never a code or node name.

**Link check (fix1 L2).** A re-import from an export or an editor change can clear the monitor's `errorWorkflow` or turn failed-run saving back on; then failures would leave no row and this check would stay OK. So, from the `List Workflows` answer it already reads (the n8n API lists each workflow's `settings`), the judge checks that the monitor has `errorWorkflow` = the error log's `WATCH` ID and `saveDataErrorExecution` = `none`. Otherwise: FAIL `n8n workflows: EnergyGrid monitor is no longer linked to its error log; ask Claude to check`. The check only runs once the error log's `WATCH` ID is set (not `''`, not its placeholder), so it cannot fire before M5; it is skipped when the monitor itself is missing (that is `workflow_missing`) or the API could not be read. Fix: re-apply M4 (the monitor's two settings), then read back.

### Kit re-tagger (#341 fix1 LATER, folded in here)

The kit re-tagger saves no executions (its token is in item data), so n8n cannot count its failures. Two checks cover it under the n8n workflows job:

- `WATCH` entry `{ id: 'REPLACE_WITH_KIT_RETAGGER_WORKFLOW_ID', label: 'kit re-tagger', errors: false, bind_later: true }`. While the ID is still its placeholder the entry is skipped, like `''`: no `not_configured` WARN and no `workflow_missing`. The owner binds it at the kits live step (the kit re-tagger runbook, "Daily health"); from then on an inactive or missing kit re-tagger is a FAIL.
- `Read Kit Re-tagger Errors` reads the `KIT-RETAG` `ERROR` rows of `xb_alert_email_log` (newest 250, read only; the same table ID as `Read Failed Alert Emails`). Each of its error paths sends exactly one such alert, so the rows of the 24 h before the check are a count of its errors: WARN `n8n workflows: N kit re-tagger error alert(s) in the last 24 h (see xb_alert_email_log)`. A failed read is the existing `alert_log_read_failed` WARN. Only the count reaches the email. This needs no ID and stays at 0 until the kit re-tagger runs.

Live order: the monitor package's steps M1-M4 first (table, error workflow, synthetic test on a copy, monitor settings), then this step. Each needs its own owner OK.

| # | Step | OK words | Rollback |
| --- | --- | --- | --- |
| M5 | Back up the live `XB Daily Health` and diff it node by node against the pre-PR export: drift beyond the known private values stops the step. Update it in place from the merged export through the local API, keeping every private value (webhook path, table IDs, sender workflow ID, `WATCH` IDs), and set the new values: `Read Monitor Error Log` -> the `energygrid_monitor_error_log` table ID; `Read Kit Re-tagger Errors` -> the `xb_alert_email_log` table ID (the same as `Read Failed Alert Emails`); `WATCH` -> the error log workflow ID. Leave `REPLACE_WITH_KIT_RETAGGER_WORKFLOW_ID` as it is unless the kit re-tagger is already live. Read back that the live monitor already has the M4 settings, or the first check will FAIL `error_log_unlinked`. Publish and read back. The checker event is unchanged, so the server checker and its settings stay as they are. | "HEALTH ERROR LOG" | Re-apply the backup; read back. |

## Privacy

- Event and records: job ids, closed codes, whole numbers and dates only. No names, phones, emails, member numbers, customer ids, paths, host names, item codes or amounts.
- Email lines are written in n8n from fixed templates; the only other text is the fixed workflow labels in the `WATCH` settings of `Judge And Build Email`.
- Credentials: Gmail stays in `XB Ops Alert Email` (reused, never copied; the health workflow has no Gmail node). No Telegram node or credential. The n8n API key lives only in an n8n `n8nApi` credential. The webhook secret is a Header Auth credential in n8n and a Credential Manager entry on the server PC.
- `XB Daily Health` saves no executions (the webhook item holds the Header Auth secret).
- The recipient is set only in `XB Ops Alert Email`, privately, at #288 slice 1.
