# Monthly in-store member discount check (#268)

**Built, switched off.** Nothing here runs until the install steps below, each with its own owner OK.

Once a month this check looks at the till receipts of the month just ended and lists the receipts where a cashier keyed the member discount rate (15% or 20%) for a customer whose AutoCount member record does not qualify for it. The shop manager uses the list to check each receipt and coach the outlet. It is the main control for in-store member discounts: the till does not stop a cashier keying the rate for anyone.

- **Code:** `scripts/xb_member_pos_exceptions.py` (read-only SQL, the rule, the private record, the email event).
- **Task installer:** `scripts/install_member_pos_exceptions_task.ps1` (`-DryRun` first).
- **Settings:** `config/member_pos_exceptions.example.json` (copy to a private `*.local.json` on the AC2 server).
- **Email workflow:** `n8n-workflows/member_pos_exceptions_alert.workflow.json` (`Member Discount Check - Monthly Email`), through the shared `XB Ops Alert Email` sub-workflow as system `MEMBER-POS`.
- **Tests:** `tests/test_member_pos_exceptions.py`, `tests/test_member_pos_exceptions_alert_workflow.py`, and the sender's `tests/test_ops_alert_email_workflow.py`.

## What it flags

A receipt is read when its `DocDate` is in the month, it is not cancelled (`Pos.Cancelled` is `F`; `T` is a cancelled receipt, any other value is skipped too) and its total is above zero (returns and zero receipts are skipped). It has the **member rate** when any of its lines carries a discount text that is one of `rule.member_rates` (`15.00%`, `15%` and `20.00%` all match; `25.00%` does not).

| Reason | When | Listed in the email |
| --- | --- | --- |
| `guest` (registered, not qualified) | The member's type today is `GUEST` or `Default` | Yes. Marked "type changed after the sale" when the member record changed on or after the receipt day |
| `expired` (membership had expired) | Type `MEMBER`, `ExpiryDate` before the receipt day | Yes |
| `same_receipt` (the receipt that made them a member) | Type `MEMBER`, the record changed on or after the receipt day, and `ExpiryDate` is the receipt day + `rule.expiry_months` (24) months, give or take a day | Yes. Nothing to undo: the rate starts from the next purchase, so this is coaching only |
| `member_valid` | Type `MEMBER`, not expired, not the same receipt | No (count only) |
| `no_member_on_receipt` | Member rate, no `MemberNo` on the receipt | No (count, also per outlet) |
| `member_not_found` | A `MemberNo` with no member row | No (count) |
| `type_other` | Any other member type | No (count) |

