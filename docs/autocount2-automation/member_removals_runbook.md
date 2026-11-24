# #268 slice E: member removals in AutoCount (expire -> `GUEST`, refund revoke) and undo

Tool: `directlocal/xb_member_removals.py`, its AutoCount child `scripts/ac2_member_removals_bridge.ps1` + `scripts/ac2_member_removals.ps1` (profiles `benefit_x1` and `benefit_x1r`), the task installer `scripts/install_member_removals_task.ps1`, configs `config/member_removals.example.json` (test book) and `config/member_removals.live.example.json`. Tests: `tests/test_member_removals.py`, `tests/test_member_removals_ps.py`.

**Status: built, off.** Nothing runs on merge. Every write switch defaults to `false` and observe is the default. External effects: none until the VM steps in section 6, each with its own owner OK.

## 1. What it does, in plain words

One move only: an AutoCount member goes from `MEMBER` to `GUEST`. Only `MemberType` changes. `ExpiryDate` is never written and must read back unchanged, so an undo puts back exactly one column.

**Expire.** A member is due when all of these hold:

- type is exactly `MEMBER` and the member is active (`IsActive` = `T`);
- `ExpiryDate` is readable and on or after 2020-01-01 (NULL, 1753-01-01, 2000-01-01 and other placeholders are never read as expired: one `expiry_unreadable` alert per member);
- the expiry's **date is before today** in Singapore. A member is valid through the expiry day and becomes `GUEST` from the next day, the same rule as the tags tool. "Today" is the AutoCount server's date, and the server clock must agree with the VM clock (Singapore time) within 10 minutes, else the run stops with `clock_mismatch` and writes nothing;
- the MemberNo fits the tool's pattern (else one `member_no_unsupported` alert, never read or written);
- the member core has no open write and no pending receipt (`ready` or `waiting_member`) for that member: a renewal may be on its way (`core_write_open` / `renewal_pending`, skipped this run).

Who made the member does not matter: an expired `MEMBER` is expired (handover rule 9, "after 2 years back to Registered").

**Revoke.** Only for a member this programme's member core made `MEMBER` itself:

- the member core receipt is `applied` with code `upgrade`, and its write is `DONE`, kind `upgrade`, from `GUEST`, same MemberNo, with the receipt's basis expiry;
- the member core flagged it `refund_on_basis` (the order was refunded or cancelled after the upgrade);
- the refund is a **positive amount fact**: money went back. `REFUNDED`, or `PARTIALLY_REFUNDED` with the measured amount below the member core's threshold (S$300). Anything else is `revoke_needs_person` (one alert, no write): a refund by money that leaves the order at S$300 or more by units, an order still `PAID` (units removed or the order cancelled with no money refunded yet), or a cancellation that is not `REFUNDED`. The member core reads the order again on every run, so a refund issued later still revokes;
- no other receipt of the member still qualifies (`revoke_other_basis`, counted);
- the link the upgrade used is still active (`revoke_link_voided` otherwise);
- **nobody touched the member since the upgrade**: same `LastModified` and same `ExpiryDate` as the upgrade left. A staff renewal in store is invisible to the tool, so this rule is what keeps it safe (`revoke_member_changed`, one alert).

A refund on an `extend` (the member was already `MEMBER`) is only an alert (`refund_on_extend`): moving an expiry back stays parked. After a revoke the member is `GUEST` with the (future) `ExpiryDate` the upgrade gave. Staff line: "a member shown as `GUEST` with a future expiry date was revoked after a refund: treat as `GUEST`". A later S$300 order upgrades the member again through the member core.

Residual gap (Controller, 22 Nov 2026): a refund made more than 60 days after the order is never read by the member core, so revoke cannot catch it. Staff handle those by hand.

**Writes.** One `SaveMember` per member through the child: `INTENT` before the child; `DONE`, `SKIPPED`, `NOT_WRITTEN`, `UNCERTAIN` or `HELD` after. Just before `SaveMember` the child re-reads the member and skips (`type_changed_since_read`, `type_not_in_scope_now`, `changed_since_read`, `not_due_now`) when the type, `LastModified` or `ExpiryDate` is not what the parent read (staff win; tried again next run). The read-back compares every other column by salted digest, `ExpiryDate` included. `UNCERTAIN` is settled only by read-back (`resolve`), never re-sent. `HELD` latches the tool until `release-held`.

