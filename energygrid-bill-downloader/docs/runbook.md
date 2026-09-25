# Energy@Grid bill downloader runbook

This project is a deterministic, local Windows utility. It is designed to log in
once per run, inventory the one settled EB Bill results table, download every
listed bill, reconcile the downloads against a private archive and SQLite
manifest, and exit with a truthful status. It does not
use n8n, email parsing, recurring LLM calls, AutoCount, or a live API in its
normal path.

## Repository and private runtime boundary

The checkout contains code, synthetic fixtures, tests, documentation, and example
configuration only. The final PDF archive may be outside the Git checkout or
under the checkout-relative private root `_MandarinGallery\`. SQLite state,
temporary downloads, logs, and the Playwright browser cache must be outside the
Git checkout and on the same local Windows volume as the archive. The program
rejects arbitrary in-checkout archive/runtime paths and overlapping paths.

The eventual archive target is an owner-controlled path such as
`C:\XB\_MandarinGallery\Utilities\EnergyGrid`. The example config is a shape
only; it is not a live configuration and contains no credential values.

## Runtime prerequisites

- Windows host with Python 3.14.x.
- The pinned `playwright==1.61.0` package installed in the approved runtime.
- Chromium provisioned separately with the official Playwright mechanism. The
  daily job does not install packages or browsers.
- An external JSON config copied from `config/energygrid.example.json` and
  adjusted to already-created private paths.
- Runtime-only `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` environment
  values supplied by a later approved host mechanism. Values must not be stored
  in this repository, in the config JSON, in CLI arguments, or in logs.
- `account_identity` set in the private JSON to the exact intended tenant/account
  identity. It is required operational data, not a credential, and must not be
  passed on the CLI or written to logs.

The repository implementation does not provision a password manager profile,
browser profile, cookies, storage state, or scheduler credentials.

## Commands

From the project directory:

```powershell
python -m energygrid_bill_downloader run --config <EXTERNAL_CONFIG_JSON>
python -m energygrid_bill_downloader list --config <EXTERNAL_CONFIG_JSON>
python -m energygrid_bill_downloader list --config <EXTERNAL_CONFIG_JSON> --headed
python -m energygrid_bill_downloader login-diagnostic --config <EXTERNAL_CONFIG_JSON>
python -m energygrid_bill_downloader navigation-diagnostic --config <EXTERNAL_CONFIG_JSON>
python -m energygrid_bill_downloader download-preflight-diagnostic --config <EXTERNAL_CONFIG_JSON>
```

For `run` and `list` the only supported command-specific options are `--headed`,
private root overrides, `--timeout-seconds`, and `--max-attempts`. A normal `run`
downloads every listed row and then publishes what is new (see
[Download-first production path](#download-first-production-path)). `list`
performs login, the EB Bill tab selection, one Search and the inventory, but
never downloads and never writes state. Because an invoice's name exists only
once it is downloaded, `list` cannot know whether a listed bill is already
archived: any listed row therefore makes `list` report `ACTION_REQUIRED` (exit
`20`) with `present_count` `0`, rather than success. A live results table that
shows only its header is not `NO_NEW_BILLS`: it fails closed as
`PORTAL_LAYOUT_CHANGED` (`EG_NAV_RESULTS_HEADER_ONLY`) until a real empty-state
contract is separately evidenced. `NO_NEW_BILLS` is reported only when an
inventory is genuinely empty, which the live portal path currently never
produces.

`login-diagnostic` accepts `--config` and nothing else. It rejects `--headed`,
every private root override, and every timeout or attempt override, because its
headed mode and its bounds are fixed properties of the operation rather than
choices. It runs the canonical login sequence up to and including exactly one
real Login submit, observes a fixed allowlist of public-safe counts and booleans
for at most a further 60 seconds, and stops. It reaches no Billing Manager click,
EB Bill, account selection, Search, inventory, pagination, download, publication,
archive, or state behaviour, and it creates no state, log, temp, or archive
artefact. Its result is one JSON document with schema
`energygrid.login_diagnostic.v2`, carrying `status`, `classification`,
`authentication_outcome`, `navigation_status`, `submit_dispatched`,
`submit_outcome`, the pre- and post-submit observations, and
a bounded `support_ref` when the result is not complete.
`authentication_outcome` is exactly `AUTHENTICATED`, `REJECTED`, or
`AUTHENTICATION_UNPROVED`, and `navigation_status` is always `NOT_TESTED`
because the diagnostic never tests business navigation. The superseded
`energygrid.login_diagnostic.v1` identifier is historical only: it reads
evidence written by an earlier build and is never emitted now. It exits `0` only when an
authorised classification is positively established, `20` on any fail-closed or
insufficient-evidence result, and `64` on a configuration, dependency, or argument
contract failure; it never exits `10`.

Exit statuses:

- `0`: `NO_NEW_BILLS`, `DOWNLOADED`, or `ALREADY_PRESENT`.
- `10`: retryable network or download failure after bounded retries.
- `20`: login/layout/PDF/archive/state action is required.
- `64`: invalid CLI/configuration or missing runtime dependency.

The JSON console summary contains only the run identifier, aggregate counts,
status, and generic failure classes. Local JSONL logs contain the same
privacy-minimised operational fields. They do not contain credentials, cookies,
headers, tokens, PDF contents, or exact private filenames.

A run that ends on a caught application error appends exactly one terminal
`run_failed` event to the local JSONL before returning its usual status and exit
code. The event is evidence, not a new status, and carries only the run
identifier, the phase, the status, and a `support_ref` code. The code identifies
which checkpoint failed, so a `PORTAL_LAYOUT_CHANGED` run can be told apart at,
for example, `EG_LOGIN_SEMANTICS_ACTIVATION_NOT_APPEAR` (the accessibility gate
never appeared) versus `EG_LOGIN_POST_ACTIVATION_NOT_READY` (the gate opened but
the Login control was hidden or disabled). A failure this build does not
recognise records `APP_ERROR_UNCLASSIFIED`. The underlying exception text is
never written to the log, the console, or any filename, so quote the
`support_ref` when escalating rather than looking for a message.

Each step of the login sequence carries its own code, so a failed login is
attributable to portal navigation, the semantics activation dispatch, the Login
entry click, the username entry, the password entry, the login submission, or
the authenticated landing. A visible portal rejection still takes precedence
over all of them and records `EG_LOGIN_PORTAL_REJECTED`. Evidence written before
those steps were told apart records the retired
`EG_LOGIN_REQUIRED_CONTROL_UNRESOLVED` instead: no current build emits it, and it
narrows a failure only to that login sequence as a whole.

Authentication and business navigation are separate contracts, and their codes
say which one failed. A successful login means only that the authenticated
landing was positively proven, from the exact `EMS` witness together with an
exact zero count for every retained login control, accessibility gate and
rejection. Billing Manager is not part of that proof. A login that cannot prove
the landing records `EG_LOGIN_AUTHENTICATION_UNPROVED`. Evidence written while
Billing Manager was still treated as the login postcondition records the retired
`EG_LOGIN_BILLING_MANAGER_WAIT_FAILED`: no current build emits it.

Since DL-XB-199 the live portal serves one surface after login, and production
navigation never clicks EMS, Billing Manager, a link, or a tenant selector. It
selects the exact accessible `tab "EB Bill"` (zero clicks when it is already
selected; otherwise exactly one click, after which EB Bill must read selected and
`Tenant Bill` must not), proves exactly one visible configured account witness
outside the results table, resolves the exact `button "Search"` on the bounded
recovery ladder and clicks it exactly once, and then settles on exactly one role
`table`. A dispatch whose outcome is uncertain is terminal and is never retried.
The production navigation codes are:

| Code | What it means |
| --- | --- |
| `EG_NAV_EB_BILL_TAB_NOT_READY` | The exact EB Bill tab was absent (including a link-only EB Bill), duplicated, contradictory or never actionable. Nothing was clicked. |
| `EG_NAV_EB_BILL_TAB_DISPATCH_UNCERTAIN` | The one tab click raised; it is never sent again. |
| `EG_NAV_EB_BILL_TAB_UNPROVED` | The tab was clicked once but EB Bill selection never became proven, or was lost before inventory completed. |
| `EG_NAV_ACCOUNT_WITNESS_UNPROVED` / `EG_NAV_ACCOUNT_WITNESS_AMBIGUOUS` | The configured account text was not visible exactly once outside the results (absent, shown only inside rows, a different account shown, or shown more than once). Search was not clicked. |
| `EG_NAV_SEARCH_NOT_READY` / `EG_NAV_SEARCH_DISPATCH_UNCERTAIN` | Search never became ready, or its one click raised. |
| `EG_NAV_PAGE_TOPOLOGY` | The browser context did not hold exactly the one bound page. |
| `EG_NAV_RESULTS_UNSETTLED` | The results never settled into one role table with a header row and exactly one Download button per invoice row. |
| `EG_NAV_RESULTS_HEADER_ONLY` | The settled table had a header and no invoice rows. |
| `EG_NAV_RESULTS_PAGINATION_PRESENT` | A `Next page`, `Next`, `Previous page` or `Load more` button or link was present. It was never clicked. |
| `EG_NAV_RESULTS_ROWCOUNT_CONTRADICTORY` | The table's `aria-rowcount` contradicted the rendered rows. |
| `EG_NAV_RESULTS_ROW_IDENTITY_INVALID` | A row's private identity was empty, duplicated, oversized or unreadable. |
| `EG_NAV_RESULTS_SAFETY_CEILING` | More invoice rows than `inventory_safety_ceiling`. |
| `EG_NAV_RESULTS_INVENTORY_CONSUMED` | A second inventory was attempted on the same portal instance; Search is never re-sent. |

The historical link-route references `EG_NAV_BILLING_MANAGER_NOT_READY`,
`EG_NAV_BILLING_MANAGER_DISPATCH_UNCERTAIN`, `EG_NAV_EB_BILL_NOT_READY`,
`EG_NAV_EB_BILL_DISPATCH_UNCERTAIN` and `EG_NAV_RESULTS_ROUTE_UNPROVED` are
retired: they still read older evidence, and no current build emits them.
`EG_NAV_EMS_ENTRY_NOT_READY` and `EG_NAV_EMS_ENTRY_DISPATCH_UNCERTAIN` are now
reachable only from the separate `navigation-diagnostic`, which still observes
the historical EMS route and is deliberately unchanged; a production `run` or
`list` never records them.

`login-diagnostic` does not actuate EMS at all. It observes the surface after
the submit and stops there, and can never record any `EG_NAV_` code. Use it to
tell a credential or landing problem from a navigation problem: if the
diagnostic proves the landing but a `run` reports an `EG_NAV_` code, the
credentials are fine and the single surface itself has drifted.

`navigation-diagnostic` is a separate direct-Python, headed-only operation that
still observes the historical EMS / Billing Manager / EB Bill link route; it is
known to be stale against the live single surface and is not used by production
navigation. It
loads and validates the external config but does not run production preflight,
create a logger, clean temporary files, open StateStore, reconcile inventory,
download, publish, or invoke the normal `run` path. It calls the canonical
`login()` exactly once, requires one context page and one frame, allows one
exact actionable `button / EMS` click, and then observes only the fixed EMS,
Billing Manager, and EB Bill role/name pairs. Billing Manager and EB Bill are
never normally clicked. The operation uses one 60-second monotonic deadline
with checkpoints at 0, 250, 1000, 5000, 10000, 30000, and 45000 milliseconds;
each waiting yield is capped at 1000 milliseconds.

Every invocation that reaches the handler emits one
`energygrid.navigation_diagnostic.v1` JSON document. Its fixed public-safe
keys are `schema`, `status`, `result`, `authentication_proven`,
`ems_dispatch_attempted`, `ems_dispatch_uncertain`, `pre_ems`, and `post_ems`;
an action-required document also carries a bounded `support_ref`. The nested
evidence contains only capped page/frame and control counts, fixed roles and
names, visibility/enabled/trial-actionability booleans, route-changed,
same-origin, and existing EB Bill route-proof booleans. URLs, query strings,
portal text, customer/account values, exception text, cookies, tokens,
screenshots, traces, HAR, and storage state are never emitted. Invalid output
evidence is replaced by a fully unobserved `OUTPUT_REJECTED` document.

The direct diagnostic is not in the runtime launcher's `run | list |
login-diagnostic | download-preflight-diagnostic` allowlist. It is an
offline/controlled evidence tool only;
this implementation does not authorize a live portal session, production run,
retry, selector correction, deployment, or Scheduler action.

`download-preflight-diagnostic` (DL-XB-199 G2-083 / G3-084) is the fixed
headless no-Download pre-dispatch diagnostic. It accepts `--config` and nothing
else, and it is reachable through the runtime launcher as a fixed `-Command` so
the existing credential boundary is reused. It loads and validates the config,
performs the canonical login and the production inventory (one EB Bill tab
selection and one Search), and then runs the one shared production pre-dispatch
proof over every row in order, stopping at the first failing row. That proof is
the same code a production Download runs before its dispatch: the latch, the row
handle, the whole frozen surface re-proof on the bounded ladder, and the final
one-shot row-identity recheck. The diagnostic never clicks Download, never
enters a download wait, never clicks pagination, Tenant Bill, EMS, Billing
Manager or a link, takes no screenshot, trace, HAR or storage state, and never
retries. It does not run production preflight, create a logger, clean temporary
files, open StateStore, create a run directory or reconcile, so it creates no
state, log, temp or archive artefact. When every row passes, the portal latches
so it can never dispatch a Download afterwards.

It prints exactly one `energygrid.download_preflight_diagnostic.v1` JSON
document and nothing on stderr. The keys are always `schema`, `status`,
`result`, `support_ref`, `download_dispatched` (always `false`),
`inventory_count`, `rows_passed` and `failure`. `result` is one of
`PREFLIGHT_ALL_ROWS_PASSED`, `PREFLIGHT_ROW_FAILED`, `CONFIGURATION_FAILED`,
`LOGIN_FAILED`, `INVENTORY_FAILED`, `INVENTORY_EMPTY`, `UNEXPECTED_FAILURE` or
`OUTPUT_REJECTED`. The two `PREFLIGHT_` results are a complete observation
(`status` `DIAGNOSTIC_COMPLETE`, exit `0`); everything else is `ACTION_REQUIRED`
with exit `20`, except `CONFIGURATION_FAILED` which exits `64`. It never exits
`10`. `support_ref` carries an existing bounded reference only for
`CONFIGURATION_FAILED`, `LOGIN_FAILED`, `INVENTORY_FAILED` and
`UNEXPECTED_FAILURE`, and is `null` otherwise. A failed row is described by
`failure`: `row_ordinal`, one of 36 closed `reason_code` values, its owning
`last_checkpoint` (one of `HANDLE`, `TOPOLOGY`, `TAB_STATE`, `ACCOUNT_WITNESS`,
`RESULTS_SURFACE`, `SNAPSHOT_COMPARE`, `CONTROL_RESOLVE`, `CONTROL_VISIBLE`,
`CONTROL_ENABLED`, `CONTROL_ACTIONABLE`, `ROW_IDENTITY_RECHECK`),
`window_expired` (true only when a bounded recovery window ran out),
`not_ready_looks`, a coarse `elapsed_bucket`, the Download `control` evidence
(`count_bucket`, `visible`, `enabled`, `trial_actionability`) and, only for
`SNAPSHOT_MISMATCH`, a `snapshot` comparison of booleans and one changed-row
count. Row text, filenames, account text, URLs, digests and exception text are
never emitted. Invalid output is replaced by one fixed `OUTPUT_REJECTED`
document.

The launcher source admits the command, but an installed launcher keeps its
earlier allowlist until a separately authorised republish, re-admission and
`ValidateOnly` accept the new launcher bytes. Committing it grants no live
diagnostic, credential use, deployment, retry or Scheduler authority.

### Future live-session diagnostic authority boundary

Any future live diagnostic requires a separate Web authority binding all of the
following before the process starts: the exact reviewed Repair-1 H/T/base (or
the exact merged authority), the exact reviewed diagnostic command, and exactly
one OS process/session. That authority is consumed at process start regardless
of success/failure/crash/interruption/configuration failure. One authority
permits at most one EMS dispatch, zero Billing Manager dispatches, zero EB Bill
dispatches, zero production `run` actions, and zero Scheduler action.

Evidence is limited to the fixed diagnostic schema. Do not collect or publish
screenshots, traces, HAR files, storage-state exports, raw URLs, portal text,
customer/private evidence, or any automatic retry under the same authority.
This documentation boundary grants no live authority; a later live session
must bind a new reviewed authority explicitly.

## Download-first production path

DL-XB-199 (G2-076, as adjudicated by Web). A normal `run` works in two phases.

**Phase A -- acquire every row first.** The portal inventories the one settled
results table exactly once per run and returns opaque row handles that carry an
ordinal only: no filename, row text or digest. Rows are downloaded in table
order, each into its own owned temporary run directory. Before every Download
attempt, including every retry, the whole frozen surface is re-proven: one
page, EB Bill still selected, the account witness, no pagination sentinel, and
the exact ordered row snapshot. Row identity is a private per-session keyed
digest of each row's normalised accessible text; the text, the digest and the
key never leave the portal object. The invoice name is learned only from the
browser's suggested filename after a successful Download and is then validated
with the existing filename rules. No state row is written and nothing is
published until every row has been acquired and every normalised filename key
has proven unique.

- A positively observed browser download failure or save failure retries only
  that row, up to `max_attempts`.
- An uncertain dispatch -- the click raised, no download event before the
  timeout, a popup/new page, or EB Bill selection lost -- is a non-retryable
  `DOWNLOAD_FAILED`. The portal latches and no later row is dispatched.
- Any row reorder or text drift before a Download latches the portal
  (`PORTAL_LAYOUT_CHANGED`) with no later Download.
- A different suggested filename for the same row across attempts latches
  (`PORTAL_LAYOUT_CHANGED`).
- One terminal row stops all later dispatch and skips Phase B. The summary keeps
  `inventory_count` at every observed row, reports `downloaded_count` and
  `present_count` as `0`, and counts `failure_count` only for the row that
  actually failed; undispatched rows are not additional failures.
- An unsafe suggested filename, or two rows whose names normalise to the same
  filename key, fails the whole run before any state write or publication.
- Identical PDF bytes under different filename keys are both valid. There is no
  cross-key hash identity and no scan of other state rows.

**Phase B -- filename-keyed reconciliation.** Each acquisition is reconciled
with the existing rules: an existing valid archive counts as present and the
fresh download is discarded; a missing archive with a same-key recorded hash is
repaired only when the fresh hash matches (a mismatch stays `ARCHIVE_CONFLICT`
and keeps its evidence directory); a first-time name is published no-replace,
revalidated and recorded; an existing valid file without state becomes
`PRESENT_RECONCILED`. State schema v1 is unchanged.

A rerun therefore downloads every row again but publishes nothing new. For an
already `ARCHIVED` row it leaves `status`, `sha256`, `byte_size`,
`archived_at_utc` and `completion_source` unchanged; `last_seen_at_utc` may
advance under the existing observational bookkeeping. Every owned temporary run
directory is removed on success and on every failure or interruption, except
the one retained same-key repair conflict; a hard kill is covered by the
existing stale-temp cleanup.

Logs, summaries and errors carry fixed messages, statuses and counts only: never
row text, digests, keys, row handles, suggested filenames, account text, page
addresses, or invoice/customer/account values.

Each `invoice_failure` event additionally carries `row_ordinal` (the failing
row's bounded table ordinal) and, when the failure is a pre-dispatch proof
failure, `preflight_reason` and `preflight_checkpoint` from the same closed
36-reason / 11-checkpoint vocabulary as `download-preflight-diagnostic`
(DL-XB-199 G2-083). The fields are never required, so logs written by an earlier
build stay valid; every value is validated before it is logged and an invalid
value is omitted rather than coerced. Status, exit code, summary, `run_complete`
and `run_failed` are unchanged. Any strict external run-log collector that
rejects unknown keys must be updated to admit these fields before it is used on
a later run.

**Result completeness is a pre-Scheduler gate.** The pagination sentinels and
the `aria-rowcount` check are inspection-only safety guards for the currently
observed surface (four invoice rows, no pagination control). Their absence does
not prove that a larger future history cannot be virtualised, lazily rendered
or paged by another mechanism.

`PRE_SCHEDULER_RESULT_COMPLETENESS_EVIDENCE_REQUIRED=YES`

Normal Scheduler enablement stays blocked until Web accepts positive evidence
that the production inventory cannot silently omit invoices when history grows
beyond the observed rows. This does not block a separately authorised,
controlled production proof on the current surface.

## Controlled first validation

Before unattended scheduling, the owner must separately approve and perform the
following sequence:

1. Validate the private directories and browser provisioning on the Windows host;
   only the checkout-relative `_MandarinGallery\` archive exception is allowed,
   while state, temp, logs, and browser cache remain outside the checkout.
2. Inject runtime credentials through the approved host mechanism.
3. Run a headed `list` only, inspect the aggregate result, and confirm the login,
   EB Bill tab, account witness, Search, results-table, download-button and
   no-pagination contracts against the live portal. `list` downloads nothing, so
   it cannot confirm filenames. Where the login step itself is what needs
   evidence, `login-diagnostic` is the narrower first move: it stops at the
   submit and reports what the portal rendered.
4. Run a controlled `run` and inspect the resulting archive, state record, and
   aggregate logs. Confirm no existing file was overwritten and no owned
   temporary run directory remains.
5. Re-run `run` to verify semantic idempotency (every row downloaded again,
   nothing new published, archived integrity fields unchanged) and then
   reconcile any remaining history from the portal before scheduling.
6. Scheduling additionally requires the accepted result-completeness evidence
   described under [Download-first production path](#download-first-production-path).

The first controlled backfill should use the same complete-inventory `run`, not
a date cutoff. The observed portal history begins in May 2026; archive all
available safe PDFs, review conflicts and state/file disagreements, repeat the
run for idempotency, and schedule only after that review is complete.

Those steps are later live-system gates. This repository implementation run did
not contact the portal, use live credentials, download a bill, create the target
directory, provision Chromium, or create a Scheduled Task.

## Runtime launcher installation and validation

The source-controlled runtime layer lives under `runtime/`, and `runtime/README.md` is its
directory-level contract. Every mutating step below is a live-system action requiring
separate explicit current-turn approval. Describing a step here is not authority to perform
it.

### Deployment and update flow

1. The implementation pull request is reviewed and merged.
2. Under owner control, the deployed checkout is updated to the reviewed commit.
3. Run `install_or_update_launcher.ps1 -ValidateOnly` and inspect its JSON. It mutates
   nothing.
4. Obtain explicit current-turn owner approval for the mutating install, naming the launcher
   root and the operation.
5. Run the installer for real. It prepares and verifies every staging file with no
   destination mutation, classifies each destination, publishes the library before the entry
   script, publishes and reads back the manifest inside the same transaction, re-verifies the
   complete package, and only then accepts. Any pre-acceptance failure triggers
   installer-owned reverse-order rollback. On a clean host every member is absent and none of
   them uses `File.Replace`.
6. Run `launcher.ps1 -ValidateOnly` to confirm runtime binding, security expectations, and
   configuration reachability on the host, in a context equivalent to the unattended job.
7. Scheduling remains blocked. Task Scheduler registration, headed validation, and any live
   run each require their own separate approval.

Every subsequent update repeats steps 2 to 6. There is no in-place edit path, and no step in
which a human edits the installed launcher directly.

### Validating in a context equivalent to the unattended job

`launcher_root_not_writable_by_run_principal` is evaluated against the access token of the
process actually running the launcher. Validating from an elevated prompt, or under any
account other than the one the unattended job uses, therefore exercises a principal the job
will not use: such a run may legitimately fail, and a pass obtained that way proves nothing
about the job. Run step 6 under the same account and the same elevation state as the
scheduled job.

The operator supplies `-AuthorisedLauncherRootWriteSid` from the launcher root's intended
administrative ownership on that host, as one or more exact security identifier strings. It
is recorded with the other private deployment state, outside Git. This runbook names the
requirement and carries no value. The parameter is the only route by which the set reaches
the launcher: there is no default, no environment-variable form, and no file it is read
from.

A run principal holding `SeTakeOwnershipPrivilege` or `SeRestorePrivilege` fails that check
by construction, so `LocalSystem` is not a candidate unattended run principal for
`launcher.ps1`. The design does not require a dedicated account: separation by account and
separation by elevation within one account are both permitted deployments.

Both launcher-root write checks passing on the production host is owner-verified evidence
recorded against the implementation criteria. It is never a claim made by the offline test
suite, which cannot observe production host state.

### Migration from the current server-only launcher

Four concerns are kept separate and must not be confused with one another.

- **Installer transaction backup cleanup** is automatic and post-acceptance. The installer
  reaps only the preimage backups it created, only after the whole package has been accepted,
  and reports a bounded status if a redundant backup could not be removed.
- **Migration recovery holding** is an owner action. The recovery copy lives at an
  owner-controlled private location that resolves outside both the launcher root and the
  deployed checkout. It is never committed, this repository records no default or example
  value for it, and no committed script gains a parameter for it.
- **Launcher functional validation** is `launcher.ps1 -ValidateOnly` run against the real
  host, which is the first evidence that the new package actually functions. A byte-for-byte
  hash comparison of the installed members does not establish that.
- **Later separately authorised recovery-copy retirement or restoration** is a distinct
  gate. It is never an automatic consequence of a passing validation, and a failed validation
  is never automatically converted into an unreviewed rollback mutation.

The ordering matters more than the mechanism.

1. Under owner control, record the SHA-256 of the installed launcher and of the historical
   in-root rollback artefact, privately on the host.
2. The historical in-root rollback artefact predates the reserved residue contract and does
   not satisfy it, so it remains Class C and the launcher fails closed while it is present.
   That is the correct behaviour for an unrecognised file beside the launcher, and it is not a
   reason to widen the residue parser to accommodate one historical artefact.
3. Under a separate approval, create the recovery copy at the approved private holding path.
   Never overwrite an existing destination: an unexpectedly present destination means the
   holding path was not what the operator believed it was.
4. Re-read the external copy from disk, hash it, and confirm it equals the recorded
   accepted-preimage SHA-256 exactly. This verification happens **before** the in-root
   artefact is removed.
5. A copy that cannot be created or cannot be verified is treated as no copy at all. The
   migration then fails closed: the in-root artefact is left untouched, `launcher.ps1
   -ValidateOnly` is not run, and the sequence stops for owner attention.
6. Only after that positive verification, and only under a separate approval naming the
   artefact, may the in-root artefact be removed. Confirm it is absent afterwards.
7. Run `launcher.ps1 -ValidateOnly` and confirm every check passes. The verified external
   copy still exists throughout this step, and that is the point of the ordering.
8. Only after step 7 passes does the external copy become eligible for retirement. Removing
   it is a separate owner-controlled cleanup action. If step 7 fails, the sequence stops with
   the copy retained and nothing restored; any restoration requires separate owner authority
   that must first define the complete safe pre-migration topology it is restoring.

Retaining a verified copy preserves the option to recover. It performs no restoration and
asserts no automatic recovery.

## One-shot supervisor boundary

The repository-only one-shot supervisor is a separate Windows PowerShell 5.1 control
boundary. It has no live authority by itself and fixes the operation to `run`. Its mandatory
parameters are `LauncherPath`, `ExpectedLauncherSha256`,
`ExpectedLauncherLibrarySha256`, `ConfigPath`, `PythonExe`, `CheckoutRoot`,
`CredentialPath`, `BrowserCachePath`, `ExpectedBranch`,
`AuthorisedLauncherRootWriteSid[]`, `LogRoot`, `EvidenceRoot`, `RunId`, and
`TimeoutSeconds` (`1..3600`). Filesystem inputs are local absolute paths; UNC, device,
control-character, embedded-double-quote, invalid-hash, invalid-RunId and oversized
serialised inputs are rejected before launcher creation. The launcher must be the fixed
`launcher.ps1` identity beside `launcher_lib.ps1`, and both expected SHA-256 values are
verified before any process is created. The supervisor never opens the credential, parses
the private configuration, or executes the launcher during input validation.

Containment is creation-time containment: an unnamed Job Object is configured with
kill-on-job-close and no active-process, CPU, memory, breakaway or job-time ceiling. The
launcher is created suspended through `STARTUPINFOEX` and the Job Object attribute list,
before its initial thread can run. Standard input, output and error use three anonymous
pipes; only the three launcher-side stream handles are inherited. Supervisor drain workers
count bytes through fixed 64-KiB buffers and retain zero raw `stdout`/`stderr` bytes. A
resume is attempted exactly once, only after the create-new INTENT has been flushed with
`Flush(true)` and the control-state gate remains healthy. A failed or late durability
operation closes that gate and never reopens it.

`TimeoutSeconds` covers the contained launcher and descendant lifetime. Timeout,
interruption, resume anomaly, membership/configuration failure, or another post-create
infrastructure failure closes the gate, calls `TerminateJobObject` once with the fixed
termination code, and performs bounded accounting/reap. Polling is bounded to 100 ms;
there is no infinite wait. Reap is confirmed only by a successful final Job Object query
showing `ActiveProcesses == 0`; closing a handle or observing a launcher exit is not proof.
If terminal accounting cannot be established, the result remains `AMBIGUOUS`. No retry is
permitted.

The durable evidence pair is `<RunId>.intent.json` and `<RunId>.outcome.json`. Both are
UTF-8 without BOM, one JSON object plus LF, create-new, exclusive while written, and
flushed durably. OUTCOME carries the SHA-256 of the exact committed INTENT bytes, or null
when INTENT was not committed. Evidence contains only bounded control/accounting fields,
the positive application-child Boolean and observation elapsed milliseconds, byte counts,
reap state, launcher exit code, and the fixed start verdict. Raw stdout/stderr, credentials,
credential lengths, cookies, private command lines, private environment, private paths,
customer content and exception bodies are never published. Evidence retention is 30 days
pending separate operator disposition; the supervisor performs no cleanup.

Start verdicts are conservative. `NOT_STARTED_PROVEN` requires a durable pre-creation
rejection or the accepted launcher-only accounting predicate (`TotalProcesses == 1`,
baseline and terminal accounting valid, `ActiveProcesses == 0`, no positive application
observation, and no containment/evidence contradiction). `STARTED_PROVEN` requires a
positive local `Win32_Process` observation matching the live launcher parent, normalized
Python image and exact canonical application command line, consistent accounting with at
least one descendant, valid durable INTENT/OUTCOME and confirmed reap. Everything else is
`AMBIGUOUS`; a missed observer is not negative proof. Launcher exit 70, a launcher-failure
log, missing application log, or stream content alone never proves `NOT_STARTED_PROVEN`.

This section does not grant later authority. Repository implementation is separate from
deployment, Credential import, `ValidateOnly`, a REAL run, and Scheduler registration.
Each of those actions requires its own explicit current-turn authority and exact target;
the supervisor implementation supplies no retry or escalation path.

## Recovery guidance

`ARCHIVE_CONFLICT`, `STATE_INCONSISTENT`, `PORTAL_LAYOUT_CHANGED`, and
`ACTION_REQUIRED` are fail-closed states. Preserve the private archive, state
database, log, and any owned temporary run directory; diagnose the generic
support reference and inspect the local files under owner control before retrying.
The program only removes UUID-shaped operation directories that it owns under the
configured temporary root. It never cleans unrelated files.

If a bill is listed but the final file is missing while the manifest says it was
archived, the next controlled run (which downloads every row) repairs it only
when the newly validated content has the recorded SHA-256. A different content hash becomes
`ARCHIVE_CONFLICT`. If the final file exists without a state row, a valid PDF is
hashed and recorded as `PRESENT_RECONCILED`; an invalid file is not silently
replaced.

## Later Task Scheduler handoff

`task-scheduler/register_task.example.ps1` is intentionally non-operational. A
separate current-turn approval is required before any scheduler registration,
account selection, environment-secret loading, or live headed validation.
Scheduler enablement is also blocked by
`PRE_SCHEDULER_RESULT_COMPLETENESS_EVIDENCE_REQUIRED=YES` until Web accepts the
result-completeness evidence.
