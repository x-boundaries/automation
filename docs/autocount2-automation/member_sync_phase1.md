# Member Sync Phase 1: Two-Way Create (#155)

Status (2026-10-21): source and offline tests only (build step 2, draft PR).
Nothing here has run against AutoCount or Shopify. Every write switch is off
by default; turning one on, any AutoCount login and any scheduling each need
their own Owner approval. Only the test book `AED_TESTINGAPI` is authorised;
`AED_XBOUNDARIES` is refused in Python and PowerShell.

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
| AC2 -> Shopify | A `dbo.ChangeLog` insert (`TableName = 'Member'`, `ChangeType = 'I'`) after the stored `Id` watermark, for example a member created at the till | A Shopify customer, or a link to an existing one |

Both directions write the mapping (Shopify customer GID <-> MemberNo, each
unique) to the sync ledger. That mapping is the loop prevention: an AC2
insert for a mapped MemberNo, or for a MemberNo with an open write, is never
sent to Shopify; a mapped customer is never sent to AC2.

### Shopify -> AC2 create

| AC2 field | Value |
|---|---|
| MemberNo | The phone in digits: `65` + 8 digits for Singapore; a foreign number keeps its country code; 8 Singapore digits starting 3/6/8/9 without `65` get `65`. `GetNextMemberNo` is not used. |
| MemberType | `REGISTERED` (test book type, upper case) |
| Name | Shopify first name + last name |
| MobilePhone | The same digits, or NULL |
| EmailAddress | Shopify default email, or NULL |
| DOB | `2000-MM-01` from the birthday-month metafield, or NULL |
| RegisterDate | `membership.start_date` value, else the registration date in Singapore time |
| ExpiryDate | NULL (the Shopify expiry metafield is ignored) |
| IsActive / Individual / OpeningPoints | `T` / `T` / 0 |

### Matching rules (no review queue)

| Situation | Action | Counted as |
|---|---|---|
| Phone already in AC2 (MemberNo equals the phone or its 8 digits, MobilePhone normalises to it, or the 8 digits are embedded in a MemberNo) and the first name matches | Link, no create | `phone_and_first_name_match` |
| Same phone on several matching records | Link to the record whose MemberNo is the phone; otherwise skip (never merge) | `phone_duplicate_member_no_exact` / `phone_on_multiple_records` |
| Phone matches, first name differs (people sharing a phone) | Create with `<phone digits><FIRST+LAST letters A-Z>` truncated to 20 characters | `shared_phone_name_fallback` |
| No phone (or an unusable one) | Create with the name letters alone | `no_phone_name_fallback` |
| Fallback MemberNo already exists (trimmed, case-insensitive) | Skip | `shared_phone_member_no_taken` / `no_phone_member_no_taken` |
| Email matches only | Ignored: email never links | - |

First-name match: case- and space-insensitive; the Shopify first name must
equal a run of consecutive words of the AC2 `Name` (so a surname-first AC2
name still matches).

### AC2 -> Shopify create

1. First run: the watermark is set to the current highest ChangeLog `Id`
   (history is not replayed; the backfill is phase 2).
2. Each run reads up to `changelog_batch` inserts after the watermark, in
   `Id` order. Gaps are normal (some vendor procedures delete their own
   ChangeLog rows). The watermark advances row by row in the same transaction
   as the queueing, so a crash or replay re-reads nothing new.
3. A snapshot (MemberNo, first ChangeLog id, salted digest) is kept in the
   ledger; the member's Guid is never used as a key (it can change).
4. Queued members are re-read from AC2 by MemberNo (so the ledger holds no
   names or phones), then looked up in Shopify by phone:
   - one customer with a matching first name: link;
   - one customer with another first name: skip (`shopify_phone_owned_by_other_name`);
   - several customers: skip;
   - none: `customerCreate` with first name = the AC2 `Name`, phone in E.164,
     email if present, the configured tags and the birthday-month metafield.
5. A member without a phone is skipped and counted (there is no safe
   duplicate key; email never links).

