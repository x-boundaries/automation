# Member Type Move: Default -> MEMBER / GUEST (#268)

Status (2026-11-06): G1 build, draft PR. Source and offline tests only.
Nothing here has run against any AutoCount book. Nothing runs on merge: the
tool is attended, has no scheduled task, and every write switch is off by
default. **Every live step below (L2-L5) needs its own owner OK and is not
done.** L6 is a read-only `reconcile` and needs no separate OK (as in G0).

Design: G0 on #268 (comment 6659862935, option b), with the owner
amendments of 6 Nov 2026 (two targets fixed by a pinned rule, re-check at
save, only `MemberType` changes, counts by target, mixed batches, rollback
for both targets, four L2 fixture kinds). L0 (read-only VM checks) is done:
nothing in either book is tied to type `Default`, and a member edit writes
ChangeLog `ChangeType` `U` (#268 comment 6661330151).

Code:

- `directlocal/xb_member_type_move.py`: CLI, config, private ledger, engine.
- `scripts/ac2_member_type_move_bridge.ps1`: the AutoCount child, one
  operation per process (its own entry point; the #155 bridge
  `scripts/ac2_directlocal_member_bridge.ps1` is not used or changed).
- `scripts/ac2_member_type_move.ps1`: the pinned reads, the rule, the
  per-member pre-check, the single `SaveMember` and the read-back.
- `scripts/ac2_directlocal_book_guard.ps1`: one addition. The four
  `type-move-*` operations are admitted on `AED_XBOUNDARIES` only with payload
  profile `type_move_p1`. The #155 sync's operations and profile are
  unchanged.
- Tests: `tests/test_member_type_move.py` (Python),
  `tests/test_member_type_move_ps.py` (real PowerShell, fake AutoCount host).
- Example configs: `config/member_type_move.example.json` (test book),
  `config/member_type_move.live.example.json` (live book). Both have every
  switch off.

## The pinned rule

`from_type` is exactly `Default`. Each member's target is decided once, at
`snapshot`, from AutoCount's own fields, and stored in the private ledger:

| Member at snapshot time | Target |
|---|---|
| Active (`IsActive` `T`) and `ExpiryDate` later than the snapshot time | `MEMBER` |
| Active, `ExpiryDate` at or before the snapshot time | `GUEST` |
| Active, `ExpiryDate` NULL | `GUEST` |
| Inactive (`IsActive` `F`) | `GUEST` |
| Active flag neither `T` nor `F`, active with an unreadable expiry, `LastModified` NULL or unreadable, or a MemberNo outside `[A-Za-z0-9][A-Za-z0-9._/-]{0,19}` | Held at snapshot. Never moved by the tool; listed for the owner |

The snapshot time is AutoCount's server local time (`GETDATE()`) when the
snapshot starts. No third target exists: the Python constants, the ledger
`CHECK`s and the PowerShell child each pin `MEMBER` and `GUEST`. `Default`
the type is never edited or deleted.

## Safety design

| Need | How |
|---|---|
| Where it runs | The AC2 VM only, attended, from its own clone at the merged SHA (the live #155 sync clone stays at its pin). Its own state root (example `C:\XB\xb268-type-move-live`) and config. No scheduled task. |
| Refuses #155 state | The #155 config schema is refused (`type_move_config_is_member_sync`); a state root holding the sync ledger or the Core ledger is refused (`type_move_state_root_shared`); a ledger file that is not this tool's is refused (`type_move_ledger_not_recognised`). The sync refuses this tool's config too. |
| Book admission | The child's four operations reach `AED_XBOUNDARIES` only with profile `type_move_p1`, then the same proofs as the sync: host pin, assembly pins, book identity, vendor telemetry off, empty outbound queues. |
| Reads | Two pinned SQL texts, each one line of printable ASCII, behind the book guard's deny check: one page (500) of members on `Default`, `MEMBER` or `GUEST` (MemberNo, active flag, expiry, LastModified), and member counts per type and active flag. The rest reuse the guard's pinned reads (type exists, POS sync state). Python builds no AutoCount SQL. |
| Dry run | `plan`: counts by target, split by active flag, held rows by code. No ledger, no writes. |
| Snapshot | `snapshot`: once per ledger. All three types must exist. Stores MemberNo, active flag, expiry, LastModified and target per `Default` member. |
| Re-check at save | The child re-reads the member and saves only if the type is still the expected one, `LastModified` equals the stored value and (move) the active flag and expiry still give the stored target. Otherwise it skips with `not_default_now`, `changed_since_snapshot`, `target_changed` or `member_not_found`, before the claim file (proven not written). A skipped member is not retried. |
| Only `MemberType` | The child assigns one column. The read-back compares every other column of the master row by salted digest (`LastModified` and `LastModifiedUserID` excepted, as the save itself sets them). A difference stops the run (`readback_fields_changed`) and names the columns, never values. |
| Write record | Per member: ledger `INTENT` before the child starts; then `DONE`, `SKIPPED`, `NOT_WRITTEN`, `UNCERTAIN` or `HELD`. One child and one `SaveMember` per member, never retried in the same run. The child holds a fence id bound to the payload digest, a deadline watchdog and an exclusive claim file created just before the save. |
| Caps | `max_moves_per_run` (default 10, hard max 500) counts saves; `--max` may lower it. `max_move_runs_per_day` (default 2) counts `move` runs in a rolling 24 hours from each run's start, not per calendar evening. A run that stops at its first member still counts. So the next evening's first batch is refused until 24 h after the previous evening's first run. Batches may mix targets. |
| Stop | Any refusal, exception, read-back mismatch or unclear outcome ends the run (exit 3). A `STOP` file in the state root is checked before each member. `type_move_enabled` false refuses `move` before any login. |
| Resume | `move` and `rollback` refuse while any `INTENT` or `UNCERTAIN` row exists. `resolve` settles them by read-back only, and only once no save child can still be live: each row records the child's deadline (the same instant the child gets as `-WriteDeadlineUtc`; it kills itself then) and the child's PID. Until deadline + 15 s (the DirectLocal writer grace) has passed, or while the recorded PID still runs, `resolve` refuses with `writer_may_be_live` and changes nothing. A stopped parent does not stop its child: the child `powershell.exe` keeps running and may still save. After stopping the parent, wait at least `write_deadline_seconds` + 15 s (default 135 s), or confirm in Task Manager that no child `powershell.exe` of the tool remains, then run `resolve`. Outcomes: no claim file -> `NOT_WRITTEN` (eligible again); on the target with the other columns unchanged -> `DONE`; still on the source type with the old `LastModified` -> `NOT_WRITTEN`; anything else -> `HELD`. |
| `HELD` latch | Any `HELD` row (read-back mismatch, unclear resolution) makes `move` refuse with `type_move_held_rows_present`. The owner decides before anything continues. `rollback` is not blocked, so the moves already done can still be undone. To continue after the owner's decision, start a second pass (below). |
| Rollback | `rollback --max N` (switch `rollback_enabled`, own owner OK): only members with a ledger `DONE` move, latest first, saved back to `Default` only if still on the type the tool set and `LastModified` equals the tool's post-save value. Otherwise skipped (`not_moved_type_now`, `changed_since_move`). Works for both targets. |
| Final check | `reconcile` (read-only): reads all three types, then lists snapshot members still pending, skipped (by code), held at snapshot, moved members no longer on the type the tool set, members changed after the tool's save, rolled-back members, and new `Default` members keyed after the snapshot. Also: net moves by target split by active flag, type counts before and now, ledger states, and outlets whose last POS sync finished before the last batch end. |
| Output | Counts, codes and column names. MemberNos appear only masked to the last 3 characters, only with `--show-masked`, on the VM console. Never paste unmasked output anywhere. |

## Commands

```text
python directlocal\xb_member_type_move.py <command> --config <private config> [--max N] [--show-masked]
```

| Command | AutoCount | Ledger | Exit |
|---|---|---|---|
| `check-config` | none | none | 0 / 2 |
| `init` | none | creates | 0 / 2 |
| `status` | none | read | 0 / 2 |
| `plan` | read | none | 0 / 2 |
| `snapshot` | read | writes the snapshot | 0 / 2 / 3 |
| `move [--max N]` | read + `SaveMember` | writes | 0 / 2 / 3 |
| `resolve` | read | writes outcomes | 0 / 2 / 3 |
| `rollback --max N` | read + `SaveMember` | writes | 0 / 2 / 3 |
| `reconcile` | read | read | 0 / 2 / 3 |

Exit 2 is a refusal before any write (`{"status":"refused","code":...}`).
Exit 3 means the command finished but an operator must look (`alerts`).

## Setup on the VM (with the L2 or L3 OK)

1. Clone the repository to a new folder at the merged SHA. Do not touch the
   #155 sync clone.
2. Create a private state root with the existing helper:
   `python directlocal\xb_directlocal.py prepare-state-root --state-root <root>`.
   Use a new root per book (test and live), never the sync's root.
3. Copy the example config for the book into the state root's parent (not
   the repository), set `state_root`, leave every switch off. The login is the
   #155 sync's own Credential Manager entry, mode `owner_shared` (live:
   `XB-AC2-MemberSync`; test: `XB-AC2-MemberSync-Test`). No new secret.