Why "type changed after the sale" is a mark and not a separate, unlisted reason: a member's `LastModified` moves on bulk edits as well as on type changes, so for the receipts seen so far it is on or after the receipt day for every `GUEST` receipt with the member rate. Hiding those would hide every real case. The mark tells the manager to check the member's history before coaching. When the member removals (#268 slice E) are live, a `MEMBER` who was moved back to `GUEST` after the sale also shows here with the mark.

## Facts it relies on (read-only check of the live book, 24 Nov 2026)

These settled the open questions of the design. Counts only; no business values are in this repository.

| Design question | Answer | Effect |
| --- | --- | --- |
| Where the member discount is stored (U1, U2, U4) | Only as hand-keyed text on the receipt line, `PosDtl.Discount` (for example `15.00%`); the receipt header has a discount amount but no text | The rule reads line texts; the rate list is a setting |
| A discount rule per member type (U3) | None: `MemberType` has no price, discount or point column, and the promotion and price-book tables are empty | Any type can get the rate at the till, so every type is checked |
| Volume (U5, U6) | Most member-rate receipts belong to valid members; a handful a month belong to `GUEST` members; none so far to an expired `MEMBER`; member-rate receipts without a member number still occur (fewer each month) | A short list each month; the list cap (12 rows) is ample |
| Outlet (U7) | `Pos` has no outlet column; the outlet comes through `PosTerminal.Outlet` | Joined through `source.terminal_column` (`TerminalID`; `LastModifiedTerminalID` is the allowed alternative) |
| Voids and returns (U8) | `Cancelled` `T` / `F`; returns show as receipts with a total of zero or less | Both skipped and counted |
| Type history (U9) | `ChangeLog` keeps member updates with old and new values in an XML column | Not read: it would mean reading personal data. The "type changed after the sale" mark is used instead |
| Member numbers (U10) | No receipt member number without a member row; a few contain `+` | Masking keeps the last 3 letters or digits only |
| Cashier (U11) | No cashier column on `Pos` | No staff field anywhere in the output |
| Sync lag (U12) | `SyncRegistration` has a last finished sync time per outlet; `Pos.DocDate` is a date only | The month is reported only after every listed outlet finished a sync after the month ended |

## Privacy

- The SQL reads only the columns it needs: receipt key, number, date, cancel flag, the **sign** of the total (decided in SQL), member number, outlet, member type, expiry, last change, active flag and the line discount text. No name, phone, email, address or amount.
- The private record (`output.record_dir/pos_exceptions_<YYYY-MM>.json`, on the AC2 server only) holds counts and the receipt list with member numbers masked to the last 3 characters (`***123`).
- The console and the task log show counts and codes only (no receipt numbers, no member references).
- The email carries counts, receipt numbers, outlet codes, days, rates, reasons and masked member numbers. Staff look a receipt up by its number in AutoCount.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `schedule.run_day`, `schedule.run_time` | `2`, `09:15` | The daily task runs at `run_time`; from `run_day` of a month it reports the previous month once. A failed run, a stale sync or a failed email is retried the next day |
| `schedule.logon_type` | `S4U` | `S4U` with `identity.auth_mode` `windows_integrated`; `Password` with `sql_login` |
| `source.*` | | Server, ODBC driver, the pinned book (`expected_database`), and `terminal_column` |
| `identity.allow_privileged_identity` | `false` | See "Login" below. The sa login is always refused |
| `output.record_dir` | required | Private folder on the AC2 server for the monthly records and `alert_state.json` |
| `rule.member_rates` | `[15, 20]` | The discount percentages that count as the member rate |
| `rule.outlets` | required | The outlet codes that must have synced (as in `SyncRegistration.Location` and `PosTerminal.Outlet`) |
| `rule.max_listed` | `12` | Receipts listed in the email (1-12, so the email fits the sender's 30-line limit); the rest are counted |
| `rule.expiry_months` | `24` | Membership length, for the same-receipt reason |
| `alert.*` | off | The stock export's alert block, unchanged: an `https` URL, a header name, and exactly one secret source (`secret_env_var` for an `S4U` task). `repeat_hours` is not used: each month's outcome is sent once |

The expected SQL login, user and database come from the task account's environment (`AUTOCOUNT_EXPECTED_SQL_LOGIN`, `AUTOCOUNT_EXPECTED_SQL_USER`, `AUTOCOUNT_EXPECTED_SQL_DATABASE`), as for the stock and AR/AP exports; a mismatch fails the run (`identity_refused`) before any business query.

## Commands

```powershell
python scripts\xb_member_pos_exceptions.py check-settings --config C:\path\to\member_pos_exceptions.local.json
python scripts\xb_member_pos_exceptions.py run --month 2026-11 --no-send --config C:\path\to\member_pos_exceptions.local.json
python scripts\xb_member_pos_exceptions.py run --scheduled --config C:\path\to\member_pos_exceptions.local.json
```

- `check-settings` reads no data; it prints the settings hash and the hash of each pinned query.
- `run --month` judges one ended month. `--no-send` reads and records, never emails.
- `run --scheduled` is what the task runs: `not_due` before `run_day`, `already_reported` once the month's report is recorded and its email handled.
- Exit codes: `0` report (with or without exceptions) or nothing to do; `3` `pos_sync_stale`; `1` failed.

| Outcome | Email | Next |
| --- | --- | --- |
| `report`, no exceptions | none | nothing |
| `report` with exceptions | `[XB alert] MEMBER-POS WARN exceptions_<YYYY-MM>` | The shop manager checks each receipt in AutoCount and coaches the outlet |
| `pos_sync_stale` | `[XB alert] MEMBER-POS WARN pos_sync_stale_<YYYY-MM>`, once a month | Check the till PC of each outlet named is on and its POS sync runs; still waiting after 3 days: forward to Claude |
| `failed` (`identity_refused`, `credential_unavailable`, `lock_timeout`, `source_error`, `write_failed`, `internal_error`) | `[XB alert] MEMBER-POS ERROR run_failed:<class>`, once per month and class | Forward to Claude |

## Login

The read-only check of 24 Nov 2026 found that the trusted connection used for read-only facts checks on the AC2 server is a SQL `sysadmin`. This check refuses a `sysadmin` or `db_owner` login unless `identity.allow_privileged_identity` is `true`, exactly like the stock and AR/AP exports. Before install, one of these is decided (owner, through the Controller):

1. **Recommended:** run the task as an account whose SQL login is read-only (see [read-only SQL login runbook](readonly_sql_login_runbook.md)); or
2. set `allow_privileged_identity` to `true` for this task (an owner decision; check first how the stock and AR/AP exports on that server are set).

Either way the script sends SELECT statements only.

## Install (each step needs its own owner OK)

1. **Settings.** On the AC2 server, in the deployed clone at the merged commit: copy the example to a private `member_pos_exceptions.local.json` outside the clone, fill the server, book, outlets and record folder; `alert.webhook_enabled` stays `false`. Run `check-settings`.
2. **By-hand run.** As the task account: `run --month <last month> --no-send`. Read the console counts and the private record; compare them with the facts check (same month, same counts for cancelled, member rate and GUEST receipts).
3. **Email workflow.** Re-import `XB Ops Alert Email` with system `MEMBER-POS` and publish it (its own owner OK; follow the n8n workflows README for live imports), then import `member_pos_exceptions_alert.workflow.json` inactive, set its two placeholders, bind its own Header Auth credential, and activate it (owner OK). Recipient for the first month: the work mailbox only (Controller decision D3); adding the shops' manager is a later owner decision.
4. **Email on.** Put the shared secret in the task account's environment variable named by `alert.secret_env_var`, set `alert.webhook_url` and `alert.webhook_enabled` `true`, run `check-settings`, then one `run --month <last month>` by hand: an email arrives only if that month had exceptions.
5. **Task.** `install_member_pos_exceptions_task.ps1 -DryRun` first, then without `-DryRun` (owner OK). Run it once with Start-ScheduledTask and read the record (`already_reported` or `not_due` is normal).

**Rollback:** `Disable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB Member Discount Check'`; set `alert.webhook_enabled` to `false`; deactivate the n8n workflow. Nothing in AutoCount is ever changed, so there is nothing else to undo. The record folder can be kept or deleted.

## Known limits

- **No daily health watch yet.** `XB Daily Health` reports a failed email (`send_failed` rows of any system) the next morning, but not a task that stopped running. Adding the task to the health check is a follow-up.
- **Today's type, not the type at the sale.** See "What it flags". Reading the `ChangeLog` history would need personal data and is not done.
- **Outlet list is fixed.** A new outlet must be added to `rule.outlets`; receipts of an outlet not listed are still judged, but its sync is not checked.
