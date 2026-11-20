# XB Kit Re-tagger: runbook (#269 Part A)

The n8n workflow `XB Kit Re-tagger` ([`n8n-workflows/xb_kit_retagger.workflow.json`](../n8n-workflows/xb_kit_retagger.workflow.json)) keeps the "Pack for this trip" gear-kit tags on the XB Shopify store up to date. The 8 kit smart collections are "tag `kit-<kit>` AND in stock", so a new product only joins a kit once it carries the tag. Every day at 03:00 SGT the workflow reads every ACTIVE product, works out its kit tags from the XT kit rules v1 and adds or removes only those tags.

Committing the workflow changes nothing live. Every live step below needs the owner's OK in the words shown.

## What it changes and what it never touches

- Changes only the 30 known kit tags: `kit-<kit>` for the 8 kits (`tropical`, `hut`, `himalaya`, `expedition`, `winter`, `travel`, `island`, `ride`) and `ks-<slot>` for the 22 slots.
- Never touches `kit-no` (staff: keep this product out of every kit), `kit-pin-<kit>` (staff: force this product into one kit; it gets `kit-<kit>` only), any other `kit-`/`ks-` tag it does not know (left alone and listed in the dry run), or any other tag, price, stock, status or product field.
- Reads ACTIVE products only, so draft and archived products keep their old kit tags (they do not show on the storefront).
- Writes with the Admin GraphQL `tagsAdd` / `tagsRemove` mutations, one product per request, one request every 500 ms. Both are safe to repeat.
- Stock is not checked by the tagger; the collections do that.

## Settings (`Run Settings` node)

| Setting | Default | Meaning |
| --- | --- | --- |
| `DRY_RUN` | `true` | Read and email the change list, write nothing. Set to `false` only for an owner-approved real run, and at activation. |
| `CHANGE_CAP` | `25` | A real run writes nothing (one WARN email) if more products would change. |
| `REMOVE_CAP` | `10` | A real run writes nothing (one WARN email) if more products would lose a `kit-<kit>` tag. |
| `CAP_OVERRIDE` | `false` | Owner only, for one manual run (for example a larger backlog the dry run showed is right). Set back to `false` straight after. The workflow never sets it. |
| `API_VERSION` | `2026-10` | Admin API version. Bumping it is a one-value change. |
| `SHOP_DOMAIN` | `REPLACE_WITH_SHOPIFY_SHOP_DOMAIN` | The store's myshopify domain. Set in the local import copy only, never in this repository. |

## Emails (through `XB Ops Alert Email`, system `KIT-RETAG`)

| When | Subject | Severity |
| --- | --- | --- |
| Dry run (always, even with no change) | `[XB daily] KIT-RETAG DRYRUN <date>` | INFO |
| Real run that changed tags | `[XB daily] KIT-RETAG CHANGED <date>` | INFO |
| Over a cap: nothing written | `[XB alert] KIT-RETAG WARN change_cap` | WARN |
| Token, read or write failure; unexpected error | `[XB alert] KIT-RETAG ERROR token_failed` / `read_failed` / `read_incomplete` / `write_failed` / `unexpected` | ERROR |
| Real run with nothing to change | no email | - |

