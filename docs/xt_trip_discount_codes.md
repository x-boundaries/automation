# XT Trip Discount Codes: runbook (#355)

The n8n workflow `XT Trip Discount Codes` ([`n8n-workflows/xt_trip_discount_codes.workflow.json`](../n8n-workflows/xt_trip_discount_codes.workflow.json)) makes the 15% Shopify discount code that each X-Trekkers traveller gets after paying a trip deposit. The X-Trekkers consultant adds one row per traveller to the Google Sheet `XT discount codes - intake` and ticks Ready. Every day at 10:00 SGT the workflow checks each ticked row, finds the traveller's own x-boundaries.com customer account by exact email, makes or adopts the code, and writes the result back into the row. The consultant sends the code to the traveller.

Owner rules (24 Nov 2026): the workflow never creates, edits or tags a Shopify customer; a code is made only for an existing customer whose email matches exactly, otherwise the row says why; errors go to the daily health check and row problems stay in the sheet; dry run is the default, with a brake of 30 code writes per run.

**Status:** built, off. Committing the workflow changes nothing live. Every live step below needs the owner's OK in the words shown.

## For the owner

- Each morning at 10 am the consultant's ticked rows get their code, written into the sheet. You get no email when it works.
- You get an email only when a run had a problem (for example Shopify or the sheet could not be reached). It says in plain words what happened and ends with "Ask Claude to check." The next morning's health check counts it too.
- Problems with a single row (no email yet, no online account yet, a typo, a code that already belongs to someone else) never reach you: the row's Status and Message tell the consultant what to do.
- It never makes customer accounts. Travellers sign up on x-boundaries.com themselves, with the email the consultant puts in the sheet.
- To stop it, ask Claude to switch it off. Codes already made stay valid; nothing else depends on it.

## What a code is

| Setting | Value |
| --- | --- |
| Name | `XT-INV<deposit invoice digits>-<NN>`, for example `XT-INV000123-01`; traveller 1 is the lead booker |
| Discount | 15% off all items (order discount) |
| Who | only that one customer account (`context.customers`) |
| Uses | 3 in total (`usageLimit` 3, `appliesOncePerCustomer` false) |
| Combining | none (order, product and shipping discounts all false), so it never stacks with the member discount |
| Starts | when it is made |
| Ends | 23:59:59 SGT on max(deposit date + 12 months, departure date) (`VALIDITY_RULE` `later`); `earlier` takes the min. Deposit + 12 months is the same day next year; 29 Feb becomes 28 Feb |
| Tag | `X-Trekkers` (a discount tag, not a customer tag) |

**Adopting existing codes.** A code made by hand before go-live, under the new name or the old name `XT-INV<digits>` (traveller 1 only), is kept, not remade. When it is locked to exactly the row's customer, has been used at most 3 times and is 15% off, one `discountCodeBasicUpdate` sets only `usageLimit` 3, `endsAt` (the rule above) and `combinesWith` (none); value, items, customer and name stay. When it already has exactly those settings, nothing is written. Anything else (another customer, several customers, more than 3 uses, not 15%, not a simple code, both names present) is `CONFLICT` and nothing changes in Shopify.

## Row statuses (the status column)

The sheet's `How to use` tab has the consultant's copy of this table.

| Status | Meaning | Who acts |
| --- | --- | --- |
| (empty) | Not processed yet (Ready not ticked, or not run yet) | Consultant ticks Ready |
| `CHECK_ROW` | A field is missing or invalid, or another row has the same invoice and traveller no.; Message names the field | Consultant fixes the row |
| `NEEDS_EMAIL` | No email for this traveller | Consultant gets it from the lead booker |
| `NEEDS_SHOPIFY_ACCOUNT` | No customer with exactly this email yet; checked again every run | Traveller signs up online with this email |
| `CHECK_EMAIL` | Another row on the same trip already uses this email, or Shopify's account email is not exactly this one | Consultant checks the email |
| `CONFLICT` | The code already exists for someone else, or an existing code fails the adopt guard, or the end date passed while a code exists | Consultant tells XB; Claude checks |
| `HELD` | A brake fired (more than 300 rows to look up, or more than 30 codes to write); nothing was looked up or written for these rows | Automatic retry next run; the run's error email and the health check show it |
| `ERROR` | Shopify did not answer or did not confirm the code | Automatic retry next run; the run's error email and the health check show it |
| `DONE` | Code made (code and end-date columns) | Consultant sends the code |
| `ADOPTED` | A code already existed for this customer and now has these settings | Consultant sends it if the traveller does not have it yet |
| `EXPIRED_NO_ACCOUNT` | The end date passed before the traveller had an account; no code made | none |

