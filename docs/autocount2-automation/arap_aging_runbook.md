# AC2 AR/AP aging export (latest snapshot only) runbook

Issue: #321 (slice A1, offline build). Script: `scripts/autocount_arap_aging.py`. Settings example:
`config/autocount_arap_aging.example.json`. Owner-run live check:
`sql/autocount2/arap_aging_live_check.readonly.template.sql`. Tests:
`tests/test_autocount_arap_aging.py`. Fake feed for the tracker build (B1):
`tests/fixtures/arap_dashboard_feed.fake.fixture` (JSON content; the repo allows no `*.json` file
under `tests/`).

Status: offline build only. Nothing here has run against the AutoCount book. The installer and the
scheduled task (A2) and error alerts (A3) are later slices, each with its own owner OK.

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
- Never emailed, never sent to an API, never committed. Alerts (A3) will carry failure classes and
  counts only.

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
and each bucket, unapplied totals, plus 10 sample documents each side. Private mailbox record only.
Pass: set `reconciled_on` to that date. One fix round; if it still does not match, write the r1
A1-b fallback note (AutoCount's own aging classes, nightly) for the Controller.

## Not in this slice

Installer and scheduled task `\XB\AC2 ARAP Aging` (A2), the Drive folder and `delivery_root` (A2),
error alerts through the shared ops alert sub-workflow (A3). POST_SHIP: move the helpers shared with
the stock exporter (connection string builder, lock) into one module.
