# Member tags runbook (#268 slice C1): the Shopify tag `member-qualified`

`directlocal/xb_member_tags.py` keeps one Shopify customer tag, `member-qualified`, in step with AutoCount. The member discount points at a Shopify customer segment of that tag, so the tag is the only thing that gives a customer the discount online. Nothing runs on merge; the default is `observe`.

## 1. What it does and never does

Does, on each run:

1. Checks the Shopify session: store GID, shop domain, and the app's scopes, which must be exactly `{write_customers}` or exactly `{read_customers, write_customers}` (output `scope_set`: `write_only` or `read_write`).
2. Reads two link ledgers read-only (SQLite `mode=ro` and `query_only`; never copied, never written, no lock taken):
   - the #155 sign-up sync ledger `member_sync.sqlite3` (schema v3): its `links`;
   - the #268 member core ledger `member_benefits.sqlite3` (schema v2): its **active** claim links (`active_claim_links`) and, separately, its **voided** pairs (a claim link joined to its `claim_unlinks` row). A voided pair is never a basis.
3. Reads AutoCount read-only through the type move's guarded reads (`ac2_member_type_move_bridge.ps1`, profile `type_move_p1`): a scan of `MEMBER`, `GUEST` and `Default` (MemberNo, IsActive, ExpiryDate, 500 a page), and one single read of a linked member found in no scan (at most `max_member_reads_per_run`). The read client admits only those two read operations and refuses every write before any child starts.
4. Reads each linked customer by ID in Shopify (tags, and whether `membership.start_date` exists; never its value), and searches for customers carrying the tag; each one found is also read by ID before any decision.
5. Decides per customer (section 2) and, in `apply` only, sends `tagsAdd` or `tagsRemove` with exactly `["member-qualified"]`.

Never:

- any other Shopify mutation: no customer create, update or delete, no metafield, no other tag;
- any AutoCount write: no save child is ever started, no new AutoCount operation, no PowerShell change;
- the birthday tag `member-bday-month` (slice D: in the tag set, refused now);
- the removal of a tag that has no basis (slice C2 switch, not in C1: such a carrier is only reported).

## 2. The rule

- **On** only for one clean link to an AutoCount `MEMBER` with `IsActive` `T` and an `ExpiryDate` that is readable and on or after **today in Singapore** (valid through the expiry day; off from the next day).
- **Off** only on a positive fact: the member is in the `GUEST` or `Default` scan, `IsActive` is `F`, the expiry is before today, or the customer's only link was voided in the member core.
- Everything else is left alone with an alert.

An `ExpiryDate` before 2020-01-01 (a placeholder such as 2000-01-01) is read as unreadable, never as expired.

| # | Case | Action | Code |
|---|---|---|---|
| 1 | customer in a link conflict | leave alone | `link_conflict_customer`, `link_conflict_member` (every customer of that member), `link_voided_pair_active_in_sync` |
| 2 | customer not found in Shopify | leave alone | `customer_not_found` |
| 3 | a tag equal to ours ignoring case or spaces, not exactly ours | leave alone | `tag_case_variant` |
| 4 | `MEMBER`, active `T`, expiry today or later | on | `would_tag` / `tagged` / `kept_tagged` |
| 5 | row 4, but the customer has `member-mg` and no `membership.start_date` | leave alone, no add | `flow_dates_guard` |
| 6 | `MEMBER`, `IsActive` `F` | off | `untag_reason:member_inactive` |
| 7 | `MEMBER`, active, expiry before today | off | `untag_reason:member_expired` |
| 8 | in the `GUEST` or `Default` scan | off | `untag_reason:member_not_member` |
| 9 | `MEMBER`, `IsActive` neither `T` nor `F` | leave alone | `member_active_unreadable` |
| 10 | `MEMBER`, expiry null, unparseable or before 2020-01-01 | leave alone (no tag if untagged; kept if tagged) | `expiry_unreadable` |
| 11 | in no scan; single read: type not `MEMBER` | off | `untag_reason:member_not_member` |
| 11a | in no scan; single read: `MEMBER` | leave alone | `member_scan_missed` |
| 11b | in no scan; single read: not found | leave alone | `member_not_found` |
| 11c | in no scan; single read cap reached | leave alone | `member_reads_capped` |
| 12 | member in two scans (moved while the scans ran) | leave alone | `member_reads_conflict` |
| 13 | carries the tag, no basis, the member core voided a pair for this customer | off | `untag_reason:link_voided` |
| 14 | carries the tag, no basis, no voided pair | leave alone (C1) | `tag_without_basis` |
| 15 | desired state already true | nothing | `kept_tagged`, `no_change_untagged` |
| 16 | an open write that could not be settled this run | leave alone | `write_unresolved` |