Rows that are `DONE`, `ADOPTED` or `EXPIRED_NO_ACCOUNT` are never processed again. Editing such a row afterwards (a new departure date, say) changes nothing in Shopify: tell XB, and Claude changes the code by hand. A row whose `ERROR` was a code that Shopify made but did not confirm is found the next run and becomes `ADOPTED`, never a second code.

## Settings (`Run Settings` node)

| Setting | Export value | Notes |
| --- | --- | --- |
| `DRY_RUN` | `true` | A dry run reads the sheet and Shopify, writes nothing anywhere and sends no email. `false` only at the capped first run (X6) and activation (X7). |
| `VALIDITY_RULE` | `later` | `later` or `earlier`; anything else stops the run (`settings_invalid`). |
| `API_VERSION` | `2026-10` | Shopify Admin GraphQL version, the same as the kit re-tagger. |
| `SHOP_DOMAIN` | `REPLACE_WITH_SHOPIFY_SHOP_DOMAIN` | The store's myshopify domain, set in the local import copy only. |
| `SHEET_ID` | `REPLACE_WITH_XT_INTAKE_SHEET_ID` | The intake sheet's ID, set in the local import copy only (it is in the private mailbox report, not here). |
| `SHEET_LAYOUT` | see below | The tab, the last row read, the invoice pattern and the header name of every column the workflow reads or writes. |

The brakes (300 lookups, 30 code writes) are fixed in the Code nodes on purpose: they change only through a reviewed PR.

### Sheet layout (`SHEET_LAYOUT`)

`SHEET_LAYOUT` is the only place the sheet's columns are named (Controller, 24 Nov 2026: the sheet follows the consultant's own layout). Each role maps to the exact header text in row 1; the workflow finds the columns by header at run time, so columns may sit anywhere, in any order, among other columns. No Code node holds a column name or letter (a test checks this).

| Role | Read or written | Required |
| --- | --- | --- |
| `ready` (tick box), `invoice`, `traveller`, `email`, `deposit`, `departure` | read | yes |
| `trip` (rows on one trip share it; else the invoice is used) | read | no |
| `status` | read and written | yes |
| `code`, `message` | written | yes |
| `ends`, `customer`, `checked`, `made` | written | no (`''` = no such column) |

- `tab`: the tab name. `last_row`: the last row read (columns A to ZZ). `invoice_pattern`: a regular expression whose first group captures the invoice digits used in the code name.
- A header the layout names that is missing or appears twice stops the run (`sheet_layout_changed`); a layout that is not valid (a required role empty, two roles on one header, an unknown role) stops it as `settings_invalid`.
- Renaming a header in the sheet means changing `SHEET_LAYOUT` in a reviewed PR and re-importing; until then runs stop safely.
- **Not final:** the export carries the addendum section 6 layout. The sheet is being rebuilt in the consultant's own layout; the v2 layout contract sets the final names before G2.

## Error emails (through `XB Ops Alert Email`, system `XT-CODES`)

At most one ERROR email per run, built by `Build Error Alert` from fixed English lines keyed by closed codes, then the run ends as failed (`End Run As Failed`). No sheet text, name, email or Shopify text reaches the email.

| Key | When | What changed |
| --- | --- | --- |
| `settings_invalid` | `VALIDITY_RULE` is not `later` or `earlier` | nothing |
| `sheet_read_failed` | the sheet could not be read after 3 tries | nothing |
| `sheet_layout_changed` | a header `SHEET_LAYOUT` names is missing or appears twice in row 1, or the sheet has more rows than `last_row` | nothing |
| `token_failed` | the Shopify sign-in failed after 3 tries | nothing |
| `too_many_rows` | more than 300 rows to look up | those rows `HELD` in the sheet; nothing in Shopify |
| `too_many_codes` | more than 30 creates plus adopt updates | those rows `HELD`; nothing in Shopify |
| `lookup_failed` | a row's Shopify read failed (HTTP, GraphQL, throttled, no data) | that row `ERROR` |
| `write_failed` | a create or update was not confirmed | that row `ERROR`; any other code of the run stands |
| `rows_moved` | a row's Invoice no., Traveller no. or Email changed during the run | that row's result is written next run |
| `sheet_reread_failed`, `sheet_write_failed` | the write-back could not be done | the results are written next run; codes made stand |
| `several_problems` | more than one of the above | as listed in the email |
| `unexpected_error` | a Code node failed (`Build Unexpected Alert`) | says whether `Write Codes` had run |