Till-created members reach the HQ ChangeLog about 15 minutes after creation
(observed 1-72 minutes), so AC2 -> Shopify lags by at least that much.

## Settings (`config/member_sync.example.json`)

Unknown keys, at the top level or nested, are refused.

| Key | Default | Notes |
|---|---|---|
| `autocount_book` | required | `AED_TESTINGAPI` only; `AED_XBOUNDARIES` refused |
| `state_root` | required | Private root (same custody check as the Core); ledger `member_sync.sqlite3`, `claims\` |
| `shopify` | required | Store, API version, auth env names, token cache (as the poller) |
| `shopify_cutover_utc` | required | Registrations before it are skipped (historic members are phase 2) |
| `ac2_create_enabled` | `false` | Shopify -> AC2 SaveMember |
| `shopify_create_enabled` | `false` | AC2 -> Shopify `customerCreate`; the app also needs `write_customers` (the session check requires exactly `read_customers` while off, and `read_customers` + `write_customers` while on) |
| `max_writes_per_run` | `5` | 0-5, both directions together |
| `birthday_month_metafield` | `custom.birthday_month`, `number_integer` | Open question: the real metafield |
| `shopify_create_tags` | `["ac2-member"]` | Open question; `member-mg` is refused (it would loop) |
| `ac2_credential_target` | `null` | Credential Manager target of the AC2 automation user; `null` keeps the attended owner-console path |
| `changelog_xml_column` | `null` | VM fact still to confirm; `null` disables AC2 -> Shopify |
| `changelog_batch` | `200` | 1-500 |
| `max_shopify_customers_per_run` | `20` | 1-100 |
| `shopify_max_pages` | `10` | 1-40 |
| `http_timeout_seconds` | `30` | 5-60 |

## What stays switched off

- Both create switches (`ac2_create_enabled`, `shopify_create_enabled`).
- The Shopify app has no write scope; `customerCreate` is refused by the
  client unless the switch is on and the token carries `write_customers`.
- No scheduled task, no production book, no n8n.

## Write safety

- Each write is recorded as `INTENT` before it starts. AC2 writes reuse the
  unchanged write-once fence (exclusive claim file, deadline watchdog,
  read-back) and its classification.
- `NOT_WRITTEN` (proven) is retried on a later run. An uncertain outcome
  (timeout, crash, Shopify transport or 5xx error) is never re-sent: later
  runs only look the record up (AC2 by MemberNo, Shopify by phone) and link
  it when found. `customerCreate` is retried only after a GraphQL throttle
  answer (not executed).
- The run summary carries counts and keyed references (`c-`/`m-`) only.

## AC2 automation user and credential (runbook; not yet done)

Creating the user is a later, Owner-approved VM step.

1. In AutoCount (test book first), create a dedicated user, for example
   `xbsync01`, with only the rights to view and create members. Never use
   `sa` (the loader refuses it), never reuse the read-only SQL login, never
   rotate an existing credential.
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
- Production (`AED_XBOUNDARIES`, member type `Default` only today) and its
  POS behaviour; the test book has no POS sync, so till behaviour is never in
  UAT.

## Open questions

1. Fallback MemberNo format: `<phone digits><FIRST+LAST letters A-Z>`,
   truncated to 20 characters (letters only without a phone)? What does the
   shop do today exactly (first name only, separator)?
2. The birthday-month metafield: namespace, key, type and value format
   (number, month name or date).
3. Tags for a customer created from AC2 (default `ac2-member`).
4. AC2 -> Shopify name: the AC2 `Name` goes into Shopify `firstName` whole
   (no split). Acceptable?
5. A Shopify customer found by phone with a different first name is skipped,
   not linked (linking would join two people). Confirm.
6. VM facts: the ChangeLog column holding the insert XML and the XML shape;
   that `DBSetting.ExecuteScalar` accepts `?` parameters; the automation
   user's rights; and Shopify's customer search syntax for a phone
   (`phone:+65...`).
