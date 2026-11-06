# Ops alert email log: Data Table `xb_alert_email_log` (#288)

The shared n8n sub-workflow `XB Ops Alert Email` ([`n8n-workflows/ops_alert_email.workflow.json`](../n8n-workflows/ops_alert_email.workflow.json)) writes one row to this n8n Data Table for every alert it handles. The fold and flood rules read it back. It lives only in the n8n instance on the server PC. It is not in this repository, Drive or the mailbox.

Creating the table is a live step that needs its own owner OK (#288, G0 section 8). Nothing in this repository creates it.

## Columns

n8n adds `id`, `createdAt` and `updatedAt` to every Data Table by itself. Do not declare them.

| Column | Type | Value |
| --- | --- | --- |
| `system` | string | `AC2-STOCK`, `ENERGYGRID` or `MEMBER-SYNC`. Every row, including a flood notice, belongs to exactly one system. |
| `key` | string | The caller's stable condition key, for example `run_failed:ledger_mismatch`; `alert_flood` for a system's flood notice (reserved: a caller cannot use it). Fold matching uses `system` + `key`. |
| `severity` | string | `ERROR`, `WARN` or `INFO` (the flood notice is `WARN`). |
| `outcome` | string | `sent`, `folded`, `folded_flood` or `send_failed`. |
| `code` | string | Empty when nothing went wrong. `send_failed` rows: `http_<status>` (for example `http_403`) or `gmail_error`. `log_read_failed` when the log could not be read before the decision. Never an error message, an address or a token. |
| `folded_count` | number | On a `sent` row: how many `folded` and `folded_flood` rows of the same `system:key` came since its previous `sent` row (the k in "(+k folded since HH:MM)"). `0` on every other row. |
| `sent_at` | string | When the row was decided, ISO-8601 UTC (for example `2026-11-06T06:30:00.000Z`). Fold and flood windows use this column; email bodies show times in SGT. |

Rejected requests (`OPS_ALERT_*` reason codes) and unexpected errors are not logged: they are answered to the caller, which records them.

## Rules that read it

- **Read:** the 500 newest rows (by `id`, descending) before each decision. Only the rows of the request's own `system` are used; another system's rows never affect the decision or the email.
- **Fold:** a `sent` row with the same `system` + `key` less than 60 minutes old: the new alert is logged `folded` and not mailed. A `send_failed` row never folds, so the next call tries again.
- **Flood, per system:** 10 or more `sent` rows of the request's `system` (not counting its `alert_flood` rows) in the last 60 minutes: the new alert is logged `folded_flood`. If that system has no `alert_flood` row `sent` in the last 60 minutes, one `[XB alert] <SYSTEM> WARN alert_flood` email goes out for it and gets its own row with that `system`. Other systems keep sending normally.
- **Daily summary (slice 3):** never folded and never counted toward the flood cap.
- The read and the insert are not one atomic step. Two calls in the same second can both decide to send: one duplicate email at worst (accepted in the G0 design). More than 500 calls in one hour (all systems together) can hide older rows from the read, so a system's count can come out low and let an extra email through.

## Housekeeping

The table grows by one row per alert. A clean-up (for example, deleting rows older than 90 days) is not built. Add it only with an owner OK if the table ever gets large.
