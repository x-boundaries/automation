# Member tags runbook (#268 slices C1 and D): the Shopify tags `member-qualified` and `member-bday-month`

`directlocal/xb_member_tags.py` keeps two Shopify customer tags in step with AutoCount:

- `member-qualified` (slice C1): the member discount points at a Shopify customer segment of that tag, so the tag is the only thing that gives a customer the discount online.
- `member-bday-month` (slice D): on a Qualified member whose Shopify birthday month is this Singapore month. The birthday discount points at a segment of **both** tags, so the birthday tag alone grants nothing.

Nothing runs on merge; the default is `observe`, and both write switches (`tags_write_enabled`, `bday_tag_enabled`) are off.

## 1. What it does and never does

Does, on each run:

1. Reads two link ledgers read-only, before any network call (SQLite `mode=ro` and `query_only`; never copied, never written, no lock taken):
   - the #155 sign-up sync ledger `member_sync.sqlite3` (schema v3): its `links`;
   - the #268 member core ledger `member_benefits.sqlite3` (schema v2): its **active** claim links (`active_claim_links`) and, separately, its **voided** pairs (a claim link joined to its `claim_unlinks` row), both in one read transaction, so one run sees one state. A voided pair is never a basis.
2. Checks the Shopify session: store GID, shop domain, and the app's scopes, which must be exactly `{write_customers}` or exactly `{read_customers, write_customers}` (output `scope_set`: `write_only` or `read_write`).
3. Reads AutoCount read-only through the type move's guarded reads (`ac2_member_type_move_bridge.ps1`, profile `type_move_p1`): a scan of `MEMBER`, `GUEST` and `Default` (MemberNo, IsActive, ExpiryDate, 500 a page), one `type-move-counts` read after the scans (the book's member counts by type and the AutoCount server's local time), and one single read of a linked member found in no scan (at most `max_member_reads_per_run`). The read client admits only those three read operations and refuses every write before any child starts.
4. Reads each linked customer by ID in Shopify (tags; whether `membership.start_date` exists, never its value; and `membership.birthday_month`, read only to compare with this month, never stored or printed), and searches for customers carrying each tag (two searches); each one found is also read by ID before any decision.
5. Decides per customer (sections 2 and 3) and, in `apply` only, sends `tagsAdd` or `tagsRemove` with exactly one tag: `["member-qualified"]` or `["member-bday-month"]`.

Never:

- any other Shopify mutation: no customer create, update or delete, no metafield write, no other tag, never two tags in one call;
- any AutoCount write: no save child is ever started, no new AutoCount operation, no PowerShell or book guard change;
- the removal of a tag that has no basis, unless `remove_unbased_tags_enabled` is on (section 4).

## 2. The rule for `member-qualified`

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
| 14 | carries the tag, no basis, no voided pair | leave alone; off only with the C2 switch (section 4) | `tag_without_basis` / `untag_reason:tag_without_basis` |
| 15 | desired state already true | nothing | `kept_tagged`, `no_change_untagged` |
| 16 | an open write that could not be settled this run | leave alone | `write_unresolved` |

Alerts of rows 1 to 3, 5, 9 to 11b, 12, 14 and 16 are raised once per customer (the `reviews` table) and counted on every run.

## 3. The rule for `member-bday-month` (slice D)

**In one line:** the tag is on only when the customer qualifies for `member-qualified` (the same verdict, from the same reads) **and** the Shopify field `membership.birthday_month` is exactly this Singapore month's English name. It comes off when the field names another month (whatever the `member-qualified` verdict is, except D1 and D2), on `member-qualified`'s positive facts, or when the field is missing or unreadable and this tool's own add was in an earlier month and is still its latest birthday write (whatever the `member-qualified` verdict is, and with no basis too: D6, D7, D9, D13). Everything else unreadable or conflicting is left alone. So a birthday tag this tool added cannot outlive its month, except in D1 and D2 (fix round 1, from the G2's section 9; slice D2 closed the D9 and D13 corners), and except two timing cases: (a) an add settled by read on a later run (section 4) counts in the month it was settled, so in that month it is counted or alerted every run, and it comes off the month after; (b) a removal held back by `bday_tag_enabled`, the pilot list, the birthday remove cap or a guard (volume, type-count, clock, stop file) stays on, explained by its count or alert, until a run sends it. A tag set by hand with no month on record is left with its alert until staff fix the field. That includes a tag set again by hand after this tool's own `DONE` removal (slice D3): only an add that is still this tool's latest `DONE` birthday write for the customer counts as "this tool's own add" here.

