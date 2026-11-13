# #268 member core: S$300 online order -> AC2 `MEMBER` with a 2-year expiry, plus claim links

Tool: `directlocal/xb_member_benefits.py` (working name, kept so later slices extend it), its AutoCount child `scripts/ac2_member_benefits_bridge.ps1` + `scripts/ac2_member_benefits.ps1` (profile `benefit_p1`), the task installer `scripts/install_member_benefits_task.ps1`, configs `config/member_benefits.example.json` (test book) and `config/member_benefits.live.example.json`. Tests: `tests/test_member_benefits.py`, `tests/test_member_benefits_ps.py`.

**Default off.** Nothing runs on merge. External effects: none until the VM steps below, each with its own owner OK.

## 1. What it does, in plain words

Every 15 minutes (about 7 minutes after `\XB\XB-MemberSync`) the tool:

1. Reads paid Shopify orders that changed since its last clean read. Read only, through its **own** Shopify app whose token must hold exactly `read_orders` and `read_customers`. A read that ends at the page cap (20 pages x 5 orders) records what it read, keeps Shopify's cursor and alerts `orders_read_incomplete`; the next run continues after that cursor, so a bulk update of many orders delays new orders by a few runs instead of stopping the reader. The watermark (newest `updatedAt`, minus a 15-minute overlap) moves only when a read reaches the end.
2. Writes one row per order into its own ledger on the VM (`member_benefits.sqlite3`), forever. Reading the same order again updates the same row.
3. Works out the order's qualifying amount: line prices **before discounts, including GST, excluding shipping**, less refunded or removed units, **less everything paid by gift card**, never below zero. Sale items count. **Buying a gift card counts; the part of an order paid with a gift card does not** (owner, 10 Nov 2026): S$500 of goods with S$200 paid by gift card is S$300 and qualifies; S$400 of goods with S$150 paid by gift card is S$250 and does not. The gift-card-paid amount is the sum of the order's successful `SALE` and `CAPTURE` transactions whose gateway is one of `gift_card_gateways`. It comes off in full, even when part of it paid for shipping: a payment cannot be tied to a line, so this can only under-count (staff can upgrade by hand). A refund back to a gift card is never added back. An order whose payments cannot be read (10 or more transactions; a transaction count from Shopify, `Order.transactionsCount`, that is missing, not `EXACT` or not equal to the transactions listed; a payment on a gift card gateway whose kind is not one of the eight kinds Shopify documents; a gift card payment that is still `PENDING`, `AWAITING_RESPONSE` or `UNKNOWN`; or a transaction amount that is not SGD or is malformed) is `amount_unverifiable` with code `payments_unreadable` and the alert `order_amount_unverifiable`; it is measured again when the order is next read. The gateway name is compared trimmed and in lower case. An order with more than 20 line items (rows, not units) cannot be read in one page: it is `amount_unverifiable` with code `currency_or_lines` and the same alert, and a person decides it. The same amount is used for the first decision, for the second look at a held claim receipt and for the check after an upgrade. **Refunds by money are never netted** (Controller, fix round 1): an order that is fully refunded (`REFUNDED`), or whose refunded money is more than the value of its refunded or removed units (an amount-only refund, a shipping refund or an adjustment), is held before the decision (`refund_unattributed`, one alert) and never decided automatically.
4. If one order is **S$300.00 or more**, the customer carries the #155 sign-up marker (tag `member-mg` and the `membership.start_date` time) and the order's Singapore date is on or after the sign-up date (or the same Singapore day), it finds the customer's AC2 member through the #155 sync's links (read only) or its own claim links.
5. Reads that AC2 member. AutoCount decides the type:
   - `GUEST` -> `MEMBER` with `ExpiryDate` = order's Singapore date + 24 months (`would_upgrade`);
   - `MEMBER` -> same type, `ExpiryDate` moved **later** only (`would_extend`); already later -> no change;
   - `Default`, held, inactive or any other type -> counted, never written.
6. **Observe** (the default): records and reports `would_upgrade` / `would_extend`; no AutoCount save is reachable. **Apply** (switch `benefits_write_enabled` on, at most `max_upgrades_per_run`, default 3): one `SaveMember` per decision through `benefit_p1`, with the #268 guard set (book guard, host pin, identity proof, lock, `STOP` file, `INTENT` / `DONE` / `NOT_WRITTEN` / `UNCERTAIN` / `HELD`, read-back of every column, exit 2 with nothing partial).

