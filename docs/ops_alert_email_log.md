# Ops alert email log: Data Table `xb_alert_email_log` (#288)

The shared n8n sub-workflow `XB Ops Alert Email` ([`n8n-workflows/ops_alert_email.workflow.json`](../n8n-workflows/ops_alert_email.workflow.json)) writes one row to this n8n Data Table for every alert it handles. The fold and flood rules read it back. It lives only in the n8n instance on the server PC. It is not in this repository, Drive or the mailbox.

Creating the table is a live step that needs its own owner OK (#288, G0 section 8). Nothing in this repository creates it.

## Columns

n8n adds `id`, `createdAt` and `updatedAt` to every Data Table by itself. Do not declare them.

| Column | Type | Value |
| --- | --- | --- |
| `system` | string | `AC2-STOCK`, `ENERGYGRID` or `MEMBER-SYNC`. Every row, including a flood notice, belongs to exactly one system. |
| `key` | string | The caller's stable condition key, for example `run_failed:ledger_mismatch`; `alert_flood` for a system's flood notice (reserved: a caller cannot use it); `daily_summary` for a daily summary (slice 3). Fold matching uses `system` + `key`. |
| `severity` | string | `ERROR`, `WARN` or `INFO` (the flood notice is `WARN`; a daily summary is `INFO` for CLEAN and `WARN` for STOP). |
| `outcome` | string | `sent`, `folded`, `folded_flood` or `send_failed`. |
| `code` | string | Empty when nothing went wrong. `send_failed` rows: usually `gmail_error` (a Gmail 2.1 error item is `{ error: <message> }`), or `http_<status>` (for example `http_429`) only when that message carries a 4xx or 5xx status. `log_read_failed` when the log could not be read before the decision. Never an error message, an address or a token. |
| `folded_count` | number | On a `sent` or `send_failed` row of an alert: how many `folded` and `folded_flood` rows of the same `system:key` came since its previous `sent` row (the k in "(+k folded since HH:MM)"). A `send_failed` row does not reset that count, so the next successful send reports the same rows again. `0` on `folded`, `folded_flood` and flood-notice rows. |
| `sent_at` | string | When the row was decided, ISO-8601 UTC (for example `2026-11-06T06:30:00.000Z`). Fold and flood windows use this column; email bodies show times in SGT. |

Rejected requests (`OPS_ALERT_*` reason codes) and unexpected errors are not logged: they are answered to the caller, which records them.

## Rules that read it

- **Read:** the 500 newest rows **of the request's own `system`** (Data Table filter `system` = the request's system, by `id`, descending) before each decision. The decision code drops any other system's row again as a second guard, so another system's rows never affect the decision or the email. Rows dated in the future are ignored.
- **Fold:** a `sent` row with the same `system` + `key` less than 60 minutes old: the new alert is logged `folded` and not mailed. A `send_failed` row never folds and is not counted in the fold note, so the next call tries again.
- **Read failure:** decided once on an empty history (no fold, no flood), and the row it logs carries `log_read_failed`. If the table itself is missing, no row can be written; the email still goes once.
- **Flood, per system:** 10 or more `sent` rows of the request's `system` (not counting its `alert_flood` rows) in the last 60 minutes: the new alert is logged `folded_flood`. If that system has no `alert_flood` row `sent` in the last 60 minutes, one `[XB alert] <SYSTEM> WARN alert_flood` email goes out for it and gets its own row with that `system`. A flood notice that failed (`send_failed`) does not count, so the next held-back alert tries the notice again. Other systems keep sending normally.
- **Daily summary (slice 3):** a `daily_summary` row is always `sent` or `send_failed`: it is never folded, never held back by a flood, and `daily_summary` rows never count toward the flood cap.
- The read and the insert are not one atomic step. Two calls in the same second can both decide to send: one duplicate email at worst (accepted in the G0 design). More than 500 calls of one system in one hour can hide that system's older rows from the read, so its count can come out low and let an extra email through. Another system's volume never does.

## Housekeeping

The table grows by one row per alert. A clean-up (for example, deleting rows older than 90 days) is not built. Add it only with an owner OK if the table ever gets large.
