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
```

The only supported command-specific options are `--headed`, private root
overrides, `--timeout-seconds`, and `--max-attempts`. A normal `run` performs
downloads and publication. `list` performs login and complete inventory but does
not download; an unresolved bill therefore remains `ACTION_REQUIRED` rather
than being reported as success. `NO_NEW_BILLS` means the full inventory
reconciled with no new publication, including an empty portal inventory.

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
the Billing Manager wait. A visible portal alert still takes precedence over all
of them and records `EG_LOGIN_PORTAL_REJECTED`. Evidence written before those
steps were told apart records the retired `EG_LOGIN_REQUIRED_CONTROL_UNRESOLVED`
instead: no current build emits it, and it narrows a failure only to that login
sequence as a whole.

## Controlled first validation

Before unattended scheduling, the owner must separately approve and perform the
following sequence:

1. Validate the private directories and browser provisioning on the Windows host;
   only the checkout-relative `_MandarinGallery\` archive exception is allowed,
   while state, temp, logs, and browser cache remain outside the checkout.
2. Inject runtime credentials through the approved host mechanism.
3. Run a headed `list` only, inspect the aggregate result, and confirm the login,
   Billing Manager, EB Bill, invoice-list, filename, download-button, and
   pagination contracts against the live portal.
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
