# XB Kit Re-tagger: runbook (#269 Part A)

The n8n workflow `XB Kit Re-tagger` ([`n8n-workflows/xb_kit_retagger.workflow.json`](../n8n-workflows/xb_kit_retagger.workflow.json)) keeps the "Pack for this trip" gear-kit tags on the XB Shopify store up to date. The 8 kit smart collections are "tag `kit-<kit>` AND in stock", so a new product only joins a kit once it carries the tag. Every day at 03:00 SGT the workflow reads every ACTIVE product, works out its kit tags from the XT kit rules v1 and adds or removes only those tags.

It is hands-off (owner, 21 Nov 2026): set up once, and the owner hears from it only when something needs fixing. Committing the workflow changes nothing live. Every live step below needs the owner's OK in the words shown.

**Status:** live since 21 Nov 2026. Steps L0-L9 are done (see "Live path"); the steps stay as the record and for any re-run.

## For the owner

- Every night at 3 am the kit tags on the website are brought up to date with the trip kit rules. You get no email when it works.
- You get an email only when something needs fixing. Each email says in plain words what happened and ends with "Ask Claude to check." Forward it to Claude or open a chat and paste it.
- If a person on the team adds or removes a kit tag by hand after the workflow changed it, the workflow leaves that product's tag as the person set it.
- If one night it plans to take products out of kits far more than usual, it changes nothing that night and emails you. Nothing on the website changes until Claude has looked.
- If many kit tags it set were changed back by someone or something else since the night before (for example a bulk import or another app), it also changes nothing that night and emails you, so this never goes unnoticed.
- To stop it completely, ask Claude to switch it off. Nothing else on the website depends on it.

## What it changes and what it never touches

- Changes only the 30 known kit tags: `kit-<kit>` for the 8 kits (`tropical`, `hut`, `himalaya`, `expedition`, `winter`, `travel`, `island`, `ride`) and `ks-<slot>` for the 22 slots.
- Never touches `kit-no` (staff: keep this product out of every kit), `kit-pin-<kit>` (staff: force this product into one kit; it gets `kit-<kit>` only), any other `kit-`/`ks-` tag it does not know (left alone and listed in the dry-run output), or any other tag, price, stock, status or product field.
- Reads ACTIVE products only, so draft and archived products keep their old kit tags (they do not show on the storefront).
- Writes with the Admin GraphQL `tagsAdd` / `tagsRemove` mutations, one product per request, one request every 500 ms. Both are safe to repeat. Adds have no limit.
- Stock is not checked by the tagger; the collections do that.

## Never fighting a person

- The workflow keeps a record in its own workflow static data, key `kitTagRecord`: `{ v: 1, products: { <product number>: { d: <SGT date>, t: { <tag>: "a" | "r" | "na" | "nr" } } } }`, one entry per product and known tag it added (`a`) or removed (`r`), written only after Shopify confirmed that part of the write.
- A known tag it removed (`r`) that is back on the product: a person put it back. It is never removed again. A known tag it added (`a`) that is gone: a person took it off. It is never added again. Both are silent, unless there are so many in one night that the outside-changes stop fires (below).
- The first run that sees such a change and gets past the anomaly stops marks it as seen: `a` becomes `na` ("never add again"), `r` becomes `nr` ("never remove again"). So each change made outside the workflow is counted once, and only changes since the last run count towards the outside-changes stop.
- The dry-run output counts them in products, not tags: `left_alone_person_removed` / `left_alone_person_added` (all), `new_person_removed` / `new_person_added` (new since the last run).
- Its own earlier changes never block a later rule change (it may add a tag it once removed, and remove a tag it once added and that is still there).
- Size: entries for products that are no longer active are dropped on every run that gets as far as a plan; past 5,000 products the oldest entries are dropped. The first run starts with an empty record; a missing or malformed record is treated as empty, entry by entry.
- n8n saves static data only after scheduled (production) runs, not manual runs. A manual real run (L6) changes tags but does not add to the record; the record starts with the first scheduled run after activation.