A dry run sends an email only for the first four (the run could not start).

## Shopify app and n8n credential (owner step O2)

- **Shopify app:** a separate Dev Dashboard app, for example "XB XT Trip Codes", with scopes `read_customers`, `read_discounts` and `write_discounts` only (no `write_customers`: owner rule 24 Nov 2026), installed on the XB store, in the same Shopify organisation as the store (needed for the client credentials grant). Do not reuse another app.
- **n8n credential:** type **Custom Auth** (`httpCustomAuth`), created by the owner in the n8n UI. Its single field `JSON` holds:

  ```json
  { "body": { "client_id": "<the app's client ID>", "client_secret": "<the app's client secret>" } }
  ```

  Paste the real values only in the n8n UI: never in chat, a file, an issue or this repository. If the form offers "Allowed HTTP Request Domains", set it to the store's myshopify domain only.
- Bind it on **Get Shopify Access Token** only. That node posts `grant_type=client_credentials` as a form body, the same shape as the kit re-tagger's proven token request. The token (valid 24 hours) is fetched fresh each run and passed to `Look Up Shopify` and `Write Codes` in the `X-Shopify-Access-Token` header; redirects are not followed.

## Google Sheets credential (owner step O4)

- **n8n credential:** type **Google Sheets OAuth2 API** (`googleSheetsOAuth2Api`), created by the owner in the n8n UI and signed in with a Google account that can edit `XT discount codes - intake`.
- Bind it on **Read Intake Sheet**, **Re-read Row Keys** and **Write Back To Sheet** only. The workflow reads the layout's tab (columns A:ZZ, rows 1 to `last_row`) and writes only the cells of its written roles in the rows it decided!L<row>:R<row>`.
- Reach: an OAuth sign-in can reach every sheet that account can open. To limit it to this one sheet, sign in with a Google account that has only this sheet (or its folder) shared with it; the workflow itself never addresses another file (the sheet ID is fixed in `Run Settings`). Which account to use is the owner's choice at O4.
- Protection on the automation's columns must be "warning only" (or the credential's account an editor of them), so API writes go through; data validation on the status column, if any, must list every status the workflow writes.

## Reading a dry run (Claude)

A dry run sends no email and saves no execution. Its plan is the output item of `Decide Code Writes` (or of `Check Intake Rows` when no row needs a Shopify lookup): `mode: "dry_run"`, `summary` (counts: rows, Ready rows, rows decided without Shopify and lookups; from `Decide Code Writes` also the planned writes, creates and adopts, and whether the brake would fire) and `plan` (row number, status or "would CREATE" / "would ADOPT", code, end date, message).

1. On the server, Claude opens the workflow in the n8n editor in Chrome (`DRY_RUN` on in the live copy).
2. Claude clicks "Test workflow" (`Manual Run`) once.
3. Claude reads `summary` and `plan` in the editor.
4. Claude reports only counts, statuses, row numbers and codes. Names and emails stay in the editor (the plan holds none; the lookup items do) and are gone when the tab closes.

"Save manual executions" is never switched on.

## Live path

