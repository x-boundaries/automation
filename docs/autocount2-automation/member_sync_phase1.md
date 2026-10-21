# Member Sync Phase 1: Two-Way Create (#155)

Status (2026-10-22): source and offline tests only (build step 2, draft PR,
fix round 1). Nothing here has run against AutoCount or Shopify. Every write
switch is off by default; turning one on, any AutoCount login and any
scheduling each need their own Owner approval. Only the test book
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
| Shopify -> AC2 | A customer tagged `member-mg` whose registration instant (`membership.start_date` metafield `createdAt`) is at or after `shopify_cutover_utc` | An AC2 member, or a link to an existing one |
| AC2 -> Shopify | A `dbo.ChangeLog` insert (`TableName = 'Member'`, `ChangeType = 'I'`) after the stored `ID` watermark, for example a member created at the till | A Shopify customer, or a link to an existing one |

Both directions write the mapping (Shopify customer GID <-> MemberNo, each
unique) to the sync ledger. That mapping is the loop prevention: an AC2
insert for a mapped MemberNo, or for a MemberNo with an open write, is never
sent to Shopify; a mapped customer is never sent to AC2.

### Shopify -> AC2 create

| AC2 field | Value |
|---|---|
| MemberNo | The phone in digits (numbering rules below). Without a usable phone: AutoCount's own `GetNextMemberNo` number. |
| MemberType | `REGISTERED` (test book type, upper case) |
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

### Matching rules (no review queue)

| Situation | Action | Counted as |
|---|---|---|
| Phone already in AC2 (MemberNo equals the phone or its 8 digits, MobilePhone normalises to it, or the 8 digits are embedded in a MemberNo) and the name rule matches | Link, no create | `phone_and_first_name_match` |
| Same phone on several matching records | Link to the record whose MemberNo is the phone; otherwise skip (never merge) | `phone_duplicate_member_no_exact` / `phone_on_multiple_records` |
| Phone matches, name differs (people sharing a phone) | Create with `<phone digits><FIRST+LAST letters A-Z>` truncated to 20 characters | `shared_phone_name_fallback` |
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

### Name rule (Owner decision 2026-10-21 14:35Z)

Stated rule: on the same phone, link when every word of the shorter name
appears in the longer one; any differing word means a separate member (the
phone + initials rule); typos count as different ("Anne" is not "Ann"); no
fuzzy matching.

Implemented rule (unchanged, `first_name_matches`): case- and
space-insensitive, the **Shopify first name** must equal a run of consecutive
words of the AC2 `Name` (Shopify -> AC2 and AC2 -> Shopify alike; Shopify's
last name is not compared).

Examples, checked against the code on one shared fake phone
(`NameRuleTests`):

| Example | Stated | Code | Locked by a test |
|---|---|---|---|
| Shopify "Ann Lee" / "Mary Lee" / "Mary Ann Lee" -> AC2 "MARY ANN LEE" | link | link | yes |
| Shopify "John Lim" -> AC2 "MARY ANN LEE" | separate | separate | yes |
| Shopify "Anne Lee" -> AC2 "ANN LEE" | separate | separate | yes |
| Shopify "Mary Tan" -> AC2 "MARY ANN LEE" | separate | **link** (first name "Mary" is a word of the AC2 name; the last name is not compared) | no: Owner decision pending |
| AC2 "Mary Ann Lee" -> Shopify "MARY ANN" + "LEE", or "MARY ANN LEE" whole in firstName | link | link | yes |
| AC2 "Mary Tan" / "John Lim" -> the same Shopify customer | separate | separate | yes |
| AC2 "Anne Lee" -> Shopify "ANN" + "LEE" | separate | separate | yes |
| AC2 "Ann Lee" / "Mary Lee" -> Shopify "MARY ANN" + "LEE", or "MARY ANN LEE" whole | link | **separate** (the Shopify first name "MARY ANN" / "MARY ANN LEE" is not a run of words of "Ann Lee" / "Mary Lee") | no: Owner decision pending |

### AC2 -> Shopify create

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
   the queueing.
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
   names or phones). Their phone is `MobilePhone`; when that is missing
   (VM fact 3: present on 198 of 2,758 production inserts), a MemberNo made
   only of digits that is itself a phone under the numbering rules (for
   example `65` + 8 digits, the phase-1 numbering). A phone + initials,
   phone + digit (`65`...) or `GetNextMemberNo` MemberNo gives no phone.
7. The member is looked up in Shopify by phone. The search result is only a
   candidate list: a node whose own phone does not normalise to the same
   digits, or has no phone, is dropped before any decision (G2 B1). Then:
   - one customer, name rule matches: link;
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

`shopify_create_enabled` stays `false` until a customer with a first name
only (no last name, phone or email) is proven to be accepted by
`customerCreate` in the Shopify **test** store. This is not tested by this
source or its CI.

## Settings (`config/member_sync.example.json`)

Unknown keys, at the top level or nested, are refused.

