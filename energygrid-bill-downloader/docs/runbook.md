# Energy@Grid bill downloader runbook

This project is a deterministic, local Windows utility. It is designed to log in
once per run, inventory the complete available bill list, reconcile it against a
private archive and SQLite manifest, and exit with a truthful status. It does not
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
```

For `run` and `list` the only supported command-specific options are `--headed`,
private root overrides, `--timeout-seconds`, and `--max-attempts`. A normal `run`
performs downloads and publication. `list` performs login and complete inventory
but does not download; an unresolved bill therefore remains `ACTION_REQUIRED`
rather than being reported as success. `NO_NEW_BILLS` means the full inventory
reconciled with no new publication, including an empty portal inventory.

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
the landing records `EG_LOGIN_AUTHENTICATION_UNPROVED`; an EMS application
entry, Billing Manager or EB Bill that never becomes usable afterwards is a
navigation failure and records `EG_NAV_EMS_ENTRY_NOT_READY`,
`EG_NAV_EMS_ENTRY_DISPATCH_UNCERTAIN`, `EG_NAV_BILLING_MANAGER_NOT_READY`,
`EG_NAV_BILLING_MANAGER_DISPATCH_UNCERTAIN`, `EG_NAV_EB_BILL_NOT_READY`,
`EG_NAV_EB_BILL_DISPATCH_UNCERTAIN`, or `EG_NAV_RESULTS_ROUTE_UNPROVED`
according to whether the control was never ready, was clicked with an outcome
that could not be established, or was clicked without the EB Bill route ever
becoming proven. A dispatch whose outcome is uncertain is terminal and is never
retried. Evidence written while Billing Manager was still treated as the login
postcondition records the retired `EG_LOGIN_BILLING_MANAGER_WAIT_FAILED`: no
current build emits it, and it means only that the pre-separation build never
saw Billing Manager after the submit.

The authenticated landing is not yet the application, so the two EMS codes sit
before the Billing Manager and EB Bill codes rather than alongside them, and
they tell an operator a different story:

| Code | What it means | Where the run stopped |
| --- | --- | --- |
| `EG_NAV_EMS_ENTRY_NOT_READY` | The exact `EMS` application entry was missing, duplicated, hidden, disabled, unactionable or unreadable for the whole bounded window. | On the landing. Nothing was clicked at all, and the application was never entered. |
| `EG_NAV_EMS_ENTRY_DISPATCH_UNCERTAIN` | The one real EMS click raised, so whether the browser acted cannot be established. | At the entry click. It is never sent again, and no re-login or fallback is attempted. |
| `EG_NAV_BILLING_MANAGER_*` / `EG_NAV_EB_BILL_*` / `EG_NAV_RESULTS_ROUTE_UNPROVED` | The one EMS dispatch had already been consumed, and the failure is later, on the existing EB Bill route. | After the entry click. These codes do not establish that the application was actually reached. |

An EMS code therefore points at the landing or at the entry control itself; a
Billing Manager or EB Bill code says the failure was observed after the EMS
dispatch had already been consumed, which is not the same as proving the
application was entered. An EMS click that lands but opens nothing is exactly
that counterexample: it consumes the one entry attempt without ever reaching the
application, and then shows up as `EG_NAV_BILLING_MANAGER_NOT_READY` because the
surface simply never became the application -- the run does not try EMS again.
A run reaches the application entry exactly once: a download that resumes from a
saved results address is already inside the application and never re-actuates
EMS.

`login-diagnostic` does not actuate EMS at all. It observes the surface after
the submit and stops there, so it never enters the application, never clicks
Billing Manager or EB Bill, and can never record any `EG_NAV_` code. Use it to
tell a credential or landing problem from an application-entry problem: if the
diagnostic proves the landing but a `run` reports `EG_NAV_EMS_ENTRY_NOT_READY`,
the credentials are fine and the entry control itself has drifted.

## Controlled first validation

Before unattended scheduling, the owner must separately approve and perform the
following sequence:

1. Validate the private directories and browser provisioning on the Windows host;
   only the checkout-relative `_MandarinGallery\` archive exception is allowed,
   while state, temp, logs, and browser cache remain outside the checkout.
2. Inject runtime credentials through the approved host mechanism.
3. Run a headed `list` only, inspect the aggregate result, and confirm the login,
   Billing Manager, EB Bill, invoice-list, filename, download-button, and
   pagination contracts against the live portal. Where the login step itself is
   what needs evidence, `login-diagnostic` is the narrower first move: it stops
   at the submit and reports what the portal rendered, without entering the
   application at all.
4. Run a controlled `run` and inspect the resulting archive, state record, and
   aggregate logs. Confirm no existing file was overwritten.
5. Re-run `run` to verify idempotency and then reconcile any remaining history
   from the portal before scheduling.

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

## Recovery guidance

`ARCHIVE_CONFLICT`, `STATE_INCONSISTENT`, `PORTAL_LAYOUT_CHANGED`, and
`ACTION_REQUIRED` are fail-closed states. Preserve the private archive, state
database, log, and any owned temporary run directory; diagnose the generic
support reference and inspect the local files under owner control before retrying.
The program only removes UUID-shaped operation directories that it owns under the
configured temporary root. It never cleans unrelated files.

If a bill is listed but the final file is missing while the manifest says it was
archived, the next controlled run attempts a repair only when the newly validated
content has the recorded SHA-256. A different content hash becomes
`ARCHIVE_CONFLICT`. If the final file exists without a state row, a valid PDF is
hashed and recorded as `PRESENT_RECONCILED`; an invalid file is not silently
replaced.

## Later Task Scheduler handoff

`task-scheduler/register_task.example.ps1` is intentionally non-operational. A
separate current-turn approval is required before any scheduler registration,
account selection, environment-secret loading, or live headed validation.
