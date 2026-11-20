# XB Kit Re-tagger: runbook (#269 Part A)

The n8n workflow `XB Kit Re-tagger` ([`n8n-workflows/xb_kit_retagger.workflow.json`](../n8n-workflows/xb_kit_retagger.workflow.json)) keeps the "Pack for this trip" gear-kit tags on the XB Shopify store up to date. The 8 kit smart collections are "tag `kit-<kit>` AND in stock", so a new product only joins a kit once it carries the tag. Every day at 03:00 SGT the workflow reads every ACTIVE product, works out its kit tags from the XT kit rules v1 and adds or removes only those tags.

It is hands-off (owner, 21 Nov 2026): set up once, and the owner hears from it only when something needs fixing. Committing the workflow changes nothing live. Every live step below needs the owner's OK in the words shown.

## For the owner

- Every night at 3 am the kit tags on the website are brought up to date with the trip kit rules. You get no email when it works.
- You get an email only when something needs fixing. Each email says in plain words what happened and ends with "Ask Claude to check." Forward it to Claude or open a chat and paste it.
- If a person on the team adds or removes a kit tag by hand after the workflow changed it, the workflow leaves that product's tag as the person set it.
- If one night it plans to take products out of kits far more than usual, it changes nothing that night and emails you. Nothing on the website changes until Claude has looked.
- To stop it completely, ask Claude to switch it off. Nothing else on the website depends on it.

## What it changes and what it never touches

- Changes only the 30 known kit tags: `kit-<kit>` for the 8 kits (`tropical`, `hut`, `himalaya`, `expedition`, `winter`, `travel`, `island`, `ride`) and `ks-<slot>` for the 22 slots.
- Never touches `kit-no` (staff: keep this product out of every kit), `kit-pin-<kit>` (staff: force this product into one kit; it gets `kit-<kit>` only), any other `kit-`/`ks-` tag it does not know (left alone and listed in the dry-run output), or any other tag, price, stock, status or product field.
- Reads ACTIVE products only, so draft and archived products keep their old kit tags (they do not show on the storefront).
- Writes with the Admin GraphQL `tagsAdd` / `tagsRemove` mutations, one product per request, one request every 500 ms. Both are safe to repeat. Adds have no limit.
- Stock is not checked by the tagger; the collections do that.

## Never fighting a person

- The workflow keeps a record in its own workflow static data, key `kitTagRecord`: `{ v: 1, products: { <product number>: { d: <SGT date>, t: { <tag>: "a" | "r" } } } }`, one entry per product and known tag it added (`a`) or removed (`r`), written only after Shopify confirmed that part of the write.
- A known tag it removed (`r`) that is back on the product: a person put it back. It is never removed again. A known tag it added (`a`) that is gone: a person took it off. It is never added again. Both are silent; the dry-run output counts them (`left_alone_person_added`, `left_alone_person_removed`).
- Its own earlier changes never block a later rule change (it may add a tag it once removed, and remove a tag it once added and that is still there).
- Size: entries for products that are no longer active are dropped on every run that gets as far as a plan; past 5,000 products the oldest entries are dropped. The first run starts with an empty record; a missing or malformed record is treated as empty, entry by entry.
- n8n saves static data only after scheduled (production) runs, not manual runs. A manual real run (L6) changes tags but does not add to the record; the record starts with the first scheduled run after activation.

## Removal brake

- When the night's plan would take known tags off more products than normal, it changes nothing that night (no adds, no removes, record untouched), sends one ERROR alert and the run ends as failed.
- "More than normal" (internal, not an owner setting, never in owner text): more products losing a known tag than 10% of the products that carry a known tag now (rounded up), and never fewer than 10 products. Reason: the rules change only through a reviewed PR, so on a normal night only a handful of products change type, vendor or title. A jump past 10% points to bad data (a bulk vendor or type rename, a read that looked complete but was wrong) that would empty kits on the website. Adds are not braked: an add only puts a matching product into a kit.
- What Claude does: run it by hand with `DRY_RUN` on (L4 procedure) and read the plan. If the plan is right (for example a deliberate bulk change by staff), the fix is a reviewed PR or a one-off, owner-approved manual cleanup; the brake has no switch.

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
| `some_updates_failed` | Any write: HTTP error, GraphQL error (THROTTLED included), missing data, wrong product, `userErrors` | ...could not update some products. The other products were updated. (or: could not update the products on its list.) |
| `unexpected_error` | A Code node failed | ...stopped on an unexpected error. Some products may already have been updated. |

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

