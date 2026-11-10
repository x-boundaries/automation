# #268 member core: S$300 online order -> AC2 `MEMBER` with a 2-year expiry, plus claim links

Tool: `directlocal/xb_member_benefits.py` (working name, kept so later slices extend it), its AutoCount child `scripts/ac2_member_benefits_bridge.ps1` + `scripts/ac2_member_benefits.ps1` (profile `benefit_p1`), the task installer `scripts/install_member_benefits_task.ps1`, configs `config/member_benefits.example.json` (test book) and `config/member_benefits.live.example.json`. Tests: `tests/test_member_benefits.py`, `tests/test_member_benefits_ps.py`.

**Default off.** Nothing runs on merge. External effects: none until the VM steps below, each with its own owner OK.

## 1. What it does, in plain words

Every 15 minutes (about 7 minutes after `\XB\XB-MemberSync`) the tool:

1. Reads paid Shopify orders that changed since its last clean read. Read only, through its **own** Shopify app whose token must hold exactly `read_orders` and `read_customers`.
2. Writes one row per order into its own ledger on the VM (`member_benefits.sqlite3`), forever. Reading the same order again updates the same row.
3. Works out the order's qualifying amount: line prices **before discounts, including GST, excluding shipping and gift-card lines**, less refunded or removed units. Sale items count.
4. If one order is **S$300.00 or more**, the customer carries the #155 sign-up marker (tag `member-mg` and the `membership.start_date` time) and the order's Singapore date is on or after the sign-up date (or the same Singapore day), it finds the customer's AC2 member through the #155 sync's links (read only) or its own claim links.
5. Reads that AC2 member. AutoCount decides the type:
   - `GUEST` -> `MEMBER` with `ExpiryDate` = order's Singapore date + 24 months (`would_upgrade`);
   - `MEMBER` -> same type, `ExpiryDate` moved **later** only (`would_extend`); already later -> no change;
   - `Default`, held, inactive or any other type -> counted, never written.
6. **Observe** (the default): records and reports `would_upgrade` / `would_extend`; no AutoCount save is reachable. **Apply** (switch `benefits_write_enabled` on, at most `max_upgrades_per_run`, default 3): one `SaveMember` per decision through `benefit_p1`, with the #268 guard set (book guard, host pin, identity proof, lock, `STOP` file, `INTENT` / `DONE` / `NOT_WRITTEN` / `UNCERTAIN` / `HELD`, read-back of every column, exit 2 with nothing partial).