Lines carry counts, per-kit counts and product handles (lower case) with `+tag` / `-tag`; never titles, customer data or tokens. A failed token, read, throttle (`THROTTLED`) or incomplete read (pages missing, scanned count different from Shopify's exact count of active products) writes nothing. A failed write (HTTP error, GraphQL error or any `userErrors`) is listed by handle with a closed code; the next daily run repairs it.

Executions are not saved (success and error both `none`, manual runs not saved), because items after the token step carry the 24-hour access token. The emails are the record. During a manual run the output is visible in the open editor.

## Shopify app and n8n credential (owner)

- **Shopify app (L0):** a separate Dev Dashboard app, for example "XB Kit Re-tagger", with scopes `read_products` and `write_products` only, installed on the XB store, in the same Shopify organisation as the store (needed for the client credentials grant). Do not reuse another app.
- **n8n credential (L1):** type **Custom Auth** (`httpCustomAuth`), created by the owner in the n8n UI. Its single field `JSON` holds:

  ```json
  { "body": { "client_id": "<the app's client ID>", "client_secret": "<the app's client secret>" } }
  ```

  Paste the real values only in the n8n UI: never in chat, a file, an issue or this repository. If the credential form offers "Allowed HTTP Request Domains", set it to the store's myshopify domain only.
- Bind it on **Get Shopify Access Token** only. That node posts `grant_type=client_credentials` as a form body with an explicit `Content-Type: application/x-www-form-urlencoded` header; with that header n8n merges the credential's `body` values into the same form body, which is the request shape shopify.dev documents for this grant. Redirects are not followed, so the credential is never sent anywhere else.
- How this was checked (G1, 21 Nov 2026): against n8n's public source (`HttpCustomAuth.credentials.ts`, HTTP Request V3 and the legacy request adapter on the current master), not against the live instance's version. The first dry run (L4) proves it: a wrong shape gives a `token_failed` email with an `http_4xx` code and nothing else happens.
- The token (valid 24 hours) is fetched fresh on every run and passed to the read and write requests in the `X-Shopify-Access-Token` header. The Code nodes never read environment variables or credentials.

## Live path

| Step | Who | What | Owner OK words |
| --- | --- | --- | --- |
| L0 | Owner | Create the Dev Dashboard app (above). | "OK create the kit re-tagger Shopify app" |
| L1 | Owner | Create the Custom Auth credential in the n8n UI (above). | (owner action) |
| L2 | Work Claude | Back up and re-import `XB Ops Alert Email` with `KIT-RETAG`, re-publish. | "OK re-import XB Ops Alert Email with KIT-RETAG" |
| L3 | Work Claude | Import `XB Kit Re-tagger` INACTIVE through the local API, set `SHOP_DOMAIN` and the alert workflow id privately, bind the credential. | "OK import kit re-tagger inactive" |
| L4 | Work Claude | One manual run with `DRY_RUN` on: reads Shopify, writes nothing, sends the DRYRUN email. | "OK kit re-tagger dry run" |
| L5 | Owner | Review the DRYRUN list. Before the first real run, confirm with staff that no other tool or person maintains kit tags (the 13 Nov product that got kit tags by itself): two writers on the same tags would fight. | "OK kit re-tagger real run, cap 25" |
| L6 | Work Claude + owner | One manual run with `DRY_RUN` off and the caps on; spot-check 2-3 changed products in Shopify admin and in their kit collections. | (from L5) |
| L7 | G2 / owner | G2 review of the exact head; owner merges. | owner merge |
| L8 | Work Claude | Re-import the merged export if it differs from what ran, set `DRY_RUN` to `false` in the live copy, activate. | "OK activate kit re-tagger" |
| L9 | later slice | Daily health WATCH entry for the re-tagger's last run (it saves no executions). | separate |

The first real run (L6) is gated on L5, including the answer about another tagger.

## Rules

- The rules are pasted into the Code node `Work Out Kit Tag Changes` as `RULES` (logic fields only: `slots[*].any`, `profiles[*].slots`, `exclude.title_words`, `exclude.types`). The display fields of the partner file (`_about`, labels, titles, `xt_url`, `pitch`) are not needed and are left out.
- [`tests/fixtures/xb_kit_rules_v1.fixture`](../tests/fixtures/xb_kit_rules_v1.fixture) holds the same subset of the XT partner pack `XB_kit_rules.json` v1 (22 Oct 2026, sha256 `55bec65ee29fe1bdc4425920d85358cfa321f2c050a01f04d2c121bde4b6e1f5`). The test fails if the pasted table and the fixture differ.
- A rules change comes from XT as a new JSON file and lands only through a reviewed repo PR that updates the fixture and the pasted table together. The workflow never fetches rules at run time.
- Matching (unchanged from the partner logic): case-insensitive, trimmed; a product gets `kit-<kit>` when it matches any slot of that kit, plus `ks-<slot>` for each matching slot that one of its kits uses; title words such as `kids`, `baby`, `brief` and the types `Underwear` and `Bra` keep a product out of every kit.

## Rollback and recovery

- Stop: deactivate the workflow. Nothing else runs it.
- A wrong run: run it by hand with `DRY_RUN` on to see what it would change now; tags can be restored by a corrected run or by hand. The DRYRUN and CHANGED emails list what changed.
- Over-cap WARN: run by hand with `DRY_RUN` on and read the list; if it is right, the owner sets `CAP_OVERRIDE` to `true` for one manual run, then back to `false`.

## Tests

[`tests/test_xb_kit_retagger_workflow.py`](../tests/test_xb_kit_retagger_workflow.py) runs offline (Node executes the committed Code nodes and the pagination expressions on fake Shopify pages, and every alert request goes through the shared sender's own `Validate Alert Request`). CI: `.github/workflows/xb-kit-retagger-tests.yml`.