- **The field.** `membership.birthday_month` is a choice list (single line text) of exactly `January` ... `December`, written by the MG Member Sign-up form. A value is a month only when the field type is `single_line_text_field` and the value is **exactly** one of those twelve names: no trimming, no case change. `march`, `MARCH`, ` March`, `March `, `Mar`, `3`, `03`, an empty value, a date or a list type are all **unreadable**: left alone with an alert, never guessed. No value on record is **missing**.
- **This month.** Taken once per run from the Singapore date of the tool's clock (UTC+8). A run that starts at 23:59 on the last day of a month uses that month for every customer; the next run (00:11) uses the new one. The month names are a fixed list in the code (no locale).
- **Not required:** the `member-qualified` tag being on the customer at that moment. The segment needs both tags, so a birthday tag alone grants nothing.
- **Q** below is the `member-qualified` verdict for the customer: `on` (row 4), `off` (rows 6, 7, 8, 11, 13: a positive fact), `leave` (rows 3, 9, 10, 11a to 11c, 12), or no basis (rows 13, 14). `B` means the customer carries exactly `member-bday-month`.

| # | Case | Birthday action | Code |
|---|---|---|---|
| D1 | rows 1, 2, 16 (conflict, not found, open write) | leave alone (both tags) | the row's own alert only |
| D2 | a tag equal to `member-bday-month` ignoring case or spaces, not exactly it | leave the birthday tag alone (the qualified decision goes on) | `bday_tag_case_variant` (alert) |
| D3 | Q `on`, the month is this month | on: if `B`, nothing; else add after D10 and D11 | `would_bday_tag` / `bday_tagged` / `bday_kept` |
| D4 | a valid month that is not this month, **whatever Q is** (`on`, `leave`, or no basis), not D1, D2 or D8 | off; if not `B`, nothing | `would_bday_untag` / `bday_untagged`, `bday_untag_reason:bday_month_other` |
| D5 | Q `on`, month missing, not `B` | nothing (no month on record, no 20%) | `bday_no_month` (count, no alert) |
| D6 | Q `on`, month missing, `B` | off when this tool's last `DONE` birthday add for the customer was in an earlier Singapore month (its month has passed) and no `DONE` birthday removal by this tool came after it (slice D3); else leave alone (a cleared field is not taken as a fact) | `bday_untag_reason:bday_added_earlier_month` / `bday_month_missing` (alert **every run**) |
| D7 | Q `on`, month unreadable | `B`: as D6; not `B`: leave alone | `bday_untag_reason:bday_added_earlier_month` / `bday_month_unreadable` (alert every run when `B`, once when not) |
| D8 | Q `off` (positive fact) | off with the same reason; if not `B`, nothing | `bday_untag_reason:member_inactive`, `..._member_expired`, `..._member_not_member` |
| D9 | Q `leave`, and the month is not another valid month (D4) | `B` with the month missing or unreadable: as D6 (off when this tool's add was in an earlier month; slice D2); otherwise leave alone, both ways | `bday_untag_reason:bday_added_earlier_month` / `bday_left_with_member` (counted every run; the row's alert is already raised) |
| D10 | add path: `member-mg` (any case or spaces) and no `membership.start_date` | no add | `flow_dates_guard` (alert) |
| D11 | add path: two `DONE` birthday adds in the last 24 hours and the tag gone again | no add | `bday_tag_keeps_disappearing` (alert) |
| D12 | `B`, no basis, a voided pair for the customer | off | `bday_untag_reason:link_voided` |
| D13 | `B`, no basis, no voided pair, and the month is not another valid month (D4) | month missing or unreadable: as D6 (off when this tool's add was in an earlier month; slice D2, before the C2 switch); otherwise leave alone; off only with the C2 switch (section 4) | `bday_untag_reason:bday_added_earlier_month` / `bday_tag_without_basis` (alert once, counted every run) / `bday_untag_reason:tag_without_basis` |
| D14 | `bday_tag_enabled` false | decide and count as above, send nothing, even in `apply` | `bday_writes_disabled` (count of birthday writes not sent) |
| D15 | `bday_only_customers` not empty and the customer not in it | decide and count, send nothing for that customer | `bday_not_in_pilot` |

"Off" for a customer without the tag is nothing (`bday_no_change_untagged`). A row 3 case variant of `member-qualified` counts as Q `leave` for the birthday tag, so it never causes an extra AutoCount read. The alerts of D2, D7 (not `B`), D10, D11 and D13 are raised once per customer and counted on every run. The alerts of D6 and D7 with `B` left on are raised on **every run** (with their refs), so the daily digest and email keep showing a tag that may be held past its month until staff fix the field.

A voided pair (D12) keeps its own reason `link_voided` whatever the month field says, and is never a month-change removal.

Every birthday removal of these rows (D4 whatever Q is, D6, D7, D9, D12, D13) stays under the birthday remove cap, the birthday volume guard (only a `bday_month_other` removal can be a month-change removal; `bday_added_earlier_month` never is), the stop file, the clock guard, the type-count check and `bday_tag_enabled`. The `member-qualified` decision is unchanged by any of them.

**Order inside a run:** every `member-qualified` write first (adds, then removals), then the birthday writes (adds, then removals), each by customer number ascending.

**Month change (the 1st of each month).** On the 1st nearly every birthday tag correctly comes off (D4) and the new month's members come on (D3). A `bday_month_other` removal is a **month-change removal** when this tool's last `DONE` birthday add for that customer was recorded in an earlier Singapore month and is still this tool's latest `DONE` birthday write for that customer (no own removal after it; slice D3) (`bday_month_change_removals`). Those are exempt from the birthday volume guard, but stay under `max_bday_removes_per_run`, the clock guard and the type-count check. Every other birthday removal (another reason, a field changed within the month, or a tag this tool never added) is under the birthday volume guard, so a misread that would take this month's tags off is withheld. With caps of 100 / 100 a month change clears in one or two runs (the 00:11 and 02:11 runs).

**Case-insensitive segments.** Shopify customer segment tag conditions are not case-sensitive (shopify.dev segment query reference). A case variant such as `Member-Bday-Month` therefore still counts in the segment, but the tool never removes it (D2, and row 3 for `member-qualified`): it raises the case-variant alert once, and staff fix the tag by hand in the admin.

## 4. Writes, caps and guards

- `apply` needs `tags_write_enabled` true; it is refused (`tags_write_disabled`) before any read or ledger write. `observe` can never send a mutation (the client's allowlist refuses both mutations unless the command is `apply` and the switch is on). Birthday writes also need `bday_tag_enabled` true (D14).
- An `INTENT` row (with its tag) is committed before each mutation. Answers: `DONE` (no user error, `node.id` is the customer, the tag present after an add or absent after a remove); `REJECTED` (`shopify_user_error`, no message stored or printed; the run goes on); `NOT_WRITTEN` (every answer was a throttled GraphQL answer and the retry budget ran out, or nothing was sent; the run stops with `shopify_retry_budget_exhausted`); `UNCERTAIN` (timeout, transport error, 5xx, an unproven answer, or a transport error the shared transport does not map, `shopify_transport_unmapped`: the run stops with `write_uncertain` and sends nothing more, of either tag).
- A mutation is retried only on a throttled GraphQL answer (not executed), never on a transport failure, timeout, 5xx or HTTP 429.
- **Settle by read.** At the start of every run, each `INTENT` or `UNCERTAIN` row is settled by that run's by-ID read, for the row's own tag: tag as asked -> `DONE` `resolved_by_read`; not as asked -> `NOT_WRITTEN` `resolved_by_read`; customer gone -> `NOT_WRITTEN` `customer_not_found`. Only then is the customer planned again. A process killed between `INTENT` and the answer is the same case. `apply` refuses (`tags_uncertain_unresolved`) when that read fails; `observe` goes on and leaves those customers alone (row 16).
- No mutation is sent when the by-ID state already matches.
- **Caps, per tag:** `max_tag_adds_per_run` (default 10, 1 to 250) and `max_tag_removes_per_run` (default 5, 1 to 50) for `member-qualified`; `max_bday_adds_per_run` and `max_bday_removes_per_run` (default 10 each, 1 to 250) for the birthday tag. Order by customer number, ascending; the rest wait for the next run (`tag_adds_capped`, `tag_removes_capped` with an alert; `bday_tag_adds_capped`, `bday_tag_removes_capped` with an alert).
- **Pilot allow-list:** `bday_only_customers` (0 to 5 customer GIDs, no duplicates; default empty). When not empty, birthday writes go only to those customers (D15). The live config holds the real test customer's GID on the VM only; the example configs keep it empty.
- **Volume guards, per tag:** when the planned `member-qualified` removals of one run exceed `max(5, 10% of the customers read with that tag)`, none is sent (`untag_volume_unexpected`, `untag_withheld`); adds go on. The birthday guard is the same over birthday removals that are not month-change removals (`bday_untag_volume_unexpected`, `bday_untag_withheld`). `observe` raises the same alerts.
- **Flapping guard, per tag:** a customer with two `DONE` adds of a tag in the last 24 hours whose tag is gone again is left alone (`tag_keeps_disappearing`, `bday_tag_keeps_disappearing`). Adds of the other tag do not count.
- **Type-count check (C2):** after the scans, the `type-move-counts` read gives the book's member count per type. When a scan's row count for `MEMBER`, `GUEST` or `Default` differs from that type's count (a member saved during the scans, or a paging fault), the run alerts `scan_count_mismatch` and **takes no tag off, of either tag** (`untag_withheld_scan_count`); adds go on.
- **Clock guard:** the same read gives `server_now_local`, the AutoCount server's local (Singapore) time. When the VM's clock in Singapore time is **more than 10 minutes** from it, the run sends **no mutation** (`stopped: clock_mismatch`, alert, exit 3); reads and the record still run. This catches a wrong VM **clock** (and so a wrong month on the 1st). It does not catch a wrong VM **time zone**: the guard compares the true instant with the server's local time, and the month is taken from UTC plus 8 hours either way. The time zone stays a VM check (section 7.2).
- **C2 switch `remove_unbased_tags_enabled`** (default false; for both tags): a carrier with no basis (row 14, D13) gets an off decision with reason `tag_without_basis` after this run's by-ID read, inside that tag's remove cap and volume guard (never month-change exempt). Withheld (alert only, as with the switch off) when both input ledgers gave no links (`tags_basis_empty` alert, for example a replaced ledger) or when that tag's carrier search was incomplete. Keep it off until U6 and U7 are done and their hand-set tags are gone.
- **Stop file:** a file `STOP` in the state root stops the run before the next mutation (`stopped: stop_file_present`); reads and the record still run.
- **Lock:** `member_tags.lock` in the state root; a second runner is refused (`ledger_locked_by_another_owner`). No other tool's lock is taken.
- **Refused before the run (exit 2, no `runs` row, nothing written, no mutation):** `apply` with the switch off (`tags_write_disabled`); this tool's ledger bound to another book, store or domain; an input ledger missing, unreadable, of another version or bound to another book, store or domain (read first, before any network call); a failed Shopify session check (token, scopes, store); in `apply`, an open write whose settle read fails (`tags_uncertain_unresolved`).
- **Stopped during the run (exit 3, the `runs` row is finished, `stopped` names the code):** an AutoCount scan, counts or single-read failure, a malformed counts answer (`bridge_output_invalid`), a failed Shopify by-ID read (including a wrong shape of the birthday field) or tag search, stops it before any write; the clock guard stops it before the first mutation; an `UNCERTAIN` answer, a spent retry budget or the `STOP` file stops it before the next mutation.

## 5. Commands and outputs

```
python directlocal\xb_member_tags.py check-config --config <config>   # no network, no ledger
python directlocal\xb_member_tags.py init         --config <config>
python directlocal\xb_member_tags.py observe      --config <config>   # scheduled default: reads + would_* counts
python directlocal\xb_member_tags.py apply        --config <config>   # needs tags_write_enabled true (birthday: also bday_tag_enabled)
python directlocal\xb_member_tags.py status       --config <config>
python directlocal\xb_member_tags.py digest       --config <config> --hours 24
```

Exit codes: 0 ok; 3 completed with alerts or stopped; 2 refused (nothing written, no `runs` row).

- **Run output keys:** `status`, `command`, `run_id`, `counts`, `alerts`, `refs`, `stopped`, `scope_set`, `query_costs` (whole points per query name, or null), `tagged_now` (linked customers carrying `member-qualified` after the run), `carriers_total` (customers the `member-qualified` search found, or null when it hit its page cap: `tag_search_incomplete`), `bday_tagged_now` (linked customers carrying `member-bday-month` after the run), `bday_month_now` (this run's month name), `bday_carriers_total` (the birthday search, or null: `bday_tag_search_incomplete`), `bday_tagged_other_month` (linked customers still carrying the birthday tag whose valid month is not this month: 0 in normal running, since such a tag comes off whatever Q is; otherwise each one is explained by a cap, a withheld guard, the switch, the pilot list, or the D1 / D2 alert).
- **`check-config`** adds `bday_tag`, `bday_tag_enabled`, `max_bday_adds_per_run`, `max_bday_removes_per_run`, `bday_only_customers` (the number of GIDs, never the GIDs) and `remove_unbased_tags_enabled`.
- **`status`:** `tag_writes` counts by `op:state` for `member-qualified` (as in C1) and by `bday:op:state` for the birthday tag.
- **`digest`:** `runs`, `counts`, `alerts` (runs per code), `alert_refs`, `open` (`uncertain_writes`, `intent_writes`), `shadow` (`would_tag`, `would_untag`, `would_bday_tag`, `would_bday_untag` of the latest run), `tagged_now`, `carriers_total`, `bday_tagged_now` and `bday_carriers_total` of the latest run, `stale_month`, and `email`.
- **`stale_month`** (the "month changed and the tags have not" warning): true when the latest completed run in the window ran in another Singapore month than the digest's own (or recorded no month). False when no run completed in the window (`runs.completed` 0 says so).
- **`email`** is a fixed, flat block for the #288 daily email (the sender is changed in a later slice): `runs_completed`, `runs_unfinished`, `tagged_now`, `bday_tagged_now`, `would_tag`, `would_untag`, `would_bday_tag`, `would_bday_untag`, `alerts` (codes only, sorted, at most 20), `stale_month`, `open_writes`. Whole numbers, codes or booleans only; a figure is 0 when no run completed in the window.

Privacy: outputs carry codes, counts, keyed refs (`c-` customer, `m-` member, 20 hex from the ledger's own key) and MemberNos masked to the last 3 characters only. A month name appears only as `bday_month_now` (today's month, not a person's). The ledger (private state root) keeps customer GIDs, MemberNos, this tool's tag operations, codes and counts; never names, phones, emails, other tags or metafield values (the birthday month included).

## 6. Config and state

- Copy `config/member_tags.live.example.json` and replace the two placeholders (`store_gid`, `shop_domain`) with the values in the member core's live config. As they stand, the placeholders are refused (`tags_config_store_not_qualified`).
- `member_sync_state_root` and `member_core_state_root` are the two input roots. The tool's own `state_root` must differ from both and hold no other tool's ledger (`tags_state_root_shared`).
- Shopify app values: user-scope environment variables `XB_SHOPIFY_TAGS_CLIENT_ID` and `XB_SHOPIFY_TAGS_CLIENT_SECRET` of the task account (never a file or chat). The access token is cached with DPAPI in `tags_token_cache.bin` in the state root.
- AutoCount: the same credential target and login mode as the member core on the live book.
- Slice D settings (defaults in both example configs): `bday_tag_enabled` false, `max_bday_adds_per_run` 10, `max_bday_removes_per_run` 10, `bday_only_customers` empty, `remove_unbased_tags_enabled` false.
- Ledger `member_tags.sqlite3`, schema v1, append-only: `meta` (immutable binding: book, store, domain, ref key, tag set), `tag_writes` (no delete; binding columns immutable; transitions only `INTENT` -> any other and `UNCERTAIN` -> `DONE`/`NOT_WRITTEN`; a settled row never changes), `runs`, `reviews`. A ledger of another version or tool is refused (`tags_ledger_not_recognised`). Slice D changes no schema: the tag set already held `member-bday-month`, so an existing ledger is kept.

## 7. VM and admin steps (each its own owner OK; not in the same sitting as a member core, index or backfill step)

### 7.1 `member-qualified` (slice C1)

| Step | What | Owner OK | Check / stop condition | Rollback |
|---|---|---|---|---|
| A0 (admin) | New customer segment "Members - Qualified" = `customer_tags CONTAINS 'member-qualified'`; move the parked member discount code to that segment only; "combines with" all off; full-price collection kept; start date stays parked | "MEMBER CODE SEGMENT" | the segment shows 0 customers | put the code back on its old segment |
| T0 (admin) | Dev Dashboard: create the app "XB Member Tagging" with the scope `write_customers` only (no orders, no discounts, no Flow). Install it. If asked for protected customer data, choose level 1 only (no name, email, phone, address). Client ID and secret into the task account's user environment variables named above | "TAGS APP" | - | uninstall the app |
| T1 (VM, after the member core ledger exists) | Pull main at the merge commit (slice D's if merged: "T1 with D", see 7.2); live config from the live example (section 6); `check-config`; `init`; one `observe` by hand. Record `scope_set`, `query_costs` (compare with section 9), the run time, `tagged_now` (expect 0), `would_tag`, every alert code | "TAGS INSTALL" | exit 2 with any code (`shopify_token_scope_not_expected` -> fix the app, never widen the code); a run over 15 minutes; a sync or member core run fails while a tags run is active | delete the new state root |
| T2 (VM) | Installer dry run, then `-Register -Mode observe` (every 2 hours from 00:11). Observe 4 days (about 48 runs, a weekend, four midnight runs); then `digest --hours 96`: `would_tag` close to the linked active `MEMBER` count; every `link_conflict_*`, `flow_dates_guard`, `expiry_unreadable`, `member_scan_missed`, `scan_count_mismatch`, `clock_mismatch` explained; no `would_untag` you would not make by hand; run times steady | "TAGS OBSERVE" | any unexplained conflict or `would_untag`; run times growing; a sync or member core run failing alongside | disable the task |
| T3 (VM + admin, pilot of 10) | Re-check the Flow list (nothing new on customer created, tags added or tags removed). Config `tags_write_enabled` true, `max_tag_adds_per_run` 10, `max_tag_removes_per_run` 5 (`bday_tag_enabled` stays false); **one `apply` by hand** (the task stays `observe`). Within the hour: (1) Flow, MG member dates, run history: 10 new runs, all ended with no action; (2) the staff sheet: no new row; (3) the segment shows 10; (4) each of the 10: tag present, `membership.start_date` unchanged; (5) the next scheduled `observe`: `tagged_now` 10, `would_tag` down by 10, no `tag_without_basis` | "TAGS PILOT" | any Flow action, sheet row, date change, or a segment size other than 10 | set `tags_write_enabled` false; remove the 10 tags by hand in the admin and stay in `observe` until fixed (the next `apply` would add them again) |
| T4 (VM) | Normal cap: `max_tag_adds_per_run` at the owner's figure (recommended 100, max 250). The installer never replaces a task, so: (1) remove the observe task `\XB\XB-MemberTags` (owner OK); (2) installer dry run with `-Mode apply`; (3) `-Register -Mode apply`. The fill takes about 1 to 3 days at 12 runs a day | "TAGS NORMAL CAP" | as T3; any `untag_volume_unexpected`; a `write_uncertain` not settled on the next run | back to `observe` (remove the apply task, then `-Register -Mode observe`) |
| T5 (admin + VM) | The segment size equals `carriers_total` of the latest run, and `tag_without_basis` is 0 or explained (a test account tagged by hand) | none (a check) | sizes differ | report to the Controller |

### 7.2 `member-bday-month` and the C2 switch (slice D; after T0 to T2; the app, scope and environment variables are unchanged)

| Step | What | Owner OK | Check / stop condition | Rollback |
|---|---|---|---|---|
| D0 (admin, any time; code parked) | New segment "Members - Birthday month" = `customer_tags CONTAINS 'member-qualified' AND customer_tags CONTAINS 'member-bday-month'`; new 20% code on that segment only, the same full-price collection as the member code, combines with nothing, no usage cap, **start date 1 Jan 2099 (parked)**; its name is a placeholder until it is named | "BIRTHDAY CODE PARKED" | the segment saves and shows 0 customers; the code shows Scheduled | delete the code and the segment |
| T1 with D (planned with T1) | Install from slice D's merge commit if merged (else C1's, then D1 later). Config: the birthday keys at their defaults (`bday_tag_enabled` false). Record, besides T1's list: `query_costs.XbMemberTagsCustomers` against **300** (stop above 500: the `nodes` page is then lowered to 50 in code), `bday_month_now`, `would_bday_tag`, `bday_no_month`, every birthday alert code, the clock guard quiet (no `clock_mismatch`), and the VM time zone Singapore (`tzutil /g`; the clock guard does not check it) | "TAGS INSTALL" | as T1, plus any `clock_mismatch` or `scan_count_mismatch` not explained | as T1 |
| D1 (only if T1 ran from C1) | Clone update to slice D's merge commit: disable `\XB\XB-MemberTags`, check out, `check-config`, one `observe` by hand (records as above), enable again. The tags ledger is kept (no schema change) | "TAGS BDAY INSTALL" | as above | check out the C1 commit again |
| D2 (observe, with T2 to T4) | `digest`: `would_bday_tag` close to the expected number of Qualified members with this month; every `bday_month_unreadable` and `bday_month_missing` explained (staff fix the field in the admin); `stale_month` false | (with "TAGS OBSERVE") | an unexplained `would_bday_untag`; `stale_month` true | - |
| D3 pilot + U7 (after T4, with a Qualified test account) | (1) Staff set the test account's birthday month to the current month in the admin. (2) Config `bday_tag_enabled` true, `bday_only_customers` = [the test account's GID] (VM live config only), `max_bday_adds_per_run` 1, `max_bday_removes_per_run` 1; one `apply` by hand: the birthday tag is on; Flow history shows **one** new MG member dates run, ended with no action; no sheet row; start date unchanged; segment "Members - Birthday month" shows 1. (3) **Code window** (separate OK, about an hour, the segment holds only the test account): at checkout without paying, the 20% is accepted on a full-price item, nothing on a sale item, refused together with the member code and with other codes, refused for a second, untagged test account. Park the code again (start 2099). (4) Staff change the test account's month to another month; the next run removes the tag (`bday_untag_reason:bday_month_other`) | "TAGS BDAY PILOT"; "BIRTHDAY CODE TEST WINDOW" | any Flow action, sheet row, date change, a segment size other than 1, or the code accepted outside the cases above | `bday_tag_enabled` false; remove the tag by hand; park the code |
| D4 birthday on (before the 1st's 00:11 run) | `bday_only_customers` empty, caps 100 / 100 (owner's figure, max 250); a config change only (the task already runs `apply` since T4) | "TAGS BDAY ON" | as D3; any `bday_untag_volume_unexpected`; a `write_uncertain` not settled on the next run | `bday_tag_enabled` false |
| D5 month change check (the 1st, morning) | `digest --hours 12`: removals = last month's tags (`bday_month_change_removals`), plus `bday_untag_reason:bday_added_earlier_month` (tags this tool added last month on customers whose month field is now cleared or unreadable, any Q or no basis; these are **not** exempt: they count toward the birthday volume guard, so a `bday_untag_volume_unexpected` on the 1st may come from many cleared fields: report it, do not raise caps), adds = this month's members (capped runs finish by mid-morning); `bday_tagged_other_month` 0 on the latest run, or each one explained by its alert (expected 0 in normal running); `stale_month` false; every `bday_month_missing` / `bday_month_unreadable` with the tag on explained (alerted every run); segment size = `bday_tagged_now` of the last run (case variants explained); run times | none (a check) | any of these off: `bday_tag_enabled` false and report | - |
| C2 switch (after U6 and U7, hand-set tags removed) | `remove_unbased_tags_enabled` true; the next run's removals are exactly the expected carriers with no basis | "TAGS UNBASED REMOVE" | `tags_basis_empty`, or a removal you would not make by hand | switch off |

- The birthday code stays parked after D5 ("20% code ready, parked"). Go-live is decided with the member 15%.
- The tags run live from D4 while the code is parked. That is harmless (a tag grants nothing on its own), and the real month change is then the dress rehearsal.

**Pause rule:** before any type move command, disable `\XB\XB-MemberTags` (as for the member core), and enable it again after.

**Off switch:** `Disable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberTags`, or a `STOP` file in the state root (stops before the next mutation). For the birthday tag alone: `bday_tag_enabled` false.

**The VM time zone** must be Singapore (a VM check at T1, since the clock guard cannot see it): the installer's 00:11 start is local time. **The VM clock** must be right: the clock guard stops every mutation when the VM and the AutoCount server are more than 10 minutes apart.

## 8. Flow interaction

Facts from the 12 Nov 2026 admin look: the "MG member dates" Flow starts on "Customer tags added" and acts only when a tag equals `member-mg` and `membership.start_date` is empty. "Welcome new subscribers" starts only on an email-marketing subscription. No workflow starts on "Customer created".

- An add of either tag starts one "MG member dates" run per customer. Row 5 and D10 never add to a customer with `member-mg` and no start date, the only case where that Flow acts, so it ends with no action in every case this tool can cause. About one birthday add per Qualified member with a month on record, once a year, so the same number of Flow runs, all ending with no action.
- A removal does not start "tags added". A tag change subscribes nobody, so the welcome Flow cannot start.
- Residual (watch at T3 and D3): the tool reads all customers first and writes later in the same run. An already-linked customer who re-submits the sign-up form inside that window could start two acting Flow runs (the same dates written twice, possibly a second sheet row).

## 9. Shopify facts (shopify.dev) and the query cost

| Fact | Page | Version |
|---|---|---|
| `tagsAdd(id, tags)` / `tagsRemove(id, tags)` -> `node`, `userErrors`; accept a Customer | `/docs/api/admin-graphql/2026-10/mutations/tagsAdd`, `.../tagsRemove` | 2026-10 |
| `nodes(ids: [ID!]!)` -> `[Node]!` | `.../queries/nodes` | 2026-10 |
| `customers(first, after, query)`; `query` supports `tag:'x'` | `.../queries/customers` | 2026-10 |
| `Customer.tags`; `Customer.metafield(key, namespace)` returns a nullable `Metafield` | `.../objects/Customer` | 2026-10 |
| `Metafield.value: String!` (always stored as a string, whatever the type); `Metafield.type: String!` | `.../objects/Metafield` | 2026-10 |
| Type names `single_line_text_field` and `list.single_line_text_field` (a list holds a JSON array) | `/docs/apps/build/metafields/list-of-data-types` | none |
| Segment `customer_tags CONTAINS`; clauses joined with `AND`; tag conditions are not case-sensitive | `/docs/api/shopifyql/segment-query-language-reference` | none |
| A scope that writes a resource also grants read access to it | `/docs/api/usage/access-scopes` | none |
| Scalar 0, object 1, connection sized by `first`, mutation 10; one query may not exceed 1,000 points, checked on the requested cost before it runs; standard plan restore 100 points a second | `/docs/apps/build/apis/graphql-admin/rate-limits` | none (links to latest) |
| Array inputs at most 250 items | `/docs/apps/build/apis/graphql-admin/rate-limits` | none |

C1 facts read 12 Nov 2026; the `Metafield`, `Customer.metafield` and rate-limit facts read again on 13 Nov 2026 for slice D.

The rate limits page gives no rule for a list sized by an `ids:` argument. The tool uses the member core's strict reading (one row per ID) and pins the result in `CostTests`:

```
XbMemberTagsCustomers, 100 IDs:  100 x (Customer 1 + start_date metafield 1 + birthday metafield 1) = 300
XbMemberTagsCarriers, first 100: connection 1 + pageInfo 1 + 100 x (Customer 1)                     = 102 (two searches a run)
tagsAdd / tagsRemove:            10 each
```

Both queries are under 500, so the `nodes` page stays at 100. Other readings give up to 400 (the `Node` interface counted per row) or 1,000 (sized by the 250 input maximum), still not over Shopify's 1,000 limit. The first VM `observe` records Shopify's own `requestedQueryCost` of each query in `query_costs` (whole points) to compare: above 500, the `nodes` page is lowered to 50.

Not stated on shopify.dev, and handled by design: what adding a tag already there or removing one not there does (never sent); a tag limit per customer (a `userErrors` answer is `REJECTED`); what `nodes` returns for a missing ID (null or a non-Customer both mean `customer_not_found`); whether the search lags (it never decides alone); which scope list a `write_customers`-only token reports (both sets pinned); any Shopify Flow limit on run volume.

## 10. Open items

- The #288 daily email: the digest's `email` block is ready; the email sender's change is a separate small slice after slice D.