**Claim links (the owner's "auto linking").** An AC2 `MEMBER` or `GUEST` with no #155 link, **exactly one** Shopify customer whose phone gives that MemberNo under the #155 phone rule, and the #155 two-word name rule matches -> linked in this tool's ledger (`claim_links`). Anything else -> no link and one `claim_needs_review` alert per customer: two MemberNo forms of one phone in AC2, more than one customer on the phone (or more candidates than the search returns in one page), a name mismatch, or a link conflict. A customer never maps to two MemberNos across both ledgers (and the reverse); a conflict holds that member (`link_conflict`) and blocks any write for it. The #155 ledger is opened read-only (`mode=ro`, `query_only`) and never copied or written.

**Claim outcomes settle the receipt.** A claim review (`claim_name_mismatch`, `claim_phone_not_unique`, `claim_link_conflict`, `claim_memberno_conflict`, `claim_pair_unlinked`), a member out of scope (`claim_member_not_in_scope`, for example still `Default`) or a customer with no phone (`claim_no_phone`, alert `qualified_member_no_phone`) puts the receipt in `held` with that code. It is not re-checked; it re-opens (back to `ready`) only when a #155 link or a claim link for that customer appears (for example after the B3 index), and only in a run whose orders read was complete (slice B2: a read that ended at the page cap counts `claim_reopen_deferred` and re-opens on the next complete read). An order 60 days old or older can never be read again (Shopify's `read_orders` window), so from that age a held claim receipt settles to `order_past_read_window` and a `waiting_member` receipt is held with that code, with one `receipt_past_read_window` alert each (slice B2; exactly 60 days settles). A genuine `waiting_member` receipt (no AC2 member yet) is re-checked at most once per `waiting_member_recheck_hours` (default 6), or at once when a link for the customer appears. Linked `ready` receipts are decided first, then other `ready` ones, then `waiting_member`. Reaching `max_member_reads_per_run` raises the alert `member_reads_capped`.

**A held claim receipt is looked at again whenever its order is read again.** If the order stops qualifying while the receipt is held (a unit refund below S$300, a cancellation, any other reason), the receipt is settled for good with code `order_no_longer_qualifies` and never re-opens: silently, because nothing was granted. A refund by money settles it as `refund_unattributed` with that alert, once. A receipt re-opens only when the order as last read still qualifies. An order that is moved to another customer (or loses its customer) while its receipt is held settles the hold the same way, silently; the new customer is handled by hand.

**Claim links are voided, never edited.** Links written in observe (B1, B2) and by the index (B3) are append-only rows. A wrong one is voided with `unlink` (section 4, "Void one claim link"): the link row stays as history and the void is a new row. A voided pair (that customer and that member) is never linked again by the tool: an order of that customer is held with `claim_pair_unlinked` and one `claim_needs_review` alert, and the index counts `index_pair_unlinked`. The customer may be claimed to another member, and the member to another customer, by the normal rule. Links of the #155 sync are never written, voided or masked by this tool: a wrong #155 link is fixed in the #155 process.

## 2. What it never does

- Any Shopify write: no tag, metafield, customer or discount change. The app holds no write scope; the module contains no GraphQL mutation (pinned by tests).
- Discount codes, the birthday tag, segments.
- POS receipts (in store stays manual by decision).
- Refund revoke, the expiry job, any `MEMBER` -> `GUEST` move, an earlier `ExpiryDate`.
- Any change to the #155 sync (code, ledger, task) or to the #268 type move (code and tests unchanged).

### Upgrade only, enforced at the write boundary

`benefit_p1` (PowerShell child) refuses, before any session or claim: a target type other than `MEMBER`; a source type other than `GUEST` (kind `upgrade`) or `MEMBER` (kind `extend`); a new `ExpiryDate` that is not later than the expected one. Just before `SaveMember` it re-reads the member and skips (`type_not_in_scope_now`, `type_changed_since_read`, `changed_since_read`, `expiry_not_later_now`) when the type, `LastModified` or `ExpiryDate` is not what the parent read. Only `MemberType` (upgrade) and `ExpiryDate` are assigned. The same rules are repeated in Python before the `INTENT` row and as ledger `CHECK`s.

## 3. The rule (config, defaults; the owner may still adjust before go-live)

| Key | Default | Meaning |
|---|---|---|
| `qualify_threshold_cents` | `30000` | S$300.00, inclusive |
| `amount_before_discounts` | `true` | `false` takes line-level and order-level discount allocations off |
| `amount_include_tax` | `true` | Only `true` is accepted: line prices are read incl. GST, which needs `Order.taxesIncluded` true (an order without it is `amount_unverifiable` + alert) |
| `amount_exclude_shipping` | `true` | Shipping lines never count |
| `amount_exclude_gift_cards` | `false` | Gift-card purchase lines count (owner, 10 Nov 2026). `true` takes them off |
| `gift_card_gateways` | `["gift_card"]` | The `OrderTransaction.gateway` names that mean "paid by gift card": 1 to 5 names of `a-z`, `0-9` and `_`; the order's gateway is compared in lower case. What is paid through these never counts. The name is **not** stated on shopify.dev: prove it in step B2 before B4 |
| `amount_count_sale_items` | `true` | Only `true` is accepted: an order line records no compare-at (sale) price, so sale items cannot be told apart |
| `same_day_signup_counts` | `true` | An order placed before sign-up counts only when both fall on the same Singapore day |
| `orders_start_utc` | set at install | Orders created before this never count (`before_start`); the first read starts here |
| `waiting_member_alert_hours` | `48` | A qualifying order with no AC2 member yet alerts `qualified_member_missing` once after this |
| `waiting_member_recheck_hours` | `6` | A `waiting_member` receipt is re-checked (Shopify customer read, phone search, AutoCount reads) at most once per this many hours, unless a link for its customer appears |
| `signup_wait_hours` | `48` | How long an order without a seen sign-up is re-checked before it settles as `not_signed_up` |
| `max_upgrades_per_run` | `3` | Saves per run (hard max 20) |
| `max_member_reads_per_run` | `20` | AutoCount member reads per run |
| `shopify_max_pages` | `20` | Pages of 5 orders read in one run: 100 orders (hard max 40 pages, 200 orders). A read that ends at the cap carries on next run |
| `type_move_state_root` | live: the type move's root | `apply` holds the type move's ledger lock (section 6); required for `apply` on the live book |

`ExpiryDate` is written as the date at 00:00 (AutoCount stores Singapore local time). 29 Feb + 24 months = 28 Feb.

**The rule fingerprint.** `init` keeps a fingerprint of the amount rule in the ledger: `AMOUNT_RULE_VERSION` (in the module; raised by any code change to how an amount is measured) and the settings `qualify_threshold_cents`, `amount_before_discounts`, `amount_include_tax`, `amount_exclude_shipping`, `amount_exclude_gift_cards`, `gift_card_gateways` (order does not matter) and `amount_count_sale_items`. `check-config` prints it (`rule_fingerprint`); `status` and `digest` print `rule_settings_match`. When it differs, `observe` and `apply` refuse with `benefits_rule_settings_changed` before anything is read or recorded, because the receipts already recorded keep their old amount: the way on is a new start (section 7, B2). `status`, `digest`, `resolve`, `index`, `release-held` and `unlink` still run. The sign-up settings, `orders_start_utc`, caps and timeouts are not in it.

Each receipt row keeps the gift-card-paid amount (`gift_card_cents`) and the gateway names of the order's transactions (`payment_gateways`, private ledger only). `digest` shows them under `gift_cards`, for all receipts (not only the last N hours): `receipts_paid_by_gift_card` (receipts with an amount paid by gift card) and `gateways_seen` (for each gateway name, the number of receipts it was seen on). A name is shown only when it is a plain code word (`a-z`, `0-9`, `_`, at most 64 characters); any other name (for example the free text of a manual payment method, or a payment with no gateway) is counted as `other`.

## 4. Commands

```
python directlocal\xb_member_benefits.py check-config --config <config>   # no login, no network, no ledger
python directlocal\xb_member_benefits.py init        --config <config>   # creates the ledger (own state root)
python directlocal\xb_member_benefits.py observe     --config <config>   # the scheduled default: reads + would_* counts
python directlocal\xb_member_benefits.py apply       --config <config>   # needs benefits_write_enabled true
python directlocal\xb_member_benefits.py resolve     --config <config>   # UNCERTAIN rows: read-back only, never a re-send
python directlocal\xb_member_benefits.py status      --config <config>
python directlocal\xb_member_benefits.py digest      --config <config> --hours 24   # read only
python directlocal\xb_member_benefits.py index       --config <config> --max 200 [--apply]
python directlocal\xb_member_benefits.py release-held --config <config> --order-ref o-<20 hex> --member-last3 <3> --finding as_intended|not_as_intended [--apply]
python directlocal\xb_member_benefits.py unlink      --config <config> --order-ref o-<20 hex> | --customer-ref c-<20 hex> --member-last3 <3> --reason wrong_person|customers_merged|member_renumbered|other [--accept-applied] [--apply]
python directlocal\xb_member_benefits.py claim-links --config <config>   # read only: the active claim links
```

`release-held` and `unlink` change only this tool's ledger (no AutoCount save, no Shopify call; `release-held` makes one read-only AutoCount member read). Both are dry runs unless `--apply`, take the keyed ref the digest shows plus the last 3 characters of the MemberNo (never a full MemberNo, phone or name), and leave a `runs` row only with `--apply`.

`claim-links` (slice B2) lists the active claim links, read only (opened as the digest is: no lock, no write, no `runs` row; it also runs while the task holds the ledger): `{"status", "command": "claim_links", "links": [{"ref": "c-...", "member_no_masked": "*******001", "source": "order" | "index", "created_day": "YYYY-MM-DD"}], "active", "voided", "truncated"}`. Entries are sorted by ref, at most 500 (`truncated` true when there are more); `created_day` is the Singapore day the link was made; voided links are counted, never listed. Each `ref` is the `c-` ref `unlink --customer-ref` takes.

`claim-links` and `digest` open the ledger read-only; SQLite may leave `-wal`/`-shm` side files next to it. The main file is unchanged: leave them.

Exit codes: 0 ok, 3 completed with alerts (an operator looks), 2 refused (nothing written). A refused query to Shopify ends with one of `shopify_graphql_error`, `shopify_retry_budget_exhausted`, `shopify_query_cost_exceeds_bucket` or `shopify_access_denied`, with nothing read, recorded or moved. Output carries codes, counts, keyed refs (`o-`, `c-`, `m-`) and MemberNos masked to the last 3 characters only. `observe` and `apply` also print `orders_query_cost`: Shopify's requested cost of the orders query on that run, in whole points, or `null` (section 9).

`apply` refuses while any write is `INTENT`/`UNCERTAIN` (`benefit_uncertain_unresolved`: run `resolve` after the writer deadline) or `HELD` and not released (`benefit_held_rows_present`: see "When a write is HELD" below). A `STOP` file in the state root stops writes before the next member.

Refusal codes of slice B1 (exit 2, nothing recorded):

- `observe`, `apply`: `benefits_rule_settings_changed` (section 3, the rule fingerprint).
- Every command that opens the ledger: `benefits_ledger_not_recognised` (a ledger made by older code; section 7, B0).
- `release-held`: `release_arguments_invalid`, `benefit_uncertain_unresolved`, `writer_may_be_live`, `release_no_held_write` (no unreleased `HELD` write for that ref), `release_member_mismatch`, `release_finding_not_proven` (the AutoCount read does not show what `--finding` says), a `bridge_read_failed:...` code, `ledger_locked_by_another_owner`.
- `unlink`: `unlink_arguments_invalid` (also both or neither of `--order-ref` and `--customer-ref`, and from slice B2 a malformed ref of either kind), `unlink_not_a_claim_link` (no active claim link for that ref: a well-formed `o-` ref that names no receipt, a receipt with no customer, or a customer with no active claim link; also the answer for a customer linked by #155 only), `unlink_member_mismatch`, `benefit_uncertain_unresolved`, `unlink_member_write_held` (release that write first), `unlink_receipt_needs_repair` (run `observe` once), `unlink_has_applied` (needs `--accept-applied`), `ledger_locked_by_another_owner`.
- `claim-links` (slice B2), as `digest`: `benefits_ledger_missing`, `benefits_ledger_unreadable`, `benefits_ledger_not_recognised`, `benefits_ledger_not_initialised`, `benefits_ledger_binding_mismatch`.

New codes of slice B2 (no new refusal besides the malformed refs above):

- Alerts (each once per order): `order_customer_changed_after_upgrade`, `claim_hold_settled_unclear`, `receipt_past_read_window` (see "Alert codes").
- Counts: `claim_reopen_deferred` (a capped read: claim holds re-open on the next complete read), `below_threshold_payments_unread` (payments unreadable, but the goods alone are below the threshold, so `below_threshold` with no alert), `release_receipt_left_open` (`release-held` on a receipt neither `ready` nor decided), `release_receipt_settled_late` (a run met a `ready` receipt whose `HELD` write was released and set it as `release-held` would have: no AutoCount read, no save).
- `resolve` and `release-held` hold `writer_may_be_live` unless the writer probe says `exited` (a probe result `unknown` now holds).

### When a write is HELD (the owner's recovery)

A `HELD` write means the read-back did not prove the save as intended (another column changed, the type or `ExpiryDate` read back wrong, an outbound queue row existed after the save, or `resolve` could not tell). `apply` then refuses every run until the owner decides; `observe` keeps running.

1. Disable the task (`Disable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberBenefits`) and run `status` and `digest` (masked MemberNo, code).
2. Open that member in AutoCount and compare with what the tool meant to write (`MEMBER`, `ExpiryDate` = order date + 24 months) and with the member's history. Note: `outbound_queue_changed` fires when any `UserScript`, `MailQueue` or `EInvoiceQueue` row exists after the save, not only a new one; check whether that row predates the save.
3. Fix the member by hand in AutoCount if needed (the tool never downgrades).
4. **Release one HELD write** (the latch). The `HELD` write row itself stays as it is for ever (rows are immutable, and that receipt is never sent again); the release is a new row.
   1. The task stays disabled (step 1). `status` shows `held_writes`; the digest's `write_held` alert carries the order's `o-` ref and the masked MemberNo. A write made `HELD` by `resolve` has no `write_held` alert (`resolve` prints codes only): its `o-` ref is the one on the earlier `write_uncertain` alert of the run that left the write open. If that run is older than the last 24 hours, use `digest --hours 720`.
   2. Decide the finding from AutoCount: `as_intended` = the member is `MEMBER` with exactly the `ExpiryDate` the tool meant (whoever made it so); `not_as_intended` = anything else (left as it was, or fixed by hand to something else).
   3. Dry run: `release-held --order-ref <o-ref> --member-last3 <last 3> --finding <finding>`. The tool reads the member once (read only) and refuses `release_finding_not_proven` if AutoCount does not show that finding. Check `receipt_now`: `applied` (as intended: the receipt becomes `applied`), `held` (not as intended: the receipt is held with `held_write_released`; upgrade that member by hand if still due), `left_decided` (the receipt was already decided; nothing changes on it) or `left_open` (slice B2: the receipt was neither `ready` nor decided; nothing changes on it now, and if it is `ready` again later the next run sets it as the finding says, count `release_receipt_settled_late`).
   4. Owner OK **"BENEFITS RELEASE HELD <o-ref>"**, then the same command with `--apply`.
   5. `status` shows `held_writes` one fewer. With none left, `apply` runs again. Re-enable the task (`Enable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberBenefits`).
   The release row and its `runs` row are the record. Two `HELD` writes are released one at a time; the latch stays until both are.

### Void one claim link (`unlink`)

Use it for a claim link this tool made (by an order or by the index) that is wrong: the wrong person, two customers merged, a member renumbered. Never for a #155 link (the tool refuses `unlink_not_a_claim_link`; fix it in the #155 process). A claim link that conflicts with a #155 link is exactly what `unlink` is for: voiding the claim ends the conflict on the next run (`link_conflict` receipts already held stay held; staff upgrade by hand if due).

1. Disable the task (as above) and run `status` and `digest`.
2. Dry run: `unlink --order-ref <o-ref> --member-last3 <last 3> --reason <reason>`. The `o-` ref is the order's: the digest's `link_conflict` and `write_held` alerts carry it, and so do the `would_upgrade` and `upgraded` refs of a run's own output. The tool voids the active claim link of that order's customer (any order of that customer names the same link). `--customer-ref <c-ref>` (exactly one of the two) takes the customer's `c-` ref, which the `claim_linked` refs of the run that made the link carry (the digest does not keep it), and which `claim-links` lists for every active link (slice B2). Use `claim-links` for a link the index made for a customer with no order in the ledger: no `o-` ref names it. The output's `claim_unlinked` ref is the `c-` ref of the link it voids. Check `counts`: `unlink_receipt_reset` (ready receipts that lose their member and are linked afresh by the next run: a #155 link, another claim, or a hold), `unlink_receipt_kept` (held, `no_change`, `not_in_scope`: nothing was written; kept), `unlink_receipt_applied`.
3. If a receipt through the link is already `applied`, the tool refuses `unlink_has_applied`. With `--accept-applied` the receipt is kept (the tool never downgrades) and the alert `unlinked_with_applied` names the order: check that member in AutoCount by hand.
4. Owner OK **"BENEFITS UNLINK <ref>"** (the `o-` or `c-` ref given to the dry run), then the same command with `--apply`.
5. `status` shows `claim_links` one fewer and `claim_links_voided` one more. Re-enable the task.

The void row and its `runs` row are the record. Undoing a mistaken unlink is not offered: staff upgrade by hand if due (a `relink` comes only if the shadow week shows a need).

### Alert codes

`refund_on_basis` / `late_refund_on_basis` (after an upgrade: a unit refund took the order below S$300, the order was cancelled, or any refund by money that no unit explains, including `REFUNDED`; nothing changes), `refund_unattributed` (before the decision: refunded by money, held), `orders_read_incomplete`, `member_reads_capped`, `qualified_member_missing`, `qualified_member_no_phone`, `signup_time_missing`, `claim_needs_review`, `link_conflict`, `member_inactive`, `expiry_unreadable`, `guest_expiry_already_later`, `member_not_found`, `order_amount_unverifiable`, `write_uncertain`, `write_held`. From slice B2: `order_customer_changed_after_upgrade` (an upgraded order now shows another customer, or none; the receipt keeps its customer; check that the upgrade went to the right member, fix by hand, and void a wrong claim link with `unlink`), `claim_hold_settled_unclear` (a claim hold was settled for a reason that is not a positive fact: unpaid, sign-up marker gone, amount unreadable or a test order; nothing is granted; upgrade by hand if still due), `receipt_past_read_window` (a held claim receipt or a `waiting_member` receipt reached 60 days and is settled with `order_past_read_window`; upgrade by hand if still due). From `unlink --accept-applied` only: `unlinked_with_applied` (the link was voided but a receipt through it is already `applied`; check that member by hand). From `resolve` only: `receipt_left_decided` (the save is proven `DONE` and the member is saved, but a run had already held or settled the receipt in between, for example a refund by money read before the resolve; the receipt is left as it is; check the member by hand).

`late_refund_on_basis` cannot fire live: `read_orders` returns only orders created in the last 60 days, so a refund after day 60 is never read. It exists for completeness and tests only.

## 5. Owner's Shopify admin step (before B1)

1. Create a **second** app in the Shopify Dev Dashboard for this store (not the #155 sync's app: the sync checks its own scopes exactly, so widening its app would stop the live sync). Scopes: **`read_orders` and `read_customers` only.** No write scope, no `read_all_orders` (we read forward from `orders_start_utc`; `read_orders` sees orders created in the last 60 days).
2. Install it on the store. Put its client ID and secret in the task account's user environment variables `XB_SHOPIFY_BENEFITS_CLIENT_ID` and `XB_SHOPIFY_BENEFITS_CLIENT_SECRET` (never in a file or chat). The access token is cached with DPAPI in the tool's state root, as #155.
3. If Shopify asks for protected customer data access for names and phone numbers, grant it for this app (read only).

Stop condition: the first run refuses `shopify_token_scope_not_expected` or `shopify_store_identity_mismatch` -> fix the app, do not widen the code.

## 6. Keeping the two AutoCount writers apart

- `apply` takes the **#268 type move's own ledger lock** (`member_type_move.lock` in `type_move_state_root`) for the whole run. While it is held, every type move command that opens its ledger (`init`, `snapshot`, `move`, `resolve`, `rollback`, `reconcile`, `status`) refuses with `ledger_locked_by_another_owner`; while a type move command holds it, `apply` refuses with `type_move_running`. The lock is an OS lock, released if a process dies. The type move code is unchanged.
- The type move is attended (no scheduled task). **Before any type move command** (step S1 below), the benefits task is paused, so no benefits run adds AutoCount sessions during a batch.
- `observe` and `index` never save, so they do not take the lock (they are still paused by S1).
- **The type move state root must stay in place** (with its `member_type_move.sqlite3`) while `type_move_state_root` is configured: if it is archived or moved, `apply` refuses every run with `type_move_state_root_invalid`. Change the config in the same sitting as any archive of the type move.
- The #155 sync only creates members (never edits a type), so it cannot touch the same member edit; it writes ChangeLog `I` rows only.

### S1. Pause the benefits task around a type move command (from B1 on)

1. `Disable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberBenefits`.
2. `Get-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberBenefits` shows `State` `Disabled`, not `Running`. If it shows `Running`, wait for the run to end (at most 10 minutes) and check again; **stop if it stays `Running`** and report.
3. Run the type move command and its read-back.
4. `Enable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberBenefits` after the type move read-back.

S1 is mirrored in the type move runbook (`member_type_move_runbook.md`, "Also pause the member benefits task").

## 7. VM steps (each its own owner OK; S1 before any type move command)

| Step | What | Owner OK | Stop condition | Rollback |
|---|---|---|---|---|
| B0 | Clone at **the latest member core merge commit (B2 or later)** (it contains PR #300, PR #301, ledger v2 and slice B2; slice B2 changed no schema and no amount rule). A ledger made by older code is refused (`benefits_ledger_not_recognised`). Live config from `member_benefits.live.example.json` with `orders_start_utc` = the minute B1 starts; `check-config` (record `rule_fingerprint`); `init`. **If slice B1 merges after B0 ran from an older commit:** stop the task, keep the old state folder (rename it, as evidence; do not delete it), create a **new empty state folder**, `init` from the B1 merge commit with the **same** `orders_start_utc`, re-enable. Observe replays every order from `orders_start_utc` (all inside the 60-day window), so receipts and claim links rebuild on the next runs; after B3 also re-run the index (dry runs, then `--apply`). No migration code exists | "BENEFITS INSTALL" | `check-config` refuses | Delete the new state root (no other system touched) |
| B-update | **Only if slice B2 merged after B0 ran.** Disable the task (S1 pattern: `Disabled`, not `Running`); in the clone, `git fetch` and check out the B2 merge commit; `check-config`: `rule_fingerprint` **equals** the one recorded at B0; `status`: `rule_settings_match` true; one `observe` by hand: exit 0 or 3, no exit 2; re-enable. **No re-init, no new state folder** (B2 changed no schema and no amount rule) | "BENEFITS UPDATE B2" | The fingerprint differs, or any exit 2: stop, leave the task disabled, report | Check out `5c61c6f` again, re-enable |
| B1 | Install the task in **observe** mode: `install_member_benefits_task.ps1` dry run, then `-Register`. Run once by hand, check exit 0/3 and `digest`. **Record `orders_query_cost` of that first run and compare it with the calculated 852** (section 9, "At B1"). **Record the run duration** of the first runs (the task logs in to AutoCount with the #155 sync's own user, up to `max_member_reads_per_run` times a run); any run over 8 minutes (the task's time limit is 10): tell the Controller. Use S1 before any type move command. Observe writes claim links (a wrong one is voided with `unlink`, section 4) | "BENEFITS SHADOW" | **Exit 2 on the first run, with any code** (section 9, "At B1"); later, exit 2 twice in a row; any `link_conflict` you cannot explain; **a `\XB\XB-MemberSync` run fails while a benefits run is active** | `Disable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberBenefits` |
| B2 | Review shadow counts for at least 7 days: `digest --hours 168` (`shadow.would_upgrade`, `would_extend`, receipts by state, `claim_needs_review`, `refund_unattributed`, `orders_read_incomplete`, `member_reads_capped`, the count `claim_hold_settled`: how often a held claim receipt was settled for good, and from slice B2 the alert `claim_hold_settled_unclear` and the counts `claim_reopen_deferred` and `below_threshold_payments_unread`), against Shopify's own order list (counts only). Check the open Shopify items of section 9. **Once in the week, force one capped read:** set `shopify_max_pages` to 1 for one observe run at a time when more than 5 orders changed since the last run, confirm the run alerts `orders_read_incomplete`, set it back, and confirm the next run counts `orders_read_resumed`, continues after the kept cursor and that the receipts match the admin list. **Gift card check (needed before B4):** find or make one order paid partly by gift card and run `digest`: `gift_cards.receipts_paid_by_gift_card` must count it. If it does not, read the gift card payment's name in `gift_cards.gateways_seen`, set `gift_card_gateways` to it (in lower case) and run `check-config`. After any change to `gift_card_gateways` or another amount setting, the receipts already recorded keep their old amount: stop the task, delete the state folder and start again from B0 with the same `orders_start_utc`. B4 needs every amount setting unchanged since the ledger was created. (The amount settings are `qualify_threshold_cents`, `amount_before_discounts`, `amount_include_tax`, `amount_exclude_shipping`, `amount_exclude_gift_cards`, `gift_card_gateways` and `amount_count_sale_items`.) The tool now refuses `observe` and `apply` with `benefits_rule_settings_changed` until the new start. Confirm the count after the first run of the new start. Without a new start the count would not move by itself: an order is read again only when it changes in Shopify, or while it is still inside the 15-minute overlap of the last complete read. If the name is shown as `other`, stop and tell the Controller. Record the result. **Also record for the Controller (shadow-week list):** (a) order move and customer merge: can staff move a paid order to another customer in the admin, does the order's `updatedAt` move when they do, and what does a customer merge do to the orders of the customer not kept; (b) the count of `payments_unreadable` receipts now that `transactionsCount` is checked; (c) whether any run failed with a Shopify 5xx on a heavy page; (d) the run durations; (e) whether another gateway (for example store credit) appears in `gateways_seen`: it counts as money unless it is named in `gift_card_gateways`; (f) after B3, one `claim-links` output (private mailbox only) | "BENEFITS SHADOW REVIEWED" | A `would_upgrade` you would not have upgraded by hand; a claim link you would not have made | Stay in observe; report to the Controller |
| B3 | One-off `index` (read only towards Shopify and AC2): first dry runs (`--max 200` until `index_member_complete` and `index_guest_complete`), then `--apply` to write claim links. **Only after the #268 L5/L6 batches are finished.** The index makes one AutoCount member read (one child session with the sync's own AutoCount user) per member examined, in a dry run and again with `--apply`: **record the duration of the first `--max 200` run** before going on. Use S1 before any type move command | "INDEX PASS" | `claim_needs_review` higher than expected; any `link_conflict`; **a `\XB\XB-MemberSync` run fails while an index run is active** | Claim links are append-only; void a wrong one with `unlink` (section 4) after the owner's OK, or stop the task and report |
| B4 | **Needs slice B1 in the clone (B0 does this) and the gift card check of B2 done** (one order paid partly by gift card counted in the digest), and `status` showing `rule_settings_match` true. After any change to `gift_card_gateways` or another amount setting, the receipts already recorded keep their old amount: stop the task, delete the state folder and start again from B0 with the same `orders_start_utc`. B4 needs every amount setting unchanged since the ledger was created; the tool now refuses `observe` and `apply` with `benefits_rule_settings_changed` until the new start. AC2 pilot: config `benefits_write_enabled` true, `max_upgrades_per_run` 3, `type_move_state_root` set (and kept in place); reinstall the task with `-Mode apply` (dry run first). S1 before any type move command (from here `apply` also holds the type move lock). Check every `upgraded`/`extended` member in AC2 by hand (type, `ExpiryDate`) | "BENEFITS AC2 PILOT" | Any `write_held` (see "When a write is HELD"), `write_uncertain` not resolved to `DONE` by `resolve`, a member not as expected, or a sync run failing while a benefits run is active | Disable the task; set `benefits_write_enabled` false; a wrong member is corrected by hand in AC2 (the tool never downgrades) |
| B5 | Normal cap: `max_upgrades_per_run` up to the owner's figure (hard max 20) | "BENEFITS NORMAL CAP" | As B4 | Back to 3, or disable |

`resolve` runs only after the writer deadline (`write_deadline_seconds` + grace); before that it holds with `writer_may_be_live`.

## 8. What staff still do by hand

- Key a counter customer as `MEMBER` (POS creates only `MEMBER`, owner 10 Nov 2026), with an `ExpiryDate` 2 years out.
- Upgrade a `GUEST` to `MEMBER` in store after a S$300 receipt (and set the `ExpiryDate`). A `MEMBER` made by staff is never undone by the tool.
- The in-store rule, one line for the staff briefing: "S$300 means what the customer keeps and pays for with money. Selling a gift card counts. The part of a bill paid **with** a gift card does not count. If an exchange within 7 days takes the bill below S$300, change the member back to GUEST."
- Check the alerts from the digest:
  - `claim_needs_review`: the customer and the AC2 member could not be linked safely (name mismatch, more than one customer on the phone, both MemberNo forms, a link conflict, or a pair voided by `unlink`: `claim_pair_unlinked`). The receipt is held and is **not** re-checked.
  - `unlinked_with_applied`: a claim link was voided after its receipt was upgraded; check that member in AutoCount by hand.
  - A receipt held with `held_write_released`: its `HELD` write was released as not intended; upgrade that member by hand if still due.
  - `qualified_member_no_phone`: a signed-up customer with a S$300 order has no phone, so the sync never creates their member.
  - `qualified_member_missing`: no AC2 member 48 hours after a qualifying order.
  - `refund_unattributed`: the order was refunded by money (not by returned units) before any upgrade; the receipt is held and never decided by the tool.
  - `member_inactive`: the linked member is inactive.
  - `orders_read_incomplete` and `member_reads_capped` need no staff action on their own (the tool carries on next run); tell the Controller if they repeat for more than a day.
- **Upgrade by hand after fixing a claim hold.** A held claim receipt re-opens only when the #155 sync links the customer or a claim link appears. When staff fix the cause themselves (correct the name, remove a duplicate customer, correct the member's type, key a member for a customer with no phone), the tool does not notice: the one-off index has already passed that member. Staff then upgrade that member to `MEMBER` by hand in AutoCount with `ExpiryDate` = order date + 2 years, after checking the order is still paid and not refunded. The same applies to a `refund_unattributed` hold the owner decides to honour.

## 9. Shopify API (verified against shopify.dev, Admin GraphQL `2026-10`, 10 Nov 2026; the transaction fields and the query cost 11 Nov 2026; `transactionsCount`, `Count`, `CountPrecision` and `OrderTransactionKind` 12 Nov 2026)

Pinned `api_version`: **`2026-10`**. Queries are pinned in the module (`ORDERS_QUERY`, `CUSTOMER_QUERY`, `PHONE_LOOKUP_QUERY`, plus the #155 session check); nothing else can be sent.

| Object | Fields relied on |
|---|---|
| `orders` query | `first`, `after` (also kept across runs when a read ends at the page cap), `pageInfo { hasNextPage endCursor }`, `sortKey: UPDATED_AT`, `query: "updated_at:>='<UTC>'"`; page size 5 orders, each with up to 20 line items, 3 shipping lines and 10 transactions (see "Query cost" below); `read_orders` sees orders created in the last 60 days |
| `Order` | `id`, `createdAt` (the order date; never changes), `updatedAt` (watermark), `cancelledAt`, `test`, `displayFinancialStatus` (`PAID`, `PARTIALLY_REFUNDED`, `REFUNDED` count; `PENDING`, `AUTHORIZED`, `PARTIALLY_PAID`, `VOIDED`, `EXPIRED` do not), `taxesIncluded`, `currencyCode`, `totalRefundedSet`, `customer` (null when the order has no customer), `lineItems`, `shippingLines`, `transactions(first: 10)` (a plain list `[OrderTransaction!]!`, not a connection: no cursor and no `pageInfo`; `first` cuts the list to that size, so a list of 10 is treated as cut short), `transactionsCount` (type `Count`, may be null, no arguments: "The number of transactions associated with the order"; the list counts as complete only when it is `EXACT` and equal to the transactions listed). Covered by `read_orders`; no gift card scope is needed |
| `Count` / `CountPrecision` | `count` (`Int!`), `precision` (`CountPrecision!`). `CountPrecision` has two values (pages `objects/Count` and `enums/CountPrecision`, 2026-10): `EXACT` ("The count is exactly the value", with the note that a write may not be reflected at once) and `AT_LEAST` ("A limit was imposed and reached"). Only `EXACT` is taken |
| `OrderTransaction` | `gateway` (`String`, may be null; compared trimmed and in lower case), `kind` (`OrderTransactionKind`, eight values on page `enums/OrderTransactionKind` 2026-10: `SALE` and `CAPTURE` count; `AUTHORIZATION`, `CHANGE`, `EMV_AUTHORIZATION`, `REFUND`, `SUGGESTED_REFUND`, `VOID` do not; any other kind on a gift card gateway makes the order `payments_unreadable`), `status` (`SUCCESS` counts; `FAILURE` and `ERROR` are ignored; `PENDING`, `AWAITING_RESPONSE`, `UNKNOWN` make the order `payments_unreadable`), `amountSet` (`MoneyBag`). No field says "gift card": the gateway name is the only marker |
| `LineItem` | `id`, `quantity` (incl. refunded and removed units), `currentQuantity` (excl. them), `isGiftCard`, `originalUnitPriceSet` (before discounts), `discountAllocations.allocatedAmountSet` (`[DiscountAllocation!]!`, a plain list with no arguments: it cannot be cut to a size) |
| `ShippingLine` | `originalPriceSet` (only with `amount_exclude_shipping` false) |
| `Customer` | `id`, `createdAt`, `tags`, `firstName`, `lastName`, `defaultPhoneNumber.phoneNumber`, `metafield(namespace: "membership", key: "start_date").createdAt` |
| `customers` query | `query: "phone:+<digits>"`, `first: 5`, `pageInfo.hasNextPage` (candidate list only; every node is re-checked with the #155 phone rule; more than one page of candidates is a review) |
| `MoneyBag` | `shopMoney { amount currencyCode }`; any currency other than SGD makes the order `amount_unverifiable` |

Not verifiable from the docs (check in the first shadow week): whether `updated_at:>=` behaves as documented for the orders search (use the `Shopify-Search-Query-Debug: 1` header once if counts look off); how a `+65` phone is stored for the `phone:` search; which customer fields count as protected customer data for this app; **the `gateway` value of a gift card payment** (no shopify.dev page states it; `gift_card` is the default of `gift_card_gateways` and is proven by the B2 gift card check); **whether the replacement items of an exchange become order lines** (if they do not, the tool under-counts, which is the safe side, and staff upgrade by hand); the order in which `transactions` are returned; whether a heavy page of orders can fail with a Shopify 5xx (recorded in the shadow week); and how Shopify really costs `ORDERS_QUERY` (the rate limits page gives no rule for nested objects or for a plain list such as `transactions`), which the first B1 run measures (next section).

### Query cost of `ORDERS_QUERY` (calculated 11 Nov 2026; compare with the measured figure at B1)

From shopify.dev (the GraphQL Admin API rate limits page, which carries no version marker; read 11 Nov 2026): a scalar or an enum costs 0 points and an object 1; a connection is "sized by `first` and `last`"; a single query may not cost more than 1,000 points, and that limit is applied to the requested cost before the query runs; the response carries the cost under `extensions.cost` (`requestedQueryCost`, `actualQueryCost`, `throttleStatus`) with no special header (the request header `Shopify-GraphQL-Cost-Debug=1` only adds a field-by-field breakdown). **Not stated there:** whether nested objects are multiplied down a connection, how a plain list is costed, and what the answer to a refused query looks like.

So the page sizes are set for the **strict reading**: every selected object is counted once for each row of every connection and list above it; a connection is its own object, plus its `pageInfo`, plus `first` rows; a plain list with `first` is `first` rows. `LineItem.discountAllocations` takes no size argument (shopify.dev, `LineItem`), so it has no size to count and is taken as one row.

| Line | Selection | Points |
|---|---|---|
| 1 | One line item: `LineItem` 1 + `originalUnitPriceSet` 2 (`MoneyBag` + `MoneyV2`) + `discountAllocations` 3 (`DiscountAllocation` 1 + `allocatedAmountSet` 2) | 6 |
| 2 | `lineItems(first: 20)`: the connection 1 + `pageInfo` 1 + 20 x line 1 | 122 |
| 3 | One shipping line: `ShippingLine` 1 + `originalPriceSet` 2 | 3 |
| 4 | `shippingLines(first: 3)`: the connection 1 + `pageInfo` 1 + 3 x line 3 | 11 |
| 5 | One transaction: `OrderTransaction` 1 + `amountSet` 2 | 3 |
| 6 | `transactions(first: 10)`: 10 x line 5 (a plain list: no connection object, no `pageInfo`) | 30 |
| 7 | `totalRefundedSet`: `MoneyBag` 1 + `MoneyV2` 1 | 2 |
| 8 | `customer`: `Customer` 1 + `defaultPhoneNumber` 1 + `metafield` 1 | 3 |
| 8a | `transactionsCount`: one `Count` object (its `count` and `precision` are scalars, 0 each); slice B1 | 1 |
| 9 | One order: `Order` 1 + lines 2, 4, 6, 7, 8 and 8a | 170 |
| 10 | `orders(first: 5)`: the connection 1 + `pageInfo` 1 + 5 x line 9 | **852** |

As one sum: 2 + orders x (11 + 6 x line items + 3 x shipping lines + 3 x transactions) = 2 + 5 x (11 + 120 + 9 + 30) = **852**, under this tool's own ceiling of 900 (847 before slice B1 added `transactionsCount`). With the sizes before slice A2 (10 orders, 50 line items, 5 shipping lines, 25 transactions) and no `transactionsCount`, the same sum is 2 + 10 x 400 = 4,002. The sizes and the total are pinned by `OrdersQueryCostTests`, which works the sum out from the query text: after any change to a page size or to the query, work the figure out again and update this table.

What the sizes mean for an order (an order is never decided on part of its rows):

- **More than 20 line items** (rows, not units): `amount_unverifiable` with code `currency_or_lines` and the alert `order_amount_unverifiable`; a person decides it (before: more than 50).
- **10 or more transactions**, or a `transactionsCount` that does not prove the list complete: `amount_unverifiable` with code `payments_unreadable` and the same alert (before: 25 or more).
- **More than 3 shipping lines**: matters only with `amount_exclude_shipping` false (before: more than 5).

Reach of one run: 5 orders a page x `shopify_max_pages` 20 (the new default) = 100 orders, as before (10 x 10). At the hard maximum of 40 pages it is 200 orders (400 before). A read that ends at the page cap still keeps the cursor and carries on next run. Between pages the client waits until Shopify's bucket holds the last query's requested cost again (at most 90 seconds of waiting in one run; past that the read fails with `shopify_retry_budget_exhausted` and nothing moves), so a run of many pages can take longer than before: B1 records the duration. If `shopify_retry_budget_exhausted` repeats, lower `shopify_max_pages` (never raise it) and tell the Controller. Do not set it above 20 without a Controller decision.

**At B1, compare the measured figure.** Every `observe` and `apply` run prints `orders_query_cost` and keeps it in the run's stored summary: the largest `requestedQueryCost` Shopify reported for a page of the orders read, as a whole number of points, or `null` when Shopify reported none. Record it on the first run.

- At or under 852: the calculation holds (a lower figure means Shopify counts less than the strict reading).
- Over 852, or `null`: the read worked, but tell the Controller before B2 (the one-row count for `discountAllocations` is the first thing to look at).
- **The first run exits 2, with any code: stop.** A query that Shopify refuses ends with `shopify_graphql_error`, or with `shopify_retry_budget_exhausted` or `shopify_query_cost_exceeds_bucket` when Shopify codes the refusal `THROTTLED`, or with `shopify_access_denied`. In every case nothing is read, recorded or moved, and the tool cannot show the figure of a refused query. Tell the Controller the code; the page sizes are constants in the module and are changed in a code round, never on the VM.

Open check for the shadow week, not a Shopify docs question: does the #155 sign-up intake ever take the `member-mg` tag off a customer, or delete and re-create the `membership.start_date` metafield (re-registration, renewal)? The tool reads both as the sign-up marker, and a claim-held receipt whose customer loses the marker for a while is settled for good (`claim_hold_settled`, with the alert `claim_hold_settled_unclear` from slice B2). If it happens, tell the Controller before B4.

## 10. Closure rows (G0 design 3.4) in this round

| Row | Test (`tests/test_member_benefits.py` unless noted) |
|---|---|
| Q1 (no tag step) | `ClosureRowTests.test_q01_linked_guest_with_a_300_order_becomes_member_with_expiry` |
| Q2 | `test_q02_299_99_is_below_threshold_with_no_read` |
| Q3 | `test_q03_300_before_discounts_qualifies_though_less_was_paid` |
| Q4 | `test_q04_shipping_never_counts` |
| Q5 | `test_q05_cancelled_test_and_unpaid_orders_are_not_receipts_that_count` |
| Q6 | `test_q06_signed_up_customer_without_an_ac2_member_waits_then_applies` |
| Q7 | `test_q07_a_customer_who_never_signed_up_gets_nothing`, `test_q07_sign_up_later_the_same_singapore_day_counts_and_a_later_day_does_not`, `test_q07_same_day_rule_can_be_switched_off`, `test_q07_sign_up_seen_on_a_later_run_moves_the_receipt_on` |
| Q8 | `test_q08_member_with_a_legacy_expiry_moves_only_later` |
| Q9 (record, no change) | `test_q09_refund_leaving_the_basis_at_300_is_recorded_only` |
| Q12 (record and alert, no change) | `test_q10_q12_refund_taking_the_basis_below_300_alerts_once_and_changes_nothing` |
| Q13 (record and alert, no change) | `test_q13_a_refund_read_after_60_days_alerts_as_late` |
| Q17 | `test_q17_staff_made_member_with_a_blank_expiry_gets_the_receipt_expiry` |
| Q18 | `test_q18_staff_edit_between_read_and_save_skips_and_retries_next_run`; PS: `BenefitsBridgeTests.test_pre_check_skips_are_proven_not_written` |
| Q19 | `test_q19_default_and_other_types_are_counted_and_never_written`; PS: `test_pre_check_skips_are_proven_not_written`, `test_boundary_refuses_every_move_but_upgrade_or_extend_before_any_session` |
| Q20 | `test_q20_uncertain_is_never_re_sent_and_resolves_by_read_back`, `test_q20_uncertain_that_never_saved_resolves_not_written_then_retries_on_a_fresh_read`, `test_q20_a_crash_inside_the_save_call_is_uncertain`; PS: `test_python_client_round_trip_and_kill_after_save_resolves_by_read_back` |
| Q21 | `test_q21_read_back_with_another_field_changed_stops_and_latches`; PS: `test_read_back_mismatch_is_held_by_column_name_only` |
| Q22 | `test_q22_a_shopify_error_moves_nothing`; `ShopifyClientTests.test_pages_are_bounded_and_429_backs_off`, `test_a_malformed_order_fails_the_read` |
| Q24 | `test_q24_the_same_order_read_again_is_a_no_op`, `test_q24_the_watermark_overlaps_and_starts_at_the_configured_start` |
| Q25 (links only) | `test_q25_a_customer_mapped_to_two_members_blocks_the_write`, `test_q25_a_member_mapped_to_two_customers_blocks_the_write` |
| Q26 (links only) | `test_q26_existing_member_signing_up_with_the_same_mobile_is_claim_linked`, `test_q26_the_eight_digit_member_no_form_also_claims`, `IndexTests.*` |
| Q27 | `test_q27_name_mismatch_or_shared_phone_is_reviewed_once_and_never_linked`, `test_q27_both_member_no_forms_present_is_reviewed` |
| Q33 | `test_q33_switch_off_reports_would_counts_and_writes_nothing`, `test_q33_cap_reached_and_stop_file` |
| Q34 | `test_q34_scope_check_fails_before_any_order_read`, `test_q34_an_autocount_guard_failure_refuses_with_nothing_written`; `ShopifyClientTests.test_scopes_must_be_exactly_read_orders_and_read_customers`; PS: `GuardTests.*`, `RealEntryOfflineTests.*` |
| Q35 | `test_q35_a_second_run_is_refused_by_the_lock`, `TypeMoveLockTests.test_apply_and_the_type_move_never_hold_the_lock_together` |

Fix round 1 (G2 at `ceae854`), `G2FixRound1Tests` unless noted: B1 refunds by money (`test_b1_r1_...`, `test_b1_a_partial_refund_by_money_before_the_decision_is_held`, `test_b1_r1b_...`, `test_b1_a_unit_refund_still_behaves_as_before`); B2 claim outcomes and back-off (`test_b2_r4_...`, `test_b2_a_held_claim_review_reopens_when_a_link_appears`, `test_b2_member_out_of_scope_and_no_phone_settle`, `test_b2_waiting_member_rechecks_back_off_until_a_link_appears`); B3 read resume (`test_b3_r5_...`, `test_b3_a_failed_resume_moves_nothing_and_restarts_from_the_watermark`, `ShopifyClientTests.test_b3_a_read_at_the_page_cap_keeps_the_cursor`); B4 (`test_b4_g16_...`, `test_b4_g17_...`, `CliTests.test_b4_g35_...`; PS `test_b4_s04_...`, `test_b4_s08_s09_...`, `test_b4_s10_...`); LATER 1 (`ClosureRowTests.test_q20_one_write_per_receipt_even_after_a_crash_before_the_receipt_update`), LATER 2 (`IndexTests.test_later2_...`), LATER 6 (`test_later6_...`, `ShopifyClientTests.test_later6_...`), LATER 8 (`test_later8_g09_...`, `test_later8_g28_...`, `SyncLedgerReadOnlyTests.test_later8_g29_...`).

Fix round 2 (G2 recheck at `86b42c1`), `G2FixRound2Tests` unless noted: B5 a held claim receipt is looked at again (`test_b5_r6a_...`, `test_b5_r6b_...`, `test_b5_r6c_...`, `test_b5_r6d_...`, `test_b5_a_claim_hold_whose_order_still_qualifies_re_opens_as_before`, `test_b5_the_re_open_rests_on_the_order_as_last_read`); B6 never twice with an open or held write (`test_b6_observe_with_an_uncertain_write_...`, `test_b6_observe_with_a_held_write_...`) and privacy at the re-open, repair and index review (`CliTests.test_b6_re_open_repair_and_index_review_carry_keyed_refs_only`); `resolve` with a receipt held in between (`test_later1_...`); repair before classifying (`test_later2_r2b_...`); the receipts trigger (`test_later3_...`); re-open and end of wait by a claim link (`test_later9_d12_...`), `ready` before `waiting_member` (`test_later9_d25_...`), truncated phone search on the index path (`IndexTests.test_later9_d45_...`).

Fix round 3 (G2 recheck at `eb1fc8b`), `G2FixRound3Tests`: B8 an order that left the receipt's customer (`test_b8_p06_...`, `test_b8_p06d_...`, `test_b8_an_order_whose_customer_was_removed_...`, `test_b8_an_order_moved_and_refunded_by_money_settles_silently`); B7 the privacy check runs after every test (`Base.tearDown`: keyed refs and masked MemberNos only in every stored run summary and every CLI output); test gaps `test_e07b_...`, `test_e26_...`, `test_e09_e10_e11_...`, `test_e13_...`, `test_e15_...`, `test_e23_...`, `test_e32_p03_...`.

Slice A (gift card rule, 11 Nov 2026), `GiftCardRuleTests`: the worked examples (`test_worked_examples_of_the_design`), the rule table (`test_gift_card_amount_table`), payments that cannot be read (`test_payments_that_cannot_be_read_...`, `test_a_malformed_transaction_list_fails_the_read`), the query and the unchanged scopes (`test_the_orders_query_reads_the_transactions_...`), the settings (`test_gift_card_settings`), the first decision (`test_the_first_decision_uses_the_rule`, `test_unreadable_payments_hold_the_order_with_the_existing_alert`), a held claim receipt (`test_a_held_claim_receipt_whose_order_becomes_gift_card_paid_settles_for_good`, `test_a_held_claim_receipt_with_a_gift_card_payment_that_still_qualifies_re_opens`), after the upgrade (`test_an_applied_receipt_is_not_alerted_for_a_gift_card_payment_that_was_already_there`), the digest (`test_the_digest_counts_gift_card_receipts_and_shows_gateway_names_as_code_words_only`, and the gateway-name check in `Base.tearDown`). The three tests left open by the last review (B9), `B9Tests`: the four held verdicts through the engine (`test_b9_the_four_held_verdicts_in_apply`, `..._in_observe`), the end of the sign-up wait when the orders are not read again (`test_b9_the_sign_up_wait_ends_...`), the `HELD` alert of `resolve` (`test_b9_resolve_reports_a_held_write_by_its_code_only`).

Slice A2 (query cost, 11 Nov 2026), `OrdersQueryCostTests`: the page sizes and the strict cost worked out from the query text (`test_the_page_sizes_and_the_strict_cost_of_the_orders_query_are_pinned`), the reach of one run (`test_one_run_still_reaches_as_many_orders_as_before`), the row limits (`test_an_order_with_more_rows_than_a_page_is_held_for_a_person`), the measured cost (`test_whole_points`, `test_the_client_keeps_the_requested_cost_of_the_orders_read_only`, `test_the_run_output_and_the_stored_summary_carry_the_cost_as_a_whole_number`, `test_the_engine_takes_a_whole_number_only_and_the_index_reports_no_cost`, and the whole-number check in `Base.tearDown`), a refused query (`test_a_query_refused_for_its_cost_moves_nothing_and_ends_with_one_code`), and the test gaps of the PR #300 review (`test_p43_p52_a_decided_receipt_read_again_with_unreadable_payments_keeps_its_amounts`; a string in place of the gateway list in `GiftCardRuleTests.test_gift_card_settings`). The check after every test (`Base.tearDown`) also requires every count key, alert code, ref code and `stopped` text to be a code word with no run of four or more digits (`OutputShapeRuleTests.test_the_check_after_every_test_takes_code_words_only`). The one exception is the computed tail of a generic bridge failure code (`bridge_read_failed:<shape>:x<exit code, 8 hex characters>:l<lines>`), tied to the real `bridge_failure_code` function by `test_the_generic_bridge_failure_code_is_the_one_exception` and `test_a_generic_bridge_failure_in_a_run_passes_the_check`. The two CLI privacy tests that failed by chance about once in 75 runs now use fake order and customer numbers of 16 digits (`order_number`, `customer_number`).

The ledger schema gained two receipt columns in place (`gift_card_cents`, `payment_gateways`) with `SCHEMA_VERSION` still 1, because no ledger exists anywhere yet (B0 has not run). From B0 on, a schema change needs a version step.

Slice B1 (unlink round, 12 Nov 2026), before B0:

- Tests first on main's tool (`OutputShapeRuleTests`, `RealOutputsReachTheCheckTests`): the check after every test reads the digest's count keys, a `resolve` alert code and an index run's `stopped` text with bad cases (B11); its patterns end with `\Z`; `counts` is an object of whole numbers; a refused output's `code` is a code word; each output has exactly its command's top-level keys; a real digest with an alert and its ref filter, and a real index run that stops (B12, H07).
- Ledger v2 (`LedgerV2Tests`): `SCHEMA_VERSION` 2 in place, no migration; a ledger made by older code is refused by every command; `meta.rule_fingerprint`; `claim_links.link_id` with the two insert triggers (`claim_link_conflict`, `claim_pair_unlinked`); `claim_unlinks` and the view `active_claim_links` read by every reader; `write_releases` (HELD only); a held claim receipt settles only to `order_no_longer_qualifies`, `refund_unattributed` or `order_past_read_window`; `recursive_triggers` on, so `INSERT OR REPLACE` is refused on every append-only table.
- Amount reading (`AmountReadingTests`, `OrdersQueryCostTests`): `transactionsCount` (M1), strict cost 852 (M2), an undocumented kind on a gift card gateway (M3), the gateway trimmed (M4), `AMOUNT_RULE_VERSION` 2 (M5).
- The rule fingerprint guard (`RuleFingerprintTests`).
- `release-held` (`ReleaseHeldTests`, including the G15 test: a released `HELD` write whose receipt is still `ready` is never sent again).
- `unlink` (`UnlinkTests`).
- Fix round 1 (G2 B13, L1, L2): `unlink --order-ref` voids the active claim link of that order's customer (the right link when two members share their last 3; each refusal records nothing); `--member-last3` is compared exactly with the last 3 characters in `release-held` and `unlink` (a shorter suffix refuses); the CLI passes `--accept-applied` only when given; a receipt of another customer through the same member is not through the link; an undocumented kind on a gift card gateway is unreadable whatever its status; the check after every test also reads the values of the keys slice B1 added (fixed words, whole numbers, true or false).

Slice B2 (member core tidy round, 13 Nov 2026; no schema, `SCHEMA_VERSION`, `AMOUNT_RULE_VERSION` or setting change, so no re-init):

- Tests first on main's tool (`B2TestGapTests`, `B2TokenCacheScopeTests`, `B2NoReinitGuardTests`): a WAL v1 ledger with a reader open is refused by `init`, `observe`, `status`, `digest` and `claim-links`, its main file unchanged (L6); A21, A23, A33, A34, A36, A37, G25, G32, D01, D32; the schema, its version, the amount rule and the example configs' `rule_fingerprint` pinned to their values at `a6e7142`.
- Alerts (`B2AlertTests`): `order_customer_changed_after_upgrade` (B2-1); `claim_hold_settled_unclear` on both settle paths (B2-2).
- Windows (`B2WindowTests`): no re-open after a capped read (B2-3); the 60-day age limit, exactly 60 days settles (B2-4).
- Record (`B2RecordTests`): carry-over resets of an undecided receipt that follows its order (B2-5); goods measured first (B2-7).
- Fix round 1 (G2 F1, F2, L2, L3): the B2-5 reset keeps `member_no` and `link_source` while a write for the receipt is open or landed (`INTENT`, `UNCERTAIN`, `HELD`, `DONE`), so `resolve` after an `observe` that moved the order settles `DONE` cleanly; the age limit also runs after a capped read; the no-re-init guard also pins `refunded_unit_cents` and the order amount parsing; the read-only side files line above.
- Digest and probe (`B2DigestProbeTests`): gateway names with 4 digits in a row shown as `other`, `Payment`/`OrderLine` reprs (B2-6); a probe `unknown` holds in `resolve` and `release-held` (B2-8).
- Release and unlink (`B2ReleaseUnlinkTests`): `left_open` and the late settle (B2-9, L5); malformed refs give `unlink_arguments_invalid` (B2-10).
- `claim-links` (`B2ClaimLinksTests`, B2-11, N1).

### Parked (one line each, for the next slice)

- **Q1 tag step**: no Shopify tag or metafield (needs `write_customers` and the I1/I9 tag rules).
- **Q10**: refund revoke (`MEMBER` -> `GUEST`) parked; today: `refund_on_basis` alert, no change.
- **Q11**: moving `ExpiryDate` back to an earlier basis parked; today: alert, no change.
- **Q14**: POS receipt reader (in store stays manual).
- **Q15**: `ExpiryDate` from a POS receipt for a staff-keyed `MEMBER` (needs the POS reader).
- **Q16**: `member_basis_missing` for a `MEMBER` with no receipt (needs the POS reader).
- **Q23**: Shopify tag write failure and repair (no tag writes).
- **Q28**: expiry job (`MEMBER` with a past `ExpiryDate` -> `GUEST`).
- **Q29**: expiry job alert for NULL / 2000-01-01 / unreadable `ExpiryDate`.
- **Q30, Q31**: birthday-month tag.
- **Q32**: `tag_without_basis` reconcile (no tags).
- **I1, I9**: tag invariants, parked with the Shopify writes.
- Tips (`Order.totalTipReceivedSet`) are not excluded yet: check in the shadow week whether tipping is on.
- **B2 L1**: an order that moves to another customer between its write and the receipt becoming `applied` is not alerted (B2-1 compares at `applied` only); later: alert when the customer at `applied` differs from the customer at the write.
- No ceiling on the new `ExpiryDate` at the child (defence in depth; slice E).
- A refused open of a real (WAL) ledger made by older code leaves `-wal`/`-shm` side files next to it; keep them with the folder (the main file is left as it is; slice B2 test).
- A refund by money hidden inside the discount of a refunded unit is not caught (the check compares refunded money with the before-discount value of refunded units); rare.
- `orders_read_incomplete` can repeat every second run while a bulk update sits inside the 15-minute overlap on a quiet shop (alert noise; progress holds).
- `refund_unattributed` also holds and alerts for a customer who never signed up (the refund is checked before the sign-up); review with the shadow-week noise.
