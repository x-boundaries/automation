# Member DirectLocal UAT Runbook (#155)

Operating procedure for the DirectLocal Core described in
[member_directlocal_contract.md](member_directlocal_contract.md). All commands
run on `DESKTOP-4I042L6` from the repository checkout at the reviewed head.
Credential-bearing steps (C, F and `Reconcile`) run the Owner console, which
refuses to start unless it runs from a protected runtime checkout (G2 A2; see
"Owner console code custody" below). `core` below means:

```powershell
python C:\XB\automation\directlocal\xb_directlocal.py --state-root C:\XB\xb155-directlocal-core-uat
```

Every command prints one JSON line (codes, counts, states, keyed `c-`/`m-`
references). Never paste GIDs, MemberNos, names, phones or emails into GitHub
or chat.

## What this UAT can and cannot prove

| Proof | Book | Possible now |
|---|---|---|
| Form registration discovered as a registration event (including an existing customer) | - | Yes, with Owner read consent (given) |
| Admission, duplicate precheck, exact dates, one create, readback, recovery | `AED_TESTINGAPI` | Yes, after approval E7b |
| Member selectable at a POS terminal, with latency | `AED_XBOUNDARIES` only | **No.** The test book has no live POS sync. Needs E9 |

## Approvals required (none is granted by this runbook)

| Step | Gate | Effect |
|---|---|---|
| A1-A3 | E0, E1 | Local folder and settings only |
| B | E3 | Read-only Shopify customer pages (ids, timestamps, tags, registration metafield id/instant) |
| C | E5 | AutoCount login and read-only checks on `AED_TESTINGAPI` |
| E | E4 | Read-only Shopify profile of the UAT customer (name, phone, email, dates) |
| F | E7b | At most one `GetNextMemberNo` sequence and one `SaveMember` on `AED_TESTINGAPI` for the UAT registration |
| G | E9 | Production book: source enablement, one create, POS observation |
| H | E8 | Standing policy and scheduling |

## A. One-time setup

1. Create the private state root (refuses an existing path):

   ```powershell
   python C:\XB\automation\directlocal\xb_directlocal.py --state-root C:\XB\xb155-directlocal-core-uat prepare-state-root
   ```

   Expect `{"private": true, "status": "prepared"}`.
2. Initialise the connector ledger (development book only):

   ```powershell
   core init-ledger --mode connector --environment development --store-gid gid://shopify/Shop/48759472277 --shop-domain xboundariessg.myshopify.com
   ```

   Expect `"autocount_book": "AED_TESTINGAPI"`.
3. Create the dedicated session folder, for example `C:\XB\xb155-directlocal-session`,
   and write `.claude\settings.json` from `config\directlocal_claude_settings.template.json`
   with `<REPO_ROOT>` = `C:/XB/automation` and `<STATE_ROOT>` = `C:/XB/xb155-directlocal-core-uat`.
   Open that folder as its own Claude Code session. Check: any MCP tool other
   than the pinned `graphql_query` is blocked by the guard.

## B. Baseline (fixes the cutover C)

1. `core begin-baseline`. Record `cutover_at` (UTC).
2. Repeat until it returns `run_pages_complete`:
   - `core next-request` returns `tool_name` and the exact `tool_input`.
   - In the dedicated session, call that tool with that exact input. The hook
     writes the capture to the inbox.
3. `core ingest-run`. Expect `historical` and `transition` counts.
   If it refuses with a capture code, run `core purge-inbox` and repeat step 2
   for the missing page.
4. `core purge-inbox`.

The current store has about 4,300 customers, so this is about 90 calls.

## C. AutoCount read-only check (elevated Owner console)

Launch it as in "Launching an Owner console" below: an elevated Windows
PowerShell, absolute paths only, from the protected runtime.

```powershell
& "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "<PROTECTED_RUNTIME>\scripts\ac2_directlocal_owner_console.ps1" -Python "<PROTECTED_PYTHON>\python.exe" -StateRoot C:\XB\xb155-directlocal-core-uat -Command BookCheck -ExpectedHead <reviewed head>
```

Expect `{"book": "AED_TESTINGAPI", "identity_verified": true, "outbound_quiet": true}`.
`-Command PosSyncStatus` prints the outlets' last sync and the `Member` change counter.

A refusal reads `bridge_read_failed:<code>` for a fixed child code. For example, `autocount_login_rejected` is assigned by elimination: the reviewed AutoCount login failed, yet a separate SQL connection to the pinned book succeeded. A wrong user ID or password is the most likely cause, but the code does not prove it; check the credentials once and do not retry blindly. Otherwise it reads `bridge_read_failed:<shape>:x<exit>:l<lines>`, where every value is computed by the core and none is child text. `x800703e9` or `xc00000fd` means a stack overflow. Before 16 October 2026 that came from the adapter's scriptblock resolve handler recursing after a successful login; `Install-XbDlResolveGuard` in `ac2_directlocal_book_guard.ps1` now prevents it.