Alerts of rows 1 to 3, 5, 9 to 11b, 12, 14 and 16 are raised once per customer (the `reviews` table) and counted on every run.

## 3. Writes, caps and guards

- `apply` needs `tags_write_enabled` true; it is refused (`tags_write_disabled`) before any read or ledger write. `observe` can never send a mutation (the client's allowlist refuses both mutations unless the command is `apply` and the switch is on).
- An `INTENT` row is committed before each mutation. Answers: `DONE` (no user error, `node.id` is the customer, the tag present after an add or absent after a remove); `REJECTED` (`shopify_user_error`, no message stored or printed; the run goes on); `NOT_WRITTEN` (every answer was a throttled GraphQL answer and the retry budget ran out, or nothing was sent; the run stops with `shopify_retry_budget_exhausted`); `UNCERTAIN` (timeout, transport error, 5xx, an unproven answer: the run stops with `write_uncertain` and sends nothing more).
- A mutation is retried only on a throttled GraphQL answer (not executed), never on a transport failure, timeout, 5xx or HTTP 429.
- **Settle by read.** At the start of every run, each `INTENT` or `UNCERTAIN` row is settled by that run's by-ID read: tag as asked -> `DONE` `resolved_by_read`; not as asked -> `NOT_WRITTEN` `resolved_by_read`; customer gone -> `NOT_WRITTEN` `customer_not_found`. Only then is the customer planned again. A process killed between `INTENT` and the answer is the same case. `apply` refuses (`tags_uncertain_unresolved`) when that read fails; `observe` goes on and leaves those customers alone (row 16).
- No mutation is sent when the by-ID state already matches.
- **Caps:** `max_tag_adds_per_run` (default 10, 1 to 250), `max_tag_removes_per_run` (default 5, 1 to 50); order by customer number, ascending; the rest wait for the next run (`tag_adds_capped`, `tag_removes_capped` with an alert).
- **Volume guard:** when the planned removals of one run exceed `max(5, 10% of the customers read with the tag)`, no removal is sent (`untag_volume_unexpected`, `untag_withheld`); adds go on. `observe` raises the same alert.
- **Flapping guard:** a customer with two `DONE` adds in the last 24 hours whose tag is gone again is left alone (`tag_keeps_disappearing`).
- **Stop file:** a file `STOP` in the state root stops the run before the next mutation (`stopped: stop_file_present`); reads and the record still run.
- **Lock:** `member_tags.lock` in the state root; a second runner is refused (`ledger_locked_by_another_owner`). No other tool's lock is taken.
- Any AutoCount or Shopify read failure stops the run before any write (`stopped` names the code, exit 3).

## 4. Commands

```
python directlocal\xb_member_tags.py check-config --config <config>   # no network, no ledger
python directlocal\xb_member_tags.py init         --config <config>
python directlocal\xb_member_tags.py observe      --config <config>   # scheduled default: reads + would_tag / would_untag
python directlocal\xb_member_tags.py apply        --config <config>   # needs tags_write_enabled true
python directlocal\xb_member_tags.py status       --config <config>
python directlocal\xb_member_tags.py digest       --config <config> --hours 24
```

Exit codes: 0 ok; 3 completed with alerts or stopped; 2 refused (nothing written, no `runs` row).

Run output keys: `status`, `command`, `run_id`, `counts`, `alerts`, `refs`, `stopped`, `scope_set`, `query_costs` (whole points per query name, or null), `tagged_now` (linked customers carrying the tag after the run), `carriers_total` (customers the tag search found, or null when the search hit its page cap: `tag_search_incomplete`). `digest` gives `runs`, `counts`, `alerts` (runs per code), `alert_refs`, `open` (`uncertain_writes`, `intent_writes`), `shadow` (`would_tag`, `would_untag` of the latest run), `tagged_now` and `carriers_total` of the latest run.

Privacy: outputs carry codes, counts, keyed refs (`c-` customer, `m-` member, 20 hex from the ledger's own key) and MemberNos masked to the last 3 characters only. The ledger (private state root) keeps customer GIDs, MemberNos, this tool's tag operations, codes and counts; never names, phones, emails, other tags or metafield values.

## 5. Config and state

- Copy `config/member_tags.live.example.json` and replace the two placeholders (`store_gid`, `shop_domain`) with the values in the member core's live config. As they stand, the placeholders are refused (`tags_config_store_not_qualified`).
- `member_sync_state_root` and `member_core_state_root` are the two input roots. The tool's own `state_root` must differ from both and hold no other tool's ledger (`tags_state_root_shared`).
- Shopify app values: user-scope environment variables `XB_SHOPIFY_TAGS_CLIENT_ID` and `XB_SHOPIFY_TAGS_CLIENT_SECRET` of the task account (never a file or chat). The access token is cached with DPAPI in `tags_token_cache.bin` in the state root.
- AutoCount: the same credential target and login mode as the member core on the live book.
- Ledger `member_tags.sqlite3`, schema v1, append-only: `meta` (immutable binding: book, store, domain, ref key, tag set), `tag_writes` (no delete; binding columns immutable; transitions only `INTENT` -> any other and `UNCERTAIN` -> `DONE`/`NOT_WRITTEN`; a settled row never changes), `runs`, `reviews`. A ledger of another version or tool is refused (`tags_ledger_not_recognised`).

## 6. VM and admin steps (each its own owner OK; not in the same sitting as a member core, index or backfill step)

| Step | What | Owner OK | Check / stop condition | Rollback |
|---|---|---|---|---|
| A0 (admin) | New customer segment "Members - Qualified" = `customer_tags CONTAINS 'member-qualified'`; move the parked member discount code to that segment only; "combines with" all off; full-price collection kept; start date stays parked | "MEMBER CODE SEGMENT" | the segment shows 0 customers | put the code back on its old segment |
| T0 (admin) | Dev Dashboard: create the app "XB Member Tagging" with the scope `write_customers` only (no orders, no discounts, no Flow). Install it. If asked for protected customer data, choose level 1 only (no name, email, phone, address). Client ID and secret into the task account's user environment variables named above | "TAGS APP" | - | uninstall the app |
| T1 (VM, after C1 is merged and the member core ledger exists) | Pull main at the merge commit; live config from the live example (section 5); `check-config`; `init`; one `observe` by hand. Record `scope_set`, `query_costs` (compare with section 8), the run time, `tagged_now` (expect 0), `would_tag`, every alert code | "TAGS INSTALL" | exit 2 with any code (`shopify_token_scope_not_expected` -> fix the app, never widen the code); a run over 15 minutes; a sync or member core run fails while a tags run is active | delete the new state root |
| T2 (VM) | Installer dry run, then `-Register -Mode observe` (every 2 hours from 00:11). Observe 4 days (about 48 runs, a weekend, four midnight runs); then `digest --hours 96`: `would_tag` close to the linked active `MEMBER` count; every `link_conflict_*`, `flow_dates_guard`, `expiry_unreadable`, `member_scan_missed` explained; no `would_untag` you would not make by hand; run times steady | "TAGS OBSERVE" | any unexplained conflict or `would_untag`; run times growing; a sync or member core run failing alongside | disable the task |
| T3 (VM + admin, pilot of 10) | Re-check the Flow list (nothing new on customer created, tags added or tags removed). Config `tags_write_enabled` true, `max_tag_adds_per_run` 10, `max_tag_removes_per_run` 5; **one `apply` by hand** (the task stays `observe`). Within the hour: (1) Flow, MG member dates, run history: 10 new runs, all ended with no action; (2) the staff sheet: no new row; (3) the segment shows 10; (4) each of the 10: tag present, `membership.start_date` unchanged; (5) the next scheduled `observe`: `tagged_now` 10, `would_tag` down by 10, no `tag_without_basis` | "TAGS PILOT" | any Flow action, sheet row, date change, or a segment size other than 10 | set `tags_write_enabled` false; remove the 10 tags by hand in the admin and stay in `observe` until fixed (the next `apply` would add them again) |
| T4 (VM) | Normal cap: `max_tag_adds_per_run` at the owner's figure (recommended 100, max 250); reinstall with `-Mode apply` (dry run first). The fill takes about 1 to 3 days at 12 runs a day | "TAGS NORMAL CAP" | as T3; any `untag_volume_unexpected`; a `write_uncertain` not settled on the next run | back to `observe` |
| T5 (admin + VM) | The segment size equals `carriers_total` of the latest run, and `tag_without_basis` is 0 or explained (a test account tagged by hand) | none (a check) | sizes differ | report to the Controller |

**Pause rule:** before any type move command, disable `\XB\XB-MemberTags` (as for the member core), and enable it again after.

**Off switch:** `Disable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberTags`, or a `STOP` file in the state root (stops before the next mutation).

## 7. Flow interaction

Facts from the 12 Nov 2026 admin look: the "MG member dates" Flow starts on "Customer tags added" and acts only when a tag equals `member-mg` and `membership.start_date` is empty. "Welcome new subscribers" starts only on an email-marketing subscription. No workflow starts on "Customer created".

- An add starts one "MG member dates" run per customer. Row 5 never adds to a customer with `member-mg` and no start date, the only case where that Flow acts, so it ends with no action in every case this tool can cause.
- A removal does not start "tags added". A tag change subscribes nobody, so the welcome Flow cannot start.

## 8. Shopify facts (shopify.dev, read 12 Nov 2026) and the query cost

| Fact | Page | Version |
|---|---|---|
| `tagsAdd(id, tags)` / `tagsRemove(id, tags)` -> `node`, `userErrors`; accept a Customer | `/docs/api/admin-graphql/2026-10/mutations/tagsAdd`, `.../tagsRemove` | 2026-10 |
| `nodes(ids: [ID!]!)` -> `[Node]!` | `.../queries/nodes` | 2026-10 |
| `customers(first, after, query)`; `query` supports `tag:'x'` | `.../queries/customers` | 2026-10 |
| `Customer.tags`, `Customer.metafield(namespace, key)` (nullable) | `.../objects/Customer` | 2026-10 |
| A scope that writes a resource also grants read access to it | `/docs/api/usage/access-scopes` | none |
| Scalar 0, object 1, connection sized by `first`, mutation 10; one query may not exceed 1,000 points, checked on the requested cost before it runs; standard plan restore 100 points a second | `/docs/apps/build/apis/graphql-admin/rate-limits` | none (links to latest) |
| Array inputs at most 250 items | `/docs/apps/build/apis/graphql-admin/rate-limits` | none |

The rate limits page gives no rule for a list sized by an `ids:` argument. The tool uses the member core's strict reading (one row per ID) and pins the result in `CostTests`:

```
XbMemberTagsCustomers, 100 IDs:  100 x (Customer 1 + metafield 1)                = 200
XbMemberTagsCarriers, first 100: connection 1 + pageInfo 1 + 100 x (Customer 1)  = 102
tagsAdd / tagsRemove:            10 each
```

Both queries are under 500, so the `nodes` page stays at 100. The first VM `observe` records Shopify's own `requestedQueryCost` of each query in `query_costs` (whole points) to compare.

Not stated on shopify.dev, and handled by design: what adding a tag already there or removing one not there does (never sent); a tag limit per customer (a `userErrors` answer is `REJECTED`); what `nodes` returns for a missing ID (null or a non-Customer both mean `customer_not_found`); whether the search lags (it never decides alone); which scope list a `write_customers`-only token reports (both sets pinned).

## 9. Open items (not in C1)

- C2 (in slice D's round): `remove_unbased_tags_enabled` (remove the tag from a carrier with no basis, inside the remove cap and the volume guard); a type-count consistency check; the tags digest in the #288 daily email.
- Slice D: `member-bday-month` (in `TAG_SET`, refused in `ACTIVE_TAGS` now).