**Put back.** A member this tool expired who is `MEMBER` again with the same `ExpiryDate` (a `restore`, or a hand undo) is never expired again until the `ExpiryDate` changes (`put_back_kept`).

## 2. What it never does

- Any Shopify call. The config refuses a `shopify` block; the module contains no Shopify client and no query text (pinned by tests). The tags tool already drops `member-qualified` on the positive facts (`GUEST`, or expiry before today), so online the benefit ends whatever this tool does.
- Any `ExpiryDate` write, any source type but `MEMBER`, any target type but `GUEST` (restore: the exact reverse, bound to one `DONE` removal).
- Inactive members, members with an unreadable or placeholder expiry, MemberNos outside the pattern.
- A revoke for a member the member core did not upgrade itself (staff-made, type-move-made or #155-created members) or only extended.
- Writes to the member core ledger or the #155 ledger: both are opened read-only (`mode=ro`, `query_only`), never copied. The member core ledger must be version 2 (`member_core_ledger_version_unknown` otherwise). The member core, tags tool, type move and #155 sync are unchanged.
- Any customer message.

### One move only, enforced at the write boundary

`benefit_x1` (`removal-save`) refuses, before any session or claim: a source type other than `MEMBER`; a target other than `GUEST`; an expire whose expected expiry is not before the run day or is before 2020-01-01 (`removal_not_due`); a revoke whose basis expiry is not the expected expiry. `benefit_x1r` (`removal-restore`) refuses anything but `GUEST` -> `MEMBER` with a removal id, and skips (`changed_since_removal`) unless the member is still exactly as the removal left it. The same rules are repeated in Python before the `INTENT` row and as ledger `CHECK`s and triggers. The book guard admits the live book for `removal-scan`, `removal-member` and `removal-save` only with `benefit_x1`, and for `removal-restore` only with `benefit_x1r` (ordinal compares; no other operation takes either profile).

## 3. Switches, caps and settings

| Key | Default | Bounds | Meaning |
|---|---|---|---|
| `expire_write_enabled` | `false` | - | `expire --apply` (and `scheduled --apply`) writes expires only when true |
| `revoke_write_enabled` | `false` | - | The same for revokes |
| `restore_enabled` | `false` | - | `restore --apply` refuses `restore_disabled` otherwise |
| `max_expires_per_run` | `3` | 1-50 | Expire saves per run |
| `max_revokes_per_run` | `3` | 1-20 | Revoke saves per run |
| `max_removals_per_day` | `20` | 1-500 | Both kinds, per Singapore day, counted from the ledger |
| `max_member_reads_per_run` | `30` | 1-100 | AutoCount single reads (`member_reads_capped` alert) |
| `surge_stop_count` | `100` | 10-1000 | More due expires than this: observe only, `removal_surge` (once a day) |
| `surge_ack_until` | `null` | a date | Owner-set end day of an approved batch plan; while it is today or later the surge stop is lifted (caps still apply) |
| `write_deadline_seconds` | `120` | 30-600 | Child self-kill deadline |
| `member_core_config` | required | path | The member core's own config file: its state root, #155 root, store binding and threshold are used read-only (no copy of the threshold exists) |
| `type_move_state_root` | `null` | path | Required on the live book once any switch is on: every write takes the type move lock, shared with the member core's `apply` and the type move |
| `ac2_credential_target` / `ac2_login_mode` | live: `XB-AC2-MemberSync` / `owner_shared` | - | As the member core; AutoCount user ID `sa` is refused; the `sa` login is never used or changed |

Fixed (not configurable): the expiry floor 2020-01-01 and the 10-minute clock tolerance (both the tags tool's constants, pinned by tests). `check-config` prints a rule fingerprint (rule versions, floor, the member core's threshold); a change refuses runs with `removals_rule_settings_changed`. The way on is a new state root and `init` (`init` on the old ledger keeps the old fingerprint). A new ledger has no history: a member that a person put back after an expire under the old ledger (`put_back_kept`) is due again under the new one, so before switching ledgers, run `undo-list --show` on the old one and re-check those members by hand. Revokes stay safe (a restored member has a new `LastModified`, so V6 alerts).

`STOP` file in the state root: no write after the current member.

**Busy is not an error.** When the type move lock is held (the member core's `apply`, or a type move command), `--apply` runs record `writer_busy`, write nothing and exit 0; only the second busy run in a row alerts (`writer_busy_again`).

## 4. Commands

```
python directlocal\xb_member_removals.py check-config --config <config>   # no login, no ledger
python directlocal\xb_member_removals.py init         --config <config>   # creates member_removals.sqlite3 in the tool's own state root
python directlocal\xb_member_removals.py expire       --config <config> [--apply]
python directlocal\xb_member_removals.py revoke       --config <config> [--apply]
python directlocal\xb_member_removals.py scheduled    --config <config> [--apply]   # the task: revoke, then expire
python directlocal\xb_member_removals.py resolve      --config <config>             # UNCERTAIN rows: read-back only
python directlocal\xb_member_removals.py release-held --config <config> --removal-ref r-<20 hex> --member-last3 <3> --finding as_intended|not_as_intended [--apply]
python directlocal\xb_member_removals.py undo-list    --config <config> [--since YYYY-MM-DD] [--kind expire|revoke] [--show]
python directlocal\xb_member_removals.py restore      --config <config> (--removal-ref r-<20 hex> --member-last3 <3> | --run <run_id> --max N) [--apply]
python directlocal\xb_member_removals.py status       --config <config>
python directlocal\xb_member_removals.py digest       --config <config> --hours 24   # read only
```

- Without `--apply` every command is observe or a dry run. `--apply` on `expire`, `revoke` or `scheduled` is refused before any login (`removals_write_disabled`) unless the kind's switch is on; in `scheduled --apply` each kind writes only when its own switch is on (the other observes).
- Exit codes: 0 ok, 3 completed with alerts, 2 refused before any write, or a run stopped by an AutoCount bridge failure. In the second case removals already `DONE` earlier in that run stay recorded: check `status` and `undo-list`.
- Output: codes, counts, keyed refs (`r-` removal, `m-` member, `o-` order) and MemberNos masked to the last 3 characters. **`undo-list --show`** is the one exception: plain text on the VM screen with the full MemberNo, kind, day and the `ExpiryDate` kept, for a hand undo. Never paste it into email, chat, issues or logs.

## 5. Alerts and who acts (hands-off rule)

Every alert is "no write, logged". It reaches people only through the daily summary as a WARN line ("forward to Claude"), never as a decision email to the owner (owner, 21 Nov 2026). The summary block for this tool is **slice E3** (not built yet: summary script, n8n workflow render, a live re-import with its own OK). **E3 must be on before any write switch** (step X3 before X4).

| Code | Meaning | Action |
|---|---|---|
| `expiry_unreadable`, `member_no_unsupported`, `active_flag_unexpected` | A due-looking `MEMBER` the tool will not touch | Staff fix the record or set the type by hand |
| `revoke_needs_person`, `revoke_member_changed`, `revoke_link_voided`, `refund_on_extend` | A refund the tool will not revoke | A person checks the order and the member; hand edit if due |
| `removal_surge` | More due expires than the surge stop | Owner approves a batch plan (section 7) |
| `clock_mismatch` | VM and AutoCount clocks over 10 minutes apart | Fix the clock; nothing was written |
| `write_uncertain` / `write_held` | An unclear write; the tool latches | `resolve` after the deadline / `release-held` after a look |
| `writer_busy_again`, `member_reads_capped` | Capacity | Usually clears next run |
| `removal_held_released` | A member whose removal was `HELD` and then released (`release-held`) is skipped for good by the tool | Staff set the type by hand if still due |

## 6. Live steps (each its own owner OK; one VM sitting at a time; never with a tags, member core, index or backfill step)

| Step | What | Owner OK words | Pass bar | Rollback |
|---|---|---|---|---|
| F-EXP | Read-only VM counts: `MEMBER`s by `ExpiryDate` month Nov 2026-Mar 2027, NULL / before 2020, `refund_on_basis` count in the member core digest (counts only) | none (read-only) | Counts recorded | n/a |
| X0 | Owner adds fake test members in the **test book** `AED_TESTINGAPI`: `MEMBER` expired, expiring today, future, NULL, 2000-01-01, inactive expired; `GUEST` and `Default` expired | "REMOVALS TEST MEMBERS" | Present as listed | Delete them by hand |
| X1 | Test book rehearsal: `init`, `expire` observe, then `--apply` with cap 2, kill one save mid-call, `resolve` after the deadline, a staff edit between read and save, `restore` one, `undo-list --show` | "REMOVALS REHEARSAL" | Only expired active `MEMBER`s moved; expiry unchanged; NULL/2000/inactive untouched; the killed save resolved without a second save; restore exact | Test book only |
| X2 | Live install in **observe**: clone at the merge commit, live config (switches off), `check-config`, `init`, installer dry run, `-Register -Mode observe`, one `scheduled` by hand | "REMOVALS INSTALL" | exit 0/3; `would_expire` matches F-EXP's past-due count; `would_revoke` matches the `refund_on_basis` upgrades | Disable the task; remove the new state root |
| X3 | E3 summary block + health line on | "SUMMARY REMOVALS ON", "HEALTH REMOVALS ON" + the n8n re-import OK | Block shows CLEAN/WARN as expected | Settings key back to off |
| X4 | **Expire pilot**: `expire_write_enabled` true, cap 3, reinstall `-Mode apply`; check each in AutoCount (type `GUEST`, expiry unchanged) and at a till | "REMOVALS EXPIRE PILOT" | 3 `DONE`, 0 `UNCERTAIN`, 0 `HELD` | `restore --run <run_id> --max 3` (own OK) or by hand; switch off |
| X5 | **Revoke pilot** with the named test person: S$300+ order -> member core upgrade -> full refund -> `refund_on_basis` -> revoke on with cap 1 | "REMOVALS REVOKE PILOT" (+ the owner's purchase and refund OK, asked mid-December) | Test member `GUEST`, expiry unchanged; tag gone next tags run; no other member touched | `restore --removal-ref <r-ref>` or by hand |
| X6 | Normal caps: expire 10/run, revoke 3/run, 20/day | "REMOVALS NORMAL CAP" | 3 clean days in the email | Back to pilot caps, or switches off |

Installer: task `\XB\XB-MemberRemovals`, hourly at :40 (away from the BF2 :57-:03 window and the tags :11 runs), `IgnoreNew`, 10-minute limit, `LogonType Password`, dry run unless `-Register`; `-Mode apply` is refused unless a write switch is on.

**Rollback, in order of reach:** (1) `STOP` file or `Disable-ScheduledTask -TaskPath \XB\ -TaskName XB-MemberRemovals`; (2) switches false; (3) `restore` from the undo list (own OK, `restore_enabled`) or a hand edit (`MEMBER` again; nothing else to put back); (4) the ledger stays as the record while any removal can be restored.

## 7. The 2028 import-dated members

About 2,700 moved `MEMBER`s carry `ExpiryDate` 2028-06-30 (import date + 24 months). Under the owner's 6 Nov 2026 policy they stay `MEMBER` until that date; this tool does nothing to them before 1 Jul 2028 (not due) and never revokes them (the member core did not upgrade them). On 1 Jul 2028 they all become due on one day: the count is above the surge stop, so the tool only observes and alerts `removal_surge`. The owner then approves a batch plan: set `surge_ack_until` and raise `max_expires_per_run` (hard max 50) with "REMOVALS EXPIRE BATCH 2028". The tags tool drops their online tag on 1 Jul by itself.

## 8. Notes against the G0 design (for the G2 reviewer)

- `member_core_config` (a path to the member core's own config) replaces the design's `member_core_state_root` + `qualify_threshold_cents` pair: the threshold is read from the member core's config, so the two can never disagree, and the store binding check uses the same values.
- The scan uses one new pinned SQL text (`expiring_members_page`); the server time comes from the book guard's existing `Get-XbDlPosSyncState` read (its pinned texts only, as the type move's counts read), not a second new text. `removal-member` also returns the server time, so a revoke-only run is clock-checked too.
- The surge stop counts due expires and turns expire writes off for that run; revokes are bounded by their own per-run cap and the shared per-day cap.
- Added beyond the design: `put_back_kept` (a member put back after an expire is not expired again until its date changes), and `active_flag_unexpected` for an active flag that is neither `T` nor `F`.
- G2 fix round 1 (24 Nov 2026): V3 needs money back (`REFUNDED`, or `PARTIALLY_REFUNDED` below the threshold); revoke checks its per-run cap before the member read; a revoke sends the upgrade's basis expiry as `basis_expiry_local`, so the child's basis compare is a real second check; a released `HELD` removal alerts once (`removal_held_released`).
- Not dependent on #155 BF2 or S6 (cancelled 22 Nov 2026): revoke reads the member core's own `sync155`/`claim` links only.