## D. Owner registration

The Owner submits the Shopify membership form once (new or existing customer)
and records the submission time (SGT, to the minute). No other step changes
Shopify.

## E. Discovery and admission

1. `core begin-discovery --recent`, then the `next-request` loop as in B2, then `core ingest-run`.
   Expect `discovered: 1`.
2. `core next-request` returns the profile request for the discovered customer
   (`customer_ref`). Call it in the dedicated session.
3. `core process --capture C:\XB\xb155-directlocal-core-uat\inbox\<tool_use_id>.json`.
   Expect `customer_state: ADMITTED`. `WAITING` means the registration is
   still being written: `membership_dates_missing`, or
   `registration_event_pending` for an existing customer whose
   `membership.start_date` metafield does not exist yet (Forms tags
   `member-mg` about three seconds before it writes that metafield). Read the
   profile again. After five reads without it the customer goes to
   `MANUAL_REVIEW` (`preexisting_customer_not_new_signup`; for example a
   `member-mg` tag added by staff with no registration).
4. `core purge-inbox`.

## F. One create on the test book (needs approval E7b)

1. `core pending` shows the GID. `core approve-write --gid <gid> --confirm-suffix <last 6 digits>`.
2. Read a fresh profile (E2), then in the elevated console (launched as in
   "Launching an Owner console" below):

   ```powershell
   & "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "<PROTECTED_RUNTIME>\scripts\ac2_directlocal_owner_console.ps1" -Python "<PROTECTED_PYTHON>\python.exe" -StateRoot C:\XB\xb155-directlocal-core-uat -Command Process -Capture <capture path> -EnableWrite -ExpectedHead <reviewed head>
   ```

   Type `AED_TESTINGAPI` when asked. Expect `fence_state: CREATED_VERIFIED` and a `member_ref`.
3. Check in AutoCount (test book): the member exists once, with Shopify's exact
   start and expiry dates and Opening Points 0.
4. `core uat-report`: keep the JSON as UAT evidence.

Any other outcome stops the UAT: `MANUAL_REVIEW` (duplicate, invalid field),
`WRITE_OUTCOME_UNCERTAIN` (run `Reconcile` with a fresh profile; never write
again), or exit code 75 (HOLD: wait, then `core recover`).

## G. POS selectability (production only, needs E9)

Not possible on `AED_TESTINGAPI`. When E9 is approved, after the create:

1. Record the HQ save time (`uat-report`) and run `PosSyncStatus` until the
   outlet's `last_finish_sync_local` is later than the save.
2. At the outlet terminal, search the member and note the time it is first
   selectable. Record it:

   ```powershell
   core record-pos-visible --member-ref <m-ref> --outlet XB01 --observed-at <UTC ISO>
   ```

3. `uat-report` then shows `registration_to_pos_visible` per outlet.

Observed 2026-10-15 (three cycles): outlet XB01 syncs every 15 minutes, so expect up to about
15 minutes after the save unless the outlet syncs on demand. A created member
that is not selectable after the next completed sync is a P0 failure.

## Acceptance tests

| Requirement | Offline evidence (test) | UAT step |
|---|---|---|
| New eligible member created once | `test_policy_admits_each_registration_once_without_per_member_approval`, `test_happy_path_writes_once_and_records_no_pii`, `test_full_synthetic_write_then_replay_is_refused` | F |
| Existing customer registering after C | `test_existing_customer_registering_after_cutover_is_admitted`, `test_existing_customer_read_before_start_date_waits_then_is_admitted`, `test_staff_tagged_old_customer_without_registration_is_bounded_to_review`, `test_recent_run_finds_existing_customer_registration_and_advances_watermark` | D-E |
| Existing / duplicate member | `test_symmetric_duplicates_and_lookup_failures_go_to_review`, `test_baseline_member_re_registering_stays_excluded` | Optional second registration |
| Ineligible or historical customer | `test_decisions`, `test_registration_before_cutover_and_malformed_registration` | B |
| Exact RegisterDate and ExpiryDate | `test_write_once_claims_saves_once_and_refuses_replay` (bridge readback) | F3 |
| Zero Opening Points | adapter `shopify_m1` readback (`OpeningPoints` at canonical scale) | F3 |
| One write only | `test_outcome_classification`, `test_fence_rows_cannot_be_rewound` | F |
| Crash / uncertain outcome | `CrashRecoveryTests`, `test_crash_after_save_under_policy_never_writes_twice`, `test_hung_writer_is_uncertain_then_reconciled_read_only` | Not induced |
| Disconnected / expired auth / partial result | `test_connector_responses_disconnected_expired_and_partial_fail_closed` | - |
| Unicode | `test_values_autocount_cannot_store_go_to_review_at_admission`, `test_cjk_and_astral_name_round_trips_exactly` | - |
| No PII in logs or GitHub | `assert_no_pii_at_rest`, `test_member_refs_are_keyed_not_enumerable` | Review `uat-report` |
| Repeated run | `test_discovery_reuses_cutover_and_suppresses_replayed_pages`, replay tests | Re-run E1 |
| POS selectable with latency | `test_pos_visibility_is_recorded_and_reported_as_latency` | G (E9) |

