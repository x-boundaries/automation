# Member Sync Phase 1: Two-Way Create (#155)

Status (2026-10-22): source and offline tests only (build step 2, draft PR,
round 9). Nothing here has run against AutoCount or Shopify. Every write
switch is off by default, and so are ChangeLog reads (`changelog_xml_column`
null); turning one on, any AutoCount login and any scheduling each need their
own Owner approval. Only the test book
`AED_TESTINGAPI` is authorised; `AED_XBOUNDARIES` is refused in Python and
PowerShell.

Code: `directlocal/xb_member_sync.py` (engine, ledger, Shopify client,
credential loader, CLI), the `sync-lookup` / `sync-changelog` operations and
the `sync_p1` write-once profile in `scripts/ac2_directlocal_member_bridge.ps1`,
and the `sync_p1` member create in `scripts/ac2_member_sync_member.ps1` (the
pinned gateway adapter `scripts/ac2_member_gateway_autocount_adapter.ps1` is
unchanged; the new script reuses its helpers).
Tests: `tests/test_member_sync.py`, `tests/test_member_sync_bridge_ps.py`.

## What phase 1 does

| Direction | Trigger | Result |
|---|---|---|
| Shopify -> AC2 | A customer tagged `member-mg` whose registration instant (`membership.start_date` metafield `createdAt`) is at or after `shopify_cutover_utc` | A link to the member staff create by hand; only if none matches by the next-morning cutoff, an AC2 member created by the sync (with an alert) |
| AC2 -> Shopify | A `dbo.ChangeLog` insert (`TableName = 'Member'`, `ChangeType = 'I'`) after the stored `ID` watermark, for example a member created at the till | A Shopify customer, or a link to an existing one |

Both directions write the mapping (Shopify customer GID <-> MemberNo, each
unique) to the sync ledger. That mapping is the loop prevention: an AC2
insert for a mapped MemberNo, or for a MemberNo with an open write, is never
sent to Shopify; a mapped customer is never sent to AC2.

### Shopify -> AC2: staff first, deferred create (Owner, 22 Oct 2026)

Shop staff key members in AC2 by hand, on the spot (round 7: the till picks
them up after about 15 minutes), and choose Member or Guest themselves.
Online and in-shop customers use the same sign-up form, so the sync cannot
tell them apart. Staff typing of names is unreliable, so the link does not
use the name (round 7). Therefore:

0. **Existing customers first (round 7c).** Before any of this, the
   one-off initial link (below) links every Shopify customer made before the
   cutover to the AC2 member whose MemberNo is their phone. Until an applied
   initial link has completed a full pass, Shopify -> AC2 reads no sign-ups
   and makes no link or create (alert `initial_link_not_complete`). The run's
   start-up housekeeping still runs before that check: crashed `INTENT` rows
   become `UNCERTAIN` and earlier unclear writes are looked up (none can
   exist on a fresh ledger before the hold opens).
1. **Link first.** A new sign-up is not created in AC2 straight away. Every
   run looks it up again (it stays a pending customer); no link writes to
   AC2. Email never links.
   - **MemberNo + created time (round 7, 7c).** The AC2 member whose
     MemberNo is exactly the sign-up's phone (`phone_member_no`: `65` + 8
     digits for a Singapore number, or round 7c the same 8 digits without
     `65`; a foreign `+` number's full digits; MemberNo trimmed, any case) is
     linked when its `CreatedTime` and the **sign-up time** are at most
     `memberno_link_window_minutes` (default 10) apart, either way, the
     boundary included (`memberno_and_created_time_match`). The sign-up time
     (round 7c) is `ShopifyCustomer.registration_at`: the `createdAt` of the
     `membership.start_date` customer metafield the sign-up form writes,
     the same instant the sync uses for the cutover and the 06:00 cutoff
     (after the bounded wait for that metafield, `Customer.createdAt` stands
     in, as it already does for those). `Customer.createdAt` itself is not
     compared, so an older Shopify account that signs up now still links.
     Both forms of the number in AC2 at once (`65` + 8 digits and the 8
     digits) is a conflict: alert `memberno_conflict`, no link, no create.
     The name is not used. AC2 stores `CreatedTime` in Singapore local time
     with no offset (VM fact G0, below); the sync reads it as Asia/Singapore
     (UTC+8), turns it into UTC and compares it with the sign-up time (UTC). Only the two stored
     times are compared, never the run's own clock, so any run frequency
     works, including once a day, and a link found after the next-morning
     cutoff still wins over a create.
   - **Same MemberNo, outside the window:** no link and no create, ever.
     The alert `memberno_exists_outside_window` names the customer's ref
     and the MemberNo masked to its last 3 characters (`member_no_masked`);
     the pending row becomes `SKIPPED` with that code, so the alert is
     raised once and the customer is not read again. An operator decides.
     A missing or unreadable time on either side is treated the same way
     (`created_time_missing`, alert).
   - **Name rule, only where still needed:** an AC2 record that holds the
     phone in another way (MobilePhone, or the 8 digits embedded in a longer
     MemberNo) and no record whose MemberNo is exactly the phone: linked when the name rule below
     matches (`phone_and_name_match`); otherwise the shared-phone fallback
     MemberNo after the cutoff, as before.
2. **Wait.** Before the cutoff, an unmatched sign-up is only counted
   (`waiting_for_staff`, pending code `waiting_for_staff`); nothing is
   written and `GetNextMemberNo` is not called.
