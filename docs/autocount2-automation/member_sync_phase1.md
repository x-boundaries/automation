# Member Sync Phase 1: Two-Way Create (#155)

Status (2026-10-27): source and offline tests, draft PR, round 10 after
Owner UAT r4 on the test book (23-24 October 2026, PASS). Nothing here has
run against production AutoCount or Shopify. Every write
switch is off by default, and so are ChangeLog reads (`changelog_xml_column`
null); turning one on, any AutoCount login and any scheduling each need their
own Owner approval. Go-live enablement (29 October 2026, Owner
SCOPE-CONFIRMED on #155): the production book `AED_XBOUNDARIES` is admitted
for this sync only (see "Go-live (production)" below); nothing in this
repository turns it on. Every live step needs its own Owner OK. (#268 adds
one more admission in the same book guard: the one-off member type move's own
`type-move-*` operations with profile `type_move_p1`, through its own entry
point; this sync's operations and profile are unchanged. See
`member_type_move_runbook.md`.)

Code: `directlocal/xb_member_sync.py` (engine, ledger, Shopify client,
credential loader, CLI), the `sync-lookup` / `sync-changelog` operations and
the `sync_p1` write-once profile in `scripts/ac2_directlocal_member_bridge.ps1`,
and the `sync_p1` member create in `scripts/ac2_member_sync_member.ps1` (the
pinned gateway adapter `scripts/ac2_member_gateway_autocount_adapter.ps1` is
unchanged; the new script reuses its helpers). #155 P2: the sign-up probe
`directlocal/xb_member_sync_probe.py` (CLI `probe`), see "Immediate create
(after S5)".
Tests: `tests/test_member_sync.py`, `tests/test_member_sync_bridge_ps.py`.

## What phase 1 does

| Direction | Trigger | Result |
|---|---|---|
| Shopify -> AC2 | A customer tagged `member-mg` whose registration instant (`membership.start_date` metafield `createdAt`) is at or after `shopify_cutover_utc` | A link to the member staff create by hand; only if none matches by the next-morning cutoff, an AC2 member created by the sync (with an alert) |
| AC2 -> Shopify | A `dbo.ChangeLog` insert (`TableName = 'Member'`, `ChangeType = 'I'`) after the stored `ID` watermark, for example a member created at the till | A Shopify customer, or a link to an existing one |

Both directions write the mapping (Shopify customer GID <-> MemberNo, each
unique) to the sync ledger. That mapping is the loop prevention: an AC2
insert for a mapped MemberNo, or for a MemberNo with an open write, is never
sent to Shopify (run count `ac2_insert_linked_or_own_write`; round 10
rename of `ac2_insert_own_or_linked`, same behaviour); a mapped customer is
never sent to AC2.

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
     `memberno_link_window_minutes` (default 60; 10 until round 10) apart,
     either way, the boundary included (`memberno_and_created_time_match`).
     `CreatedTime` is when staff open the New Member form, not when they
     save it (Owner UAT r4, below), so the window runs from form open. The sign-up time
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
     (masked MemberNo) and is never created while stuck. Shopify -> AC2
     still links the sign-up by time on its own when it reads it. Both holds
     share the member's one `checks` counter, so a member is held at most 4
     runs in all. Round 10 (G2 L8): stuck only means the pending sign-ups
     could not all be read. At the start of every AC2 -> Shopify pending
     step in which no more sign-ups are pending than
     `max_pending_rechecks_per_run` (the backlog has cleared), every stuck
     member is re-queued (`PENDING`, code `hold_requeued`, `checks` 0, count
     `ac2_member_hold_requeued` with its `m-` ref) and decided like any
     other: already linked, linked by time, or, for a genuine walk-in with
     no sign-up, created in Shopify (when `shopify_create_enabled`). A new
     backlog can hold it again for the same bounded wait, with one more
     stuck alert;
   - already linked to another member: alert
     `shopify_customer_linked_to_other_member` (or `possible_duplicate_member`
     when the sync created that member), never merged;
   - otherwise the Shopify -> AC2 rule decides: `CreatedTime` within
     `memberno_link_window_minutes` of the customer's sign-up time
     (`registration_at`) links (`memberno_and_created_time_match`; inclusive:
     at the default exactly 60:00 apart links and 60:00.001 does not, pinned
     by tests on both sides, round 9 G2 L7, round 10 default); farther
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

Go-live (Owner Q9, 29 October 2026): the proof moves to step S6 and runs
through the TEST book with the real `customerCreate` path (one fake member,
deleted straight after), not as a hand-made customer; see "Go-live
(production)".

## Settings (`config/member_sync.example.json`)

Unknown keys, at the top level or nested, are refused.

| Key | Default | Notes |
|---|---|---|
| `autocount_book` | required | `AED_TESTINGAPI` or `AED_XBOUNDARIES` (go-live), compared exactly; anything else is refused. The live book needs `ac2_credential_target` `XB-AC2-MemberSync` and `ac2_login_mode` (in `dedicated` mode also `ac2_refused_user_ids`), and refuses `ac2_create_without_phone` `true` |
| `state_root` | required | Private root (same custody check as the Core); ledger `member_sync.sqlite3`, `claims\` |
| `shopify` | required | Store, API version, auth env names, token cache (as the poller) |
| `shopify_cutover_utc` | required | Registrations before it are skipped by the regular sync; customers made before it are linked once by the initial link (round 7c); older `member-mg` sign-ups with no member are brought in by the one-off backfill (slice BF1, below) |
| `ac2_create_enabled` | `false` | Shopify -> AC2 SaveMember |
| `shopify_create_enabled` | `false` | AC2 -> Shopify `customerCreate`; Q3 gate above. The app also needs `write_customers` (the session check requires exactly `read_customers` while off, and `read_customers` + `write_customers` while on) |
| `max_writes_per_run` | `5` | 0-5, both directions together |
| `birthday_month_metafield` | `custom.birthday_month`, `number_integer` | Owner: placeholder until Shopify's developer confirms it |
| `ac2_member_no_metafield` | `custom.ac2_member_no`, `single_line_text_field` | Sent on every create from AC2 (Owner round 2, Q2) |
| `shopify_create_tags` | `["ac2-member"]` | Owner-confirmed; `member-mg` is refused (it would loop) |
| `ac2_credential_target` | `null` | Credential Manager target holding the AutoCount account the sync logs in with; `null` keeps the attended owner-console path (test book only). Go-live V9: on the live book it must be `XB-AC2-MemberSync`, holding the AutoCount user that `ac2_login_mode` names. G2 #281 LATER-3: the test book uses its own target (example `XB-AC2-MemberSync-Test`) and refuses `XB-AC2-MemberSync` in any case (`sync_config_ac2_credential_target_live_only`), so the live sync user is never used on the test book |
| `ac2_login_mode` | `null` | Owner, 30 Oct 2026 ("Use my login"): which AutoCount user the credential holds. `"owner_shared"`: the Owner's own user, shared with the Owner's all-day session on the VM; `ac2_refused_user_ids` must then be empty (`sync_config_ac2_refused_user_ids_owner_shared_refused`). `"dedicated"`: a separate sync user, with the Owner's ID in `ac2_refused_user_ids`. Compared exactly; anything else is `sync_config_ac2_login_mode_invalid`. Required on the live book, with no default (`sync_config_ac2_login_mode_live_required`); optional on the test book. `check-config` prints it |
| `ac2_refused_user_ids` | `[]` | Go-live V9: AutoCount user IDs the sync must never log in as, up to 10, compared case-insensitively before any login (`ac2_credential_user_refused`). `dedicated` mode: at least the Owner's own account, required on the live book. `owner_shared` mode: must be empty. Kept only in the private config on the VM |
| `ac2_create_without_phone` | `null` | Go-live R8 (Owner Q2 = A): `null` means the book's default: `true` on the test book (phase 1: a no-phone sign-up gets `GetNextMemberNo`), `false` on the live book (skipped with the alert `no_phone_sign_up_needs_staff`). `true` with the live book is refused |
| `changelog_xml_column` | `null` | Owner Q4: off. AC2 -> Shopify then reads no ChangeLog and makes no links. Set to `"Content"` (VM fact r5-1) at UAT, deliberately, after the name rule is confirmed; Shopify creates still need `shopify_create_enabled` |
| `changelog_batch` | `200` | 1-500 new rows per run |
| `changelog_overlap` | `50` | 0-200 (below `changelog_batch`); IDs re-read below the watermark; 0 = no overlap |
| `max_shopify_customers_per_run` | `20` | 1-100. Round 8: new sign-ups only; pending ones never use it |
| `max_pending_rechecks_per_run` | `100` | Round 8 (G2 M1): 1-500 pending sign-ups re-checked per run, oldest first, outside the budget above; more pending than this: alert `shopify_pending_backlog` (the rest wait for the next run) |
| `shopify_max_pages` | `10` | 1-40 |
| `http_timeout_seconds` | `30` | 5-60 |
| `deferred_create_local_time` | `"06:00"` | Round 6: `HH:MM` (00:00-23:59) in Asia/Singapore on the day after sign-up; before it an unmatched sign-up waits for staff |
| `ac2_create_member_type` | `"REGISTERED"` | Round 6: the AutoCount member type of a member the sync creates (1-20 of `A-Z a-z 0-9 _ -`, starting with a letter or digit). The test book type is `REGISTERED`. Production: `GUEST` since S5 went live (5 November 2026). The live book has three member types, `Default`, `GUEST` and `MEMBER`, all Level 1: website sign-ups the sync creates get `GUEST`; staff-keyed counter members are `MEMBER` (existing members stay on `Default` until #268). Owner Q1 and VM fact V2 first named `Default`, then the only type there. Go-live R7: the bridge checks the type exists in the book before every create (`member_type_unknown`, not written) |
| `memberno_link_window_minutes` | `60` | Round 7: 1-60. The largest gap, either way, between AC2 `CreatedTime` (when the New Member form was opened) and the sign-up time (round 7c: `registration_at`) for a MemberNo link. Round 10 (Owner, after UAT r4): default 60, was 10 |
| `initial_link_enabled` | `false` | Round 7c: the Owner's gate for `initial-link --apply`; a dry run needs no gate |
| `immediate_create_enabled` | `false` | #155 P1, "Immediate create (after S5)": an unmatched sign-up is created before the 06:00 cutoff, inside the window and under the caps. Needs `ac2_create_enabled` too. Off: today's rule, unchanged |
| `immediate_create_window_local` | `"all_day"` | `"all_day"` (owner D1, also in the live example) or `"HH:MM-HH:MM"` in Asia/Singapore: start included, end excluded, a start after the end runs over midnight; start = end refused. Anything else, `All_Day` or `00:00-24:00` included, is `sync_config_immediate_create_window_local_invalid` |
| `max_creates_per_hour` | `6` | Owner D5: 1-30. Writes in both directions together in the last 60 minutes, from the ledger's `writes` rows. Gates only the immediate path |
| `max_creates_per_day` | `144` | Owner D5: 1-720, at least `max_creates_per_hour`. The same count over the last 24 hours |
| `probe_wake_enabled` | `false` | #155 P2: the probe starts `\XB\XB-MemberSync` only when true (and the immediate path is on); otherwise it records "would wake" (P3 observe) |
| `probe_max_wakes_per_day` | `60` | 1-240 task starts per Singapore day by the probe |
| `backfill_registrations_enabled` | `false` | #155 slice BF1: the gate for `backfill-registrations --apply` (refused with `backfill_registrations_disabled` before any login, read or lock while off); a dry run needs no gate. Gates nothing else: `run`, `initial-link`, `status`, `digest`, `probe` and `check-config` behave exactly as before. Only in the backfill's own config copy, never in the 15-minute task's live config (see the backfill section) |
| `member_core_state_root` | `null` | #155 slice BF1: the member core's (#268) state root. When set, the backfill and `coverage` read its active claim links read-only (its ledger version must be 2, the one this module knows, else `member_core_ledger_version_unknown` before any decision; its meta must be the member core's, bound to the same book and store); a member is then linked if either ledger says so. A pair staff unlinked there is never linked by the backfill. `null`: not read, and the output says `"member_core_ledger": "not_configured"`. Must not be this sync's own `state_root` |

Neither BF1 key is printed by `check-config` while it is absent or off, so
its output is unchanged; when set it adds `backfill_registrations_enabled:
true` or `member_core_state_root_set: true` (never the path).

## What stays switched off

- Both create switches (`ac2_create_enabled`, `shopify_create_enabled`).
- ChangeLog reads (`changelog_xml_column` null), so no AC2 -> Shopify links.
- The Shopify app has no write scope; `customerCreate` is refused by the
  client unless the switch is on and the token carries `write_customers`.
- No scheduled task until go-live step S4 (`scripts/install_member_sync_task.ps1`,
  a dry run unless `-Register`), and no production run until step S1; no n8n.
- The immediate path (`immediate_create_enabled`), probe wakes
  (`probe_wake_enabled`), and the probe task until step P3.

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
- Owner UAT r4 (test book, 23 October 2026): `CreatedTime` is when the New
  Member form is **opened**, not when it is saved (one member: CreatedTime
  1 min 15 s before its ChangeLog insert and `LastModified`; another: about
  14 minutes before). The link window therefore runs from form open, which
  is why the default became 60 minutes in round 10.

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

1. Test book (UAT): the sync logged in with the Owner's own AutoCount
   account (Owner, 2026-10-21). Live book (Owner, 30 Oct 2026, "Use my
   login", replacing the separate sync user of 29 Oct): the sync logs in
   with the Owner's **own** AutoCount user too, and the live config says so
   with `"ac2_login_mode": "owner_shared"` and `"ac2_refused_user_ids": []`.
   The other mode, `"dedicated"`, is kept for a switch-back (a separate
   sync user, with the Owner's user ID in `ac2_refused_user_ids` so a
   credential holding it is refused before any login); see "AutoCount
   sign-in and licence" for when to switch. No user ID is written in this
   repository. AutoCount rights do not limit the member API (VM fact r5-6)
   and the SQL login is always `sa` (see Write safety), so the limits are
   this source's guards.
2. Signed in as the Windows account that will run the sync (the same account
   that owns the state root), store the credential in Windows Credential
   Manager as a generic credential. The password lives only there, never in
   the repository or the config:

   ```bat
   rem Test book (UAT) config:
   cmdkey /generic:XB-AC2-MemberSync-Test /user:<the test-book AutoCount user ID> /pass
   rem Live config only (go-live S1), owner_shared mode:
   cmdkey /generic:XB-AC2-MemberSync /user:<the Owner's own AutoCount user ID> /pass
   rem (dedicated mode instead: /user:<the separate sync user ID>)
   ```

   `cmdkey` prompts for the password, so it is not in the command history.
   The two targets are separate (G2 #281 LATER-3): a test-book config that
   names `XB-AC2-MemberSync` (any case) is refused with
   `sync_config_ac2_credential_target_live_only`, so the live sync user is
   never used on the test book.

   Migration for an existing UAT config (Owner, once, before S0b): the UAT
   config still names `XB-AC2-MemberSync`. Store the same test-book login
   again under `XB-AC2-MemberSync-Test` (first `cmdkey` line above), set the
   UAT config's `ac2_credential_target` to `XB-AC2-MemberSync-Test`, and run
   `check-credential` on it (step 3). The old entry is then read by nothing
   until S1, whose `cmdkey /generic:XB-AC2-MemberSync` replaces it with the
   live login (in `owner_shared` mode the same Owner user).
3. Set `ac2_credential_target` (test book `XB-AC2-MemberSync-Test`, live
   book `XB-AC2-MemberSync`) and check it:

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
  MemberNo `65` + 8 digits or the 8 digits (round 7c) with the New Member
  form opened within `memberno_link_window_minutes` (default 60, round 10)
  of the sign-up (round 7), or
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
  one run reads (`shopify_pending_backlog` alerts too); nothing was created.
  Round 10 (G2 L8): no manual re-queue is needed. The member is re-queued by
  itself (count `ac2_member_hold_requeued`) on the first AC2 -> Shopify run
  with no more sign-ups pending than `max_pending_rechecks_per_run`, then
  linked (a sign-up with that phone) or, for a walk-in, created in Shopify.
  What to do: clear the backlog (staff key the waiting members, or the
  approved AC2 creates run) or raise `max_pending_rechecks_per_run` (up to
  500). If the same masked MemberNo alerts stuck again, the backlog came
  back before it cleared: check `shopify_pending_backlog` first.
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

### Backfill of older sign-ups (#155 slice BF1, one-off)

The live sync creates an AutoCount member only for a sign-up made on or
after `shopify_cutover_utc`; older sign-ups were only linked where a member
with the same phone already existed (the initial link). The owner decided
on 11 and 12 November 2026 that both member lists are synced up completely.
`backfill-registrations` brings in the older `member-mg` sign-ups; the
read-only `coverage` command proves the result. External effect of an
applied batch: new `GUEST` members in AutoCount, each visible at the tills.
Nothing is sent to Shopify and nothing is sent to any customer.

**The rules** (G0 two-way backfill plan, sections 2.1 to 2.4, and the
Controller's decisions):

| Customer (tagged `member-mg`, signed up before the cutover, not linked) | Result | Code |
|---|---|---|
| Phone is in no AutoCount member | Created: MemberNo = the phone (`65` + 8 digits, or a foreign number in full), the configured create type (live: `GUEST`), no expiry, RegisterDate = the sign-up date, DOB from the configured birthday field. Through the unchanged write-once child, profile `sync_p1` | `backfill_ac2_member_created` |
| One member's MemberNo is exactly the phone (or its 8 digits), and its MobilePhone is blank, unusable or the same number | Linked, no time window, no name rule | `backfill_linked` + `backfill_member_no` |
| One member's MemberNo is exactly the phone, but its own MobilePhone is a usable, different number | Held for a person (backfill only; the 15-minute run and `initial-link` keep their rule) | `backfill_member_no_phone_differs` |
| One member holds the phone in MobilePhone or inside a longer MemberNo, same name (the 2-word rule) | Linked | `backfill_linked` + `backfill_phone_and_name` |
| The phone is held under another name (a shared family phone) | Held for a person (never the fallback number) | `backfill_shared_phone` |
| Two members hold it under the same name | Held | `backfill_phone_on_multiple_records` |
| Both forms of the number are MemberNos | Held | `backfill_memberno_conflict` |
| Also carries a legacy tag (`member-legacy` or `XB Member`, any case, spaces trimmed), no member found | Held: never created by a tool | `backfill_legacy_needs_person` |
| No phone; malformed phone; no name; no sign-up time | Held, before any AutoCount lookup | `backfill_no_phone`, `backfill_phone_invalid`, `backfill_no_name`, `backfill_signup_time_missing` |
| The member it would link or create is already linked to another customer (this ledger, the member core's active claim links, or earlier in the same call) | Held | `backfill_member_linked_elsewhere` |
| Staff unlinked exactly this customer and this member in the member core (a voided claim link) | Held for a person: never linked again by a tool (the member core refuses the pair too) | `backfill_claim_pair_unlinked` |
| A text field AutoCount cannot store exactly | Held | `backfill_text_unstorable` |
| Already linked (this ledger, or a claim link) | Skipped, no lookup | `backfill_already_linked`, `backfill_claim_linked` |
| An open (INTENT or UNCERTAIN) write for the customer, or for the MemberNo | Held, never re-sent | `backfill_held_by_open_write`, `backfill_member_held_by_open_write` |
| Signed up on or after the cutover | Left to the live sync | `backfill_signed_up_after_cutover` |

The sign-up time is the live rule's: the `membership.start_date` metafield's
creation time, else Customer.createdAt. That metafield was created at the
cutover, so for older sign-ups it is Customer.createdAt, which is also the
RegisterDate (an account made before the sign-up gives an earlier date).
Customers with a legacy tag but no `member-mg` tag are not read by the
backfill; `coverage` lists those tagged `member-legacy` for a person. The
legacy tags are a fixed list in the code (no setting): a link can never be
removed, so a customer the store treats as legacy is never created as a new
member. Email is never read.

**Bounds, per call.** At most `--max` creates (1 to `max_writes_per_run`,
default 1; anything else is `backfill_max_invalid`, before any login); never
a create while the ledger holds 4 x `max_writes_per_run` writes or more in
the last 60 minutes (the daily summary's hourly allowance; at the live
`max_writes_per_run` of 5 that is 20, so the email never stops with
`writes_over_cap`); at most `max_shopify_customers_per_run` customers looked
up in AutoCount (one sign-in each); creates also need the live
`ac2_create_enabled`. A call stops (`stopped`) before the next customer when
`STOP` exists in the sync's state root (`stop_file_present`), when a bound is
reached (`backfill_max_reached`, `backfill_hour_cap_reached`,
`backfill_lookup_budget_reached`, `ac2_create_disabled`), and after any
create that is not verified (`backfill_create_not_written`,
`backfill_create_uncertain`; exit 3, an operator looks first). A Shopify read
that does not reach the last page decides nothing
(`backfill_read_incomplete`, exit 3; raise `shopify_max_pages` in the
backfill's config copy). The backfill raises no alert: counts only, so the
daily summary's expected-alert list is unchanged.

**Resumable.** Customers are taken in customer-number order after the mode's
cursor (`backfill_registrations_cursor`, or `..._dry_run_cursor` for a dry
run). A call that reaches the end clears its cursor; an applied one also sets
`backfill_registrations_completed_at`. A customer once linked or created is
skipped by every later call; an uncertain write is held, never re-sent (the
15-minute run's existing resolution links it when it is found).

**What it records.** A dry run writes only its cursor. An applied call writes
its links (origin `matched`, or `created_in_ac2` for a create), its `writes`
rows, the cursor and one `runs` row of its own kind (`run_id` starting
`backfill-`, summary `"kind": "backfill_registrations"`, counts, no alerts,
keyed refs with masked MemberNos), so a later summary can show "backfill: N".
`digest` already adds its counts (for example `backfill_ac2_member_created`)
and its writes per hour. No schema change (v3): the ledger still opens under
the commit the 15-minute task runs.

**Effect on the 15-minute run after creates.** Members the backfill creates
are `created_in_ac2` links, so the run's possible-duplicate watch checks the
10 newest of them made in the last 7 days, two lookups each, on every run.
Until 7 days after the last create, each run may take up to 20 more
AutoCount sign-ins. Step 6 of the procedure checks the run times after the
pilot and after each sitting, and says when to stop batching.

#### Procedure (attended, owner OK for each step)

Prerequisites: the slice is merged; the birthday field setting on the live
sync is fixed (G0 step 1); the dry run's counts are reviewed.

1. Clone the merged commit into its own folder on the VM (never the
   15-minute task's clone). Copy the live config to a private file of the
   backfill's own; in that copy only, set `backfill_registrations_enabled`
   (`false` for the dry run), `member_core_state_root` (the member core's
   root, from 17 November), and if needed `shopify_max_pages` (enough pages
   for every `member-mg` customer, 50 a page). The state root stays the
   sync's own (the links must land in its ledger). **Never put either BF1 key
   into the live config the 15-minute task reads**: a commit older than this
   slice refuses unknown keys and the task would stop with
   `sync_config_keys_invalid`. `check-config` on the copy.
2. Pause the 15-minute task (`\XB\XB-MemberSync`) and, from 17 November,
   the member tool's task (`\XB\XB-MemberBenefits`, it signs in with the
   same AutoCount user). Wait until neither is running. The ledger lock
   refuses a second process anyway.
3. Dry run: `backfill-registrations --config <copy>` until `complete` is
   true (each call looks up at most `max_shopify_customers_per_run`
   customers). The first call's `population` is the exact join: every
   `member-mg` customer, by class before any lookup (`backfill_needs_lookup`
   is the gap the batches work on). Read `counts` and `refs` (codes, keyed
   refs, masked MemberNos). **Live check of the search here:** the backfill
   searches only `tag:'member-mg'` (no date filter); `population.tagged`
   must equal the admin's count of customers tagged `member-mg`
   (Customers, filter by tag). If it does not, stop. From 17 November the
   output must say `"member_core_ledger": "read"`; `"not_configured"` is a
   stop (set `member_core_state_root` in the copy).
4. Resume both tasks between sittings.
5. Pilot: set `backfill_registrations_enabled: true` in the copy, pause the
   tasks, `backfill-registrations --apply --max 3 --config <copy>`. Check each
   created member in AutoCount by hand (masked MemberNo in `refs`): type
   `GUEST`, no expiry, RegisterDate the sign-up date, name and mobile right.
   Check links count in `status`. Resume the tasks.
6. **Run-time check, after the pilot and after each sitting.** With both
   tasks resumed, wait about an hour, then run
   `digest --hours 1 --config <live config>`: that window holds only
   15-minute runs. If `runs.duration_seconds_max` is above about 300 s (the
   task is stopped at 600 s), stop batching until 7 days after the last
   create, then check again. Do not use the daily summary for this check:
   its run durations include the backfill's own calls.
7. Batches: `--apply --max 5`, at most 4 calls in a clock hour (20 creates;
   the code refuses more in any 60 minutes), tasks paused for each sitting,
   until `complete` is true. End each sitting with headroom for the
   15-minute run's own writes: keep the sitting's last hour at 15 creates or
   fewer (the daily summary counts both in the same clock hour). Exit 3 or
   any `stopped` other than a bound: stop and look before the next call.
   After each sitting, step 6.
8. To stop at once: create an empty file `STOP` in the sync's state root;
   the call stops before the next customer. Delete it before the next call.
9. When done: `backfill_registrations_enabled` back to `false` in the copy;
   resume both tasks; next morning, read the daily summary.

**A wrong member** (created for the wrong person, or a duplicate staff then
keyed): staff make the member inactive by hand in AutoCount. The tool never
deletes or updates a member, and a link can never be removed.

#### Coverage check (read-only, any day)

`coverage --config <config>` reads the customers tagged `member-mg` or
`member-legacy` (Shopify reads only; the session's one mutation is switched
off), this sync's ledger read-only (no lock, no migration, no write) and, when
`member_core_state_root` is set, the member core's claim links read-only
(active links from its `active_claim_links` view; a voided link is history,
never a link; a ledger version other than 2 is refused with
`member_core_ledger_version_unknown`). It opens no AutoCount session and writes nothing to either ledger, AutoCount or
Shopify (with `token_cache` `dpapi_current_user` the Shopify token cache in
the state root may be refreshed, as for every command). It needs no paused
task.

- `shopify.classes`: every tagged customer in exactly one class, adding up
  to `tagged_total`: `linked`, `linked_claim_only` (an active member core
  claim only), `open_write`, `claim_unlinked_not_linked` (staff unlinked a
  claim of this customer in the member core and it has no active link
  anywhere: a person decides), `legacy_not_linked` (a legacy tag, no `member-mg`), `after_cutover_not_linked`
  (the live sync's own), the person classes of the table above (no phone,
  malformed phone, no name, no sign-up time, and each customer's latest hold
  from the applied backfill calls), and `gap` (signed up before the
  cutover, usable phone, not linked, not held: the backfill's remaining
  work).
- `ac2.classes`: every linked member in exactly one class, adding up to
  `linked_members_total`: `linked_both` (the same customer in both ledgers),
  `linked_sync_only`, `linked_claim_only`, `linked_conflict` (two
  customers); plus `customers_linked_to_two_members`. The members the book
  holds are not read (`members_in_book: "not_read"`): the daily summary
  email gives the member totals per type.
- `complete` is true when `gap`, `after_cutover_not_linked`, `open_write`
  and `claim_unlinked_not_linked` are 0 and there is no conflict on either
  side. Everyone else is on a named
  list.
- `refs`: keyed customer refs and the code for every customer outside the
  linked, gap and after-cutover classes (at most 200). `refs_truncated` is
  true when more customers than that are listed in those classes: the counts
  in `shopify.classes` are still whole, but the list is not; work through
  the listed ones and run `coverage` again.

**The weekly list for a person.** Run `coverage` once a week and give the
person classes (counts and refs) to the person who fixes them: for a conflict
pair, decide in AutoCount which record is real and give the other a new code,
then the next backfill pass links the customer; for no phone, get the number
from the customer and key the member (the sync links it); for a legacy member
or a shared phone, a person decides whether to key or link by hand.

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
   are more than `memberno_link_window_minutes` apart (default 60 from
   round 10; UAT r4 ran at 10; it must alert, never create). `CreatedTime`
   is the time the New Member form was opened. Compare one
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

## Go-live (production)

Spec: the G0 go-live design (private store; public summary on #155) with the
Owner's SCOPE-CONFIRMED of 29 October 2026 (Q1 type `Default`, live `GUEST`
since S5, see `ac2_create_member_type`; Q2 = A no
create without a phone on the live book; Q3, Q4, Q6, Q8, Q9 as recommended;
Q5 start at 15 minutes and state the safe minimum; Q7 the member type check
approved). Nothing in this repository runs any step below. VM Claude runs
them on the AC2 VM; every step from S1 on needs its own Owner OK, named in
the OK.

### What the code admits on the live book

| Layer | Live book `AED_XBOUNDARIES` | Still refused |
|---|---|---|
| Python config (`parse_config`) | Exact name only; `ac2_credential_target` must be `XB-AC2-MemberSync`; `ac2_login_mode` must be stated: `owner_shared` (the Owner's own user, `ac2_refused_user_ids` empty) or `dedicated` (`ac2_refused_user_ids` lists at least the Owner's AutoCount user ID) | A missing or unknown `ac2_login_mode` (`sync_config_ac2_login_mode_live_required`, `sync_config_ac2_login_mode_invalid`); `owner_shared` with a refused list; any other name or lookalike (`autocount_book_not_authorised`); `ac2_create_without_phone` `true`; `XB-AC2-MemberSync` in a test-book config (`sync_config_ac2_credential_target_live_only`) |
| Ledger | Schema v3, a fresh ledger in its own state root (example `C:\XB\xb155-member-sync-live`) | A test ledger with the live config and the reverse (`sync_ledger_binding_mismatch`, also for `status` and `digest`) |
| Engine | Same rules as the test book, plus: a sign-up with no usable phone is never created (R8) | `GetNextMemberNo` (`member_no_generator_not_authorised`) |
| PowerShell book guard | `sync-lookup`, `sync-changelog`, and `write-once` with payload profile `sync_p1`. The operation is checked before stdin; the profile once the payload is read, before any session, login or claim. Also (#268) the one-off member type move's `type-move-*` operations, only with profile `type_move_p1` and only through their own entry point (`member_type_move_runbook.md`) | Every other operation, `next-member-no` included, any other profile (`type_move_p1` on a sync operation and `sync_p1` on a `type-move-*` operation included) and any caller without an operation (`production_book_not_authorised`) |
| Write-once `sync_p1` | MemberNo free, then the member type exists (`SELECT COUNT_BIG(*) FROM MemberType WHERE MemberType = ?`, the one new pinned read, one parameter), then the claim | `member_no_taken`, `member_type_unknown`: refused before the claim, proven not written |

Unchanged: the identity proof after login (declared `DBName` = `DB_NAME()` =
the pinned book, host), the outbound-quiet check, the host and assembly
pins, the SQL deny pattern (no SQL text may name the production book), the
Core's environment gate (`production_environment_not_authorised`), the
owner console and E7 scripts (they type and pin `AED_TESTINGAPI`), the
pinned gateway adapter and the `member_gateway` tree. Every book-name
compare is ordinal (closes open item 4). The closure matrix of the design
is pinned row by row by the `test_row_*` tests in `tests/test_member_sync.py`
and `tests/test_member_sync_bridge_ps.py`.

### Order, OKs, pass bars and rollbacks

| Step | What | Owner OK | Pass bar | Off switch / rollback |
|---|---|---|---|---|
| S0 | This PR: G2 review, Owner merge | OK-0 (merge) | G2 PASS; CI green on the exact head | Revert the PR (nothing runs on merge) |
| S0b | Test-book smoke at the merged SHA (inside the existing UAT gate): first the credential migration (Owner re-stores the test-book login under `XB-AC2-MemberSync-Test` and sets it in the UAT config, "AutoCount login and credential" above), copy the UAT ledger, then `check-config`, `check-credential`, one `initial-link` dry run, one `run --direction shopify_to_ac2` | none (test book, no writes) | The v2 UAT ledger opens as v3 with no other change; no new refusal code (a UAT config still naming `XB-AC2-MemberSync` gets `sync_config_ac2_credential_target_live_only`: do the migration, then repeat); 0 writes either side | Restore the ledger copy |
| S1+S2 | Live read-only check: `prepare-state-root` for the new root, the live config (all switches off, cutover = the minute S2 starts), `check-config`, `check-credential`, `init`, initial-link dry runs until `complete: true`, then one `run --direction shopify_to_ac2` | OK-1 "LIVE READ-ONLY" | Book `AED_XBOUNDARIES`; `check-config` shows `ac2_login_mode` `owner_shared` and `ac2_refused_user_ids_count` 0; credential masked (the Owner's user, last 3 characters); the Owner's own AutoCount session on the VM is still signed in after each login of S1 (Owner session check, see "AutoCount sign-in and licence"; recorded yes/no); no `book_*`, `vendor_query_*`, `outbound_*`, `user_script_present` refusal; counts recorded (would link, no phone, no member, conflicts, `initial_link_cursor_stuck` 0); the run returns only `initial_link_not_complete`; 0 writes; after the first initial-link dry run ends, AutoCount's logged-in users list shows the Owner's user only for the Owner's own session (the sync's session released when the bridge child exits; read-only check, see "AutoCount sign-in and licence") | Nothing live to undo; the live ledger may be deleted (no links yet) |
| S3 | `initial_link_enabled` true, `initial-link --apply` until complete, back to false | OK-2 "INITIAL LINK" (may be merged with OK-3, Q8) | `initial_link_linked` = the last dry run's would-link; one linked member unchanged in AutoCount | Before S5 only: archive the live ledger and redo S1-S3 (no external effect). After S5, never delete the live ledger |
| S4 | Shadow schedule: `changelog_xml_column` `"Content"`, installer dry run (no `-Register`) output reviewed, one manual `run --direction both` as the task account, then `-Register` | OK-3 "SHADOW SCHEDULE" | 3 scheduled runs with result 0 or 3 and only expected alerts (`waiting_for_staff` counts, `sign_up_past_cutoff_create_disabled`, `no_phone_sign_up_needs_staff`, `memberno_*`, `ac2_pending_would_create_backlog`); 0 writes; `digest` readable and its `runs.duration_seconds_max` well under 600 s (above about 300 s: lower `max_pending_rechecks_per_run` first, "Schedule" below); then at least 2 days in shadow | Disable `\XB\XB-MemberSync` in Task Scheduler |
| S5 | `max_writes_per_run` 1 and `ac2_create_enabled` true before a 06:00 run; check the first create in AutoCount and at one till; then `max_writes_per_run` 5 | OK-4 "AC2 CREATES" (never merged) | First create correct (MemberNo, type = `ac2_create_member_type`, live `GUEST`, name, mobile, DOB, RegisterDate, no ExpiryDate) and seen at a till; no `uncertain` write; `created_after_wait` raised; no duplicate next run | `ac2_create_enabled` false (next run). A wrong record is fixed or deleted by the Owner in AutoCount; the sync has no update or delete |
| S6 | In one sitting: Owner adds `write_customers`; the Q3/Q9 probe through the TEST book (one fake member, real `customerCreate`, deleted straight after); production `shopify_create_enabled` true | OK-5 "SHOPIFY CREATES" (never merged) | Probe customer accepted with first name only; a second customer with the same phone refused by Shopify; first production create checked in admin | `shopify_create_enabled` false AND remove `write_customers` (with only one of them every run refuses on scope, `shopify_token_scope_not_expected`, which is safe but stops the sync) |
| S7 | Hypercare, 14 days: `digest --hours 24` and `status` daily, masked counts and `runs.duration_seconds_max` to the Controller | none (read-only) | No action-needing alert left open; `duration_seconds_max` stays under about 300 s (else lower `max_pending_rechecks_per_run`) | Disable the task after two non-0/3 results in a row |
| P3 | Probe in observe mode, after S5 has passed: VM clone to the merged #155 P1+P2 SHA, the probe task registered with `probe_wake_enabled` false, 3 days ("Immediate create (after S5)" below) | "PROBE OBSERVE" | Would-wake times match real sign-ups (one `would_wake` per new sign-up, within about 2 minutes of it); no wake storm (fewer than about 3 per sign-up, digest `probe.days`); the probe finishes in under 10 s; no AutoCount session from the probe in AutoCount's logged-in users list; the main task unchanged | Disable `\XB\XB-MemberSync-Probe` |
| P4 | Immediate creates: staff briefed on the procedure below first; blocker A6 cleared; the A5 test-book UAT passed (one immediate create, plus a duplicate-MemberNo save in the HQ screen, which AutoCount must refuse); `immediate_create_enabled` and `probe_wake_enabled` true, window `all_day`, caps 6 and 144; 7 days | "IMMEDIATE CREATES" (never merged with OK-4 or OK-5) | 7 days: digest `create_latency_seconds.p95` at most 120 (sign-up to HQ create at most 2 minutes); every counter case seen at the till, with its time recorded; 0 till-side duplicate MemberNos; no `uncertain_*`; the Owner's session never signed out; caps never hit by real traffic (`caps.create_rate_capped` 0) | Any of the three off switches below |

S5 and S6 must stay separate OKs, and never merge with OK-0. P3 and P4 are
separate OKs too, after S5 (at least 3 clean S5 creates seen at a till and
the P0 checks done, owner 4 Nov 2026).

Before S6 (the Q9 probe login, G2 #281 LATER-3): the probe runs with the
test-book config, which signs in with its own target
`XB-AC2-MemberSync-Test` (the test-book login), never the live sync user.
Before the S6 sitting, run `check-credential` on that test config: it must
show the test-book user's last 3 characters, and `check-config` must not
return `sync_config_ac2_credential_target_live_only`. Otherwise do the
credential migration first, so the probe does not fail
(`autocount_login_rejected`) inside the one-sitting window.

### Schedule: interval and the safe minimum

`scripts/install_member_sync_task.ps1 -IntervalMinutes` defaults to **15**
(Owner Q5) and refuses anything below **10 minutes, the safe minimum**:

- Run time and login cost. Every bridge operation is its own PowerShell
  process with its own AutoCount login (one lookup, one ChangeLog read, one
  write each). A run makes them one after another: about 2 plus one per
  pending sign-up re-checked plus one or two per pending AC2 member (at most
  20 per run) plus up to 20 for the duplicate watch. Worst case (G2 #281
  LATER-2) about 160 children in one run: up to 100 pending sign-up
  re-checks (`max_pending_rechecks_per_run` 100), 20 new sign-ups, 20
  duplicate-watch lookups, 1-2 ChangeLog reads, 1 batch lookup and up to 20
  more lookups on the AC2 side. The re-check backlog grows during shadow,
  because past-cutoff sign-ups stay PENDING while AC2 creates are off. How
  long one login takes on the VM is not known yet; S4 measures it (`digest`
  prints `runs.duration_seconds_max` and `duration_seconds_median`).
- Run-time check (S4, then daily in S7): compare the digest's
  `duration_seconds_max` with the 600 s execution limit. If it passes about
  300 s, lower `max_pending_rechecks_per_run` (config edit, next run). A
  longer interval does not help: it does not shorten a run, only spaces the
  runs out, and a run that reaches the limit is still stopped part-way.
- At the limit Task Scheduler stops the run part-way. That is safe (the OS
  releases the ledger lock; an open INTENT becomes UNCERTAIN and holds its
  customer; nothing is re-sent), but the run's remaining work waits for
  the next run.
- The task's execution limit is 10 minutes (`PT10M`). With an interval of at
  least 10 minutes a new start never meets a run that is still inside its
  limit, so `IgnoreNew` only ever skips a start behind a hung run.
- AutoCount licences (VM fact V9: 5 concurrent): the sync holds one, only
  while one bridge child runs. Within a run the children run one after
  another, and `IgnoreNew` and the ledger lock stop a second runner. One
  exception (G2 #281 LATER-2): when Task Scheduler hard-stops a run at the
  limit, a write-once child it started may be left running on its own (at
  most 120 s: its own deadline watchdog stops it), so it can briefly
  overlap the next run and hold a second licence for that time.
- Licence release (carried from G1/G2 #281; confirm at S1, read-only): when
  the first initial-link dry run (the first command that logs in) has
  ended, open AutoCount's logged-in users list (as the Owner, on the VM)
  and check that the sync's session is gone (in `owner_shared` mode: the
  Owner's user is listed only for the Owner's own session, as before the
  run), and that no `powershell.exe` bridge child is left in Task Manager.
  If the sync's session is still listed after the child exits, stop and
  report before any further step: every run would then hold a licence
  until AutoCount times the session out.
- Run-time measurement before any interval under 15 (carried from G1/G2
  #281): S4's `digest` gives the first `duration_seconds_max`; the rule
  below needs the S7 digests on 3 days in a row.
- ChangeLog volume (V5): about 272,000 rows in 30 days, but the sync reads
  only Member inserts after its ID watermark (3-8 new members a day, median
  3), at most 200 per run: the volume is no limit at any interval.
- Shopify: a run sends a handful of Admin GraphQL queries (session check,
  one or a few registration pages, customer reads and phone searches for the
  pending rows). Shopify's limit is cost-based per app and store; this load
  stays far below it at 10 minutes too.
- What a shorter interval changes: only how soon links and walk-in creates
  appear. Decisions do not depend on it: links compare stored times, and
  AC2 creates still wait until 06:00 the next day.

Rule: go below 15 minutes only when the S7 digests show the longest run
under half of the new interval on 3 days in a row. Below 10 minutes needs a
code change (the execution limit and this floor) and its own review.

### AutoCount sign-in and licence (VM fact V9)

- Owner decision, 30 Oct 2026 ("Use my login"): the sync signs in with the
  Owner's own AutoCount user, the same login the October UAT used, read
  from the Credential Manager target `XB-AC2-MemberSync` of the task
  account. The Owner's user is also signed in on the VM all day, so each
  bridge child opens a second session of the same user. The live config
  records the choice (`"ac2_login_mode": "owner_shared"`,
  `"ac2_refused_user_ids": []`) and refuses any other target; the code has
  no default for the mode on the live book. `check-credential` shows only
  the user ID's last 3 characters.
- Other mode, `dedicated` (the 29 Oct design, kept for a switch-back): a
  separate AutoCount user for the sync, with the Owner's user ID in
  `ac2_refused_user_ids`, so a credential holding the Owner's user is
  refused (`ac2_credential_user_refused`, exit 2) before any bridge child
  starts. Switching is a config edit plus a new `cmdkey` entry; the Owner
  creates that user in AutoCount first.
- Owner session check (S1, read-only, `owner_shared` mode): it is not known
  whether a second sign-in of the same user signs the first one out (the
  October UAT did not record it). Before S1, the Owner has their AutoCount
  window open and signed in on the VM. After `check-credential` (no login
  yet: a baseline), after the first initial-link dry run (the first command
  that logs in) and again after the S2 run, the Owner checks that window is still signed in
  and usable (open any list, no sign-in prompt, no "signed out" or
  "session ended" message), and records yes or no each time. If any answer
  is no: stop, run nothing further, report; the sync then moves to
  `dedicated` mode (a separate AutoCount user, Owner's OK), and S1 is
  repeated in that mode.
- When AutoCount refuses the sign-in (the user is already signed in
  elsewhere in a way AutoCount rejects, all licences are in use, a wrong
  password): the reviewed login fails, the bridge returns a fixed code
  (`autocount_login_rejected` or the stage code), the run stops with exit
  2, and nothing is written (a create fails before its claim and is proven
  not written). This is the same in both modes (closure matrix row V9d).
  The next scheduled run tries again; a run never retries a login. In
  `owner_shared` mode the Owner's own attended use of AutoCount continues
  while the task runs; in `dedicated` mode do not run attended commands
  with the sync user while the task is running: disable the task first.
- Licence use: the Owner's all-day session holds one licence; the sync
  holds one more (of the five), for the length of one bridge child, also
  in `owner_shared` mode, where it is a second session of the Owner's user.
  Not two sync sessions at once, except briefly after a run is
  hard-stopped at the execution limit, when an orphan write-once child (at
  most 120 s) can overlap the next run (above). Assumed, not VM-checked:
  that the second session of the same user takes its own licence. S1
  checked only that the sync's session is released from the logged-in
  users list after the child exits.
- Audit fields (`owner_shared` mode, accepted by the Owner on 30 Oct 2026):
  AutoCount records the signed-in user as the creator of a record, so a
  member the sync creates shows the Owner's user as its creator, the same
  as one the Owner keys by hand. The sync's own ledger (`writes`, keyed
  refs) is the record of which members the sync made. Assumed, not
  VM-checked: that AutoCount records the signed-in user as creator. It is
  confirmed on the first S5 create.

### Scheduled task account (VM fact V8, design T1)

The task runs as the 2-letter Windows account that owns the Credential
Manager target, the DPAPI token cache, the Shopify environment variables
(user scope) and the state root, with logon type **Password** (the Owner
types the Windows password into the prompt; it goes straight to Task
Scheduler). S4U is refused by the installer: an S4U task can read neither
Credential Manager nor the DPAPI cache. Dry run first (the default), then
register only with the OK for S4:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_member_sync_task.ps1 -UserId "<PC>\<account>" -ConfigPath "<live config>" -PythonExe "<full path to python.exe>"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_member_sync_task.ps1 -UserId "<PC>\<account>" -ConfigPath "<live config>" -PythonExe "<full path to python.exe>" -Register
```

The dry run prints the task XML that `-Register` registers: folder `\XB\`
(pinned: `-TaskPath` accepts only exactly `\XB\`, G2 #281 LATER-4),
name `XB-MemberSync`, one trigger repeating every `-IntervalMinutes` with no
end, `IgnoreNew`, `PT10M`, `LeastPrivilege`, logon `Password`, action
`python <repo>\directlocal\xb_member_sync.py run --config <live config> --direction both`.
It never replaces an existing task.

### Hypercare alerts that need action

`create_rate_capped` (#155 P1: a cap was reached; the held sign-ups wait
for room or the 06:00 rule; at most one alert per hour), `possible_duplicate_member`, `memberno_exists_outside_window`,
`memberno_conflict`, `initial_link_member_no_conflict`,
`customer_held_by_open_write`, `uncertain_write_*`, `ac2_member_hold_stuck`,
`shopify_pending_backlog`, `no_phone_sign_up_needs_staff` (staff key that
member by hand; it stays unlinked in phase 1), `member_type_unknown`
(the configured type is missing in the book; every create is refused until
fixed), `ac2_pending_would_create_backlog` (expected in shadow; after S6 it
means the backlog is draining: once `shopify_create_enabled` is true the
whole AC2 queue is read oldest first by ChangeLog ID, so a new walk-in waits
behind the older backlog, G2 #281 LATER-1), and any refusal (exit 2), for example
`ac2_credential_user_refused`. They reach the Owner by email in the daily
summary at 06:40 (#288 slice 3, below); there is no real-time push for them
until S6 is reviewed (#288 G0 Q8).

### Daily summary email (#288 slice 3)

A separate read-only task, `\XB\XB-MemberSync-DailySummary`, runs once a day
at 06:40 SGT (after the 06:00 deferred-create run and the 06:15 and 06:30
runs) as the sync's own task account, and emails one summary that replaces
the manual daily check: `[XB daily] MEMBER-SYNC CLEAN <date>` or
`[XB daily] MEMBER-SYNC STOP <date>`. Design: #288 G0 sections 3, 4 and 6.
Script `directlocal/xb_member_sync_daily_summary.py`, settings
`config/member_sync_daily_summary.example.json`, installer
`scripts/install_member_sync_summary_task.ps1`, n8n workflow
`n8n-workflows/member_sync_daily_summary.workflow.json` (it calls the shared
`XB Ops Alert Email` sub-workflow).

**What it reads (each step read-only):**

| Daily-check step | Automated as |
|---|---|
| 1 Task state | `Get-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-MemberSync' \| Get-ScheduledTaskInfo` (a PowerShell child that only reads) |
| 2 Config | `xb_member_sync.py check-config` (child process) |
| 3 Digest | `xb_member_sync.py digest --hours 24` (child process; no lock, no migration, no login) |
| 4 `status` | **Never run**: it takes the ledger lock, so a 15-minute run starting meanwhile would refuse. Its counts come from the digest. |
| 5 Members per type | `SELECT MemberType, COUNT_BIG(*) FROM dbo.Member GROUP BY MemberType` |
| 6 Creates in 24 h | Digest counts (`ac2_member_created`, `created_immediately`, `created_after_wait`). The digest has no per-create list, so the email has no per-create time or link line; alert refs show `***` plus the last 3 characters. "FIRST CREATE" when no earlier daily record saw a create (settings `first_create_already_seen` covers creates made before the task existed). |
| 6a New AC2 members | `SELECT COUNT_BIG(*) FROM dbo.ChangeLog WHERE TableName = N'Member' AND ChangeType = N'I' AND ChangeTime >= DATEADD(hour, -24, GETDATE())`, minus the sync's creates; links compared with yesterday's record |
| 7 Owner session | Not automatable without touching AutoCount: always `owner_session: not_checked` (G0 Q4) |

The two SELECTs use Windows authentication, `ApplicationIntent=ReadOnly`, the
sync's own book (from `check-config`), and the stock export's identity guard
on the same connection: never the `sa` login, `DB_NAME()` must be the book,
and a `sysadmin` or `db_owner` identity is refused unless the settings say
`allow_privileged_identity: true`. If the account has no `SELECT` right (or
pyodbc is missing), those two lines say `not available (<reason>)` and the
summary still sends.

**It never** takes the ledger lock, opens the ledger itself, runs `status`,
`run`, `init`, `initial-link` or an installer, starts the bridge, signs in
to AutoCount, uses the `sa` login, writes the ledger or the sync config,
changes a scheduled task, or prints personal data. Static and runtime tests
prove each (`tests/test_member_sync_daily_summary.py`).

**STOP codes (closed list, in email order):**

| Code | Meaning |
|---|---|
| `task_state_unavailable` | The sync task could not be read |
| `task_last_result` | `LastTaskResult` is not 0 or 3. Only a run in progress (0x41301) is fine: a run killed by the time limit (0x41306) or a task that has not run yet (0x41303) stops. The task is read once a day, so one bad result stops (the manual check asked for two in a row). |
| `task_disabled` | The sync task's state is `Disabled`. A dead sync must not read CLEAN, even when its last result was 0. |
| `check_config_failed` | `check-config` refused or did not answer. This is also what a broken or missing live sync config gives: the summary still records and sends. |
| `config_unexpected` | `shopify_create_enabled` is not exactly false (missing counts), the member type is not `expected_member_type` (GUEST), or the live config's `state_root` is not the `sync_state_root` in the summary settings (the email line says `state_root_as_in_settings=no`) |
| `digest_failed` | `digest` refused or did not answer |
| `no_runs` | No completed run in 24 hours, with the digest readable. A dead sync must not read CLEAN. |
| `unfinished_runs` | More than 1 unfinished run in 24 hours |
| `shopify_write` | Any Shopify write: every outcome code of the sync's one Shopify mutation that means a create was sent (`shopify_customer_created`, `shopify_separate_customer_created_without_phone`, `shopify_create_uncertain`). A test reads `xb_member_sync.py` and fails if it counts a create outcome that is not on this list. |
| `uncertain_writes` | Uncertain writes open |
| `possible_duplicate_member` | The alert, any count |
| `member_type_unknown` | The alert, any count |
| `login_refused` | An `autocount_*` or `bridge_read_failed*` alert (most refusals stop a run before it records; they then show as `unfinished_runs` or `task_last_result`). A single login refusal followed by good runs reads CLEAN (1 unfinished run), as in the manual rule. |
| `writes_over_cap` | More writes in one Singapore clock hour than `max(max_writes_per_run x 4, max_creates_per_hour)`. The digest has no per-run write count, so this is the per-hour form of "more writes in one run than `max_writes_per_run`". |
| `alert_unexpected` | Any alert code that is not expected. Expected (not a stop): `waiting_for_staff`, `created_after_wait`, `no_phone_sign_up_needs_staff`, `max_writes_per_run_reached` and every `memberno_*` |

**Auto off: observe mode only.** The task reports `auto_off: would_switch`
with its reasons for the three clearest conditions (a Shopify write while
Shopify creates are off, uncertain writes, `possible_duplicate_member`) and
nothing else. No code path writes the sync config. Switching it on (the
guarded, hash-pinned restore of G0 6.4) is a later round after 7 observed
mornings and its own owner OK.

**Email failure never fails the task.** The task writes
`daily_summary_<YYYY-MM-DD>.json` in its own output folder (never inside the
sync state root) before the POST (`email_status: pending`) and after it
(`sent`, `send_failed` with `http_status`, `secret_unavailable`, `disabled`
or `not_sent_check_only`), and exits 0. A missing morning email is itself
the signal that the email route is down. Exit 2 (no record, no email) only
when the summary's own set-up is wrong: its settings file is unreadable or
refused, its output folder is missing or cannot be written, or its output
folder sits inside the sync state root (the one in the settings, or the one
in the live sync config when that can be read). A broken, missing or
unreadable **live sync config** is not one of these: the summary carries on,
`check-config` refuses, and the email says STOP `check_config_failed`.

**Settings** (`config/member_sync_daily_summary.example.json`): the sync
config path, `sync_state_root` (the sync's state folder, the same value as
`state_root` in the live sync config), the output folder,
`expected_member_type`, `first_create_already_seen`, the timeouts, the SQL
server and ODBC driver, and the send block: the webhook URL
(`https://<n8n host>/webhook/<private path>`), the header name and the
**name** of the Credential Manager target that holds the webhook secret
(`XB-MemberSync-DailySummary-Webhook` by default). No secret value is in any
file. `check-settings` checks the file without any network, child process or
SQL, and without reading any other file: it refuses an output folder inside
`sync_state_root`, and timeouts that cannot fit the task's 5-minute limit
(three children at `child_timeout_seconds`, five SQL waits at
`sql.timeout_seconds` when SQL is on, and the POST must total 285 seconds
or less; the example uses 60, 15 and 20). `sql.timeout_seconds` is both the
login timeout and the query timeout of every statement. `daily --no-send`
builds and records the summary without posting it.

**Where to install it.** The summary runs the `xb_member_sync.py` that sits
next to it. Install the summary task from the same deployed clone as the
live sync task, or from one whose `directlocal/xb_member_sync.py` is
byte-identical to the live one (compare the file hashes first), so
`check-config` and `digest` read the config and ledger exactly as the live
sync does.

**Live steps, in order, each with its own owner OK** (nothing in the repo
does any of them):

1. n8n PC: import `Member Sync - Daily Summary` inactive through the local
   API `http://127.0.0.1:5678/api/v1`, set the private webhook path and the
   sub-workflow ID in the local import copy, bind a new Header Auth
   credential (OK: n8n import and credential). Requires `XB Ops Alert
   Email` imported and published first (slice 1 steps).
2. n8n PC: activate it (OK: activation).
3. AC2 VM, read-only V-check as the task account: `python -c "import
   pyodbc"`, then `SELECT SUSER_SNAME()` and the two SELECTs above. If
   refused, either an owner OK to grant `SELECT` on `Member` and `ChangeLog`,
   or accept `not available` lines.
4. AC2 VM: store the webhook secret in the task account's Credential
   Manager under the target name in the settings (OK: VM secret store).
5. AC2 VM: write the private settings file; run `check-settings`, then
   `daily --no-send` once as the task account and read the record (read
   only). The live settings must set `first_create_already_seen` to `true`
   (S5 creates have run since 5 Nov 2026, before this task existed;
   otherwise the first email says "FIRST CREATE: run S5 Part B"), and
   `sync_state_root` to the `state_root` of the live sync config.
6. AC2 VM: dry-run the installer, then `-Register` (OK: VM task
   registration). Then one `Start-ScheduledTask` and check the record and
   the email.
7. Observe 3 good mornings; then the Controller and owner retire the
   server-PC desktop reminder. Auto off ON is a later round after 7
   mornings (separate OK).

Off switch: `Disable-ScheduledTask -TaskPath '\XB\' -TaskName
'XB-MemberSync-DailySummary'` (by hand; it touches only the summary task).

### Known risks

- Python on the VM is a per-user install (V8), writable by the task account
  itself, so the task runs an interpreter that account can change (the same
  class of risk as EnergyGrid CB1). Not changed in this round; recorded for
  a later hardening step.
- The first production sign-in (S1) may take a licence during shop hours:
  run S1 outside them.
- `owner_shared` mode (Owner, 30 Oct 2026): if AutoCount signs a user's
  earlier session out when the same user signs in again, every run would
  sign the Owner out on the VM. The S1 Owner session check finds this
  before anything is scheduled; the remedy is `dedicated` mode.
- The reverse case: the Owner signing in during a run ends the sync's bridge
  child. That fails safe: a read fails (the run stops, nothing more written;
  a create made earlier in the same run stays complete and recorded), or
  a create in flight is UNCERTAIN and held, never re-sent.
- Shadow runs exit 3 every run while past-cutoff sign-ups wait for AC2
  creates; the digest groups them.
- On rollback of Shopify creates the switch and the scope go together, or
  every run refuses (safe, but the sync stops).
- `sqlcmd` on the VM needs `-S localhost\A2006` (VM fact), for any check a
  later prompt runs by hand.

### Immediate create (after S5)

Spec: the G0 r2 design (#155, "Shopify form to till in minutes", slices P1
and P2) with the owner decisions of 4 November 2026 (D1 all hours, D2 type
`Default`, live `GUEST` since S5, D3 staff keying stays, D4 unchanged, D5 caps 6 and 144). Nothing
here runs on merge: the VM clone stays at its go-live SHA until the P3 OK,
and with the new keys absent or false every run decides exactly as before
(pinned by `DefaultsOffEquivalenceTests` against a golden trace made on the
32e9b31 source).

**What changes with the switches on** (`immediate_create_enabled` and
`ac2_create_enabled`):

- A sign-up that would be created at 06:00 the next day (the sync found no
  member staff keyed) is created at once, at any hour (`all_day`; a window
  `HH:MM-HH:MM` is possible, and outside it the 06:00 rule applies). The run
  counts `created_immediately` instead of the alert `created_after_wait`,
  which stays for the 06:00 path. Every other rule is the same: link first,
  MemberNo rules, member type `ac2_create_member_type` (live `GUEST`, never
  a qualified type), the write-once fence and `max_writes_per_run`, UNCERTAIN
  holds, and no create without a usable phone on the live book (the alert
  `no_phone_sign_up_needs_staff` now comes at once instead of the next
  morning).
- **Caps** (owner D5): before each immediate write the ledger's `writes`
  rows of both directions are counted over the last 60 minutes and the last
  24 hours. At `max_creates_per_hour` (6) or `max_creates_per_day` (144) the
  sign-up is held (pending code `create_rate_capped`). A later run creates
  it once there is room, or at the latest the 06:00 rule does, which the
  caps never gate. One alert `create_rate_capped` per hour at most (the time
  is kept in the ledger, so a restart resets neither the caps nor the
  alert).
- **Wall-clock holds**: the bounded waits that counted runs (the sign-up time
  hold, `REGISTRATION_WAIT_MAX_CHECKS`, and the AC2 `held_for_sign_up` hold
  that ends as `ac2_member_hold_stuck`) last 60 minutes from the first hold
  (today's 5 checks at the 15-minute schedule). The first hold time is kept
  in the ledger's `watermarks` table (`hold_since:*` rows), so woken runs do
  not shorten the wait. With the switch off they count runs, as before.
- **Monitoring**: each create records the seconds from sign-up to the HQ
  create (only while `immediate_create_enabled` is true). `digest` prints
  `create_latency_seconds` (count, p50, p95, max), `creates`
  (`created_immediately`, `created_after_wait`, writes per Singapore clock
  hour and its maximum), `caps` and the probe's days (`probe`: checks, wakes
  and would-wakes per Singapore day; null without a status file).

**The sign-up probe** (`python directlocal\xb_member_sync.py probe --config
<config>`, task `\XB\XB-MemberSync-Probe` every minute):

- A search for `member-mg` customers updated since the sync's own
  watermark, newest first. Each tick makes a session check plus up to 2
  search pages of 50, so at most 3 GraphQL queries over 2 query texts, plus
  bounded retries on throttling or errors, plus a token exchange on a cold
  cache. It reads only ID, tags and `updatedAt`
  (never a name, email or phone; an answer with any other field is
  refused). It opens the ledger
  read-only with no lock, as `digest` does. It never starts the bridge,
  signs in to AutoCount, or writes to the ledger or to Shopify (static and
  runtime tests).
- It wakes the main task (`schtasks /Run /TN \XB\XB-MemberSync`, the only
  process it may start; the main task's own `IgnoreNew`, `PT10M` and ledger
  lock still apply) only for a sign-up the ledger has never seen, once per
  piece of work, and only when all of these hold: `probe_wake_enabled` and
  the immediate path are on; inside the window; under the caps; at least
  60 s after the last run start or wake; no run unfinished (started under 10
  minutes ago) and none hard-stopped at that limit in the last 120 s; and
  under `probe_max_wakes_per_day`. With `probe_wake_enabled` false it
  records `would_wake` only (P3). Before the initial link has completed it
  never wakes.
- It writes `member_sync_probe_status.json` in the state root: counts,
  timestamps and reason codes only. An unreadable status file counts the
  day's wakes as spent (no wake until the next Singapore day). Exit 0; 3
  when the task start failed (`wake_failed`); 2 when refused.
- Installer: `install_member_sync_task.ps1 -Probe` (a dry run by default;
  `-Register` registers): `\XB\XB-MemberSync-Probe`, every 1 minute with no
  end, `IgnoreNew`, `PT2M`, `StartWhenAvailable` false, logon Password,
  `LeastPrivilege`. It refuses `-IntervalMinutes` and the main task's name,
  and never replaces, reads or changes `\XB\XB-MemberSync`.

**Three independent off switches** (each works alone; test-pinned):

| Switch | Effect | Takes effect |
|---|---|---|
| 1. Disable `\XB\XB-MemberSync-Probe` | Back to the 15-minute cadence; with switch 2 on, scheduled runs still create at once | Next minute |
| 2. `immediate_create_enabled` false | Back to the 06:00 rule; the holds count runs again | Next run |
| 3. `ac2_create_enabled` false | No AC2 creates at all (the S5 rollback); the immediate path is off with it, and the probe no longer wakes | Next run |

**Staff procedure (owner D3: manual keying stays available).**

- Staff may still key any member by hand, at the HQ backend or at the till,
  as today. The sync never changes or merges a member staff keyed.
- Keying at the **HQ backend** is covered in both orders. A member saved
  before the sync looks is linked by MemberNo and CreatedTime
  (`memberno_and_created_time_match`, no write). A member saved between the
  sync's lookup and its save makes the sync's save refuse before the claim
  (`member_no_taken`, proven not written), and the next run links it. If
  staff save after the sync created the member, AutoCount should refuse the
  duplicate MemberNo at HQ (G0 r2 assumption A5, checked on the test book
  in UAT); a member keyed under another MemberNo raises
  `possible_duplicate_member`.
- Keying a form sign-up **at the till** while the sync creates it at HQ is
  not covered, and code cannot cover it (G0 r2 A6). The sync cannot see a
  till-keyed member until the till uploads it (1-72 minutes), and what
  AutoCount POS sync does with the same MemberNo created at HQ and at a
  till is unknown (vendor question 5). **This blocks P4** until either the
  vendor answers that it is safe, or the Owner adopts a staff rule such as:
  "a customer who used the sign-up form is not keyed at the till; key at
  the HQ backend if needed, or wait for the member to reach the till (0-15
  minutes)". No "staff grace" delay was added: it does not remove the
  till-side race (G0 r2).
- Staff never key by hand a sign-up the sync is holding (UNCERTAIN,
  `customer_held_by_open_write`); the Owner resolves it first.

**Later VM steps** (not run by this PR; each needs the OK named in the
table above, after S5):

P3 "PROBE OBSERVE":

1. Between two main-task runs, move the VM clone to the merged SHA
   (`git fetch`, then fast-forward or check out that exact SHA) and check
   `git rev-parse HEAD`.
2. Optionally write the six new keys into the live config at their defaults
   (`immediate_create_enabled` false, `immediate_create_window_local`
   `"all_day"`, 6, 144, `probe_wake_enabled` false, 60). `check-config` must
   show them, with both switches false.
3. As the task account: `probe --config <live config>` once (exit 0; reason
   `no_new_sign_up` or `observe_mode`), then `digest --hours 1`: `probe` is
   shown, nothing else changed.
4. Installer dry run with `-Probe`; review the XML (`PT1M`, `IgnoreNew`,
   `PT2M`, `probe --config`); then `-Probe -Register`.
5. Three days: `digest --hours 24` daily (`probe.days`, would-wakes against
   new sign-ups), the probe task's last result and run time in Task
   Scheduler, and once in shop hours AutoCount's logged-in users list. Off
   switch: disable `\XB\XB-MemberSync-Probe`.

P4 "IMMEDIATE CREATES" (after P3 passes, A6 cleared, the A5 test-book UAT
passed, staff briefed). A5: on the test book, one immediate create, then
staff save the same MemberNo in the HQ screen, and AutoCount refuses the
duplicate.

1. Between two main-task runs, set `immediate_create_enabled` true and
   `probe_wake_enabled` true in the live config (window `all_day`, caps 6
   and 144; `ac2_create_enabled` is already true since S5); `check-config`.
   Then check that the probe's first wake records `wake`, not `wake_failed`
   (`member_sync_probe_status.json` `last_decision`, or `digest` `probe`).
   If no wake has happened yet, check it instead with one
   `schtasks /Run /TN \XB\XB-MemberSync` as the task account between two
   runs, under the P4 OK. On `wake_failed` (the task account cannot start
   the main task): switch 1 (disable `\XB\XB-MemberSync-Probe`), then
   report.
2. For the first counter sign-ups, staff note the form submit time and when
   the member first appears at the till; compare with the digest's latency.
3. Seven days: `digest --hours 24` daily against the P4 pass bar. Off
   switches as above.

## Commands

```powershell
python directlocal\xb_member_sync.py check-config --config <config>
python directlocal\xb_member_sync.py init --config <config>
python directlocal\xb_member_sync.py run --config <config> [--direction shopify_to_ac2|ac2_to_shopify]
python directlocal\xb_member_sync.py status --config <config>
python directlocal\xb_member_sync.py initial-link --config <config> [--apply]
python directlocal\xb_member_sync.py digest --config <config> [--hours 24]
python directlocal\xb_member_sync.py probe --config <config>
python directlocal\xb_member_sync.py backfill-registrations --config <config> [--apply] [--max N]
python directlocal\xb_member_sync.py coverage --config <config>
```

`backfill-registrations` and `coverage` (#155 slice BF1) are described in
"Backfill of older sign-ups" above. `backfill-registrations` exits 0, or 3
when an operator must look before the next call (a create not verified, or a
Shopify read that did not finish), or 2 when refused. `coverage` exits 0, or
2 when refused (for example `coverage_read_incomplete`).

`digest` (go-live S7) is read-only: it opens the ledger read-only (no lock,
no migration, no login, no network) and prints, for the runs of the last
`--hours` (1-720): completed and unfinished runs, the longest and median run
time, counts per code, alerts per code with keyed refs (`c-`/`m-`) and
MemberNos masked to the last 3 characters, and the open work (pending
sign-ups, pending AC2 members, `would_create` rows, uncertain writes,
links). It exits 0, or 2 when the ledger cannot be read. It refuses a
ledger made for another book or store (`sync_ledger_binding_mismatch`), as
`status`, `run` and `initial-link` do.

`run` exits 0 when completed, 3 when completed with alerts (see the
summary's `alerts`), 2 when refused.

## Ledger

Schema v3 (go-live R2): a fresh ledger may hold either book (`meta` CHECK
`autocount_book IN ('AED_TESTINGAPI', 'AED_XBOUNDARIES')`). SQLite cannot
change a table CHECK in place, so an existing v2 ledger is bumped to v3
with no other change (it is always a test-book ledger and keeps its old
CHECK); v1 goes through v2 to v3. The production ledger is always created
fresh, in its own state root. The `runs` table's summary now holds the
run's counts, alert codes and keyed refs (older rows: counts only), which
`digest` reads.

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
- Production (`AED_XBOUNDARIES`, member types `Default`, `GUEST` and `MEMBER`
  since 5 November 2026) and its
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

Round 10 (27 October 2026, after Owner UAT r4 PASS on 23-24 October and G2
round 9 PASS-WITH-NOTES on `43c8c70`): L8 (a member stuck during a sign-up
backlog is re-queued once the backlog clears, so a genuine walk-in is still
created in Shopify), L9 (tests pin that a non-form phone hit, MobilePhone or
an embedded 8-digit MemberNo, is not an AC2-side conflict), the UAT fact
that `CreatedTime` is form-open time, the run count rename
`ac2_insert_own_or_linked` -> `ac2_insert_linked_or_own_write` (same
behaviour) and `memberno_link_window_minutes` default 10 -> 60 (range 1-60
unchanged). Gates unchanged: Shopify and AC2 creates off by default, test
book only, `book_guard.ps1` unchanged.

Login mode (30 October 2026, Owner "Use my login", after the go-live merge
at `03acf97`): the live sync logs in with the Owner's own AutoCount user on
both books, not a separate sync user. Recorded explicitly as
`"ac2_login_mode": "owner_shared"` (the live config must state a mode; the
29 Oct separate-user design stays available as `"dedicated"`). Accepted by
the Owner: one more licence in use while a bridge child runs, and the
Owner's user shown as creator of sync-created members. S1 adds the Owner
session check; if the Owner's session is signed out, stop and switch to
`dedicated`. No change to the bridge, book guard, live-book admission,
ledger or installer.

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
4. Closed by the go-live round (R6): every book-name compare in
   `scripts/ac2_directlocal_book_guard.ps1` is now ordinal, and lookalikes
   of both names are refused in both layers (closure matrix row B8). The
   original note is kept for the record: ordinal book-name checks, deferred
   to a later separate change (G2 round 5 NOTE-2). The book checks in `scripts/ac2_directlocal_book_guard.ps1`
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
10. #155 slice BF1, the customers search filter: `initial-link` searches
    `created_at:<'...'`. The Admin GraphQL `customers` reference for 2026-07
    and 2026-10 documents `customer_date` (the createdAt filter), not
    `created_at`, and the search syntax guide says an invalid field is
    ignored and all results are returned. So the first link-up read every
    customer and its own per-customer createdAt check did the work (the
    3 November run read every older customer). The backfill and
    `coverage` use only `tag:'...'` and keep their own per-customer checks;
    the dry run compares `population.tagged` with the admin's tag count.
11. #155 slice BF1: the backfill's RegisterDate for an older sign-up is
    Customer.createdAt in Singapore (no `membership.start_date` before the
    cutover). For a customer whose account is older than the sign-up, that
    date is earlier than the sign-up.
12. #155 BF1 G2 LATER 2 (not changed): `coverage` reads the ledger in
    several statements, not one read transaction. While the 15-minute run
    writes, a customer can be read in an older class for that moment; the
    totals still add up. Run it again if a figure looks off.
13. #155 BF1 G2 LATER 5 (not changed): `member_core_state_root` left unset
    after the member core goes live is not refused; the output says
    `"member_core_ledger": "not_configured"`. The procedure treats that as a
    stop from 17 November.
14. #155 BF1 G2 LATER 7 (not changed, existing helper): a MemberNo of 3
    characters or fewer is shown whole by the last-3 mask.
15. #155 BF1 G2 LATER 8 (not changed): `backfill_registrations_completed_at`
    is written but read by nothing yet.
16. #155 BF1 G2 recheck 1 LATER 1 (not changed in fix round 2): procedure
    step 6 reads `runs.duration_seconds_max` only. A run that Task Scheduler
    stops at 600 s has no duration and shows only in `runs.unfinished`; the
    G2 suggests adding "or `runs.unfinished` above 1". Controller to decide.
17. #155 BF1 G2 recheck 1 LATER 2 (not changed): `coverage` searches
    `member-mg` and `member-legacy`, not `XB Member`; a customer tagged only
    `XB Member` is outside its population.