**Claim links (the owner's "auto linking").** An AC2 `MEMBER` or `GUEST` with no #155 link, **exactly one** Shopify customer whose phone gives that MemberNo under the #155 phone rule, and the #155 two-word name rule matches -> linked in this tool's ledger (`claim_links`). Anything else -> no link and one `claim_needs_review` alert per customer. A customer never maps to two MemberNos across both ledgers (and the reverse); a conflict holds that member (`link_conflict`) and blocks any write for it. The #155 ledger is opened read-only (`mode=ro`, `query_only`) and never copied or written.

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
| `amount_exclude_gift_cards` | `true` | Gift-card purchase lines never count |
| `amount_count_sale_items` | `true` | Only `true` is accepted: an order line records no compare-at (sale) price, so sale items cannot be told apart |
| `same_day_signup_counts` | `true` | An order placed before sign-up counts only when both fall on the same Singapore day |
| `orders_start_utc` | set at install | Orders created before this never count (`before_start`); the first read starts here |
| `waiting_member_alert_hours` | `48` | A qualifying order with no AC2 member yet alerts `qualified_member_missing` once after this |
| `signup_wait_hours` | `48` | How long an order without a seen sign-up is re-checked before it settles as `not_signed_up` |
| `max_upgrades_per_run` | `3` | Saves per run (hard max 20) |
| `max_member_reads_per_run` | `20` | AutoCount member reads per run |
| `type_move_state_root` | live: the type move's root | `apply` holds the type move's ledger lock (section 6); required for `apply` on the live book |

`ExpiryDate` is written as the date at 00:00 (AutoCount stores Singapore local time). 29 Feb + 24 months = 28 Feb.

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
```

Exit codes: 0 ok, 3 completed with alerts (an operator looks), 2 refused (nothing written). Output carries codes, counts, keyed refs (`o-`, `c-`, `m-`) and MemberNos masked to the last 3 characters only.

`apply` refuses while any write is `INTENT`/`UNCERTAIN` (`benefit_uncertain_unresolved`: run `resolve` after the writer deadline) or `HELD` (`benefit_held_rows_present`: the owner decides). A `STOP` file in the state root stops writes before the next member.

### Alert codes

`refund_on_basis` / `late_refund_on_basis` (refund took an applied order below S$300; nothing changes), `qualified_member_missing`, `signup_time_missing`, `claim_needs_review`, `link_conflict`, `member_inactive`, `expiry_unreadable`, `guest_expiry_already_later`, `member_not_found`, `order_amount_unverifiable`, `write_uncertain`, `write_held`.

## 5. Owner's Shopify admin step (before B1)

1. Create a **second** app in the Shopify Dev Dashboard for this store (not the #155 sync's app: the sync checks its own scopes exactly, so widening its app would stop the live sync). Scopes: **`read_orders` and `read_customers` only.** No write scope, no `read_all_orders` (we read forward from `orders_start_utc`; `read_orders` sees orders created in the last 60 days).
2. Install it on the store. Put its client ID and secret in the task account's user environment variables `XB_SHOPIFY_BENEFITS_CLIENT_ID` and `XB_SHOPIFY_BENEFITS_CLIENT_SECRET` (never in a file or chat). The access token is cached with DPAPI in the tool's state root, as #155.
3. If Shopify asks for protected customer data access for names and phone numbers, grant it for this app (read only).

Stop condition: the first run refuses `shopify_token_scope_not_expected` or `shopify_store_identity_mismatch` -> fix the app, do not widen the code.

## 6. Keeping the two AutoCount writers apart

- `apply` takes the **#268 type move's own ledger lock** (`member_type_move.lock` in `type_move_state_root`) for the whole run. While it is held, every type move command that opens its ledger (`snapshot`, `move`, `resolve`, `rollback`, `reconcile`, `status`) refuses with `ledger_locked_by_another_owner`; while a type move command holds it, `apply` refuses with `type_move_running`. The lock is an OS lock, released if a process dies. The type move code is unchanged.
- Runbook rule on top: **no benefits step runs while a `\XB\XB-TypeMove-*` task is running**, and the benefits task is disabled (`Disable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberBenefits`) before an attended type move batch and enabled after it.
- `observe` and `index` never save, so they do not take the lock.
- The #155 sync only creates members (never edits a type), so it cannot touch the same member edit; it writes ChangeLog `I` rows only.

## 7. VM steps (each its own owner OK; never while `\XB\XB-TypeMove-*` runs)

| Step | What | Owner OK | Stop condition | Rollback |
|---|---|---|---|---|
| B0 | Clone at the merged commit; live config from `member_benefits.live.example.json` with `orders_start_utc` = the minute B1 starts; `check-config`; `init` | "BENEFITS INSTALL" | `check-config` refuses | Delete the new state root (no other system touched) |
| B1 | Install the task in **observe** mode: `install_member_benefits_task.ps1` dry run, then `-Register`. Run once by hand, check exit 0/3 and `digest` | "BENEFITS SHADOW" | Exit 2 twice in a row, or any `link_conflict` you cannot explain | `Disable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberBenefits` |
| B2 | Review shadow counts for at least 7 days: `digest --hours 168` (`shadow.would_upgrade`, `would_extend`, receipts by state, `claim_needs_review`), against Shopify's own order list (counts only) | (review, no OK) | A `would_upgrade` you would not have upgraded by hand | Stay in observe; report to the Controller |
| B3 | One-off `index` (read only towards Shopify and AC2): first dry runs (`--max 200` until `index_member_complete` and `index_guest_complete`), then `--apply` to write claim links. **Only after the #268 L5/L6 batches are finished** | "INDEX PASS" | `claim_needs_review` higher than expected, or any `link_conflict` | Claim links are append-only; stop the task and report (a later slice adds an unlink) |
| B4 | AC2 pilot: config `benefits_write_enabled` true, `max_upgrades_per_run` 3, `type_move_state_root` set; reinstall the task with `-Mode apply` (dry run first). Check every `upgraded`/`extended` member in AC2 by hand (type, `ExpiryDate`) | "BENEFITS AC2 PILOT" | Any `write_held`, `write_uncertain` not resolved to `DONE` by `resolve`, or a member not as expected | Disable the task; set `benefits_write_enabled` false; a wrong member is corrected by hand in AC2 (the tool never downgrades) |
| B5 | Normal cap: `max_upgrades_per_run` up to the owner's figure (hard max 20) | "BENEFITS NORMAL CAP" | As B4 | Back to 3, or disable |

`resolve` runs only after the writer deadline (`write_deadline_seconds` + grace); before that it holds with `writer_may_be_live`.

## 8. What staff still do by hand

- Key a counter customer as `MEMBER` (POS creates only `MEMBER`, owner 10 Nov 2026), with an `ExpiryDate` 2 years out.
- Upgrade a `GUEST` to `MEMBER` in store after a S$300 receipt (and set the `ExpiryDate`). A `MEMBER` made by staff is never undone by the tool.
- Check `claim_needs_review`, `qualified_member_missing` and `member_inactive` alerts from the digest.

## 9. Shopify API (verified against shopify.dev, Admin GraphQL `2026-10`, 10 Nov 2026)

Pinned `api_version`: **`2026-10`**. Queries are pinned in the module (`ORDERS_QUERY`, `CUSTOMER_QUERY`, `PHONE_LOOKUP_QUERY`, plus the #155 session check); nothing else can be sent.

| Object | Fields relied on |
|---|---|
| `orders` query | `first`, `after`, `sortKey: UPDATED_AT`, `query: "updated_at:>='<UTC>'"`; page size 10 orders x 50 lines; `read_orders` sees orders created in the last 60 days |
| `Order` | `id`, `createdAt` (the order date; never changes), `updatedAt` (watermark), `cancelledAt`, `test`, `displayFinancialStatus` (`PAID`, `PARTIALLY_REFUNDED`, `REFUNDED` count; `PENDING`, `AUTHORIZED`, `PARTIALLY_PAID`, `VOIDED`, `EXPIRED` do not), `taxesIncluded`, `currencyCode`, `totalRefundedSet`, `customer` (null when the order has no customer), `lineItems`, `shippingLines` |
| `LineItem` | `id`, `quantity` (incl. refunded and removed units), `currentQuantity` (excl. them), `isGiftCard`, `originalUnitPriceSet` (before discounts), `discountAllocations.allocatedAmountSet` |
| `ShippingLine` | `originalPriceSet` (only with `amount_exclude_shipping` false) |
| `Customer` | `id`, `createdAt`, `tags`, `firstName`, `lastName`, `defaultPhoneNumber.phoneNumber`, `metafield(namespace: "membership", key: "start_date").createdAt` |
| `customers` query | `query: "phone:+<digits>"` (candidate list only; every node is re-checked with the #155 phone rule) |
| `MoneyBag` | `shopMoney { amount currencyCode }`; any currency other than SGD makes the order `amount_unverifiable` |

Not verifiable from the docs (check in the first shadow week): whether `updated_at:>=` behaves as documented for the orders search (use the `Shopify-Search-Query-Debug: 1` header once if counts look off); how a `+65` phone is stored for the `phone:` search; which customer fields count as protected customer data for this app.

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
- An unlink command for a wrong claim link (claim links are append-only today).