3. **Deferred create.** The cutoff is `deferred_create_local_time` (default
   `06:00`, Asia/Singapore, which is UTC+8 all year) on the day after the
   sign-up's Singapore date, never the same morning (a sign-up at 00:10 waits
   until 06:00 the next day). A sign-up still unmatched at the cutoff is
   created by the sync with the member type `ac2_create_member_type`
   (default `REGISTERED`, the test book's type), under the numbering and
   matching rules below, and raises the alert `created_after_wait` (the run
   exits 3). With `ac2_create_enabled` off it is counted only
   (`ac2_create_disabled` plus the decision code).
4. **Never change a staff member.** The sync has no update path at all: it
   only links to a member staff made, and never changes its MemberType or any
   other field (the bridge has no update operation; static test).
5. **Possible duplicates.** All duplicate guards stay. When a member staff
   make appears AFTER the sync already created one for the same person (same
   phone, name rule; round 7 keeps the name rule here, alert only), the sync
   raises the alert `possible_duplicate_member` and merges, links or writes
   nothing:
   - Shopify -> AC2 runs re-check the members this sync created in the last
     7 days (at most 10 per run; read-only lookups by MemberNo and phone);
   - AC2 -> Shopify (when ChangeLog reads are on) flags the staff insert
     whose Shopify customer is already linked to a sync-created member
     (pending code `possible_duplicate_member`).
   A member created without a phone cannot be checked (phone is the only
   key; email never links).

### Shopify -> AC2 create

| AC2 field | Value |
|---|---|
| MemberNo | The phone in digits (numbering rules below). Without a usable phone: AutoCount's own `GetNextMemberNo` number. |
| MemberType | `ac2_create_member_type` (default `REGISTERED`, the test book's type); validated in Python and in the bridge (`member_type_invalid`) |
| Name | Shopify first name + last name |
| MobilePhone | The same digits, or NULL |
| EmailAddress | Shopify default email, or NULL |
| DOB | `2000-MM-01` from the birthday-month metafield, or NULL |
| RegisterDate | `membership.start_date` value, else the registration date in Singapore time |
| ExpiryDate | NULL (the Shopify expiry metafield is ignored) |
| IsActive / Individual / OpeningPoints | `T` / `T` / 0 |

### Phone numbering rules (`phone_member_no`)

- With `+`: the full number with its country code (`+65` must be followed by
  8 digits starting 3/6/8/9).
- Without `+`: 8 digits starting 3/6/8/9 get `65` prepended; `65` + such 8
  digits stays as typed.
- Without `+` and not a recognised Singapore number (Owner, L1 option B: "if
  really dont know just leave without country code"): the digits exactly as
  typed, 9-15 of them, never a country code added, stripped or guessed.
- Invalid (no usable phone, so `GetNextMemberNo`): a leading `0`; fewer than
  8 or more than 15 digits; 8 digits not starting 3/6/8/9; a `65`-prefixed
  number that is not `65` + a Singapore 8-digit number; any letter.
- The Shopify phone lookup normalises both sides with this same function.
- Sent to Shopify (Owner Q2, 2026-10-21: keep today's behaviour): the number
  in E.164, `+` and the digits above. A number without `+` that is not a
  recognised Singapore number is therefore sent as `+` + its digits as typed,
  and Shopify reads its leading digits as a country code. This is the Owner's
  accepted choice.

### Matching rules (no review queue)

Before the cutoff (above) every CREATE row below is `waiting_for_staff`
instead; after it, every create also raises `created_after_wait`.

| Situation | Action | Counted as |
|---|---|---|
| Round 7c: Shopify -> AC2 before an applied initial link has completed | No sign-up read, no link, no create (start-up housekeeping of earlier writes still runs) | `initial_link_not_complete` (alert) |
| Round 7: an AC2 MemberNo is exactly the phone (round 7c: `65` + 8 digits or the 8 digits), and its `CreatedTime` is within `memberno_link_window_minutes` of the sign-up time (`registration_at`, either way) | Link, no create; name not used | `memberno_and_created_time_match` |
| Round 7c: both forms of the number are MemberNos in AC2 | No link, no create (pending `SKIPPED`); alert with the masked MemberNo. Round 9 (G2 M2): AC2 -> Shopify applies the same rule | `memberno_conflict` |
| Round 7: that MemberNo exists, outside the window | No link, no create (pending `SKIPPED`); alert with the MemberNo masked to its last 3 characters | `memberno_exists_outside_window` |
| Round 7: that MemberNo exists, but a time is missing or unreadable | No link, no create (pending `SKIPPED`); alert | `created_time_missing` |
| No MemberNo is the phone, but the phone is in AC2 otherwise (MobilePhone normalises to it, or the 8 digits are embedded in a longer MemberNo) and the name rule below matches | Link, no create | `phone_and_name_match` |
| Several such records match the name | Skip (never guessed, never merged) | `phone_on_multiple_records` |
| Phone matches that way, the name rule does not match (people sharing a phone) | Create with `<phone digits><FIRST+LAST letters A-Z>` truncated to 20 characters | `shared_phone_name_fallback` |
| That MemberNo is already held (in AC2, trimmed and case-insensitive, or by another customer's link or open write in the ledger) | The first free of the same value with a digit 2-9 appended (letters trimmed to stay within 20) | `shared_phone_suffix_digit` |
| Shared phone but the name has no Latin letters (Owner round 2, Q1) | `<phone digits>` + the first digit 2-9 free in both AC2 and the ledger | `shared_phone_digit_fallback` |
| Digits 2-9 all taken | Skip | `shared_phone_member_no_exhausted` |
| No phone, or an unusable one | Create with AutoCount's next member number (`GetNextMemberNo`) | `no_phone_generator_member_no` (+ `shopify_phone_invalid_treated_as_absent`) |
| Email matches only | Ignored: email never links | - |

`GetNextMemberNo` (no phone): the number is taken only when the write will
really start (never while `ac2_create_enabled` is off). On the test book the
preview does not advance after a save (observed 16 October 2026), so a taken
preview continues in the generator's own format above every number this
ledger has used and every number found taken in AC2, at most three candidates
per member (`member_no_successor`, as in the Core). A generator failure writes
nothing and the customer is retried on a later run. As in the Core, numbers
derived this way run ahead of AutoCount's own counter: production numbering
stays an E9 decision.

### Name rule (Owner decisions Q1 and r3a, 2026-10-21)

Round 7: Shopify -> AC2 no longer uses this rule for a record whose
MemberNo is exactly the phone (that record links by created time, above). It
is left only for records holding the phone in another way, where it chooses
between a link and the shared-phone fallback MemberNo, and for the
`possible_duplicate_member` alert. AC2 -> Shopify is unchanged.

The rule (`full_name_matches`, both directions): on the same phone, the names
match when the **shorter name has at least 2 words** and **every one of them
is a word of the longer name** (2 to 2, or 2 to 3 and more). Any other case
is a different person and takes the no-link path of that direction:

- Shopify -> AC2: the shared-phone MemberNo (phone + name letters, a digit
  2-9 when taken; phone + digit 2-9 when the name has no Latin letters).
- AC2 -> Shopify: a separate customer without the phone, created only while
  `shopify_create_enabled` is on (counted and kept pending while it is off).

Details:

- The Shopify name is the full name, `firstName` + " " + `lastName` (either
  may be empty). The AC2 name is `Name`.
- Both sides go through one shared helper (`name_words`): Unicode NFKC, then
  case folding, then every character that is not a Unicode letter, mark or
  digit (punctuation, hyphens, apostrophes, dots, commas, any whitespace)
  becomes a space, then the text is split into words. A name is a set of
  words: word order and repeated words do not matter.
- Exact words only: no prefixes, initials, edit distance or fuzzy matching
  ("Anne" is not "Ann"; "T" is not "Tester").
- A name of 0 or 1 words never links, even when both names are identical
  ("Mary" and "MARY"). A blank or punctuation-only name has 0 words; the
  existing rule still skips a Shopify -> AC2 create without a name
  (`shopify_name_missing`).
- Any script: the helper is script-agnostic and identical normalised words
  match. A name written without spaces (common in CJK) is **one word**, so it
  never links, even when identical; the same name written with a space (2
  words) links when identical. A Latin and a non-Latin name share no words.
  The non-Latin shared-phone path is unchanged (MemberNo = phone + the first
  free digit 2-9, checked in AC2 and the ledger; skipped and counted when all
  are taken).
- B2 (unclear AC2 create) uses the same normalisation but stays an exact
  check of our own write: the found member's words, in order, must equal the
  intended name's (never a subset; see Write safety).

Examples, all on one shared fake phone (`FullNameRuleTests`, one test per
example and direction). "Split" means Shopify `firstName` "MARY ANN" +
`lastName` "LEE"; "whole" means `firstName` "MARY ANN LEE" and an empty
`lastName`, as AC2 -> Shopify creates write it.

| Example | Shopify -> AC2 (Shopify name -> AC2 `Name`) | AC2 -> Shopify (AC2 `Name` -> Shopify split / whole) |
|---|---|---|
| "Ann Lee" vs "MARY ANN LEE" | link | link / link |
| "Mary Lee" vs "MARY ANN LEE" | link | link / link |
| "Mary Ann Lee" vs "MARY ANN LEE" | link | link / link |
| "Mary Tan" vs "MARY ANN LEE" | separate (phone + `MARYTAN`) | separate / separate |
| "John Lim" vs "MARY ANN LEE" | separate (phone + `JOHNLIM`) | separate / separate |
| "Anne Lee" vs "ANN LEE" | separate (phone + `ANNELEE`) | separate / separate (Shopify "ANN" + "LEE", or "ANN LEE" whole) |
| Word order: "Lee Mary Ann" vs "MARY ANN LEE" | link | link / link |
| Punctuation and spacing: "Mary-Ann  Lee." vs "MARY ANN LEE" | link | link / link |
| One word: "Lee" vs "MARY ANN LEE" | separate | separate (Shopify "Lee" alone) |
| One word: "Mary" vs "MARY ANN LEE" | separate | separate (Shopify "Mary" alone) |
| One word, identical: "Mary" vs "MARY" | separate | separate |
| One word on the AC2 side: Shopify "Mary Lee" vs AC2 "LEE"; AC2 "Lee" vs Shopify split | separate | separate |
| Empty or punctuation only ("" or ".." on both sides) | never links (an empty Shopify name is skipped) | never links |
| Non-Latin, identical, written without spaces (1 word) | separate (phone + digit 2-9) | separate |
| Non-Latin, identical, 2 words | link | link |
| Non-Latin, the same characters split into 2 words vs written as 1 word | separate (phone + digit 2-9) | - |
| Non-Latin Shopify name vs Latin AC2 name | separate (phone + digit 2-9) | separate |
| Latin Shopify name vs non-Latin AC2 name | separate (phone + letters) | - |

Before this round the code compared only the Shopify first name with a run
of words of the AC2 `Name`. Re-added as tests (each failed on `3562a21`):
Shopify "Mary Tan" (linked through "Mary"), and AC2 "Ann Lee" and "Mary Lee"
against Shopify split and whole (stayed separate).

### AC2 -> Shopify create

0. Off by default (Owner Q4, 2026-10-21): with `changelog_xml_column` null
   the run reads no ChangeLog, queues nothing and makes no ledger links
   (`changelog_column_unconfigured`). The operator sets it to `"Content"` at
   UAT, deliberately, after confirming the name rule above.
1. First run: the watermark is set to the current highest ChangeLog `ID`
   (history is not replayed; the backfill is phase 2). Inserts already
   visible within the overlap window below it are recorded as seen
   (history), so only an insert that commits later is processed.
2. Each run reads the inserts after `watermark - changelog_overlap`, in `ID`
   order (up to `changelog_batch` new ones). An ID already processed is a
   no-op; an unseen ID below the watermark (an insert that committed late,
   VM fact 7) is processed (`changelog_late_row_processed`). Gaps are normal
   (26 vendor procedures, including `bsp_ChangeMemberNo`, delete their own
   rows by ID). The watermark advances row by row in the same transaction as
   the queueing. The overlap the run used is stored in the ledger
   (`watermarks` key `ac2_changelog_overlap`).

   Overlap baseline (G2 H3, Option A): the seen IDs cover the overlap window
   only when the last run used at least this overlap. When a watermark exists
   and either no overlap is stored (a ledger upgraded from v1, or written
   before this rule) or the configured overlap is larger than the stored one,
   every insert already in the window below the watermark is marked seen as
   history, without processing (alert `changelog_overlap_baselined`, count
   `changelog_overlap_baselined_rows`), and the new overlap is stored. This
   is what the first run already does for a fresh ledger, so it reuses that
   rule; the cost is the same accepted risk: an insert that commits late
   inside the window on that one run is treated as history. Option B
   (refuse until the operator confirms) was not chosen: A needs no new CLI
   flag or schema change and leaves no stalled direction. A smaller overlap
   is stored without a baseline (pruning follows it).
3. A row that cannot be parsed stops the run's ChangeLog read: the watermark
   stays at the last good ID, the run status is `completed_with_alerts`
   (alert and count `changelog_row_unparsed`) and the CLI exits 3. Later rows
   wait for the operator.
4. A highest ID below the watermark (numbering went backwards) is refused:
   alert and count `changelog_id_reset_detected`, nothing processed, the
   watermark never reset automatically.
5. A snapshot (MemberNo, first ChangeLog ID, salted digest) is kept in the
   ledger; the member's Guid is never used as a key (it can change).
6. Queued members are re-read from AC2 by MemberNo (so the ledger holds no
   names or phones). Their phone is `MobilePhone` (through
   `phone_member_no`, unchanged, including L1). When MobilePhone gives no
   phone (VM fact 3: present on 198 of 2,758 production inserts), the
   MemberNo gives one **only when it is a Singapore number** (Owner Q3,
   2026-10-21): `65` + 8 digits starting 3/6/8/9 (used as is), or those 8
   digits alone (`65` added). Any other MemberNo gives no phone (see Known
   limits for the shapes).
7. The member is looked up in Shopify by phone. The search result is only a
   candidate list: a node whose own phone does not normalise to the same
   digits, or has no phone, is dropped before any decision (G2 B1).

   **Round 8 (G2 H1): a staff-keyed member is never a second Shopify
   customer.** When the member's MemberNo is exactly that phone (`65` + 8
   digits or the 8 digits, as Shopify -> AC2 uses), AC2 -> Shopify never
   creates a Shopify customer for it and never judges it by name:
   - round 9 (G2 M2): when both forms of the phone (`65` + 8 digits and the
     8 digits) are MemberNos in AC2, the Shopify -> AC2 conflict rule
     applies: never linked, never created (with or without a Shopify
     customer), pending `SKIPPED`, alert `memberno_conflict` with the
     MemberNo masked to its last 3 characters. The check is one AC2 lookup
     by phone per such member (the batched lookup by MemberNo is capped at
     20 numbers by the unchanged bridge);
   - the Shopify customer is found by the phone search or, if the search
     does not show it yet, among the pending sign-ups of this ledger (read
     once per run, at most `max_pending_rechecks_per_run`; more pending than
     that and no match: held, alert `ac2_member_held_for_sign_up`). Round 9
     (G2 L6): this hold has the same bound as the sign-up-time hold below
     (`REGISTRATION_WAIT_MAX_CHECKS`, 5 runs: at most 4 holds); on the 5th
     run the member is `SKIPPED` with the alert `ac2_member_hold_stuck`
     (masked MemberNo) and is never created. Shopify -> AC2 still links the
     sign-up by time on its own when it reads it. Both holds share the
     member's one `checks` counter, so a member is held at most 4 runs in
     all;
   - already linked to another member: alert
     `shopify_customer_linked_to_other_member` (or `possible_duplicate_member`
     when the sync created that member), never merged;
   - otherwise the Shopify -> AC2 rule decides: `CreatedTime` within
     `memberno_link_window_minutes` of the customer's sign-up time
     (`registration_at`) links (`memberno_and_created_time_match`; inclusive:
     exactly 10:00 apart links and 10:00.001 does not, pinned by tests on
     both sides, round 9 G2 L7); farther
     apart: alert `memberno_exists_outside_window`, no link, no create; a
     missing time: alert `created_time_missing`;
   - a sign-up whose `start_date` metafield is not readable yet is held
     (pending `held_for_sign_up`, alert `ac2_member_held_for_sign_up`) for
     the same bounded wait as Shopify -> AC2 (`REGISTRATION_WAIT_MAX_CHECKS`,
     5 runs); after that `Customer.createdAt` stands in, as it does there. A
     Shopify -> AC2 run in between usually links it first, and AC2 ->
     Shopify then finds the member linked (`ac2_already_linked`).

   Otherwise (a MemberNo that is not the phone, for example a till number
   with the phone in MobilePhone), unchanged:
   - one customer, name rule matches: link (`shopify_phone_and_name_match`);
   - one customer, name rule does not match: a different person (Owner,
     21 October 2026). Never linked. A separate customer is created
     **without the phone** (Shopify phones are unique), with the AC2 `Name`,
     email if AC2 has one, the tags and the metafields below
     (`shopify_phone_owned_by_other_name`,
     `shopify_separate_customer_created_without_phone`);
   - several customers: skip;
   - none: `customerCreate` with first name = the AC2 `Name`, phone in E.164,
     email if present, the configured tags and the metafields below.
8. A member without a phone is skipped and counted (there is no safe
   duplicate key; email never links).

Every `customerCreate` (with or without a phone) sends the tags
(`ac2-member`) and the `custom.ac2_member_no` metafield holding the AC2
MemberNo (Owner round 2, Q2), plus `custom.birthday_month` when AC2 has a
DOB. Shopify requires a name, phone number or email address on
`customerCreate` (shopify.dev, `customerCreate`, API 2026-10, read 21 October
2026); the AC2 `Name` is always present (a member without a name is skipped
and counted).

Till-created members reach the HQ ChangeLog about 15 minutes after creation
(observed 1-72 minutes), so AC2 -> Shopify lags by at least that much.

## Go-live gate: Shopify create (Owner round 2, Q3)

A first-name-only customer must be proven accepted before
`shopify_create_enabled` is enabled. There is no Shopify test store: the only
store this source qualifies is the live one (G2). Owner's chosen method
(2026-10-21): at UAT, one fake customer on the live store, created only with
the Owner's OK at that moment and deleted straight after. The code stays off
(`shopify_create_enabled` default `false`); this source and its CI never test
it.

## Settings (`config/member_sync.example.json`)

Unknown keys, at the top level or nested, are refused.

| Key | Default | Notes |
|---|---|---|
| `autocount_book` | required | `AED_TESTINGAPI` only; `AED_XBOUNDARIES` refused |
| `state_root` | required | Private root (same custody check as the Core); ledger `member_sync.sqlite3`, `claims\` |
| `shopify` | required | Store, API version, auth env names, token cache (as the poller) |
| `shopify_cutover_utc` | required | Registrations before it are skipped by the regular sync; customers made before it are linked once by the initial link (round 7c); creating historic members is phase 2 |
| `ac2_create_enabled` | `false` | Shopify -> AC2 SaveMember |
| `shopify_create_enabled` | `false` | AC2 -> Shopify `customerCreate`; Q3 gate above. The app also needs `write_customers` (the session check requires exactly `read_customers` while off, and `read_customers` + `write_customers` while on) |
| `max_writes_per_run` | `5` | 0-5, both directions together |
| `birthday_month_metafield` | `custom.birthday_month`, `number_integer` | Owner: placeholder until Shopify's developer confirms it |
| `ac2_member_no_metafield` | `custom.ac2_member_no`, `single_line_text_field` | Sent on every create from AC2 (Owner round 2, Q2) |
| `shopify_create_tags` | `["ac2-member"]` | Owner-confirmed; `member-mg` is refused (it would loop) |
| `ac2_credential_target` | `null` | Credential Manager target holding the AutoCount account the sync logs in with (the Owner's own account; example `XB-AC2-MemberSync`); `null` keeps the attended owner-console path |
| `changelog_xml_column` | `null` | Owner Q4: off. AC2 -> Shopify then reads no ChangeLog and makes no links. Set to `"Content"` (VM fact r5-1) at UAT, deliberately, after the name rule is confirmed; Shopify creates still need `shopify_create_enabled` |
| `changelog_batch` | `200` | 1-500 new rows per run |
| `changelog_overlap` | `50` | 0-200 (below `changelog_batch`); IDs re-read below the watermark; 0 = no overlap |
| `max_shopify_customers_per_run` | `20` | 1-100. Round 8: new sign-ups only; pending ones never use it |
| `max_pending_rechecks_per_run` | `100` | Round 8 (G2 M1): 1-500 pending sign-ups re-checked per run, oldest first, outside the budget above; more pending than this: alert `shopify_pending_backlog` (the rest wait for the next run) |
| `shopify_max_pages` | `10` | 1-40 |
| `http_timeout_seconds` | `30` | 5-60 |
| `deferred_create_local_time` | `"06:00"` | Round 6: `HH:MM` (00:00-23:59) in Asia/Singapore on the day after sign-up; before it an unmatched sign-up waits for staff |
| `ac2_create_member_type` | `"REGISTERED"` | Round 6: the AutoCount member type of a member the sync creates (1-20 of `A-Z a-z 0-9 _ -`, starting with a letter or digit). The test book type is `REGISTERED`; production uses other codes (a production change is E9) |
| `memberno_link_window_minutes` | `10` | Round 7: 1-60. The largest gap, either way, between AC2 `CreatedTime` and the sign-up time (round 7c: `registration_at`) for a MemberNo link |
| `initial_link_enabled` | `false` | Round 7c: the Owner's gate for `initial-link --apply`; a dry run needs no gate |

## What stays switched off

- Both create switches (`ac2_create_enabled`, `shopify_create_enabled`).
- ChangeLog reads (`changelog_xml_column` null), so no AC2 -> Shopify links.
- The Shopify app has no write scope; `customerCreate` is refused by the
  client unless the switch is on and the token carries `write_customers`.
- No scheduled task, no production book, no n8n.

## Write safety

- SQL login (fact, closes open item 2): every AutoCount API session runs as
  SQL login `sa` (sysadmin, `db_owner`). This is hard-coded in AutoCount.dll,
  whichever AutoCount user logs in. Every limit is therefore in this code: the
  book guard, the SQL allowlist below, the one-write guards and the write
  switches. `ac2_credential_sa_refused` refuses only an AutoCount **user ID**
  of `sa`; it does not change the SQL login.
- SQL allowlist (G2 H1): `Get-XbDlScalar` in
  `scripts/ac2_directlocal_book_guard.ps1` (not a pinned worker script, so
  hardened in place for every caller, main's included) sends only pinned
  texts. First an ASCII gate (G2 LOW-1): every pinned text and both
  templates are one line of printable ASCII (U+0020-U+007E; U+0020 is the
  only whitespace, no tab, CR or LF), so a text with any other character
  (for example U+00AD, NUL, U+200B, U+200D, U+FEFF, U+00A0 or a line break)
  is refused before any other check (`vendor_query_not_ascii`). Every exact
  comparison is ordinal (`[string]::Equals(..., Ordinal)`), because
  PowerShell's `-ccontains` and `-ceq` are culture-aware and ignore
  characters such as U+00AD. Then a deny check on every text, case-insensitive: `;`, `--`, `/*`,
  `*/`, `EXEC`, `EXECUTE`, `TRUNCATE`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`,
  `DROP`, `ALTER`, `CREATE`, `GRANT`, `REVOKE`, `DENY`, `INTO`, `DBCC`,
  `SHUTDOWN`, `KILL`, `BACKUP`, `RESTORE`, `BULK`, `OPENROWSET`,
  `OPENDATASOURCE`, `OPENQUERY`, `WAITFOR`, `DECLARE`, `SET`, `USE`, any
  `xp_`/`sp_` name, `AED_XBOUNDARIES` and any 3- or 4-part object name
  (`vendor_query_not_read_only`). Then the text must be one of the named
  texts in `$script:XbDlSqlText` (`db_name`, `machine_name`,
  `user_script_rows`, `mail_queue_rows`, `einvoice_queue_rows`,
  `pos_sync_registrations`, `server_now_local`, `member_change_counter`,
  `changelog_max_id`), the `sync-changelog` template (only `TOP (1-1000)` and
  the checked `[column]` vary) or the `sync-lookup` template (only the
  condition list varies: phone block, Singapore block, up to 20 MemberNo
  conditions, at most 25 parameters; round 7, Owner-approved: the template
  also reads `CONVERT(varchar(23), CreatedTime, 126) AS created_time`, the
  one change to the book guard, so the lookup without it is refused), with exactly one parameter per `?`
  (`vendor_query_not_allowlisted` otherwise). Python builds no AutoCount SQL
  (`SqlPlaceholderStaticTests`).
- AutoCount access rights (`GM_MEMBER_SHOW/OPEN/VIEW/NEW`) are checked only
  in the AutoCount screens; `SaveMember` and the member API check none (VM
  fact r5-6). Every write limit therefore comes from this code: the book
  guard, the create switches, `max_writes_per_run`, the write-once fence
  (one `SaveMember` per fence, exclusive claim, deadline watchdog) and, for
  `sync_p1`, a pre-claim refusal of a MemberNo that already exists
  (`member_no_taken`, proven not written).
- Each write is recorded as `INTENT` before it starts. For a Shopify -> AC2
  create, the intent row also stores a salted digest (the ledger's ref key)
  of the intended person's normalised name and phone; no name or phone is
  stored in plain text. The name is normalised by the name rule's helper
  (case, spacing, punctuation), keeping the words in order.
- Round 6: the sync never changes an existing AC2 member (no update
  operation exists); a member staff made is only linked. A sync create comes
  only after the staff wait and raises `created_after_wait`; a later staff
  member for the same person raises `possible_duplicate_member` and is never
  merged.
- One customer, one open write (G2 H2): a Shopify -> AC2 create is refused
  while a write for the same customer is `INTENT` or `UNCERTAIN` (alert and
  count `customer_held_by_open_write`; the pending row becomes `SKIPPED` and
  is never re-sent blindly). Crash recovery (a left `INTENT` becomes
  `UNCERTAIN`) holds that customer the same way, because the write-once child
  of a killed parent may still save (deadline 120 s): a re-run can no longer
  register the person again under the next generator number. The phone path
  keeps its `open_write_for_member` guard. A write that is `DONE`, proven
  `NOT_WRITTEN`, `REJECTED` or `RESOLVED` does not hold the customer.
- `NOT_WRITTEN` (proven) is retried on a later run. An uncertain outcome
  (timeout, crash, Shopify transport or 5xx error) is never re-sent. A later
  run only looks it up:
  - AC2 create: the member found under that MemberNo is linked only when
    the digest of its Name and MobilePhone, normalised the same way, equals
    the intent digest (G2 B2, exact; a digest stored before fix round 3 for
    a name with punctuation no longer matches, so that write stays
    `UNCERTAIN` and counted, which is safe). A different person holding the number
    (`uncertain_write_identity_mismatch`) or an older intent row without a
    digest (`uncertain_write_identity_unverifiable`) is never linked; the
    write stays `UNCERTAIN`.
  - Shopify create with a phone: looked up by phone (verified as in B1) and
    linked when the name rule matches.
  - Shopify create without a phone (Owner round 2, Q4): it cannot be found
    by phone, so it stays `uncertain_write_unresolved`, is never re-sent, and
    its pending row stays `SKIPPED`. Phase 2 adds a lookup by the
    `custom.ac2_member_no` metafield to resolve it.
- `customerCreate` is retried only after a GraphQL throttle answer (not
  executed).
- The run summary carries counts, alerts and keyed references (`c-`/`m-`)
  only.

## Member.CreatedTime fact (VM read-only check G0, 2026-10-22)

- `dbo.Member.CreatedTime` is `datetime`, always filled, with millisecond
  precision, stored in Singapore local time with no offset (the SQL server's
  clock is +08:00), on the test book.
- The bridge sends it as `CONVERT(varchar(23), CreatedTime, 126)`
  (`YYYY-MM-DDTHH:MM:SS.mmm`; style 126 drops a zero fraction). The sync
  accepts only that shape (`ac2_created_time`), reads it as Asia/Singapore
  and compares in UTC. Anything else is unreadable and never links.

## ChangeLog facts (VM read-only check r5, 2026-10-21)

- Columns: `ID` bigint IDENTITY(1,1) primary key, `TableName` nvarchar(50),
  `Guid`, `NewGuid`, `Content` nvarchar(max) (the XML), `ContentType`
  nchar(1), `ChangeType` nchar(1), `ChangeTime` datetime, `TransactionID`
  bigint. Member inserts: `TableName = 'Member'`, `ChangeType = 'I'`.
- Insert XML: one root `<Member .../>` with attributes only (`MemberNo`,
  `Name`, `MobilePhone`, `EmailAddress`, `DOB`); a NULL column is omitted
  (read as NULL); `DOB` as `YYYY-MM-DDTHH:MM:SS`; no DOCTYPE, declaration or
  namespace (a DOCTYPE is refused; the 65,536-character cap stays).
- The PR's ChangeLog query returned 3 rows on the test book and all 3
  parsed.
- `ExecuteScalar(String, Object[])` rewrites every `?` into a real `@pN`
  parameter, so the SQL text holds no other `?` (static test
  `SqlPlaceholderStaticTests`).
- RCSI (closed): both books have READ_COMMITTED_SNAPSHOT and snapshot
  isolation OFF; `ID` is IDENTITY(1,1); no out-of-order rows were found;
  nothing truncates or purges ChangeLog; 26 procedures delete their own rows
  by ID, which the gap handling covers. The overlap re-read above covers an
  insert that commits after a higher ID was read; no further trailing
  re-read is needed.

## Known limits

- L4: a MemberNo that merely contains the 8 Singapore digits counts as the
  same phone (the embedded-digits rule, Python `_phone_hit` and the bridge
  `CHARINDEX` condition). Rare, and safe only where that is intended.
- L5 (round 8, G2 M1): pending Shopify sign-ups (waiting for staff, or
  past the cutoff while `ac2_create_enabled` is off) are re-checked under
  their own bound, `max_pending_rechecks_per_run` (100), oldest first, and
  never use `max_shopify_customers_per_run`, so new sign-ups are always read.
  More pending than the bound raises `shopify_pending_backlog`; the rest are
  re-checked in later runs. Each pending sign-up costs one Shopify read and
  one AC2 lookup per run. A sign-up past the cutoff while creates are off
  stays pending for good and raises `sign_up_past_cutoff_create_disabled`
  on every run (with the MemberNo it would get, masked to the last 3
  characters). AC2 -> Shopify still re-reads the same first 20 pending
  members each run while its writes are off.
- Waiting customers (round 6) stay in the Shopify -> AC2 pending list until
  they are linked or created (about a day).
- Round 7c: the time link needs the MemberNo to be the phone (`65` + 8
  digits or the 8 digits). A member keyed with the phone only in MobilePhone
  under another MemberNo falls back to the name rule and, without a name
  match, to a second member with the shared-phone MemberNo after the cutoff.
- Round 7c: the initial link reads customers in Shopify `updatedAt` order
  from a cursor; each run reads at most `shopify_max_pages` x 50 customers.
  More customers sharing one `updatedAt` than one run reads stops the pass
  (`initial_link_cursor_stuck`); raise `shopify_max_pages` (max 40) and run
  again. Dry-run counts are per run, and the customer at the cursor is
  counted again in the next run.
- Round 7c: a customer made before the cutover with no phone, or whose
  phone is no AC2 MemberNo, is not linked by the initial link; if they sign
  up later, the regular rules apply to them.
- A sign-up without a usable phone can never be linked to a staff member
  (email never links), so it is created after the cutoff even if staff made
  one, and that duplicate cannot be detected. Staff should not make members
  for sign-ups without a phone, or the Owner accepts these duplicates.
- MemberNo as the phone (AC2 -> Shopify, only when MobilePhone gives none;
  Owner Q3):

  | MemberNo shape | Phone |
  |---|---|
  | `65` + 8 digits starting 3/6/8/9 | itself |
  | 8 digits starting 3/6/8/9 | `65` + the 8 digits |
  | A generator number (letters, or all digits such as 9 digits) | none |
  | A foreign number, with or without a suffix digit | none |
  | A Singapore phone + a suffix digit 2-9 | none |
  | `65` + 8 digits starting with another digit | none |
  | A leading `0`, or a phone + initials | none |

  A member with no phone is skipped and counted (`ac2_member_without_phone`).
- A number without `+` that is not a Singapore number is sent to Shopify as
  `+` and its digits, which Shopify reads as a country code. Owner Q2
  (2026-10-21): accepted, no change.

## Runbook: pre-flight, AutoCount login and credential (not yet done)

Each step is a later, Owner-approved VM step.

### Pre-flight before the first VM run

1. Ledger: confirm there is no old ledger, in particular none with an
   `ac2_changelog_id` watermark or with links made under the old first-name
   rule. Check (read-only; nothing is written; it prints nothing if the file
   is absent):

   ```powershell
   $ledger = "<state_root>\member_sync.sqlite3"
   if (Test-Path -LiteralPath $ledger) { python -c "import sqlite3, sys, pathlib; c = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True); print(c.execute('SELECT name, value FROM watermarks ORDER BY name').fetchall(), c.execute('SELECT COUNT(*) FROM links').fetchone()[0])" $ledger }
   ```

   If a ledger exists, delete it (and its `-wal`/`-shm` files) and run
   `init` for a new one. If any ledger made links under the old first-name
   rule, wipe it before relying on its counts. Nothing was written to
   AutoCount or Shopify by deleting it. (The `status` command is not used
   here: opening a v1 ledger migrates it.)
2. Keep `changelog_xml_column` null until the name rule is confirmed at UAT;
   then set it to `"Content"` deliberately.

### AutoCount login and credential

1. The sync logs in with the Owner's own AutoCount account (Owner,
   2026-10-21). Its user ID is not written in this repository. No separate
   AutoCount user is created. AutoCount rights do not limit the member API
   (VM fact r5-6) and the SQL login is always `sa` (see Write safety), so
   the limits are this source's guards.
2. Signed in as the Windows account that will run the sync (the same account
   that owns the state root), store the credential in Windows Credential
   Manager as a generic credential. The password lives only there, never in
   the repository or the config:

   ```bat
   cmdkey /generic:XB-AC2-MemberSync /user:<the Owner's AutoCount user ID> /pass
   ```

   `cmdkey` prompts for the password, so it is not in the command history.
3. Set `ac2_credential_target` to `XB-AC2-MemberSync` and check it:

   ```powershell
   python directlocal\xb_member_sync.py check-credential --config <config>
   ```

   The output shows only the last three characters of the user ID and
   `secret_present: true`.
4. When the Owner changes that AutoCount password, update the Credential
   Manager entry to match (step 2 again), or the sync fails to log in
   (nothing is written; the run is refused with a login code).

How it is read: the loader (`load_ac2_credential`) calls `CredReadW` in
memory, refuses an AutoCount user ID of `sa` (`ac2_credential_sa_refused`;
the SQL login is `sa` regardless), and passes the user ID and password only in the bridge
child's environment (`XB_AC2_USER_ID`, `XB_AC2_PASSWORD_ENV_VAR` ->
`XB_AC2_SYNC_PASSWORD`). This follows the EnergyGrid pattern (secret read in
memory, injected only into the child, never printed), but from Credential
Manager as the Owner decided; EnergyGrid itself uses a DPAPI CLIXML file.
The attended `ac2_directlocal_owner_console.ps1` path is unchanged.

### Waiting customers, deferred creates and possible duplicates (round 6)

- `waiting_for_staff` in a run's counts is normal: a sign-up not yet matched
  to a staff-made member. It is linked as soon as staff key the member with
  MemberNo `65` + 8 digits or the 8 digits (round 7c) within 10 minutes of
  the sign-up (round 7), or
  hold the phone otherwise with the same name (at least 2 matching words, no
  initials).
- `created_after_wait` (alert): the sync created a member after the cutoff.
  Tell staff not to make that customer by hand any more.
- `possible_duplicate_member` (alert): staff made a member after the sync
  created one for the same person. Nothing was merged. The Owner decides in
  the AutoCount screens which record to keep; the sync never deletes or
  merges. The Shopify -> AC2 alert repeats on every run for up to 7 days
  after the sync's create while both records exist.
- `memberno_exists_outside_window` (alert, round 7): AC2 already has a
  member with this sign-up's phone as its MemberNo, created more than
  `memberno_link_window_minutes` before or after the sign-up (and the
  initial link did not link it: the customer was made after the cutover).
  `memberno_conflict` (round 7c): both `65` + 8 digits and the 8 digits are
  MemberNos in AC2. The
  summary's `refs` show the customer ref and `member_no_masked` (last 3
  characters). Nothing was linked or created, and the customer is not read
  again. Check in AutoCount whether that member is the same person (for
  example a member keyed long after the sign-up); linking by hand is a later, separately approved
  step. `created_time_missing` is handled the same way.
- Round 8: `ac2_member_held_for_sign_up` (alert): staff keyed a member whose
  MemberNo is a sign-up's phone, but the sign-up time is not readable yet;
  usually the next run links it. `shopify_customer_linked_to_other_member`
  and `member_linked_to_other_customer` (alerts): the customer or the
  member is already linked to someone else; nothing was merged.
  `sign_up_past_cutoff_create_disabled` (alert): a sign-up is past the
  cutoff while AC2 creates are off; it stays pending.
  `shopify_pending_backlog` (alert): more sign-ups are pending than one run
  re-checks; raise `max_pending_rechecks_per_run` or let later runs catch up.
- Round 9: `memberno_conflict` can also come from AC2 -> Shopify (the
  ChangeLog member's ref): both forms of one phone are MemberNos; nothing
  was linked or created on either side. `ac2_member_hold_stuck` (alert):
  the member above was held 4 runs because more sign-ups were pending than
  one run reads, and is now left to an operator (never created). Check
  whether a pending sign-up has that phone; Shopify -> AC2 links it by time
  when it reads it.
- The pending list is private ledger data; read it only on the VM (the
  read-only query style of the pre-flight step).

### Initial link: existing customers first (round 7c, one-off)

Run on the VM before the first Shopify -> AC2 run (that direction alerts
`initial_link_not_complete` and does nothing until it is done):

1. Dry run (the default; writes no link, only its own progress mark):
   `python directlocal\xb_member_sync.py initial-link --config <config>`.
   It reads the Shopify customers made before `shopify_cutover_utc` (any
   tag; Customer.createdAt), looks their phone up in AC2 by MemberNo only
   (`65` + 8 digits, or the 8 digits), and reports `initial_link_would_link`,
   `initial_link_no_phone`, `initial_link_no_member`,
   `initial_link_already_linked`, `initial_link_held_by_open_write` and the
   alert `initial_link_member_no_conflict`, with `refs` holding each
   customer ref and the MemberNo masked to its last 3 characters. `complete`
   says whether the pass reached the end; run again until it does.
2. The Owner reviews the counts and approves the apply step, then sets
   `initial_link_enabled: true`.
3. Apply: the same command with `--apply` (refused with
   `initial_link_disabled` while the setting is off, before any login or
   read). It writes only ledger links (origin `matched`, code
   `initial_link_member_no`): no AC2 or Shopify write, no field change, no
   time or name rule. A conflict (both forms of the number in AC2, or the
   member already linked to another customer) alerts and is never merged.
   Run until `complete` is true; that pass opens Shopify -> AC2.
4. Set `initial_link_enabled` back to `false`.

### Releasing a customer held by an open write

There is no release command (checked at `017ec98`): the CLI has only
`check-config`, `init`, `run`, `status` and `check-credential`; the ledger
triggers forbid deleting a `writes` row and allow `UNCERTAIN` to become only
`RESOLVED`, which only a run sets (`_resolve_uncertain`) when the member
found under that MemberNo has the intended identity (G2 B2). Crash recovery
(`_recover_crashed_intents`) and `_create_in_ac2` hold the customer while
the write is `INTENT` or `UNCERTAIN`. A held customer is released only by
that lookup succeeding:

1. Signs: the alert `customer_held_by_open_write` (or the count
   `ac2_create_uncertain`); later runs count `uncertain_write_unresolved`;
   `status` shows `uncertain_writes` above 0.
2. Wait past the 120-second write-once deadline, with no sync run and no
   bridge child (`powershell.exe` running the bridge) alive, then run one
   normal `run --config <config> --direction shopify_to_ac2`. If the save
   landed, B2 links it (`uncertain_write_resolved_linked`): done.
3. Still unresolved: list the open writes read-only, in the pre-flight
   style. The output is private (MemberNos and customer GIDs): keep it on
   the VM, never paste it into a ticket, PR or chat.

   ```powershell
   $ledger = "<state_root>\member_sync.sqlite3"
   python -c "import sqlite3, sys, pathlib; c = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True); print(c.execute('SELECT write_id, member_no, customer_gid, created_at, code FROM writes WHERE state = ? AND direction = ?', ('UNCERTAIN', 'shopify_to_ac2')).fetchall())" $ledger
   ```

4. Check that MemberNo in the AutoCount screens.
   - Absent: the Owner may (an approved live step) create it by hand under
     exactly that MemberNo, with the customer's first + last name (the same
     words in the same order) and phone (none when the customer had no valid
     phone). The next run links it through B2.
   - `uncertain_write_identity_mismatch` on the next run means the typed
     name or phone differs (the Owner corrects it and runs again) or another
     person holds the number: then never link by hand.
5. Never edit ledger rows. The pending row stays `SKIPPED`; the link is the
   record. A customer that stays held is safe: the sync fails closed and
   never creates a second member.

### UAT checklist

Per UAT run, before any create switch goes on for good:

1. Record `shared_phone_name_fallback`, `shared_phone_digit_fallback`,
   `shopify_phone_owned_by_other_name`, `name_fallback_one_word` and
   `name_fallback_initial` from the run summary's `counts`. All of them are
   counted in dry runs too (round 5): while `ac2_create_enabled` is off, a
   Shopify -> AC2 create refused only by that switch still counts the
   MemberNo rule it **would** use (`new_member_phone`,
   `shared_phone_name_fallback`, `shared_phone_digit_fallback`,
   `no_phone_generator_member_no`) next to `ac2_create_disabled`, plus the
   name reason; a create refused for another reason (for example
   `max_writes_per_run_reached` with the switch on) counts no decision,
   as before. `shopify_phone_owned_by_other_name` is counted whether or not
   `shopify_create_enabled` is on. With the switches on, the counts are those
   of the creates actually made. `name_fallback_one_word` counts a same-phone
   pair that did not link where either name has fewer than 2 words (it can
   never link); `name_fallback_initial` one where either name has a
   one-character word (an initial, or a one-character non-Latin word).
   Pending customers are re-read on every dry run, so the same customer is
   counted again in each run (L5).
2. Check in AutoCount which of those fallbacks came from a one-word name or
   initials on either side (for example a Shopify customer with only "Lee",
   or an AC2 `Name` with an initial), since those never link under the name
   rule.
3. Report to the Owner how many second AC2 member records a one-word name
   created (or, for AC2 -> Shopify, how many separate Shopify customers),
   before any create switch goes on.
4. Round 6: record `waiting_for_staff`, `phone_and_name_match`,
   `created_after_wait` and `possible_duplicate_member`. Check at least one
   sign-up that staff made by hand before the cutoff (it must be linked, with
   no AC2 write and its MemberType unchanged) and one left unmatched (it must
   wait until the cutoff, then be created with `ac2_create_member_type`).
   Report how many sign-ups staff did not make by the cutoff.
5. Round 7: record `memberno_and_created_time_match`,
   `memberno_exists_outside_window` and `created_time_missing`. Key at least
   one member on the spot with MemberNo `65` + 8 digits (it must link, with
   no AC2 write, whatever name staff typed) and check one where the times
   are more than 10 minutes apart (it must alert, never create). Compare one
   member's `CreatedTime` with that customer's sign-up time
   (`registration_at`: the `createdAt` of the `membership.start_date`
   metafield, UTC; round 7c, not `Customer.createdAt`) to confirm the
   8-hour conversion.
6. Round 7c: run the initial link dry run and record its counts and
   conflicts; after the Owner's approval, apply it and check one linked
   customer (link only, nothing changed in AC2 or Shopify). Then check a
   sign-up whose staff member was keyed with the 8 digits only (it must
   link by time) and one sign-up from an older Shopify account (it must
   link by its sign-up time).

## Commands

```powershell
python directlocal\xb_member_sync.py check-config --config <config>
python directlocal\xb_member_sync.py init --config <config>
python directlocal\xb_member_sync.py run --config <config> [--direction shopify_to_ac2|ac2_to_shopify]
python directlocal\xb_member_sync.py status --config <config>
python directlocal\xb_member_sync.py initial-link --config <config> [--apply]
```

`run` exits 0 when completed, 3 when completed with alerts (see the
summary's `alerts`), 2 when refused.

## Ledger

Schema v2 (2026-10-22): `writes.intent_digest` and the `changelog_seen`
table were added. A v1 ledger is migrated on open in one transaction
(additive only; existing rows keep a NULL digest and are never linked on
resolution). Fix round 3 adds no schema change: the overlap of the last run
is a `watermarks` row (`ac2_changelog_overlap`), and a ledger without it is
baselined once (G2 H3 above).

## Relation to the DirectLocal Core

The Core's attended path (`xb_directlocal.py`, owner console, fences,
`GetNextMemberNo` allocator) is unchanged and still works. Phase 1 supersedes
its create path for this sync; do not run both against the same book with
writes enabled. The sync ledger is separate from the Core ledger.

## Phase 2 notes (deferred, not built)

- $300 spend detector and the REGISTERED -> QUALIFIED upgrade.
- Expiry downgrade.
- Refund exceptions.
- The fix list for existing data (including the 15 shared-phone pairs).
- The 2,744-member rehearsal / historic backfill (creating members; the
  round 7c initial link only links existing pairs).
- The online-order path.
- A lookup by the `custom.ac2_member_no` metafield for unresolved phoneless
  Shopify creates (Q4).
- Production (`AED_XBOUNDARIES`, member type `Default` only today) and its
  POS behaviour; the test book has no POS sync, so till behaviour is never in
  UAT.

## Owner decisions

21 October 2026:

1. No phone or an invalid phone: AutoCount's `GetNextMemberNo` number,
   counted. Same phone, different name: `<phone digits><FIRST+LAST letters>`
   cut to 20; when taken by a member not linked to this customer, a digit
   2-9 (letters trimmed); skip and count only when 2-9 are all taken.
2. Birthday month: placeholder `custom.birthday_month` (`number_integer`)
   until Shopify's developer confirms it.
3. Tag on customers created from AC2: `ac2-member` (`member-mg` refused).
4. The AC2 `Name` goes whole into Shopify `firstName`.
5. One Shopify customer with the same phone but another name is a separate
   person: a separate customer without the phone, with email if any, name,
   tags and the AC2 MemberNo metafield; behind `shopify_create_enabled`.

Round 2 (21-22 October 2026): Q1 non-Latin shared-phone names use phone +
2-9; Q2 keep `custom.ac2_member_no` on every create; Q3 the go-live gate
above; Q4 phoneless uncertain creates stay unresolved until phase 2; the
name rule examples above; L1 option B (digits as typed); VM facts r5 applied.

Round 3 (21 October 2026, fix round 3):

- Q1: the full-name word rule above replaces the first-name rule; r3a: the
  shorter name needs at least 2 words (a name of 0 or 1 words never links,
  even identical).
- Q2: a no-`+` non-Singapore number keeps going to Shopify as `+` + digits.
- Q3: the MemberNo gives a phone only when it is a Singapore number.
- Q4: `changelog_xml_column` defaults to null; set at UAT.
- Shopify gate: one fake customer on the live store at UAT, with the Owner's
  OK at that moment, deleted straight after.
- AutoCount login: the Owner's own account, password in Credential Manager.

Round 4 (G2 delta review of `017ec98`), decisions accepted by the Owner:

- (a) The attended E7 test-book script keeps its own SELECT check
  (`Get-E7Scalar`) for now. A follow-up is **required** before it runs
  again: the same exact-text set and ASCII gate as `Get-XbDlScalar`.
- (b) Initials never count toward a name match (exact words only).
- (c) The H3 trigger "no overlap stored" is kept (not "`changelog_seen`
  empty", which could treat a real late row as history on a quiet
  ChangeLog).

Round 6 (22 October 2026): shop staff keep creating members by hand in AC2
from the Google Sheet the sign-up form fills, choosing Member or Guest; the
sync links first, waits until the next morning (06:00 Asia/Singapore,
configurable) and only then creates an unmatched sign-up with a configurable
member type and the alert `created_after_wait`; it never changes a member
staff made, never links on email, and alerts `possible_duplicate_member`
when staff make a member after the sync created one.

Round 7 (22 October 2026, 10:49Z and 11:20Z): staff key members in AC2 on
the spot and their name typing is unreliable, so Shopify -> AC2 links on the
exact phone MemberNo plus `CreatedTime` within 10 minutes (configurable) of
Shopify `createdAt`, with no name; the same MemberNo outside the window
alerts and is never created; no MemberNo match keeps the round 6 wait and
create. The Owner approved exactly one book-guard change: `CreatedTime` in
the member lookup. The ChangeLog query is unchanged and ChangeLog reads stay
off by default.

Round 7c (22 October 2026, 12:41Z): the window compares `CreatedTime` with
the sign-up time the sync already reads (`registration_at`), not
Customer.createdAt; existing (pre-cutover) Shopify customers are linked
first by exact MemberNo (including the 8-digit form) with no time rule, in a
one-off initial link (dry run by default, link-only, its own gate
`initial_link_enabled`; conflicts alert, never merge); the time link also
accepts the 8 digits without `65`; foreign `+` numbers get the same
10-minute rule.

Round 8 (22 October 2026, 13:52Z, G2 review of `b91d2b2`): fix H1 (AC2 ->
Shopify never creates a Shopify customer for a staff member whose MemberNo
is a sign-up's phone; the Shopify -> AC2 time rule decides, otherwise hold
and alert; linked-elsewhere cases alert), M1 (pending sign-ups never block
new ones; alert past the cutoff while creates are off), L4 and L5 (wording);
L1-L3 recorded as open items.

Round 9 (22 October 2026, 16:37Z, G2 review of `938f7cf`): fix M2 (AC2 ->
Shopify applies the both-forms conflict rule: never link, alert
`memberno_conflict` with the masked MemberNo, both directions agree), L6
(the "more pending than read" hold gets a test and the same 5-run bound,
then `ac2_member_hold_stuck`) and L7 (the AC2-side 10-minute boundary is
pinned by tests). Recorded with no code change:
- L1: keep the 2-word name rule for legacy records that hold the phone only
  in MobilePhone (or an embedded 8-digit MemberNo) in phase 1.
- The hold alert (`ac2_member_held_for_sign_up`, including the few-second
  `start_date` race) stays an alert until UAT shows how often it fires.

## Open items

Closed in fix round 3: the name rule (Q1, r3a) and the SQL login (always
`sa`, see Write safety).

1. Required follow-up before the attended E7 test-book script
   (`scripts/ac2_directlocal_e7_testbook_create.ps1`, unchanged) runs again:
   give `Get-E7Scalar` the exact-text set and ASCII gate (round 4 decision
   (a) above).
2. The `custom.ac2_member_no` metafield as a customer metafield definition in
   Shopify admin, so staff can see it.
3. Shopify's customer search syntax for a phone (`phone:+<digits>`), checked
   at UAT (results are verified by B1 either way).
4. Ordinal book-name checks, deferred to a later separate change (G2 round 5
   NOTE-2). The book checks in `scripts/ac2_directlocal_book_guard.ps1`
   (`Assert-XbDlAuthorisedBook`, `Assert-XbDlBookIdentity`,
   `Get-XbDlSessionFailureStage`) still compare with the culture-aware
   `-ccontains` / `-cnotcontains` / `-cne`. On Windows PowerShell 5.1 a
   lookalike of the test-book name (for example `AED_TESTINGAPI` with a U+00AD
   soft hyphen inside) is therefore **accepted** by the authorised-book check
   and by the `DB_NAME()` identity check (checked offline, 2026-10-22). It
   cannot reach production: Python's `check_book` compares the configured
   book name exactly (`AUTHORISED_BOOKS`, ordinal string equality) before any
   bridge child starts and passes exactly that name as `-TargetBook`; any
   other name, the production name included, is refused there first; and the
   SQL texts the guard sends are ASCII-gated and ordinal
   (round 4). The fix (ordinal compares) stays a later, separately approved
   change.
5. Staff sign-up Sheet (round 6, from issue #225; read-only, no live check):
   sign-ups reach the staff Google Sheet through Shopify Flow (Shopify
   member form -> Shopify Flow -> tab `Members (from Shopify)`), with the
   headers `Phone`, `First name`, `Email`, `Start date`, `Expiry date`,
   `Birthday month`, `Email consent`. The Phone column's format (for example
   whether it shows `65` + 8 digits like the AC2 MemberNo) is not recorded
   anywhere in the repository or the issue: check the Flow's Sheets action
   and one row on the VM. The Sheet shows only `First name`: if staff type
   only that one word as the AC2 `Name`, the name rule (at least 2 words)
   can never link it and the sync creates a second member at the cutoff
   (`possible_duplicate_member` then needs both names to match, so it may not
   fire). Owner to confirm what staff type as `Name` before UAT. Round 7:
   no longer needed for a member keyed with MemberNo `65` + 8 digits (it
   links by time, without the name); still needed for records holding the
   phone in another way.
6. Closed in round 7c: the window uses the sign-up time (`registration_at`).
7. Closed in round 9 (Owner: keep for phase 1): G2 L1, when no MemberNo is
   exactly the phone but MobilePhone or an embedded 8-digit MemberNo holds
   it, the 2-word name rule still **links** without the time rule
   (`phone_and_name_match`) and still drives the shared-phone fallback
   creates. This affects legacy records only.
8. G2 L2 (round 8, no change): the `possible_duplicate_member` watch still
   needs a name match, so a later staff member with a mistyped name is not
   flagged. Not silent: every sync create already alerts
   `created_after_wait`.
9. G2 L3 (round 8, no change): the initial-link hold ends only when an
   applied pass completes. `initial_link_cursor_stuck` (more than
   `shopify_max_pages` x 50 customers sharing one `updatedAt`) is remedied
   only by raising `shopify_max_pages` (max 40). The UAT dry run shows it.