## Anomaly stops: removal brake and outside changes

Both use the same internal threshold (not an owner setting, never in owner text): more products than 10% of the products that carry a known tag now (rounded up), and never fewer than 10 products. Counts are products, not tags. When either fires, nothing changes that night (no adds, no removes, record untouched), one ERROR alert is sent and the run ends as failed. A dry run shows both (`braked`, `held`) without an email. If both fire, the brake email is sent.

- **Removal brake** (`stopped_unusual_removals`): more products would lose a known tag than the threshold. Adds are not braked: an add only puts a matching product into a kit.
- **Outside changes** (`stopped_outside_changes`): more products than the threshold had tags this workflow set changed back by someone or something else since the last run (new `left_alone` counts). Without this stop a bulk tool (a CSV import, another app's sync) that strips tags the workflow added would be taken, silently, as people's edits, and those tags would never come back.
- **What they catch:** store-wide anomalies: bad or missing product data, a read that looked complete but was wrong, a bulk tool, a broken rules paste. They are not meant to catch ordinary edits: one vendor or type rename usually stays under them (with about 720 tagged products the threshold is about 72 products), and such changes are rules-correct for the new data and reverse the next night if the rename is undone.
- **Another tagger:** no gate is needed before the first real run. The record, the brake and the outside-changes stop together keep a second tool from causing a nightly fight or a silent loss.
- **What Claude does:** run it by hand with `DRY_RUN` on (L4 procedure) and read the summary and plan. If the change is unwanted (a tool stripped tags), put the tags back by hand or by a bulk edit; the next run then sees nothing new. If it is wanted (a deliberate bulk change by staff), the fix is a reviewed one-run PR (for example a temporary threshold change, reverted after one scheduled run) or a one-off, owner-approved manual cleanup. The stops have no switch.

## Settings (`Run Settings` node)

| Setting | Default | Meaning |
| --- | --- | --- |
| `DRY_RUN` | `true` | Read and plan, write nothing, send nothing. The plan is in the output of `Work Out Kit Tag Changes`. Anything but `false` is a dry run. Set to `false` only for an owner-approved real run, and at activation. |
| `API_VERSION` | `2026-10` | Admin API version. Bumping it is a one-value change. |
| `SHOP_DOMAIN` | `REPLACE_WITH_SHOPIFY_SHOP_DOMAIN` | The store's myshopify domain. Set in the local import copy only, never in this repository. |

## Emails (through `XB Ops Alert Email`, system `KIT-RETAG`)

Only errors are emailed. A good run, a quiet run and a dry run send nothing. Every email is one `ERROR`, subject `[XB alert] KIT-RETAG ERROR <short>`, first line in plain English ending "Ask Claude to check.", second line `Detail for Claude: ...` with closed codes. After the email the run ends as failed (`End Run As Failed`).

| `short` | When | First line (plain English) |
| --- | --- | --- |
| `could_not_sign_in` | Token request failed (`http_4xx`, `request_error`, `no_token`) | ...could not sign in to Shopify. Nothing was changed. |
| `could_not_read_products` | A product page failed: HTTP error, `throttled` (THROTTLED), `graphql_errors`, `no_data`, `no_page` | ...could not read the product list from Shopify. Nothing was changed. |
| `product_list_incomplete` | Pages missing, count not exact, scanned count different from Shopify's exact count, duplicate ids, bad rows | ...got an incomplete product list from Shopify. Nothing was changed. |
| `stopped_unusual_removals` | Removal brake | "Kit tags on the website: tonight's run stopped itself because it planned to remove more tags than normal. Nothing was changed. Ask Claude to check." |
| `stopped_outside_changes` | Outside-changes stop | "Kit tags on the website: many kit tags were changed outside the re-tagger since last night, so nothing was changed tonight. Ask Claude to check." |
| `some_updates_failed` | Any write: HTTP error, GraphQL error (THROTTLED included), missing data, wrong product, `userErrors` | ...could not update some products. The other products were updated. (or: could not update the products on its list.) |
| `unexpected_error` | A Code node failed | Before `Write Tag Changes` ran: ...stopped on an unexpected error before it changed anything. Nothing was changed. After it ran (or if n8n cannot tell): ...stopped on an unexpected error. Some products may already have been updated. |

Lines carry counts, product handles (lower case) and closed codes; never titles, tag lists, customer data or tokens. A failed write is not added to the record and is tried again the next night (tag writes are safe to repeat).

Executions are not saved (success and error both `none`, manual runs not saved), because items after the token step carry the 24-hour access token.

## Shopify app and n8n credential (owner)

- **Shopify app (L0):** a separate Dev Dashboard app, for example "XB Kit Re-tagger", with scopes `read_products` and `write_products` only, installed on the XB store, in the same Shopify organisation as the store (needed for the client credentials grant). Do not reuse another app.
- **n8n credential (L1):** type **Custom Auth** (`httpCustomAuth`), created by the owner in the n8n UI. Its single field `JSON` holds:

  ```json
  { "body": { "client_id": "<the app's client ID>", "client_secret": "<the app's client secret>" } }
  ```

  Paste the real values only in the n8n UI: never in chat, a file, an issue or this repository. If the credential form offers "Allowed HTTP Request Domains", set it to the store's myshopify domain only.
- Bind it on **Get Shopify Access Token** only. That node posts `grant_type=client_credentials` as a form body with an explicit `Content-Type: application/x-www-form-urlencoded` header; with that header n8n merges the credential's `body` values into the same form body, which is the request shape shopify.dev documents for this grant. Redirects are not followed, so the credential is never sent anywhere else.
- How this was checked (G1, 21 Nov 2026): against n8n's public source (`HttpCustomAuth.credentials.ts`, HTTP Request V3 and the legacy request adapter on the current master), not against the live instance's version. The first dry run (L4) proves it: a wrong shape gives a `could_not_sign_in` email with an `http_4xx` code and nothing else happens.
- The token (valid 24 hours) is fetched fresh on every run and passed to the read and write requests in the `X-Shopify-Access-Token` header. The Code nodes never read environment variables or credentials.

## Reading a dry run (Claude)

A dry run sends no email and saves no execution. Its plan is the output item of `Work Out Kit Tag Changes`: `mode: "dry_run"`, `summary` (scanned, tagged, changed, losing, brake_at, braked, held, tags to add and remove, people's changes left alone in all and new since the last run, unknown tags) and `plan` (handle, add, remove per product). The same item is the output of the last node that runs, `Error To Email` (false branch). Controller ruling, 21 Nov 2026: it is read in the open n8n editor, never through a saved execution.

1. On the server, Claude opens the workflow in the n8n editor in Chrome (`DRY_RUN` on in the live copy).
2. Claude clicks "Test workflow" (`Manual Run`) once.
3. Claude opens the output of `Work Out Kit Tag Changes` (or `Error To Email`) in the editor and reads `summary` and `plan` there.
4. Claude reports only counts and closed codes (and, if needed, a few product handles). Nothing is copied out of the editor: the token step's output stays in the editor and is gone when the tab closes.

The export stays no-save: "Save manual executions" is never switched on, and no execution holding the token is saved or read through the API.

## Re-importing this workflow (keeps the record)

The export has `staticData: null` and the repo import helper sends the repository file, not a merge, so a plain re-import would wipe `kitTagRecord` and the next run would undo people's own tag changes once. Every re-import of this workflow after the first scheduled run (L8 if it runs later, any rules change, any fix) therefore:

1. Backs up the live workflow through the local API (`http://127.0.0.1:5678/api/v1`), including its `staticData`, to a local `.tmp` file (never committed), and notes the number of products in `staticData.global.kitTagRecord.products`.
2. Re-imports the merged export, keeping the private values (`SHOP_DOMAIN`, the alert workflow id, the credential binding).
3. Re-applies the backed-up `staticData` in the same update: one public-API `PUT /workflows/{id}` with the new nodes and connections, the private values, and the backup's `staticData`. Not near the 03:00 SGT run.
   - On an **active** workflow this PUT also re-publishes (`publishIfActive` defaults to `true`), which needs the API key scope `workflow:activate` **and** the caller's `workflow:publish` permission on this workflow (n8n checks it on the workflow: an instance-wide (global) role, a project role or a share of the workflow all count).
   - **403:** one of the two is missing. The old version stays live, but n8n has **already saved** the new nodes as an unpublished draft and written the sent settings and `staticData` to the workflow. Stop, roll back by the 403 path below, and report.
   - On an inactive workflow (L8 before activation) nothing is published and a 403 cannot come from publishing.
4. Reads back with a `GET /workflows/{id}`: the PUT answered HTTP 200; on an active workflow `activeVersionId` changed and `activeVersion` carries the new nodes; and, from the workflow itself, not from `activeVersion` (settings and `staticData` are not versioned), the settings and `staticData.global.kitTagRecord` with the same product count as the backup. Any difference stops the step. With n8n 2.48.5's default publication service (`N8N_USE_WORKFLOW_PUBLICATION_SERVICE=true`), `activeVersionId` changes as soon as the publication is queued and the triggers are registered a moment later; an unchanged Schedule Trigger is not re-registered at all, and a failed registration does not unpublish the workflow (the public API does not show it).

Rollback (read back each time with a `GET`, settings and `staticData` from the workflow itself):

- **After a 403 in step 3** (the new version never went live): `PUT /workflows/{id}?publishIfActive=false` with the backup's nodes, connections, settings and `staticData`. It needs only `workflow:update` and publishes nothing. Read back that `activeVersionId` is unchanged (the backup's) and that the settings and the `kitTagRecord` product count equal the backup's. Afterwards `versionId` differs from `activeVersionId`: the rollback saves a new `versionId` whose nodes are the backup's nodes (the live version's only if the backup held no unpublished draft). That is expected, not an unknown draft.
- **After a successful re-import that must be undone:** first `GET` the workflow and note the current `kitTagRecord` product count (a 03:00 run since the re-import may have added to it). Then the same PUT without `publishIfActive=false` and **without `staticData`** (a field left out is not written, so the current record is kept), so the backup's nodes are published again (the scopes step 3 already had). Send the backup's `staticData` only when the record itself must be undone, and say so in the report. Read back that `activeVersion` carries the backup's nodes, that the settings equal the backup's, and that the record count equals the one noted just before the PUT.
- **Settings are merged, not replaced** (both rollbacks): n8n merges the sent `settings` over the stored ones, so a settings key the re-import added and the backup lacks is not removed. For `errorWorkflow`, send `"DEFAULT"` to clear it; for any other such key, the read-back "settings equal the backup's" stops the step: report it.
- On any other error answer, or if `activeVersionId` is not what the case above expects (for example, with `N8N_USE_WORKFLOW_PUBLICATION_SERVICE=false`, a failed re-publication can leave the workflow unpublished), stop and report: re-publishing is the Controller's call.

Before the first scheduled run the record is empty and steps 1, 3 and 4 have nothing to keep, but they are still run, so the procedure is the same every time. Checked against n8n's source at tag `n8n@2.48.5`: the public `PUT /workflows/{workflowId}` (`workflow:update`; re-publishing also `workflow:activate` and `workflow:publish`), `UpdateWorkflowQueryDto` (`publishIfActive` default `true`) and `WorkflowService.update` (`staticData` and settings are saved before the publish check, which refuses with 403 and keeps the draft; `workflow:publish` is checked with the workflow's id); `workflow-entity-mapper.ts` (only fields in the payload are written); `workflows.config.ts` (`useWorkflowPublicationService`, default `true`) and `publication/trigger-diff.ts`; `workflow.service.ts` (`update` merges the sent settings over the stored ones) and `workflow-helpers.ts` (`removeDefaultValues` deletes a settings key whose value is `DEFAULT`).

## Live path

| Step | Who | What | Owner OK words | Rollback |
| --- | --- | --- | --- | --- |
| L0 | Owner | Create the Dev Dashboard app (above). | "OK create the kit re-tagger Shopify app" | Uninstall the app from the store and delete it in the Dev Dashboard. |
| L1 | Owner | Create the Custom Auth credential in the n8n UI (above). | (owner action) | Delete the credential in the n8n UI. |
| L3 | Work Claude | Import `XB Kit Re-tagger` INACTIVE through the local API, set `SHOP_DOMAIN` and the alert workflow id privately, bind the credential. | "OK import kit re-tagger inactive" | Archive the imported workflow (it never ran on a schedule). |
| L4 | Work Claude | One manual dry run in the open n8n editor (Chrome on the server), read there as above: reads Shopify, writes nothing, sends nothing, saves no execution. Report the summary (and whether the brake or the outside-changes stop would fire) to the Controller. | "OK kit re-tagger dry run" | Nothing to roll back (no write, no record, no execution). |
| L5 | Controller / owner | Review the dry-run plan. If the L4 plan already trips the brake or the outside-changes stop, stop here and hand it to the Controller. Note (not a gate): staff may say whether any other tool or person maintains kit tags in bulk (the 13 Nov product that got kit tags by itself); the record, the brake and the outside-changes stop cover it either way, and it is never routed to the owner. | "OK kit re-tagger real run" | Nothing to roll back. |
| L6 | Work Claude | One manual run with `DRY_RUN` off; spot-check 2-3 changed products in Shopify admin and in their kit collections. An anomaly stop or error sends its email as it would at night (from L2 on; before L2 the sender refuses `KIT-RETAG`, which Claude sees in the open editor). | (from L5) | Tags are restored by a corrected run or by hand; the record is not involved (manual runs do not save it). |
| L7 | G2 / owner | G2 review of the exact head; owner merges. | owner merge | Revert PR. |
| L2 | Work Claude | After L7, before L8: update the live `XB Ops Alert Email` with `KIT-RETAG` from the **merged** export, by the house procedure (`docs/daily_health_check.md`, #325 L1): back up the live sender, diff it node by node against the pre-PR export and stop on any drift beyond the known private values (recipient, table ID, Gmail credential binding); re-import from the merged export keeping those private values (local `.tmp` copy); re-publish; read back. | "OK re-import XB Ops Alert Email with KIT-RETAG" | Re-apply the backup taken from the live read; read back. |
| L8 | Work Claude | Re-import the merged export by the procedure above (back up `staticData` first, re-apply, read back) if it differs from what ran, set `DRY_RUN` to `false` in the live copy, activate. | "OK activate kit re-tagger" | Deactivate; if it was re-imported, re-apply the backup (nodes, settings, `staticData`) and read back. |
| L9 | Work Claude | After L8: bind the re-tagger in the live `XB Daily Health` (below): set `REPLACE_WITH_KIT_RETAGGER_WORKFLOW_ID` in `WATCH` to the live re-tagger's id by the health workflow's own update procedure (`docs/daily_health_check.md`: back up, diff node by node and stop on drift, keep every private value, publish, read back). | "OK bind kit re-tagger in daily health" | Re-apply the health workflow backup; read back. |

L2 runs after the merge (L7) so that only a merged export goes into the shared, active sender. No email is needed before L8: dry runs send none, and an error at L4 or L6 is seen in the open editor.

## Daily health (added by PR #340, now on main)

Controller ruling, 21 Nov 2026: the daily health entry for the re-tagger was folded into PR #340, which is merged; this PR does not edit `XB Daily Health`. Its `error_runs` count (`errors: true`) needs saved failed executions, which this workflow does not keep (the access token is in item data), so #340 added (`docs/daily_health_check.md`, "Kit re-tagger"):

- a `WATCH.n8n_workflows` entry `{ id: 'REPLACE_WITH_KIT_RETAGGER_WORKFLOW_ID', label: 'kit re-tagger', errors: false, bind_later: true }`. It is skipped while the id is still its placeholder; Work Claude binds it at L9, and from then on an inactive or missing re-tagger is a FAIL (the `bind_later` flag can stay: it skips the entry only while the id still starts with `REPLACE_WITH_`); and
- a count of `xb_alert_email_log` rows with `system` `KIT-RETAG` and `severity` `ERROR` in the 24 hours before the check (every error path here, both anomaly stops included, sends exactly one such alert): WARN `kit_retag_errors` under the n8n workflows job. It needs no id and stays at 0 until the re-tagger runs.

Every error path already ends the run as failed (`End Run As Failed`), so if failed executions are ever kept, `errors: true` would count them too.

## Rules

- The rules are pasted into the Code node `Work Out Kit Tag Changes` as `RULES` (logic fields only: `slots[*].any`, `profiles[*].slots`, `exclude.title_words`, `exclude.types`). The display fields of the partner file (`_about`, labels, titles, `xt_url`, `pitch`) are not needed and are left out.
- [`tests/fixtures/xb_kit_rules_v1.fixture`](../tests/fixtures/xb_kit_rules_v1.fixture) holds the same subset of the XT partner pack `XB_kit_rules.json` v1 (22 Oct 2026, sha256 `55bec65ee29fe1bdc4425920d85358cfa321f2c050a01f04d2c121bde4b6e1f5`). The test fails if the pasted table and the fixture differ.
- A rules change comes from XT as a new JSON file and lands only through a reviewed repo PR that updates the fixture and the pasted table together. The workflow never fetches rules at run time.
- Rules-change procedure, so the owner never gets an anomaly email for a planned change:
  1. Reviewed PR with the new fixture and pasted table; owner merges.
  2. Re-import the merged export by the re-import procedure above (record kept), with `DRY_RUN` on in the live copy.
  3. One editor dry run with the merged rules (L4 procedure). Report the summary to the Controller.
  4. If neither stop would fire, set `DRY_RUN` back to `false` (read back). If the brake or the outside-changes stop would fire (for example a rules change that drops a kit), the change goes live only through a reviewed one-run PR (for example a temporary threshold change, reverted after one scheduled run), each step with the owner's OK.
- Matching (unchanged from the partner logic): case-insensitive, trimmed; a product gets `kit-<kit>` when it matches any slot of that kit, plus `ks-<slot>` for each matching slot that one of its kits uses; title words such as `kids`, `baby`, `brief` and the types `Underwear` and `Bra` keep a product out of every kit.

## Rollback and recovery

- Stop: deactivate the workflow. Nothing else runs it.
- A wrong run: deactivate, then do a dry run (above) to see what it would change now; tags can be restored by a corrected run or by hand. A tag restored by hand is then left alone by the workflow.
- Brake or outside-changes email: dry run in the editor (above), read the summary and plan, hand it to the Controller (see "Anomaly stops").
- Any re-import keeps the record (see "Re-importing this workflow").

## Tests

[`tests/test_xb_kit_retagger_workflow.py`](../tests/test_xb_kit_retagger_workflow.py) runs offline: Node executes the committed Code nodes (with a fake workflow static data record), the pagination and IF expressions on fake Shopify pages, and every alert request goes through the shared sender's own `Validate Alert Request`. CI: `.github/workflows/xb-kit-retagger-tests.yml`.
