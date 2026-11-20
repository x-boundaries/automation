# AC2 AR/AP aging export (latest snapshot only) runbook

Issue: #321 (slice A1, offline build). Script: `scripts/autocount_arap_aging.py`. Settings example:
`config/autocount_arap_aging.example.json`. Owner-run live check:
`sql/autocount2/arap_aging_live_check.readonly.template.sql`. Tests:
`tests/test_autocount_arap_aging.py`. Fake feed for the tracker build (B1):
`tests/fixtures/arap_dashboard_feed.fake.fixture` (JSON content; the repo allows no `*.json` file
under `tests/`).

Status: A1 (exporter) merged; its first VM run passed on 18 Nov 2026, with the Drive folder and
`delivery_root` set. A2 (the Task Scheduler installer `scripts/install_autocount_arap_aging_task.ps1`,
see "A2: hourly task") merged, and the task was registered on the VM on 18 Nov 2026. V3 passed
(`reconciled_on` 2026-11-18). Error alerts (A3) are built offline, off by default; their live
steps are in "A3: error alerts". See "What happens next".

## What it does

Once an hour (at minute `schedule.run_minute`, default 30) the job reads AutoCount 2's debtor (AR)
and creditor (AP) documents, their knock-offs and the debtor/creditor masters **read-only**, and
replaces one "latest" set of files. Only the latest picture is kept: no hourly folders, no daily
files, no history and no retention or prune settings (owner, 17 Nov 2026: "extract and replace
every hr").

- **Extract everything.** Every open document is exported with its document date, due date, type,
  currency, document and local amounts, and both aging bases. Filters (post-dated, zero balance,
  online-shop customer, start dates) are flags or tracker-page settings, never export filters.
- **Start dates live in the tracker page**, not here (owner, 17 Nov 2026: "actually make it a page
  thing. we just take in all info for exporter to make it robust"). Every total in the feed covers
  every open item; the page computes its own in-scope totals from the item rows.
- AutoCount is never written. The job only reads, checks and copies files.

## Read path and safety

- Fixed `SELECT` texts only, no parameters, no comments, no `;`. Their SHA-256 hashes are in the
  manifest (`source.queries_sha256`) and pinned in the tests, so any change is reviewed.
- One connection per attempt, `READ COMMITTED`, `SET LOCK_TIMEOUT` (`read.lock_timeout_ms`), query
  timeout (`read.query_timeout_seconds`), no `NOLOCK`.
- The identity is checked on every connection before any business query (the stock exporter's
  `check_identity`): the `sa` login is always refused; sysadmin or db_owner only with
  `identity.allow_privileged_identity: true`; `DB_NAME()` must equal `source.expected_database`; the
  `AUTOCOUNT_EXPECTED_SQL_*` environment values must match.
- **Stable read.** All reads sit inside one `dbo.ChangeCount` window over `ARInvoice`, `ARDN`,
  `ARCN`, `ARPayment`, `ARDeposit`, `ARRefund`, `ARAPContra`, `APInvoice`, `APDN`, `APCN`,
  `APPayment`, `APDeposit`, `APRefund`, `Debtor`, `Creditor`. If any of those counter rows is missing
  or doubled, the business reads run twice and must give the same row counts and per-column control
  sums (`read_mode: double_read` in the manifest). Otherwise the run retries up to
  `read.max_attempts`, then fails `unstable_snapshot`.

### Sources (from the VM read-only check, 17 Nov 2026)

| Kind | AR | AP | Open when | Amounts |
| --- | --- | --- | --- | --- |
| Invoice | `ARInvoice` (IV) | `APInvoice` (PI) | stored `Outstanding` <> 0 | `NetTotal`, `PaymentAmt`, `Outstanding`; local `LocalNetTotal - LocalPaymentAmt` |
| Debit note (guess, off) | `ARDN` (DN) | `APDN` (SD) | same as invoice | same as invoice |
| Credit note | `ARCN` (CN) | `APCN` (SC) | `NetTotal - KnockOffAmt - RefundAmt` <> 0 | negative; local = unapplied x `CurrencyRate` |
| Payment | `ARPayment` (PM) | `APPayment` (PV) | `PaymentAmt - KnockOffAmt - RefundAmt` <> 0 | negative; local = `LocalUnappliedAmount` |
| Deposit (guess, off) | `ARDeposit` (DP) | `APDeposit` (DP) | stored `Outstanding` <> 0 | negative; local = outstanding x `ToHomeRate` |

Cancelled documents (`Cancelled = 'T'`) are excluded. A flag column holding anything other than
`T` or `F` fails the run (`source_error`).

**Outstanding check (fails closed).** For every non-cancelled invoice (and debit note, when on),
stored `Outstanding` must equal `NetTotal` minus the knock-offs from non-cancelled payments
(`Amount + DiscountAmt`), credit notes and contras. A difference, or a knock-off line pointing at a
document that does not exist, fails the run `outstanding_mismatch` (counts only). The last knock-off
date is the latest paying document's date (AutoCount has no knock-off date column; the VM check found
`GainLossDate` equal to it on every row). Whether `DiscountAmt` reduces the invoice is proven at V3
(the book has no discounts today).

**Unapplied credits.** Unapplied payments, credit notes and deposits are their own rows with
negative amounts (`unapplied: true`, buckets `unapplied`). They are never netted against invoices.

**Guess sources.** Columns the VM check could only guess sit behind `guess_sources.*`, all off by
default: `ar_debit_notes`, `ap_debit_notes`, `ar_deposits`, `ap_deposits`, `payment_home_rate`
(`ToHomeRate` as the payment's `currency_rate`) and `payment_handover_date` (`HandOverDate` as
`cheque_date`). An open row of a source that is off is counted (`guess_rows_skipped`) and warned
about, never exported. Turn one on only after the V3 compare shows it matches the menu.

**Knock-off target types (`knockoff_target_types`).** Each knock-off line names the type of the
document it pays (`KnockOffDocType`). Only `RI` -> AR invoice is proven (the only code in the book).
Any other code fails the run `unknown_knockoff_target` until it is mapped, for example when the
first AP payment or AR debit note is knocked off. Fix: run step 2 of the live-check SQL, confirm
which document type the new code points at, add it (for example `"ap": {"<code>": "PI"}`), then
re-run V1 and V2.

### `ap_payments_recorded`

AutoCount holds supplier invoices only today (no AP payments, credit notes or knock-offs), so every
AP invoice reads unpaid. The feed and the manifest carry `ap_payments_recorded`:

- `false` while the book has no non-cancelled AP payment and no AP knock-off line;
- **turns `true` on the first run that sees a non-cancelled `APPayment` row or any AP knock-off
  line** (`APPaymentKnockOff`, `APCNKnockOff` or `APContraKnockOff`).

When it turns `true`: check the AP knock-off codes (`knockoff_target_types.ap`), then run a V3 compare
of the creditor side before relying on AP figures. The tracker shows a banner while it is `false`.

**In plain words: what happens when staff start keying supplier payments.**

- The first supplier payment that is knocked off against an invoice in AutoCount **stops the whole
  export, AR included**. Every run then fails `unknown_knockoff_target`, publishes nothing, and the
  AR/AP page keeps showing the last good hour (it goes stale).
- It stays stopped until a short VM step, **with the owner's OK**, maps the one new knock-off code
  in `knockoff_target_types.ap`:
  1. VM Claude runs step 2 of the live-check SQL (read-only) and sees which code the new AP
     knock-off lines use.
  2. VM Claude adds that code to the VM's local settings.
  3. VM Claude reruns V1 and V2.
- Because the run that first sees an AP knock-off publishes nothing, **`ap_payments_recorded` is
  published `true` only after that mapping**.
- A supplier payment keyed without a knock-off does not stop the export. It shows as an unapplied
  AP credit and turns `ap_payments_recorded` true on the next run.

## Settings

Copy the example to `config/autocount_arap_aging.local.json` on the VM (gitignored, never committed).
Unknown keys, bad values, a missing `source.expected_database`, or `schedule.frequency` other than
`hourly` fail validation before any read.

| Setting | Default | Meaning |
| --- | --- | --- |
| `schedule.run_minute` | `30` | Minute past each hour (clear of the stock export at :00). |
| `stale_after_minutes` | `70` | `--check-latest` and the feed's stale badge. |
| `source.*`, `identity.*`, `read.*` | as stock | Same meaning and checks as the stock exporter. |
| `output.archive_root` | required | Local folder on the VM (latest set, status, lock). |
| `output.delivery_root` | `null` | Drive for desktop folder; `null` until A2. |
| `aging.boundaries` | `[0,30,60,90,120]` | Bucket edges in days, both bases; labels `current`, `1-30` ... `over_120`. |
| `parties.online_shop_debtor_codes` | `[]` | Debtor codes flagged `online_shop` (real codes only in the VM file). |
| `knockoff_target_types` | `{"ar": {"RI": "IV"}, "ap": {}}` | See above. |
| `guess_sources.*` | all `false` | See above. |
| `reconciled_on` | `null` | Date the last V3 compare passed; the tracker shows "not yet proven" while null. |
| `alert.*` | off | Error alerts (A3): same keys, defaults and checks as the stock export's `alert` block. See "A3: error alerts". |

## Outputs (all replaced together, every successful run)

| File | One row per | Notes |
| --- | --- | --- |
| `ar_open_items_latest.csv`, `ap_open_items_latest.csv` | open document | `as_at_local, side, party_code, party_name, doc_type, doc_no, doc_date, due_date, terms, currency, currency_rate, original_doc, original_local, knocked_off_doc, knocked_off_local, outstanding_doc, outstanding_local, last_knockoff_date, unapplied, post_dated, cheque_date, days_past_due, days_since_doc, bucket_due, bucket_doc, agent, description, ref` |
| `ar_parties_latest.csv`, `ap_parties_latest.csv` | every debtor / creditor (active, inactive, zero balance) | master fields, `active`, `online_shop`, `outstanding`, `overdue`, `unapplied`, `due_<bucket>` and `doc_<bucket>` totals, `docs_open`, `oldest_open_doc_date`, `oldest_overdue_days`, `last_payment_date`, `last_payment_amount` |
| `arap_dashboard_feed.json.gz` | the run | schema `xb.ac2.arap_dashboard_feed` v1 (below) |
| `latest_manifest.json` | the run | status, counts, check counts, file hashes, query hashes, identity flags, warnings; moved last |
| `last_run_status.json` | every run, failed or not | status, failure class, message (counts only), delivery status, next run |

- Amounts are local currency (SGD) unless named `_doc`; credits are negative. `days_past_due` is
  from the due date (document date when no due date); `days_since_doc` from the document date.
  Buckets cover invoices and debit notes; unapplied credits are their own figure, so on both bases
  `sum(buckets) + unapplied = outstanding`.
- `post_dated` is true for a payment dated after the run date (the book has no post-dated cheque
  flag; none exist today). It is a flag, not a filter.
- CSVs go through `safe_csv_row` (formula cells neutralised); amounts stay numeric. UTF-8 with BOM.

### Feed keys (`xb.ac2.arap_dashboard_feed` v1, pinned by the tests)

`schema, schema_version, job, run_id, as_at_local, generated_at, timezone, cadence_minutes,
next_run_local, stale_after_minutes, currency_local, aging{bases, boundaries, bucket_labels},
reconciled_on, ap_payments_recorded, ar, ap, health{status, failure_class, self_checks, counts}`

- `ar` / `ap`: `totals{outstanding, overdue, unapplied, by_bucket_due[], by_bucket_doc[],
  parties_open, docs_open}`, `parties[]{code, name, type, terms, currency, credit_limit,
  flags{active, online_shop}, outstanding, overdue, unapplied, by_bucket_due[], by_bucket_doc[],
  docs_open, oldest_open_doc_date, oldest_overdue_days, last_payment_date, last_payment_amount}`,
  `docs[]{side, party, type, doc_no, doc_date, due_date, currency, original_doc, outstanding_doc,
  outstanding, days_past_due, days_since_doc, bucket_due, bucket_doc, flags{post_dated, unapplied}}`.
- `ap.schedule{week_starts, overdue, this_week, next_week, later}`: each band `{total,
  parties[{code, amount}]}` from AP invoice due dates; weeks run Monday to Sunday, local time.
- Not in the feed: `history`, `closed_since_prev`, start dates.

### Publish rules

- Every file of the set is written to a temp name, re-read and hash-checked before any is moved
  (the stock exporter's `publish_verified_set`); the manifest moves last. The archive set is
  published first; only then the same set goes to `delivery_root`.
- A failed run publishes nothing and leaves the previous set in place (labelled by its own
  `as_at_local`); `last_run_status.json` records the failure in both folders.
- The job never deletes anything in `delivery_root` (only its own temp files on a failed copy).
- Exit codes: `0` success (delivered or no delivery set), `1` failed run or failed delivery;
  `--check-latest`: `0` fresh, `2` stale, `3` missing or invalid.

### Self-checks (fail closed: nothing published, counts only)

`row_identity` (original - knocked off = outstanding, credits negative), `party_totals` (each
party's items add up to its totals, and parties to the side total), `buckets` (both bases),
`ap_schedule` (bands add up to the AP invoice total), `feed_matches_csv` (the rendered CSVs re-read
to the feed totals and counts, to the cent).

## Failure classes

| Class | Meaning | Action |
| --- | --- | --- |
| `config_invalid` / `timezone_unavailable` | settings rejected before any read | fix the local settings; V1 |
| `already_running` / `stale_lock` | another run holds `.ac2_arap.lock` | wait; if stale, check no run is active, then remove the lock |
| `identity_refused` / `credential_unavailable` | identity or database pin check failed | check the task account and settings |
| `lock_timeout` / `unstable_snapshot` | AutoCount busy or data changing on every attempt | next hour normally recovers |
| `source_error` | SQL error or unexpected column value | check the VM; rerun V2 |
| `outstanding_mismatch` | stored outstanding disagrees with knock-offs, or an orphan knock-off | VM Claude investigates read-only (live-check step 3) |
| `unknown_knockoff_target` | a knock-off code not in `knockoff_target_types` | map it (above) |
| `self_check_failed` | a self-check failed, or an item names a party missing from the master | report to the build gate with the counts |
| `latest_publish_failed` | the archive set could not be written | check disk space and folder rights |
| `internal_error` | unexpected error | report with the run id |

Delivery results (`delivery_status`): `not_configured`, `handed_to_drive_sync_folder`,
`verify_failed`, `copy_failed`, `skipped_failed_run`.

## Privacy

- The CSVs and the feed hold customer and supplier names, codes and amounts (confidential financial
  data). They live only in the VM archive folder and the owner's private Drive folder.
- **Drive folder (A2): in the owner's My Drive, not shared with anyone**, like the stock dashboard
  folder. Never "anyone with the link". The tracker (B1) reads it through the owner's own Drive
  connector and is owner-only.
- Console output, `latest_manifest.json` and `last_run_status.json` carry counts, hashes and flags
  only: no party names or codes, no document numbers, no amounts.
- Never emailed, never sent to an API, never committed. Alerts (A3) carry closed codes only (failure
  class, delivery status, condition names and the `ap_payments_recorded` flag).

## Read identity

The same pairs as the stock task: `windows_integrated` with `schedule.logon_type: S4U` (the owner's
Windows account; set `identity.allow_privileged_identity: true` only if that account is sysadmin, as
for stock), or `sql_login` with `Password` and a Credential Manager target. `sa` is never allowed. A
SELECT-only login for both jobs is a later, shared change.

## VM proof steps (each with its own owner OK; VM Claude, read-only)

**V1 - settings check.** Copy the example to `config\autocount_arap_aging.local.json`, set
`source.server`, `source.expected_database` (`AED_XBOUNDARIES`), `output.archive_root` and the real
online-shop debtor code(s); keep `delivery_root: null`. Run:

```
python scripts\autocount_arap_aging.py --config config\autocount_arap_aging.local.json --dry-run-config
```

Pass: `"status": "valid"`, `run_minute` 30. Nothing is read from SQL.

**V2 - one manual run, local only.** With `delivery_root: null`, at a quiet time clear of :00 and
of 22:55-23:45 SGT:

```
python scripts\autocount_arap_aging.py --config config\autocount_arap_aging.local.json
```

Pass: exit 0, `read_mode` `change_count`, the six files plus `last_run_status.json` in the archive
root and nothing else, check counts zero, `ap_payments_recorded` false. Record counts, feed size
and run time in the private mailbox only. Then run `--check-latest` (expect 0).

**V3 - reconciliation with the menus.** Staff run the **Debtor Aging Report** and the
**Creditor Aging Report** with their usual options (as-at today, their aging format and basis,
currency option), and save them (Excel or PDF) privately on the VM. Run the live-check SQL once (V3 OK
covers it). VM Claude compares to the cent, on the matching basis (`due_*` or `doc_*` columns) and
filters (online-shop flag, zero balance, post-dated): per debtor and per creditor total outstanding
and each bucket, unapplied totals, plus 10 sample documents each side. The menu buckets are calendar
months by document date with credits netted in, not the export's 30-day columns: recompute them from
the item rows on the compare side (see "What happens next"). Private mailbox record only.
Pass: set `reconciled_on` to that date. One fix round; if it still does not match, write the r1
A1-b fallback note (AutoCount's own aging classes, nightly) for the Controller.

## A2: hourly task

Task `\XB\AC2 ARAP Aging`, registered by `scripts/install_autocount_arap_aging_task.ps1` (a copy of
the stock installer pattern). Not run by the build.

What the installer registers:

- One trigger: a one-time start at 00:00 + `schedule.run_minute` (default 30) that repeats every hour
  with no end. No daily trigger, so no minute is started twice.
- `MultipleInstances IgnoreNew`, 15 minute limit (the shared lock rules assume it), `RunLevel Limited`,
  start when available.
- The action runs `scripts\autocount_arap_aging.py --config <settings>` with the full `python.exe` path
  given, in the clone the installer sits in.
- The minute and the logon type come from the exporter's own `--dry-run-config`, run with that same
  `python.exe`: `windows_integrated` gives `S4U` (no stored password), `sql_login` gives `Password`
  (Windows password prompt). Any other pair, or invalid settings, is refused before anything is built.
- It refuses if a task with the same name is already in `\XB\` or in the Task Scheduler root. It never
  replaces, moves or removes a task.

**Schedule on the VM.** `\XB\XB-MemberSync` runs at :00/:15/:30/:45; the member core at
:07/:22/:37/:52; stock at :00; ARAP at :30.

The :30 overlap with MemberSync is fine: the two jobs hold separate locks, ARAP only reads (direct
SQL) and fails closed on `lock_timeout` / `unstable_snapshot`, and a failed run keeps the previous
published set. Stock has shared :00 with MemberSync the same way.

### VM steps (VM Claude; elevated Windows PowerShell 5.1, no transcript, from the clone root)

1. **Dry run (reads no data, registers nothing).** Check that the `AUTOCOUNT_EXPECTED_SQL_LOGIN` /
   `_USER` / `_DATABASE` environment values are set for the task's Windows account (user or machine
   level); the job fails closed without them. If it is the account that runs the stock task, they are
   already set.

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autocount_arap_aging_task.ps1 -UserId <Windows account> -ConfigPath <full path to config\autocount_arap_aging.local.json> -PythonExe <full path to python.exe> -DryRun
   ```

   Pass: `Dry run: would register \XB\AC2 ARAP Aging`, `minute 30`, the expected logon type,
   `Trigger: one trigger, starts 00:30, repeats every PT1H, no end`, `Nothing was registered.`
   It checks `pyodbc` and runs `--dry-run-config`; it reads no data and asks for no password.
2. **Register (needs the owner's OK).** Run the same command without `-DryRun`, at a minute clear
   of :30. For the `Password` pair it then prompts for the Windows password.
3. **Check what was registered.**
   `Get-ScheduledTask -TaskPath "\XB\" -TaskName "AC2 ARAP Aging"` shows it `Ready`, and
   `(Get-ScheduledTask -TaskPath "\XB\" -TaskName "AC2 ARAP Aging").Triggers[0].Repetition` shows
   `Interval PT1H` and an empty `Duration`.
4. **First run.** Note `finished_at` in `<archive_root>\last_run_status.json`, then either wait for the
   next :30 or run `Start-ScheduledTask -TaskPath "\XB\" -TaskName "AC2 ARAP Aging"` at once. Wait
   until `Get-ScheduledTaskInfo -TaskPath "\XB\" -TaskName "AC2 ARAP Aging"` shows a new
   `LastRunTime`, `LastTaskResult` 0, and the task is no longer running (at most 15 minutes).
   Pass only if `last_run_status.json` is newer than the noted value and shows `status: success`
   and `delivery_status: handed_to_drive_sync_folder`, in the archive root and in `delivery_root`.
5. **Freshness check.** Use the task's own full `python.exe` path (the `-PythonExe` value), not bare
   `python`: a `PATH` Python without `tzdata` gives a false exit 3.

   ```
   <full path to python.exe> scripts\autocount_arap_aging.py --config config\autocount_arap_aging.local.json --check-latest
   ```

   Pass: exit 0 (fresh). Record the result, counts only, in the private mailbox.

### Off switch

`Disable-ScheduledTask -TaskPath "\XB\" -TaskName "AC2 ARAP Aging"` stops the hourly runs at once
and keeps the task and the last published set (`Enable-ScheduledTask` turns it back on). If step 4
fails, disable the task straight away and report the `failure_class`. Removing the task
(`Unregister-ScheduledTask`) is a separate owner-approved step.

## A3: error alerts

Built offline, **off by default** (`alert.webhook_enabled: false`); the n8n workflow ships inactive.
A copy of the stock export's alert path ([stock runbook, "Error alerts"](stock_balance_hourly_runbook.md#error-alerts-email-through-n8n)):
the same settings, secret handling, de-duplication rule, one bounded POST and `alert_status` values,
reusing the stock exporter's helpers. Every live step below needs its own owner OK.

How it works:

1. At the end of every run, just before `last_run_status.json` is written (still under the lock), the
   exporter works out which conditions the run raises, from the same values it writes to the status.
2. With alerts on, it checks the shared secret is readable (every run; nothing is sent for that) and,
   if the de-duplication rule says so, POSTs **one** sanitized `xb.ac2.arap_alert.v1` event to
   `alert.webhook_url`: one attempt, `alert.timeout_seconds` per socket operation, no proxy, no
   redirect, the secret in the `alert.auth_header_name` header, `User-Agent: xb-ac2-arap-alert/1`.
   Only the answer's status is used.
3. The n8n workflow `AC2 ARAP Export - Error Alert`
   ([`n8n-workflows/ac2_arap_alert.workflow.json`](../../n8n-workflows/ac2_arap_alert.workflow.json))
   checks its own Header Auth secret, rejects any event outside the strict schema (400, `ARAP_ALERT_*`),
   and calls the shared `XB Ops Alert Email` sub-workflow with system **`AC2-ARAP`** (its own system,
   apart from `AC2-STOCK`: its own fold keys and flood cap). Answers: 200 sent or folded, 400
   rejected, 403 wrong or missing secret, 404 inactive or wrong path, 500 unexpected error, 502 email
   send failed.
4. The outcome is written as `alert_status` and `alert_http_status` (the answer's HTTP status, or
   `null` when no POST was made or there was no usable answer) in `last_run_status.json`, in the
   archive root and in `delivery_root`. **An alert, sent or lost, never changes the run's status,
   exit code or the latest set.** There are no hourly folders, so there is no per-run alert record:
   `last_run_status.json` shows the latest run only.

### Conditions

| Condition | Raised when (in `last_run_status.json`) | Severity | Repeat |
| --- | --- | --- | --- |
| `run_failed` | `status: failed`, any `failure_class`. A failed run raises only this one. | ERROR | daily while it lasts |
| `delivery_failed` | `delivery_status: verify_failed` or `copy_failed` (Drive kept the previous set) | ERROR | daily while it lasts |
| `ap_payments_recorded` | `ap_payments_recorded: true` on a successful run (see "`ap_payments_recorded`") | WARN | **once**: a notice, never repeated daily |

`run_failed` is keyed by its failure class, so a different failure is a new alert. Not alerted
(accepted gaps, as for stock): a run that never starts (VM off, task disabled: the daily health check
and the tracker's stale badge show it); `config_invalid` and `timezone_unavailable` from loading the
settings (`alert_status: skipped_config_invalid`); `--check-latest` and `--dry-run-config`.

The AP notice: the first knock-off of a supplier payment fails the run `unknown_knockoff_target`
(a `run_failed` alert, daily until the code is mapped). A supplier payment keyed without a knock-off,
or the first successful run after the mapping, turns `ap_payments_recorded` true and sends the WARN
notice once. It is not repeated while the flag stays true; if the flag ever turns false and back,
it is sent again.

### De-duplication rule (the stock rule)

- State: `alert_state.json` in `archive_root` (schema `xb.ac2.arap_alert_state.v1`, local only, never
  in Drive). It appears only once alerts are on; with alerts off the archive root holds only the latest
  set, `last_run_status.json` and the lock, as before.
- **Send** when an active key is new, or was last sent `alert.repeat_hours` (24) or more ago (except
  the AP notice, above). The email lists every active condition and every active key's time is reset.
- **Suppress** otherwise (`alert_status: suppressed`). A cleared key is dropped, so a recurrence is new;
  recovery sends nothing. A failed run keeps `delivery_failed` and the AP notice with their times.
- A failed send leaves the state as it was: the next hour tries again (at most one POST an hour).

### Event (schema `xb.ac2.arap_alert.v1`)

Exactly these keys: `schema`, `event` (`ac2_arap_alert`), `run_id`, `run_hour_local` (the run's hour,
for example `2026-11-19T14:00:00+08:00`), `alert_kind` (`new` or `repeat`), `status`, `failure_class`
(`null` on success, `unknown` for a class outside the list), `conditions`, `delivery_status`,
`ap_payments_recorded` (`true`/`false` on success, `null` on a failed run). **No party name or code,
document number, amount, count, path, message, host or login.** Example email (fake values):

```text
Subject: [XB alert] AC2-ARAP ERROR run_failed:outstanding_mismatch

AC2 AR/AP export: ERROR

- Hourly AR/AP export FAILED: outstanding_mismatch (nothing new published; Drive keeps the last good set)

Hour: 2026-11-19 14:00 (UTC+08:00)
Run: 0123abcd
Details: last_run_status.json in the Drive folder; runbook docs/autocount2-automation/arap_aging_runbook.md, section "Failure classes".

--
System: AC2-ARAP | Severity: ERROR | Key: run_failed:outstanding_mismatch
Times are SGT (UTC+08:00). Repeats of one system:key within 60 minutes are folded; at most 10 alert emails per system per 60 minutes.
Log: n8n Data Table xb_alert_email_log. Contract: n8n-workflows/README.md, ops_alert_email.workflow.json.
```

`alert_status` values are the stock export's: `disabled`, `no_alert`, `suppressed`, `sent`,
`send_failed`, `secret_unavailable`, `error`, `skipped_config_invalid` (meanings in the stock runbook,
"alert_status values").

### The secret on the VM: an environment variable, not Credential Manager

The task `\XB\AC2 ARAP Aging` runs with logon type **S4U** (pair `windows_integrated`), and an S4U
task cannot read Windows Credential Manager. The settings check therefore refuses
`alert.secret_credential_target` with S4U (the stock rule). The secret goes in a **user environment
variable** of the task's Windows account, `XB_AC2_ARAP_ALERT_SECRET`, exactly as the stock alert's
`XB_AC2_STOCK_ALERT_SECRET`. Use a **new** secret: never the stock alert's. A Credential Manager
target applies only if the task ever moves to the `Password` pair.

### Live steps (E1-E6)

Each step needs its own owner OK, given with its OK words. n8n steps run on the server PC through the
local API `http://127.0.0.1:5678/api/v1` only; VM steps run as the task's Windows account. Results go
to the private mailbox only: never the secret, the webhook path or the operations inbox address.
Prerequisite: `XB Ops Alert Email` and `xb_alert_email_log` are live (#288).

| # | Step | OK words | Rollback |
| --- | --- | --- | --- |
| E1 | **Re-import the sender** `XB Ops Alert Email` (system `AC2-ARAP`) from the merged export, keeping its private values (local `.tmp` copy, read back). Before the update, back up the live workflow and diff it node by node against the export it was last imported from (the #325 H1 version): any drift beyond the known private values (recipient, table ID, Gmail credential binding) stops the step. Read back: `'AC2-ARAP'` is in `Validate Alert Request`, and the workflow is still published. | "ARAP SENDER UPDATE" | Re-apply the backup taken from the live read; read back. |
| E2 | **Secret and credential.** The owner generates a new random secret and keeps it (never in chat, the repo or a mailbox file). On the server PC: create a new Header Auth credential, header name `X-XB-Alert-Token`, value = that secret. On the VM, signed in as the task's account, store it without echoing it (paste with a **right-click**; Ctrl+V at `Read-Host` stores a control character): `$s = Read-Host -AsSecureString "Alert secret"` then `[Environment]::SetEnvironmentVariable("XB_AC2_ARAP_ALERT_SECRET", [System.Net.NetworkCredential]::new("", $s).Password, "User")`. | "ARAP ALERT SECRET" | Delete the new Header Auth credential; `[Environment]::SetEnvironmentVariable("XB_AC2_ARAP_ALERT_SECRET", $null, "User")` on the VM. |
| E3 | **Import inactive** `ac2_arap_alert.workflow.json` as a new workflow. In the local import copy only: replace `REPLACE_WITH_ARAP_ALERT_WEBHOOK_PATH` with a new private, unguessable path (not the stock alert's) and `REPLACE_WITH_OPS_ALERT_EMAIL_WORKFLOW_ID` with the sender's ID; bind the E2 Header Auth credential on the webhook. Read back: inactive, MCP off, no saved executions, the path and IDs set, the credential bound. | "ARAP N8N IMPORT" | Archive or delete the inactive `AC2 ARAP Export - Error Alert` (never active; nothing calls it). |
| E4 | **Test event** (sends one email). In the editor, listen for a test event; from the server PC POST the fake event below to `http://127.0.0.1:5678/webhook-test/<private path>` with the secret header. Pass: one email `[XB alert] AC2-ARAP ERROR run_failed:internal_error` reading "AC2 AR/AP export: ERROR ... Run: 00000000", one `sent` row with system `AC2-ARAP` in `xb_alert_email_log`; the same event with an extra key answers 400 and sends nothing; without the header 403. | "ARAP TEST EMAIL" | None needed (nothing is active). The one test email cannot be recalled. |
| E5 | **Activate** `AC2 ARAP Export - Error Alert`; read back `active: true` through the local API. | "ARAP ALERT ACTIVATE" | Deactivate it. The VM then gets 404 and records `send_failed` for any problem hour; runs and exit codes are unchanged. |
| E6 | **VM: update the clone and switch alerts on.** At a minute clear of :30, `git pull` the merged `main` in the VM clone the task runs from (the task action is unchanged). Add `"alert": {"webhook_enabled": true, "webhook_url": "https://<n8n host>/webhook/<private path>", "secret_env_var": "XB_AC2_ARAP_ALERT_SECRET"}` to `config\autocount_arap_aging.local.json` (other keys default), open a **new** PowerShell window and run `--dry-run-config` with the task's `python.exe` (pass: `"status": "valid"`). Then wait for the next scheduled :30 run. Pass: `last_run_status.json` shows `status: success` and `alert_status: no_alert` with `alert_http_status: null`. Because the secret is checked on every run, `no_alert` proves the S4U task sees the variable; `secret_unavailable` means it does not. | "ARAP ALERTS ON" | Set `alert.webhook_enabled` back to `false` (the next run shows `disabled`); optionally delete `alert_state.json` in the archive root. To roll the code back as well, first remove the `alert` block from the local settings (the older version refuses it as an unknown key), then check out the previous commit in the VM clone. |

Fake test event for E4:

```json
{"schema": "xb.ac2.arap_alert.v1", "event": "ac2_arap_alert", "run_id": "00000000000000000000000000000000", "run_hour_local": "2026-11-19T14:00:00+08:00", "alert_kind": "new", "status": "failed", "failure_class": "internal_error", "conditions": ["run_failed"], "delivery_status": "skipped_failed_run", "ap_payments_recorded": null}
```

Optional, with its own OK ("ARAP FORCED FAILURE"): one forced-failure run, as the stock A4. In a new
window as the task's account, at a minute clear of :30, set only that window's
`$env:AUTOCOUNT_EXPECTED_SQL_DATABASE = "ALERT_TEST_WRONG_BOOK"` and run the normal command. It stops
at the identity check before any business query (`identity_refused`), keeps the published set, and
writes a failed `last_run_status.json` (Drive shows `failed` until the next hourly run). Pass: one
email `[XB alert] AC2-ARAP ERROR run_failed:identity_refused` and `alert_status: sent`,
`alert_http_status: 200`. The next scheduled run must read `success` and `no_alert`.

If a send fails: a 403 from Cloudflare (not n8n) means its bot protection blocked the request; n8n's
own 403 means a missing or wrong secret; 404 means the workflow is inactive or the path is wrong; 502
means the email send failed (the `send_failed` row in `xb_alert_email_log` has the code); 500 with no
log row means the sub-workflow call failed (for example it lacks `AC2-ARAP`: redo E1).

## What happens next

- **Done (18 Nov 2026):** the hourly task `\XB\AC2 ARAP Aging` is registered on the VM, and V3 passed
  with `reconciled_on` 2026-11-18.
- **V3 finding, month buckets.** AutoCount's Debtor and Creditor Aging menus age by **document date in
  calendar months** (current month, then 1, 2, 3, 4 months, then 5 and over), with credits (receipts,
  credit notes) netted into the buckets. The export's own bucket columns (`due_*`, `doc_*`) are
  30-day steps (`aging.boundaries`) with unapplied credits kept apart, so they cannot equal the menu
  buckets. V3 compared buckets by recomputing month buckets from the export's item rows, on the
  compare side only. No code or settings changed.
- **Open owner decision:** whether the exporter or the tracker should also give month buckets that
  match the menus. Not built; until then a V3 rerun compares buckets the same way.
- **A3:** error alerts, built offline (above). Live steps E1-E6 each need the owner's OK.

## Not in this slice

The daily health check job for the ARAP alert route is built separately: `arap_alert_route` (#321 L3, in the daily health check PR for #288 T2; see docs/daily_health_check.md). It goes live with the health step H1.
POST_SHIP: move the helpers shared with the stock exporter (connection string builder, lock, alert
step) into one module.