A dry run sends no email. Its plan is the output item of `Work Out Kit Tag Changes`: `mode: "dry_run"`, `summary` (scanned, tagged, changed, losing, brake_at, braked, tags to add and remove, people's changes left alone, unknown tags) and `plan` (handle, add, remove per product). Because the export saves no executions, a dry run at a live step is read like this, as one owner-approved step:

1. In the live copy only, turn on "Save manual executions" for this workflow.
2. Run it once by hand with `DRY_RUN` on.
3. Read that execution through the local n8n API (`http://127.0.0.1:5678/api/v1/executions/<id>?includeData=true`), only the `Work Out Kit Tag Changes` output.
4. Delete that one execution through the local API (it holds a 24-hour access token) and turn "Save manual executions" off again.

## Live path

| Step | Who | What | Owner OK words |
| --- | --- | --- | --- |
| L0 | Owner | Create the Dev Dashboard app (above). | "OK create the kit re-tagger Shopify app" |
| L1 | Owner | Create the Custom Auth credential in the n8n UI (above). | (owner action) |
| L2 | Work Claude | Back up and re-import `XB Ops Alert Email` with `KIT-RETAG`, re-publish. | "OK re-import XB Ops Alert Email with KIT-RETAG" |
| L3 | Work Claude | Import `XB Kit Re-tagger` INACTIVE through the local API, set `SHOP_DOMAIN` and the alert workflow id privately, bind the credential. | "OK import kit re-tagger inactive" |
| L4 | Work Claude | One manual dry run, read through the local API as above: reads Shopify, writes nothing, sends nothing. Report the summary (and whether the brake would stop it) to the Controller. | "OK kit re-tagger dry run" |
| L5 | Controller / owner | Review the dry-run plan. Before the first real run, confirm with staff that no other tool or person maintains kit tags in bulk (the 13 Nov product that got kit tags by itself): a second tagger would trip the brake or be left alone. | "OK kit re-tagger real run" |
| L6 | Work Claude | One manual run with `DRY_RUN` off; spot-check 2-3 changed products in Shopify admin and in their kit collections. A brake or error sends its email as it would at night. | (from L5) |
| L7 | G2 / owner | G2 review of the exact head; owner merges. | owner merge |
| L8 | Work Claude | Re-import the merged export if it differs from what ran, set `DRY_RUN` to `false` in the live copy, activate. | "OK activate kit re-tagger" |
| L9 | LATER (Controller) | Daily health entry for the re-tagger (below). | separate |

The first real run (L6) is gated on L5, including the answer about another tagger. If the L4 plan already trips the brake, stop at L5 and hand it to the Controller.

## Daily health (LATER for the Controller)

`XB Daily Health` does not watch this workflow today, and this PR does not edit it. Its `error_runs` count (`errors: true`) needs saved failed executions, which this workflow does not keep (the access token is in item data). What would be needed, in a separate daily health slice:

- a `WATCH.n8n_workflows` entry `{ id: 'REPLACE_WITH_KIT_RETAGGER_WORKFLOW_ID', label: 'kit re-tagger', errors: false }`, so an inactive or missing re-tagger shows; and
- a count of `xb_alert_email_log` rows with `system` `KIT-RETAG` and `severity` `ERROR` in the 24 hours before the check (every error path here, the brake included, sends exactly one such alert), shown under the n8n workflows job like `alert_email_failed`.

Every error path already ends the run as failed (`End Run As Failed`), so if failed executions are ever kept, `errors: true` would count them too.

## Rules

- The rules are pasted into the Code node `Work Out Kit Tag Changes` as `RULES` (logic fields only: `slots[*].any`, `profiles[*].slots`, `exclude.title_words`, `exclude.types`). The display fields of the partner file (`_about`, labels, titles, `xt_url`, `pitch`) are not needed and are left out.
- [`tests/fixtures/xb_kit_rules_v1.fixture`](../tests/fixtures/xb_kit_rules_v1.fixture) holds the same subset of the XT partner pack `XB_kit_rules.json` v1 (22 Oct 2026, sha256 `55bec65ee29fe1bdc4425920d85358cfa321f2c050a01f04d2c121bde4b6e1f5`). The test fails if the pasted table and the fixture differ.
- A rules change comes from XT as a new JSON file and lands only through a reviewed repo PR that updates the fixture and the pasted table together. The workflow never fetches rules at run time.
- Matching (unchanged from the partner logic): case-insensitive, trimmed; a product gets `kit-<kit>` when it matches any slot of that kit, plus `ks-<slot>` for each matching slot that one of its kits uses; title words such as `kids`, `baby`, `brief` and the types `Underwear` and `Bra` keep a product out of every kit.

## Rollback and recovery

- Stop: deactivate the workflow. Nothing else runs it.
- A wrong run: deactivate, then do a dry run (above) to see what it would change now; tags can be restored by a corrected run or by hand. A tag restored by hand is then left alone by the workflow.
- Brake email: dry run (above), read the plan, hand it to the Controller.

## Tests

[`tests/test_xb_kit_retagger_workflow.py`](../tests/test_xb_kit_retagger_workflow.py) runs offline: Node executes the committed Code nodes (with a fake workflow static data record), the pagination and IF expressions on fake Shopify pages, and every alert request goes through the shared sender's own `Validate Alert Request`. CI: `.github/workflows/xb-kit-retagger-tests.yml`.