| Key | Default | Notes |
|---|---|---|
| `autocount_book` | required | `AED_TESTINGAPI` only; `AED_XBOUNDARIES` refused |
| `state_root` | required | Private root (same custody check as the Core); ledger `member_sync.sqlite3`, `claims\` |
| `shopify` | required | Store, API version, auth env names, token cache (as the poller) |
| `shopify_cutover_utc` | required | Registrations before it are skipped (historic members are phase 2) |
| `ac2_create_enabled` | `false` | Shopify -> AC2 SaveMember |
| `shopify_create_enabled` | `false` | AC2 -> Shopify `customerCreate`; Q3 gate above. The app also needs `write_customers` (the session check requires exactly `read_customers` while off, and `read_customers` + `write_customers` while on) |
| `max_writes_per_run` | `5` | 0-5, both directions together |
| `birthday_month_metafield` | `custom.birthday_month`, `number_integer` | Owner: placeholder until Shopify's developer confirms it |
| `ac2_member_no_metafield` | `custom.ac2_member_no`, `single_line_text_field` | Sent on every create from AC2 (Owner round 2, Q2) |
| `shopify_create_tags` | `["ac2-member"]` | Owner-confirmed; `member-mg` is refused (it would loop) |
| `ac2_credential_target` | `null` | Credential Manager target of the AC2 automation user; `null` keeps the attended owner-console path |
| `changelog_xml_column` | `"Content"` | VM fact r5-1. Set (the default), AC2 -> Shopify reads ChangeLog and writes ledger links on every run; `null` turns that direction off. Shopify creates still need `shopify_create_enabled` |
| `changelog_batch` | `200` | 1-500 new rows per run |
| `changelog_overlap` | `50` | 0-200 (below `changelog_batch`); IDs re-read below the watermark; 0 = no overlap |
| `max_shopify_customers_per_run` | `20` | 1-100 |
| `shopify_max_pages` | `10` | 1-40 |
| `http_timeout_seconds` | `30` | 5-60 |

## What stays switched off

- Both create switches (`ac2_create_enabled`, `shopify_create_enabled`).
- The Shopify app has no write scope; `customerCreate` is refused by the
  client unless the switch is on and the token carries `write_customers`.
- No scheduled task, no production book, no n8n.

## Write safety

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
  stored in plain text.
- `NOT_WRITTEN` (proven) is retried on a later run. An uncertain outcome
  (timeout, crash, Shopify transport or 5xx error) is never re-sent. A later
  run only looks it up:
  - AC2 create: the member found under that MemberNo is linked only when
    the digest of its Name and MobilePhone, normalised the same way, equals
    the intent digest (G2 B2). A different person holding the number
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
- L5: while writes are off, the pending queues stall: Shopify -> AC2 stops
  recording new registrations once `max_shopify_customers_per_run` (20)
  are pending, and AC2 -> Shopify re-reads the same first 20 pending
  members each run. Dry-run and UAT counts therefore under-report.
- A purely numeric MemberNo of 9-15 digits that is not a phone (and does not
  start with `0` or `65`) is read as a phone when MobilePhone is missing.
- A number without `+` that is not a Singapore number is sent to Shopify as
  `+` and its digits, which Shopify reads as a country code (Owner to
  decide; `shopify_create_enabled` stays off under the Q3 gate).

## AC2 automation user and credential (runbook; not yet done)

Creating the user is a later, Owner-approved VM step.

1. In AutoCount (test book first), create a dedicated user, for example
   `xbsync01`, with only the rights to view and create members. Never use
   `sa` (the loader refuses it), never reuse the read-only SQL login, never
   rotate an existing credential. AutoCount rights do not limit the member
   API (VM fact r5-6); they only keep the user out of other screens.
2. Signed in as the Windows account that will run the sync (the same account
   that owns the state root), store the credential in Windows Credential
   Manager as a generic credential:

   ```bat
   cmdkey /generic:XB-AC2-Automation /user:xbsync01 /pass
   ```

   `cmdkey` prompts for the password, so it is not in the command history.
3. Set `ac2_credential_target` to `XB-AC2-Automation` and check it:

   ```powershell
   python directlocal\xb_member_sync.py check-credential --config <config>
   ```

   The output shows only the last three characters of the user ID and
   `secret_present: true`.

How it is read: the loader (`load_ac2_credential`) calls `CredReadW` in
memory, refuses `sa`, and passes the user ID and password only in the bridge
child's environment (`XB_AC2_USER_ID`, `XB_AC2_PASSWORD_ENV_VAR` ->
`XB_AC2_SYNC_PASSWORD`). This follows the EnergyGrid pattern (secret read in
memory, injected only into the child, never printed), but from Credential
Manager as the Owner decided; EnergyGrid itself uses a DPAPI CLIXML file.
The attended `ac2_directlocal_owner_console.ps1` path is unchanged.

## Commands

```powershell
python directlocal\xb_member_sync.py check-config --config <config>
python directlocal\xb_member_sync.py init --config <config>
python directlocal\xb_member_sync.py run --config <config> [--direction shopify_to_ac2|ac2_to_shopify]
python directlocal\xb_member_sync.py status --config <config>
```

`run` exits 0 when completed, 3 when completed with alerts (see the
summary's `alerts`), 2 when refused.

## Ledger

Schema v2 (2026-10-22): `writes.intent_digest` and the `changelog_seen`
table were added. A v1 ledger is migrated on open in one transaction
(additive only; existing rows keep a NULL digest and are never linked on
resolution).

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
- The 2,744-member rehearsal / historic backfill.
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

## Open items

1. Name rule: the stated subset rule and the implemented first-name rule
   differ on "Mary Tan" (links today) and on AC2 "Ann Lee" / "Mary Lee"
   against Shopify "MARY ANN LEE" (separate today). Owner to decide; no
   logic change was made.
2. SQL login of an AutoCount API session: unknown (the book registry stores
   `sa`). Needs an Owner-attended check on the test book.
3. The `custom.ac2_member_no` metafield as a customer metafield definition in
   Shopify admin, so staff can see it.
4. Shopify's customer search syntax for a phone (`phone:+<digits>`), checked
   in the test-store UAT (results are verified by B1 either way).