| Step | Who | What | Owner OK words | Rollback |
| --- | --- | --- | --- | --- |
| O2 | Owner (hand step) | Create the Shopify app and the Custom Auth credential (above). | (owner action) | Uninstall and delete the app; delete the credential. |
| O4 | Owner (hand step) | Create the Google Sheets OAuth2 credential (above). | (owner action) | Delete the credential; revoke the app access in the Google account. |
| X1 | G2 / owner | Full G2 review of the exact head; the owner merges. | owner merge | Revert PR. |
| X2 | Work Claude | After X1: update the live `XB Ops Alert Email` with `XT-CODES` from the **merged** export by the house procedure (`docs/daily_health_check.md`, #325 L1): back up, diff node by node and stop on any drift beyond the known private values (recipient, table ID, Gmail credential binding), re-import keeping them (local `.tmp` copy), re-publish, read back. Then the daily health step H-XT (`docs/daily_health_check.md`, "XT trip discount codes"). | "OK XT CODES SENDER AND HEALTH" | Re-apply each backup; read back. |
| X3 | Work Claude | Import `XT Trip Discount Codes` INACTIVE through the local API (`http://127.0.0.1:5678/api/v1`) from a local `.tmp` copy with `SHOP_DOMAIN`, `SHEET_ID` and the alert workflow id set; bind the two credentials (O2 on the token node, O4 on the three sheet nodes); read the nodes back in the editor. | "OK XT CODES IMPORT INACTIVE" | Archive the imported workflow (it never ran). |
| X4 | Work Claude | One manual dry run in the open editor, read as above: reads the sheet and Shopify, writes nothing, sends nothing, saves no execution. Report the summary and plan (statuses and counts only) to the Controller. | "OK XT CODES DRY RUN" | Nothing to roll back. |
| X5 | Controller | Review the plan. Stop if the sign-in or the lookup query failed, a brake would fire, or a status looks wrong. | (Controller) | Nothing to roll back. |
| X6 | Work Claude | Capped first real run: exactly one row ticked Ready that the X4 plan shows as one write (preferably an adopt of a hand-made code, addendum section 3), `DRY_RUN` `false` in the live copy, one manual run. Tick Ready on that one row only if the owner's OK names it. Read back in Shopify admin (the code's uses, end date, combinations and customer) and in the row (L:R). If the dry run just before shows more than one write, stop and ask the Controller. Set `DRY_RUN` back to `true`. | "OK XT CODES CAPPED RUN ROW <n>" | Put the code back by hand in Shopify admin (an adopt) or delete the new code (a create); clear the row's L:R. |
| X7 | Work Claude | Activate: `DRY_RUN` `false` in the live copy, activate, read back that the schedule is registered. Then bind the live `XB Daily Health` `WATCH` entry `REPLACE_WITH_XT_CODES_WORKFLOW_ID` to this workflow's id by the health workflow's own update procedure. Then hand the "how it works" note for the consultant to the Controller (the owner sends it). | "OK XT CODES ACTIVATE" | Deactivate; re-apply the health workflow backup; read back. |

Agent UAT after X6 (Controller decides who): checkout tests on an existing staff test customer account (not logged in with the matching email, logged in, a different email must fail, a 4th use must fail, with the member code must not combine). Owner UAT after X7: the consultant adds a real row, and the next morning its code is in the sheet.

## Daily health

- `XB Daily Health` counts the `XT-CODES` `ERROR` rows of `xb_alert_email_log` in the 24 hours before the check: WARN `xt_code_errors` under the n8n workflows job. It needs no id and stays at 0 until this workflow runs.
- Its `WATCH.n8n_workflows` entry `{ id: 'REPLACE_WITH_XT_CODES_WORKFLOW_ID', label: 'XT trip codes', errors: false, bind_later: true }` is skipped until X7 binds it; from then on an inactive or missing workflow is a FAIL.
- Details: `docs/daily_health_check.md`, "XT trip discount codes".

## Troubleshooting (Claude)

- First look for any email or health WARN: run it by hand with `DRY_RUN` on (above) and read the plan and `problems`.
- `token_failed`: the Shopify app is uninstalled, its secret was rotated, or the credential shape is wrong (above).
- `sheet_read_failed` or `sheet_write_failed`: the Google credential lost access, or the sheet moved; check the sheet ID and the account's access.
- `sheet_layout_changed`: a header named in `SHEET_LAYOUT` was renamed, deleted or doubled. Put it back, or change `SHEET_LAYOUT` in a reviewed PR; moving or adding other columns is fine. The workflow does not guess columns.
- `lookup_failed` with `graphql_errors` on every row after a Shopify API change: the lookup query in `Check Intake Rows` needs a reviewed fix; nothing was written meanwhile.
- `write_failed`: read the row's code in Shopify admin. If it exists, the next run adopts it; if Shopify refused it, the row stays `ERROR` each morning until fixed.

## Rollback and recovery

- Switch off: deactivate the workflow. Codes already made stay valid and unchanged.
- A wrong code: change or delete it by hand in Shopify admin, then clear the row's automation cells and untick Ready, or fix the row and leave it for the next run.
- Nothing is stored in n8n (no static data, no saved executions): re-importing the workflow loses nothing.

## Tests

`tests/test_xt_trip_discount_codes_workflow.py` (run by `.github/workflows/xt-trip-discount-codes-tests.yml`) executes the committed Code nodes, IF expressions and the shared sender's own `Validate Alert Request` with Node against fake sheets and fake Shopify answers (`example.com` emails, fake invoice numbers). Nothing live is contacted.