## Deployment and rollback

- Deployment is a reviewed checkout plus A1-A3; nothing is installed or
  scheduled. No Shopify configuration changes.
- Stop: close the dedicated session; no process runs between commands.
- Revoke automatic admission: `core revoke-policy`.
- Ledger schema v4 (16 October 2026) records each MemberNo attempt's raw
  generator preview and whether the candidate was derived from it. A v3
  ledger, such as the existing UAT ledger, is refused
  (`ledger_schema_migration_required`) until `core migrate-ledger` is run once.
  That command adds two nullable columns and one ledger event in one
  transaction. Existing rows, bindings and events are unchanged, and earlier
  attempts show provenance as not recorded.
- Rollback of source: check out the previous reviewed head; a v4 ledger is
  refused by older code, so keep it read-only for evidence.
- A created test-book member is not deleted by automation. Member triggers
  write `ChangeLog`/`ChangeCount`; removal, if wanted, is an Owner decision in
  AutoCount.

## Owner console code custody (G2 A2, 16 October 2026)

The Owner console holds the AutoCount credentials in an elevated process, so
it must not run code another local principal can change. Before it runs git
or Python, and before any prompt, it checks
(`scripts/ac2_directlocal_code_custody.ps1`, read-only) three trees and every
ancestor of each:

- the code root (a standalone checkout, not a worktree);
- the Python interpreter's directory (`-Python`);
- the git installation (`-GitRoot`, default `C:\Program Files\Git`).

Only the current user, SYSTEM, Administrators and TrustedInstaller may own,
or hold any write-class right on, anything in them. Reparse points are
refused. It then requires the exact reviewed head with no modified,
untracked or ignored file, and it runs Python with `-I -B`, plus `-S` for the
core.

Read-only checks on 16 October 2026 showed that this guard stops on this VM
today:

- `C:\XB` grants Authenticated Users Modify (`code_root_ancestor_writable`).
- The per-user Python install sits under `C:\Users\XB\AppData`, which grants
  an app-package SID full control (`interpreter_ancestor_writable`).
- `C:\Program Files\Git` passes.

The next credential-bearing step therefore needs an Owner host decision: a
protected runtime checkout, plus an all-users Python whose tree passes,
installed under a protected location such as `C:\Program Files` and qualified
on the host first. No host ACL change or move is made by this source or this
runbook. Never run the console from `C:\XB\automation`.

### Launching an Owner console (G2 A4 and O11)

- Open Windows PowerShell with "Run as administrator". Do not use `cmd.exe`,
  which searches the current directory for `powershell`.
- Start Windows PowerShell and the console script by their full paths, as in
  C and F: `<PROTECTED_RUNTIME>` is the protected runtime checkout and
  `<PROTECTED_PYTHON>` the qualified all-users Python directory, both from the
  Owner host decision. Never use `C:\XButomation`, a relative path or a
  bare `powershell` or `python`.
- The console makes the verified code root its working directory and sets
  `NoDefaultCurrentDirectoryInExePath` before its first native command, and
  the collector starts `tasklist`, `schtasks` and `powershell` from the
  system directory by absolute path. The directory it was started from
  therefore cannot supply an executable, but start it from a protected
  directory anyway (the elevated default `C:\Windows\system32` is fine).
- The interpreter must be a regular install, not a venv: a `pyvenv.cfg`
  beside `python.exe` or in its parent directory stops the console
  (`interpreter_pyvenv_cfg_present`), and so does a parent directory to which
  anyone else can add a file (`interpreter_parent_writable`; G2 A5).

Residual: the guard proves custody when it runs; it is not a trusted
bootstrap. The current user's own non-elevated processes are trusted by
design.

## Path to unattended near-real-time (E8)

The connector is held by the claude.ai account and is available only inside a
Claude session, and every read passes through the model. That is acceptable for
attended UAT but not for unattended, immediate production:

| Option | Latency | Owner action | Notes |
|---|---|---|---|
| Read-only Shopify custom app token (`read_customers` only) used by a local poller of the `recent` query every 1-2 minutes | ~1-2 min + POS sync | Create the app in Shopify admin (a configuration change, not a customer write) and store the token on the VM | Recommended: no model in the data path; reuses the Core unchanged except the capture provenance |
| Scheduled Claude session running the same loop | interval + session start | Approve scheduling and unattended model-mediated PII | No new credential; slower and less private |
| Shopify Flow `Send HTTP request` (Grow plan qualifies) to the VM | seconds | Flow change and a public HTTPS ingress | Adds a listener; not proposed |

POS: with HQ creation in about 1-2 minutes, end-to-end latency is dominated by
the outlet's 15-minute sync interval. Shortening it is an outlet POS setting
(Owner decision).
