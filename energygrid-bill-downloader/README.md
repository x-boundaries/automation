# Energy@Grid daily bill downloader

Deterministic Windows downloader for the Energy@Grid tenant portal. The normal
runtime is Python 3.12.x plus pinned Playwright and a freshly created browser
context. It inventories the complete available bill list, validates PDFs, and
publishes each new bill exactly once to a private archive.

This project is intentionally self-contained under this directory. It does not
add n8n, email parsing, recurring LLM use, AutoCount integration, or a live API
client. The browser is used only through the selectors in
`energygrid_bill_downloader/portal.py`; all tests use the local synthetic portal.

## Install and configure

Use a dedicated Python 3.12.x environment. Install the pinned package and
provision Chromium separately with the official Playwright mechanism:

```powershell
python -m pip install --requirement requirements.txt
python -m playwright install chromium
```

Copy `config/energygrid.example.json` to a private path outside the checkout.
The archive, SQLite state, temporary downloads, logs, and browser cache must be
absolute paths outside the checkout and must not overlap. The archive and temp
root must be on the same local Windows volume. The archive directory must exist
before a run; the program creates only its private state/temp/log parents.

Inject `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` only at runtime through
an approved host mechanism. They are never accepted as CLI arguments, stored in
the config, or written to logs. Do not use a persistent browser profile or
`storage_state` file.

## CLI

Run a reconciliation or a non-downloading inventory from this directory:

```powershell
python -m energygrid_bill_downloader run --config <EXTERNAL_CONFIG_JSON>
python -m energygrid_bill_downloader list --config <EXTERNAL_CONFIG_JSON>
python -m energygrid_bill_downloader list --config <EXTERNAL_CONFIG_JSON> --headed
```

Supported overrides are `--archive-root`, `--state-path`, `--temp-root`,
`--log-root`, `--timeout-seconds`, `--max-attempts`, and `--headed`. The CLI has
no browser-install, profile, credential, or live-debug options.

Exit `0` means `NO_NEW_BILLS`, `DOWNLOADED`, or `ALREADY_PRESENT`; `10` means a
retryable network/download failure; `20` means action is required for login,
layout, PDF, archive, or state; `64` means invalid CLI/configuration or a
missing runtime dependency. `NO_NEW_BILLS` means the complete portal inventory
reconciled without new publication, not that the portal contained zero bills.

## Runtime contract

- A fresh browser context is created for every run and closed on exit.
- Credentials are read only from `ENERGYGRID_USERNAME` and
  `ENERGYGRID_PASSWORD`; cookies, session storage, headers, traces, screenshots,
  and persistent profiles are not retained.
- The portal filename is the identity after NFC normalization and casefolding.
  A duplicate normalized filename or unsafe Windows filename fails closed.
- SQLite outside the checkout is the durable manifest. It stores filename
  identity, first/last discovery time, archive time, byte size, SHA-256, status,
  error class/time, attempt count, and completion source; it stores no PDF data.
- A download is saved below an owned UUID-shaped temp directory, checked for a
  non-empty `%PDF-` header and terminal `%%EOF`, hashed, and moved with
  Windows `MoveFileExW` without replacement. The final file is revalidated
  before the manifest is committed.
- The program never silently overwrites a different existing file. It removes
  only owned temp directories after successful cleanup checks.

## Tests

The suite is standard-library `unittest` plus browser-backed tests against a
local `ThreadingHTTPServer` fixture. It requires no Energy@Grid credentials or
authenticated network call:

```powershell
python -m unittest discover -s tests -v
```

The fixture covers successful and failed login, pagination, empty inventory,
idempotency, duplicate/unsafe identities, failed and malformed downloads,
publication conflicts, state/file disagreement, temp recovery, selector drift,
and log redaction. CI installs Chromium and runs this suite on Windows only.

## Controlled live handoff

The repository implementation does not contact the live portal, create the
eventual archive, provision a host, install a browser on a host, or register a
Task Scheduler job. Before unattended scheduling, a separately approved
validation must run a headed `list`, confirm the observed selectors and complete
pagination, perform a controlled download/backfill, verify idempotency, and
review private archive/state/log results. The scheduler file is an inert design
template only; it contains no account, password, start time, or secret-loading
implementation.