4. `check-config`, then `init`.

## Pausing the #155 sync per live window

G0 Q1 (recommended yes): the sync does not need the pause (it reads only
ChangeLog `I` rows; L0 confirmed an edit writes `U`), but the pause keeps one
extra AutoCount session at a time and the window clean for the `I` check.

Never unregister either task: disable and enable only.

1. Before the window, in an elevated Windows PowerShell on the VM, disable both
   tasks:

   ```powershell
   Disable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-MemberSync'
   Disable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-MemberSync-Probe'
   ```

   The probe line applies only if the probe task (#155 P2) is registered.
2. Check that neither task is still running:

   ```powershell
   Get-ScheduledTask -TaskPath '\XB\' | Select-Object TaskName, State
   ```

   Both must show `Disabled`, not `Running`. If one shows `Running`, wait for
   it to finish and check again. Do not start the step until neither runs.
3. Run the step.
4. After the batch read-back, enable both tasks again:

   ```powershell
   Enable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-MemberSync'
   Enable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-MemberSync-Probe'
   ```

   The sync's next run should return 0 or 3 with no new alert. Shopify -> AC2
   resumes from its watermark; sign-up links wait up to about an hour.

### Also pause the member benefits task (step S1 of `member_benefits_runbook.md`)

Once the member benefits task `XB-MemberBenefits` is installed (its step B1
on), pause it too around **every** type move command (`init`, `snapshot`,
`move`, `resolve`, `rollback`, `reconcile`, `status`), so no benefits run adds
AutoCount sessions during a batch. Its `apply` also takes this tool's ledger
lock, so a type move command refuses `ledger_locked_by_another_owner` while a
benefits `apply` runs.

1. Disable it:

   ```powershell
   Disable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-MemberBenefits'
   ```

2. `Get-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-MemberBenefits'` must
   show `State` `Disabled`, not `Running`. If it shows `Running`, wait for the
   run to end (at most 10 minutes) and check again. **Stop if it stays
   `Running`** and report; do not start the type move command.
3. Run the type move command and its read-back.
4. Enable it again after the read-back:

   ```powershell
   Enable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-MemberBenefits'
   ```

## Snapshot time and a second pass

- The snapshot time is fixed for the whole ladder. A member whose `ExpiryDate`
  passes between the snapshot and their batch still moves to `MEMBER`. This is
  the owner rule as written: the target is decided at snapshot.
- Staff edits after the snapshot change `LastModified`, so those members are
  skipped with `changed_since_snapshot`. Changes to the active flag or expiry
  are skipped with `target_changed`. Neither is retried in the same ledger.
- Second pass over skipped members (and to continue after a `HELD` stop, once
  the owner has decided):
  1. Get an owner OK, as for L3 ("LIVE TYPE-MOVE DRY RUN").
  2. Keep the old state root unchanged. Its `DONE` moves stay rollback-able
     from there.
  3. Create a new state root (`prepare-state-root`) and a new config pointing
     at it.
  4. Run `init`, `plan`, then `snapshot`. The new snapshot sees only members
     still on `Default`. Members the first pass moved are no longer
     candidates.
  5. Continue as L4/L5, each batch with its own owner OK.

## Rollout ladder (L2-L6)

Every row except L6 needs its own owner OK and is **not done**. L6 is a
read-only `reconcile` and needs no separate OK, as in G0 (Controller
decision). Allow one owner OK per evening for L5 (G0 Q5). Run after the last
outlet closes.

| Step | What | Owner OK | Pass bar | Undo |
|---|---|---|---|---|
| L2 Rehearsal on `AED_TESTINGAPI` | Owner adds types `MEMBER` and `GUEST` to the test book by hand first. Seed about 30 fake `Default` members (fake names, example.com emails, fake 9-digit MemberNos) covering all four kinds: active with future expiry, active with past expiry, active with NULL expiry, inactive (and at least one inactive with a future expiry). Test config with `type_move_enabled` true, `rollback_enabled` true, `max_move_runs_per_day` up to 10. Then: `plan`; `snapshot`; `move --max 10`; stop the parent mid-batch (Ctrl+C while a save child runs); run `resolve` at once and see it refuse `writer_may_be_live` with nothing settled; wait at least `write_deadline_seconds` + 15 s (or confirm no child `powershell.exe` of the tool remains), then run `resolve` again; owner edits one member after the snapshot (must be skipped `changed_since_snapshot`) and changes one member's expiry or active flag (must be skipped `target_changed`); `move` the rest; `rollback --max 5`; `reconcile`; `move` again | "TEST REHEARSAL" (test-book writes) | Every moved member reads back on its rule target with the other columns unchanged (no `readback_fields_changed`; if it fires, record the column names: the exclusion list may need a G1 amendment. A `readback_fields_changed` stop naming `LastUpdate` means a G1 amendment, not a retry); each of the four kinds got its rule target; the killed one resolved without a second save; edited ones skipped with their codes; rollback touched exactly 5 and restored `Default` for both targets; re-run moves 0; `member_change_counter` rose by the number of saves; VM read-only check: 0 new ChangeLog Member `I` rows in the window; seconds per member recorded; LastModified matched between the SQL read and `GetMember` for every member (no false `changed_since_snapshot`) | Test book only; `rollback` |
| L3 Live dry run and snapshot | Sync paused. Live config, every switch off. `check-config`, `init`, `plan`, then `snapshot` | "LIVE TYPE-MOVE DRY RUN" | `plan` and `snapshot` counts equal the L0 count minus changes since, split by target and active flag; held rows listed for the owner; 0 writes; owner's own AutoCount session still signed in | Delete the new live state root (no moves yet) |
| L4 Live pilot, 10 members | Sync paused. `type_move_enabled` true, `max_moves_per_run` 10. `move --max 10`, then `reconcile` | "LIVE PILOT 10" | 10 `DONE`, 0 `UNCERTAIN`, 0 `HELD`; the pilot takes the first 10 candidates in MemberNo order, whatever their targets (the tool has no target filter, so the pilot does not promise both targets); owner opens 3 in AutoCount (type as the rule says, nothing else changed); till check after the next outlet cycle (`reconcile` shows no outlet behind the batch end; at one till, look up 3 moved members); VM read-only check: 0 Member `I` rows; sync re-enabled and its next run returns 0 or 3 with no new alert; then 48 h with no staff or till issue | `rollback --max 10` (own OK, `rollback_enabled` true) |
| L5 Remaining members in batches | Per evening: sync paused; `max_moves_per_run` 250; at most 2 `move` runs (`max_move_runs_per_day` 2); `reconcile` after each | "LIVE BATCHES" per evening, naming the count | Per batch: saves = `DONE` rows, 0 `UNCERTAIN`, 0 `HELD`; outlets synced after the batch end; VM read-only check: 0 Member `I` rows; sync re-enabled clean | `rollback --max N` (own OK) |
| L6 Final reconciliation | `reconcile --show-masked` on the VM | none (read-only; Controller decision, as G0) | `complete` true, or every remaining member listed: pending 0; skipped and held members listed for the owner to decide; new `Default` members listed (moved later only with their own OK); ledger kept 90 days for rollback | n/a |

After L6: set `type_move_enabled` and `rollback_enabled` false in the live
config. Keep the state root (private, never in Git) for 90 days.

## What to verify at L2 (assumptions)

- `SaveMember` on a loaded member changes only `MemberType`,
  `LastModified` and `LastModifiedUserID` in the master row. If it changes
  another column, the run stops with `readback_fields_changed` and the column
  name; nothing further is moved.
- `LastModified` read through SQL (`CONVERT(varchar(23), LastModified, 126)`)
  and through `GetMember` give the same value to the millisecond. A mismatch
  shows as `changed_since_snapshot` on members nobody edited (fails safe:
  nothing is written).
- An edit writes ChangeLog `U`, never `I` (L0 confirmed on both books).
- Seconds per member (each member is one child process and one AutoCount
  login, as in the sync).

## Codes

| Code | Meaning | Action |
|---|---|---|
| `not_default_now` | The member is no longer on `Default` | None; listed as skipped |
| `changed_since_snapshot` | `LastModified` differs from the snapshot | Owner decides; a new snapshot needs a new state root |
| `target_changed` | The active flag or expiry now gives the other target | Owner decides |
| `member_not_found` | The member no longer exists | None |
| `not_moved_type_now` / `changed_since_move` | Rollback skips: changed after the tool's save | Owner decides |
| `readback_fields_changed` / `readback_type_mismatch` | Read-back mismatch after a save (`HELD`, run stopped) | Stop the ladder; report the column names. `move` now refuses (`type_move_held_rows_present`) |
| `writer_*`, `save_call_failed`, `fence_already_claimed` | Unclear save outcome (`UNCERTAIN`, run stopped) | `resolve`, once no child can be live |
| `writer_may_be_live` | `resolve` refused: a save child's deadline + 15 s has not passed, or its recorded PID still runs. Nothing was settled | Wait at least `write_deadline_seconds` + 15 s after the parent stopped, or confirm no child `powershell.exe` of the tool remains, then run `resolve` again |
| `type_move_held_rows_present` | `move` refused: a `HELD` row exists | The owner decides; `rollback` still works; continue only by a second pass (new state root and snapshot) |
| `member_type_unknown` | `Default`, `MEMBER` or `GUEST` is missing in the book | Owner adds the type |
| `type_move_disabled` / `type_move_rollback_disabled` | Switch off | As intended |
| `type_move_uncertain_unresolved` | Open `INTENT`/`UNCERTAIN` rows | `resolve` first |
| `type_move_runs_per_day_reached` | `max_move_runs_per_day` `move` runs started in the last rolling 24 hours (counted from each run's start; a run that stopped at its first member still counts) | Wait until 24 h after the earliest of those runs; the next evening's first batch may need to start later than the previous evening's first |
| `stop_file_present` | `STOP` file in the state root | Remove it to continue |

## Differences from the G0 design

- Two targets by the pinned rule (owner amendments, 6 Nov 2026) replace the
  G0 "fixed direction" row (`MEMBER` only, `GUEST` never touched). Members
  already on `MEMBER` or `GUEST` are still never touched: the source type is
  only `Default`.
- The snapshot stores `LastModified`, the active flag and the expiry from one
  pinned SQL read instead of a salted digest of every field. Unchanged other
  fields are proven per member at save time instead: the child digests the
  whole master row before and after the save. A change between snapshot and
  save shows in `LastModified` and is skipped.
- The type move has its own child entry point and its own two pinned SQL
  texts (`Get-XbTmScalar`) instead of new operations and texts in the #155
  bridge and the guard's allowlist, so the #155 bridge and its pinned SQL
  table stay byte-for-byte unchanged. The guard gains only the operation and
  profile admission.
- `max_move_runs_per_day` enforces G0's "at most 2 batches per evening" in
  the tool, not only in this runbook (rolling 24 hours).
- G2 fix round (HARDEN-1 and LATER a, c-h, j):
  - `resolve` holds (`writer_may_be_live`) until each open row's recorded
    child deadline + 15 s has passed and its recorded PID no longer runs.
    This reuses the DirectLocal Core's writer grace and process probe.
  - A `HELD` row latches `move` until the owner decides.
  - Payloads are built from the stored `INTENT` row.
