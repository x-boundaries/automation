# Energy@Grid Runtime Source-Durability Design

Status: design only. This change adds no launcher, no installer, no runtime
implementation, no test, and no CI change. It records the reviewed contract that a
later, separately approved implementation change must satisfy.

Design lock: `DL-XB-141-RUNTIME-005-SOURCE-DURABILITY`.
Accepted amendment: `DL-XB-141-RUNTIME-005-SOURCE-DURABILITY-A1`, which narrowly amends
the invocation contract of sections 5.1 and 5.3 to admit one fixed repository-controlled
`login-diagnostic` operation, and adds its bounded contract as section 5.4. Every other
clause of `DL-XB-141-RUNTIME-005-SOURCE-DURABILITY` remains controlling and unchanged:
the eleven-parameter surface, the twenty-one ordered preflight checks, credential import
last, the three injected process variables, exact environment restoration, the
installer/manifest/source-integrity architecture, the exit bands, and Scheduler `run`
semantics are all untouched by the amendment.
Architecture: Option 2, Git canonical for reusable runtime behaviour.
Authority at authoring time: repository `x-boundaries/automation`, remote `main` at
`893f319a3e9ccf5055723a201377fe9261cda42a`.

## 1. Problem Statement

The Energy@Grid application is source-controlled under `energygrid-bill-downloader/`
and is covered by an offline synthetic test suite. The operational layer that starts
that application on the production Windows host is not. It currently exists as:

- an installed launcher that lives only on the server;
- behaviour established through one-shot owner bridge scripts run under the `#141`
  Run119 sequence, which were temporary by construction;
- decisions recorded in issue comments and session history rather than in reviewed
  files.

That distribution of authority has three concrete consequences.

1. **The operational layer is unreviewable.** No pull request has ever shown the
   launcher's replacement, validation, or rollback semantics as a diff.
2. **The operational layer is unreproducible.** Rebuilding the host after a failure
   would require reading retired bridges, `%TEMP%` scripts, and chat history. None of
   those are durable, and two of them are already gone.
3. **The operational layer is untested.** Two independently proven defects (section
   18) reached a live host precisely because no offline regression existed for the
   behaviour they broke. Patching the one affected server would leave the same defects
   available to the next host and the next script.

The repository already demonstrates the correct pattern elsewhere. The single-member
creation UAT keeps a pure, dot-sourceable PowerShell library
(`scripts/member_create_uat_runner_lib.ps1`) separate from the side-effecting entry
script (`scripts/ac2_member_create_uat_runner.ps1`), and exercises the library offline
from Python `unittest` (`tests/test_member_create_uat_runner_ps.py`). The Energy@Grid
runtime layer has no equivalent. This design closes that gap.

## 2. Goals And Non-Goals

### Goals

- Make Git canonical for the reusable, non-secret Energy@Grid launcher and runtime
  behaviour.
- Encode the two proven Run119 defects as reusable, regression-tested contracts rather
  than as a single-server patch.
- Keep every private value (credentials, DPAPI material, private absolute paths,
  account identity, host identities) outside the repository.
- Fit the existing repository conventions so the implementation needs no CI change and
  no new validation entrypoint.
- Make a clean-host rebuild reconstructable from a reviewed commit plus separately
  supplied private host configuration.

### Non-Goals

This design does not authorise, and the implementation change must not include:

- registering, altering, starting, or removing a Scheduled Task;
- creating, rotating, replacing, relocating, or inspecting the private DPAPI credential
  artefact, or changing how the host protects it. The implementation adds the
  secret-free import, injection, and cleanup behaviour of section 9.2 and nothing more;
- provisioning, installing, updating, or repairing the private browser cache. The
  implementation binds and validates it, per section 9.3, and never writes into it;
- updating the deployed checkout, or any Git operation that fetches or mutates state,
  per section 10.3;
- contacting the live Energy@Grid portal, or any headed or live run;
- replacing, editing, or cleaning up anything on the live server;
- changing the Python application, its status vocabulary, or its exit codes;
- changing any GitHub Actions workflow;
- creating another one-shot owner bridge;
- promoting any retired Run119 bridge or diagnostic probe into a repository artefact.

## 3. Source-Of-Truth Boundary

This boundary is the load-bearing decision of the design. Every later section is
constrained by it.

### 3.1 Git is canonical for

| Concern | Canonical form in Git |
| --- | --- |
| Production launcher behaviour, secret-free | `runtime/launcher.ps1` plus its pure library |
| Launcher installation and update mechanics | `runtime/install_or_update_launcher.ps1` |
| ValidateOnly behaviour | A switch on both entry scripts, contract in section 8 |
| Runtime binding and validation rules | Pure library predicates, contract in section 5 |
| Atomic replacement semantics | `Invoke-AtomicFileReplace`, contract in section 7 |
| Rollback semantics | Installer transaction only, contract in section 6.5 |
| Exception and diagnostic reporting | Bounded `EG_LAUNCHER_*` vocabulary, section 11 |
| Security and ACL expectations | Named checks with expected outcomes, section 17 |
| Reusable environment hardening | Governed Git invocation, section 10 |
| Credential import, injection, and cleanup behaviour | Secret-free logic in the pure library, contract in section 9.2 |
| Private browser-cache binding behaviour | Secret-free logic in the pure library, contract in section 9.3 |
| Unattended source-integrity rules | Path-scoped governed checks, contract in section 10.3 |
| Installed-launcher verification method | Manifest shape and comparison, contract in section 6.4 |
| Scheduler integration contract | The action shape only, once separately approved |
| Tests | `energygrid-bill-downloader/tests/` |
| Operator and runbook documentation | `energygrid-bill-downloader/docs/` and `runtime/README.md` |

### 3.2 The server remains canonical for

Credentials; DPAPI credential material; the private JSON configuration and its
`account_identity`; every private absolute path; Python and Chromium installation
state; downloaded PDFs; the SQLite operational manifest; logs; temporary and runtime
data; host-specific secrets, principals, and identities.

### 3.3 Rules that keep the boundary from drifting

- A value that is host-specific is a parameter or a private configuration key. It is
  never a default, a fallback, or a literal in a committed file.
- A behaviour that would be identical on a second Energy@Grid host is reusable and
  belongs in Git, even if it was first discovered on this host.
- Committed example configuration carries placeholders only. The existing
  `config/energygrid.example.json` shape is the precedent: a replace-me
  `account_identity` and illustrative paths, never a live configuration.
- One-shot Run119 owner bridges and diagnostic probes are not repository artefacts and
  are not to be reconstructed as such. Only the reusable behaviour they proved is
  represented here, as contracts and tests.
- Git is never made authoritative for private deployment state. The installer writes a
  deployed copy; it never reads private state back into the checkout.

## 4. Proposed Repository Structure

The Energy@Grid project is deliberately self-contained under
`energygrid-bill-downloader/`, and its README says so. The runtime layer stays inside
that boundary.

```text
energygrid-bill-downloader/
  runtime/
    launcher.ps1                    # entry script: side effects, argument surface, exit codes
    launcher_lib.ps1                # pure, dot-sourceable library: no side effects
    install_or_update_launcher.ps1  # entry script: install or update the deployed copy
    launcher.settings.example.json  # placeholder-only host settings shape
    README.md                       # directory-level runtime contract and source of truth
  task-scheduler/
    register_task.example.ps1       # unchanged, still inert
  tests/
    test_runtime_launcher.py        # offline unittest driver for the runtime layer
```

Naming follows the repository, not the request packet's illustrative names. Committed
PowerShell in this repository is snake_case (`member_create_uat_runner_lib.ps1`,
`install_autocount_stock_extract_task.ps1`, `register_task.example.ps1`), so
`install_or_update_launcher.ps1` is used rather than a hyphenated form.

Two structural properties matter and are not incidental:

- **Pure library, impure entry.** `launcher_lib.ps1` contains no filesystem mutation
  at import time, no network, no Git invocation at load, and no live action. Every
  function is deterministic given its inputs and its explicitly supplied paths. This is
  what makes the offline tests possible, and it mirrors
  `scripts/member_create_uat_runner_lib.ps1`.
- **Tests inside the discovered suite.** `energygrid-bill-downloader/tests/` is already
  the directory the CI job runs `python -m unittest discover -s tests -v` against, and
  default discovery matches `test*.py`. Adding `test_runtime_launcher.py` therefore
  needs no workflow edit.

Separation of the seven concerns named in the design brief maps onto that structure as
follows: launcher behaviour, validation and security checks, atomic replacement, and
rollback live in `launcher_lib.ps1` as separate functions; launcher invocation lives in
`launcher.ps1`; installation and update live in `install_or_update_launcher.ps1`;
private host configuration is external input only; tests live in
`tests/test_runtime_launcher.py`; operator documentation lives in `runtime/README.md`
with the operator sequence added to `energygrid-bill-downloader/docs/runbook.md`.

## 5. Launcher Contract

`runtime/launcher.ps1` starts the reviewed Python application on the production host.
It is the only thing the Scheduled Task will ever invoke, once scheduling is separately
approved.

### 5.1 Parameter surface

| Parameter | Required | Meaning |
| --- | --- | --- |
| `-ConfigPath` | yes | Absolute path to the external private JSON configuration |
| `-PythonExe` | yes | Absolute path to the approved Python 3.14.x interpreter |
| `-CheckoutRoot` | yes | Absolute path to the deployed checkout root |
| `-CredentialPath` | yes | Absolute path to the private DPAPI PSCredential CLIXML artefact, section 9.2 |
| `-BrowserCachePath` | yes | Absolute path to the approved private Playwright browser cache, section 9.3 |
| `-ExpectedBranch` | yes | Branch the deployed checkout must be on, or the literal `ANY_BRANCH`, section 10.3 |
| `-AuthorisedLauncherRootWriteSid` | yes | One or more SID strings naming the exhaustive set of trustees permitted to hold write-capable access on the launcher root, section 17.2.1 |
| `-Command` | no | `run` (default), `list`, or `login-diagnostic` (section 5.4) |
| `-LogRoot` | no | Private diagnostics root for the launcher's own terminal event |
| `-ValidateOnly` | no | Switch, contract in section 8 |
| `-RunId` | no | Correlation identifier for the terminal event only |

`-CredentialPath` and `-BrowserCachePath` locate private host artefacts; they never
carry a credential value or a committed path. `-ExpectedBranch` is mandatory with an
explicit `ANY_BRANCH` sentinel rather than optional, so branch binding is never
disabled by omitting an argument. There is no parameter that accepts a credential
value, no portal parameter, no browser-install parameter, no commit-pin parameter, and
no headed switch. `-Command` is a closed allowlist of three fixed operation names and
is the only thing that varies in the invocation.

Amendment A1 does not add a headed switch and does not make headed execution selectable.
Headed execution is an implicit and non-overridable property of the fixed
`login-diagnostic` operation alone: the application constructs a headed browser for that
one command and for no other, and there is no argument -- launcher or application -- by
which a caller can request it, suppress it, or apply it to `run` or `list`. The
application's own `--headed` flag remains reachable only on `run` and `list` through a
separately approved manual invocation, never through the launcher.

`-AuthorisedLauncherRootWriteSid` is the launcher root's write-authority binding. It is
mandatory for the same reason `-ExpectedBranch` is: omitting an argument must never
silently disable a security expectation. It carries SID strings rather than principal
names, it has no default, and its contract is section 9.1 with its evaluation in section
17.2.1.

The launcher deliberately has no `-ExpectedCommit`. Pinning normal unattended execution
to one fixed whole-repository commit is the wrong contract for a monorepo, and section
10.3 replaces it with a path-scoped integrity check. Exact-commit admission survives
only in the operator-controlled install lane, section 6.1.

### 5.2 Preflight, in order

Every check runs before any child process is started. A failure is terminal; the
launcher never continues past a failed check, and never downgrades one to a warning.

1. Launcher-root integrity, in this order (section 6.7 defines the classes, section 6.4
   the manifest rules):
   a. Enumerate the launcher-root entries and classify each as Class A package member,
      Class B recognised installer-owned residue, or Class C unexpected.
   b. Any Class C entry is terminal. Recognised residue is tolerated but never treated as
      a warning that could mask another integrity failure.
   c. All three Class A members are present at their fixed names.
   d. `launcher.ps1` and `launcher_lib.ps1` parse cleanly and hash-match the manifest
      recorded by the last accepted installation, and the manifest describes exactly those
      two executable members.
   If the package itself is invalid, the run fails regardless of how residue classified.
2. `-CheckoutRoot`, `-ConfigPath`, and `-PythonExe` exist and are absolute.
3. `-ConfigPath` resolves outside `-CheckoutRoot`.
4. The private configuration parses as JSON and carries every key the application
   requires. The launcher does not re-validate the application's own path rules; the
   application already enforces them in `energygrid_bill_downloader/config.py` and
   duplicating them here would create two sources of truth.
5. The interpreter reports a 3.14.x version.
6. Governed source integrity over the EnergyGrid runtime-critical surface passes
   (section 10.3).
7. The private browser cache resolves and passes its readiness check (section 9.3).
8. Security expectations on the launcher root hold, in the order the section 17.2
   table lists them (section 17.2.1 defines the exact semantics):
   a. `launcher_root_not_writable_by_run_principal`: the token this launcher process
      is actually running under has no write-capable effective access to the launcher
      root or to any package member.
   b. `launcher_root_write_trustees_authorised`: every trustee holding a write-capable
      grant on the launcher root or on a package member, and each examined object's
      owner, is inside the set supplied on `-AuthorisedLauncherRootWriteSid`.
   c. `launcher_files_not_reparse_points` and
      `launcher_files_not_unexpectedly_readonly`.
   Check 8a asks Windows to evaluate the running token. Check 8b inspects the
   discretionary access control list. They are different questions, neither implies
   the other, and both are terminal.
9. The private DPAPI credential artefact imports and yields a non-empty username and a
   non-empty password (section 9.2).

Steps 1 to 8 are the non-secret preflight. Every one of them completes and passes before
step 9 runs, so credential import is literally the last check and the only one that
materialises secret-derived values in memory. A run that will fail for any other reason
never opens the credential artefact at all.

The ordering is a security property, not a performance preference, and it is asserted by
`EGRT-T48` rather than left to reading order. Placing the launcher-root ACL and
reparse-point checks after a credential import would mean decrypting a credential on a
host whose launcher root had already failed its integrity expectations, which is exactly
backwards.

### 5.3 Invocation and exit codes

On success the launcher invokes
`<PythonExe> -m energygrid_bill_downloader <Command> --config <ConfigPath>` with the
`energygrid-bill-downloader` directory beneath `-CheckoutRoot` as the working
directory, and propagates the child's exit code verbatim.

The child argument vector is exactly those five elements, in that order, for every
admitted command including `login-diagnostic`. Nothing is appended conditionally, no
argument is derived from the operation, and there is no path by which a caller-supplied
script, module, path, portal address, credential value, or arbitrary child argument
reaches the child.

The environment is not passed through unchanged. Immediately before the child starts,
the launcher sets exactly three process-scope variables from the values established in
preflight: `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` from the imported credential
(section 9.2), and the Playwright browser-cache variable from `-BrowserCachePath`
(section 9.3). Each is restored to its exact prior process state in a finally-equivalent
path once the child exits or fails to start. No other environment change is made.

The application returns `0`, `10`, `20`, or `64`. The launcher's own failures therefore
use a disjoint band, so a launcher failure can never be mistaken for an application
status:

| Code | Meaning |
| --- | --- |
| `0`, `10`, `20`, `64` | Propagated unchanged from the application child process |
| `70` | Launcher preflight or validation failed; no child process was started |
| `71` | Installation failed before any destination was mutated; the installed package is unchanged |
| `72` | Installation or package verification failed before acceptance; installer-owned package rollback completed and was positively verified |
| `73` | Package rollback failed, could not be verified, or an advanced destination had no recoverable preimage; manual owner action required |

The disjointness of the two bands is asserted by a test, not left to convention.

### 5.4 Bounded login diagnostic operation

`login-diagnostic` exists because the launcher is the only thing that materialises the
private DPAPI credential, and the mechanism gap it closes is narrow: observing what the
portal actually renders after one real Login submit previously required either a headed
switch on the launcher or a second credential route, and both were refused.

The operation runs the same canonical pre-submit login sequence the ordinary `login()`
path runs -- portal navigation, the existing public semantics activation, exactly one
Login-entry click, the existing typed Username entry, the existing typed Password entry,
the existing submit readiness resolution, and exactly one real submit dispatch. That
sequence exists in one place in `portal.py`; the diagnostic reuses it rather than
carrying a second set of selectors or credential-entry semantics, so the two cannot
drift apart. There remains exactly one normal `submit.click()` call site, no submit
fallback, no alternate selector, and no submit retry.

Where the two paths differ is what happens next. `login()` continues into
`_await_billing_manager()` exactly as before. The diagnostic stops the normal
application flow at the submit and performs only a bounded read-only observation.

**Submit uncertainty.** The real submit invocation is the explicit one-shot boundary.
Once invocation begins the submit is recorded as dispatched: a successful call reports
`DISPATCHED`, and a call that raises reports `DISPATCH_UNCERTAIN`, because an exception
cannot prove whether the browser acted. It is never retried. A readiness or resolution
failure before that boundary reports `NOT_DISPATCHED`. A dispatch-uncertain result may
still be observed, since the page is the only remaining evidence, but it is never
converted into a proven successful dispatch.

**Observation allowlist.** Only fixed public-safe counts and booleans are read:
host and render counts for `flt-glass-pane`, `flt-semantics-host`, `flt-semantics`,
`flt-text-editing-host`, `flt-scene-host` and `canvas`; the count and presence of
`flt-semantics-placeholder`; count and visibility for Billing Manager, Username and
Password; count, visibility and actionability for the exact `Login` and exact
`Enable accessibility` controls; a visible-alert boolean; and, after the submit, a
URL-changed boolean. No page text, HTML, DOM dump, accessibility-tree dump, attribute
outside these observations, screenshot, trace, storage capture, network capture, or URL
is read or reported. Locators are freshly resolved at every observation checkpoint. The
observation runs on the existing bounded portal recovery ladder and deadline; it does
not raise the timeout and adds no unbounded polling loop. Post-submit observation is at
most 60 seconds.

**Observation settling.** Only two outcomes end the window early. A positive
`BILLING_MANAGER_VISIBLE` and a decisive `VISIBLE_ALERT` are terminal on sight, because
neither can be improved on by looking again. `LOGIN_ROUTE_PERSISTED_OR_RETURNED`,
`SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS` and
`FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT` are provisional while the window
runs. They are what a post-submit Flutter route legitimately presents on its way
somewhere else, so an early one is preserved as the current observation rather than
concluded, and the window keeps looking with freshly resolved locators until the
deadline; a later Billing Manager or visible alert supersedes it. Once the window is
spent without either, the persistent classifications are determined from the **final**
bounded observation, under the same classifier and the same priority order below: a
persistent login route finally reports (3), a persistent semantics host (4), and a
persistent render shell (5). This governs when a classification is concluded, not what
any classification means. The vocabulary, the priority order, the positive evidence each
arm requires, the checkpoint ladder, the 60-second ceiling, the single submit and the
no-retry rule are all unchanged, and an unreadable or ambiguous final observation still
fails closed with no classification at all.

**Classification.** The first satisfied classification wins, in this order:

1. `BILLING_MANAGER_VISIBLE`
2. `VISIBLE_ALERT`
3. `LOGIN_ROUTE_PERSISTED_OR_RETURNED`
4. `SEMANTICS_HOST_PRESENT_WITHOUT_APP_CONTROLS`
5. `FLUTTER_RENDER_SHELL_PRESENT_SEMANTICS_HOST_ABSENT`
6. `FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT`

Anything else fails closed with no classification at all. Each arm needs positive
evidence, never the absence of a contradiction:

- `BILLING_MANAGER_VISIBLE` requires an unambiguous positive visible Billing Manager
  witness. A stale, hidden, or multiple locator does not classify.
- `LOGIN_ROUTE_PERSISTED_OR_RETURNED` requires at least one positively **visible**
  Username, Password, or exact Login witness. A locator with a count above zero whose
  matches are all hidden is ambiguous evidence and is not sufficient.
- The three shell-only classifications require every known post-submit application and
  public control absent **by count**: Billing Manager, Username, Password, exact Login,
  and exact `Enable accessibility` all zero. One counted control -- including the public
  accessibility gate -- blocks all three.
- With controls absent, a present semantics host or surface yields (4); otherwise a
  present non-semantics render or shell witness yields (5).
- `FLUTTER_SHELL_DISAPPEARED_AFTER_SUBMIT` additionally requires at least one
  allowlisted shell or render witness positively present immediately before the submit,
  and every allowlisted shell or render witness zero throughout the whole post-submit
  observation. A shell that was never positively established cannot be inferred to have
  disappeared. This is unchanged, and it is never an early conclusion: it is decided
  from the throughout-window record once the window is spent, before the final
  observation is classified. A shell or a known control seen at any checkpoint breaks
  that record, so it cannot collide with (3), (4) or (5).

A witness that could not be read is null, and null satisfies neither a positive test nor
an absence test, so an unreadable surface fails closed rather than classifying.

**Side-effect boundary.** The diagnostic is structurally unable to reach
`_await_billing_manager`, the Billing Manager click, EB Bill, tenant or account
selection, Search, inventory, pagination, download, PDF publication, archive mutation,
state mutation, temp cleanup, idempotency or reconciliation, the Scheduler, n8n, or
AutoCount. On the application side it loads and validates the configuration without
mutating the filesystem, and it does not call `config.preflight()`, construct the
`SafeLogger`, run stale-temp cleanup, or open the `StateStore`. Its only intended real
effects, when separately authorised live, are one Chromium process from the accepted
private cache, one portal navigation, one semantics activation, one Login-entry click,
typed credentials, at most one normal Login submit, the bounded observation, and browser
teardown.

**Result document.** The operation emits exactly one JSON document with schema
identifier `energygrid.login_diagnostic.v1`, carrying `schema`, `status`,
`classification`, `submit_dispatched`, `submit_outcome`, `pre_submit`, `post_submit`,
and `support_ref` on a non-complete result where applicable. `status` is
`DIAGNOSTIC_COMPLETE` or `ACTION_REQUIRED`; `classification` is one of the six accepted
values or null; `submit_outcome` is `DISPATCHED`, `DISPATCH_UNCERTAIN`, or
`NOT_DISPATCHED`. Every remaining value is a count, a boolean, or null. No free-form
exception text, URL, filesystem path, account identity, host identity, security
identifier, credential, credential derivative, page text, or business datum is a field,
so none can reach the surface.

**Exit codes.** The diagnostic returns `0` only when an authorised classification is
positively established, `20` for any fail-closed diagnostic, layout, login, or
insufficient-evidence result, and `64` for a configuration, dependency, or argument
contract failure. It never emits `10`. The launcher's own `70`-`73` band is unchanged
and remains disjoint from it.

**What A1 does not change.** `run` and `list` behave exactly as before. Scheduler
semantics are unchanged: the Scheduled Task invokes `run`, and no scheduler artefact
names the diagnostic. `-ValidateOnly` is unchanged and still invokes no command at all.

## 6. Installation And Update Contract

`runtime/install_or_update_launcher.ps1` publishes the reviewed launcher from the
deployed checkout into the launcher root. It is the only sanctioned way the installed
launcher ever changes.

### 6.1 Parameters

`-CheckoutRoot` (source, required), `-LauncherRoot` (destination, required),
`-AdmissionCommit` (required, full 40-character hex), `-ValidateOnly` (switch),
`-LogRoot` (optional), `-RunId` (optional). No credential, browser-cache, scheduler,
ACL-mutation, or cleanup parameter exists.

`-AdmissionCommit` is the operator-controlled admission lane, and it is where an exact
reviewed source commit legitimately belongs. Installation is a supervised action that
publishes reviewed bytes outside the checkout, so requiring the operator to name the
exact commit being published is proportionate, and it is what the Run119 repair lane
historically enforced. It is mandatory rather than optional so that admission can never
be skipped by omitting an argument.

That requirement stops at this lane. It is not inherited by normal unattended
execution, which uses section 10.3 instead. Conflating the two would make every
unrelated merge on `main` block the daily EnergyGrid job, which is precisely what
`DL-XB-141-SCHEDULER-005` forbids.

### 6.2 Deployed package set and transaction sequence

**The deployed package is exactly three members**, and the installer publishes nothing
else into the launcher root:

| Member | Role |
| --- | --- |
| `launcher.ps1` | The entry script the Scheduled Task invokes |
| `launcher_lib.ps1` | The pure library the entry script dot-sources |
| Installation-integrity manifest | Generated, describes the other two, section 6.4 |

Sharing the `runtime/` directory does not make a file deployable. These stay checkout,
source, or operator material and are never copied to the launcher root:
`install_or_update_launcher.ps1` (the operator runs it from the checkout, and deploying
it would place an installer inside the surface it installs),
`launcher.settings.example.json` (a placeholder shape, superseded on the host by real
private settings), and `runtime/README.md` (documentation). The set is enumerated
explicitly in source rather than derived from a directory listing, so a file added to
`runtime/` later cannot become deployable by accident. `EGRT-T47` asserts this.

**The three members are one transaction.** Publishing `launcher.ps1` successfully and
committing before `launcher_lib.ps1` or the manifest succeeds would leave a mixed
installation: a new entry script dot-sourcing an old library, or executable bytes that
the manifest does not describe. Section 5.2 step 1 would then fail every subsequent run,
having been made to fail by the installer itself. The four phases below exist to make
that outcome unreachable.

#### Phase 1 - Admission and prepare (no destination mutation whatsoever)

1. Verify `-AdmissionCommit` and the source checkout.
2. Resolve the exact deployable source set from the explicit enumeration above.
3. Parse-check and SHA-256 every source file.
4. Classify every destination as `Existing` (recording its preimage hash and byte length)
   or `Absent`. This classification selects the publish primitive in section 7.1 and is
   established before anything is written.
5. If every deployed member already matches its source hash and the manifest already
   agrees, report `ALREADY_CURRENT`, mutate nothing, and exit `0`. Installation stays
   idempotent by hash, not by timestamp.
6. Write and fully verify every staging file, for all changed members, before publishing
   any of them.
7. Construct the candidate manifest contents from the admitted source set, the source
   hashes, and `-AdmissionCommit`.
8. Validate the candidate manifest against its shape rules.

Any failure in Phase 1 leaves **zero destination mutation**. The installer exits `71`
with the destination package byte-identical to its pre-transaction state.

#### Phase 2 - Publish the executable members

For each changed executable member, in a recorded order:

- `Existing` preimage: publish through `Invoke-AtomicFileReplace` (section 7.2) with an
  explicit same-directory backup path.
- `Absent` preimage: publish through `Invoke-PublishToAbsentDestination` (section 7.4).

After each publication the destination hash is verified immediately. **No backup is
deleted in this phase.** The installer records, for every touched destination, its
preimage state, its preimage hash where one existed, its backup path where one exists,
and whether it has been published. That record is the transaction state that Phase 4 and
section 6.5 both depend on.

#### Phase 3 - Publish and verify the manifest, inside the same transaction

The manifest is a package member, not an epilogue.

1. Publish it only after every executable member has individually verified.
2. If a manifest already exists, publish it through the explicit-backup replacement path
   and retain that backup exactly like any other member's. If none exists, record
   `PreimageState = Absent` and use the publish-to-absent path.
3. Re-read the published manifest from disk and verify: the exact expected entry set, the
   exact expected hashes, the exact expected byte lengths, the admitted commit, and no
   extra or missing deployed member.
4. Re-verify every deployed executable file against the manifest just read back, so the
   installed bytes and the record of them are proven mutually consistent rather than
   assumed to be.

#### Phase 4 - Commit

The transaction is accepted only when every executable member has verified, the manifest
has been published and read back correctly, and the full package re-verification in
Phase 3 step 4 has passed. Only after acceptance may retained backups be reaped, under
section 6.6. Any failure before acceptance triggers package rollback under section 6.5.

### 6.3 Constraints

- The installer never writes into the Git checkout, never reads private configuration,
  never reads or decrypts credentials, and never touches the archive, the SQLite state,
  the logs, or the Scheduled Task.
- Staging and backup files are created in the destination directory only, are never
  created in a shared temporary directory, and are named strictly according to the
  reserved Class B contract in section 6.7. That contract is what makes them recognisable
  later; a staging or backup file created under any other name would be classified Class C
  by the launcher and would fail the next run.
- A file the installer did not create is never deleted.
- Failure-path retention is scoped, not blanket. Installed package destinations are
  restored or removed exactly as section 6.5 requires: a destination whose preimage was
  `Absent` is removed and confirmed absent after a verified rollback, and is **not** kept
  behind for diagnosis. What may be retained for inspection is transaction-owned residue
  that rollback did not consume: staging files, unused backups, and diagnostic artefacts.
  Retention must never obstruct restoring the package to its exact pre-transaction state.

### 6.4 Installed-launcher integrity manifest

Section 5.2 step 1 requires the launcher to confirm that the installed files still match
the last accepted installation. That expected state has to come from somewhere, and the
boundary matters: the verification **method** is reusable and belongs in Git, while the
**record** is private deployment state.

- The installer generates an installation manifest in the launcher root as a member of
  the same transaction, published in Phase 3 after every executable member has verified
  and before package acceptance. It records, for each deployed executable file, the
  relative file name, its SHA-256, its byte length, and the `-AdmissionCommit` the bytes
  came from.
- The manifest is generated from the reviewed source during installation. It is never
  hand-authored, never copied from a GitHub issue comment, never reconstructed from a
  Run119 bridge or a `%TEMP%` script, and never committed to Git.
- Git is canonical for the manifest's **shape** and for the comparison rules: which
  fields exist, how the hash is computed, and that a missing, unparsable, or
  non-matching manifest is terminal rather than a warning.
- **Manifest completeness is evaluated over the deployed package-member domain only**, not
  over every file present in the launcher root. The launcher recomputes each executable
  member's hash and compares it to the manifest. A hash or length mismatch, a manifest
  entry with no corresponding package member, or a package member with no manifest entry
  each fail closed.
- Unexpected files are caught by a separate step, not by manifest membership. The
  launcher-root inventory is classified under section 6.7 and any Class C entry is
  terminal. Splitting the two concerns is what lets the manifest stay an exact description
  of the deployed package while arbitrary extra files still fail closed.
- The manifest never describes transaction residue. Residue is Class B, is excluded from
  manifest membership by design, and its absence from the manifest is therefore not a
  completeness failure.

An honest limitation, stated because a reader will otherwise assume more: the manifest
sits beside the files it describes, so a writer who can modify the launcher can also
modify the manifest. It is tamper-evident only to the extent that the launcher-root ACL
expectations in section 17.2 hold. Those checks are therefore a precondition for
trusting the manifest, and both are terminal. The manifest defends against accidental
drift, partial installation, and unreviewed hand-editing; it does not by itself defend
against an attacker who already has write access to the launcher root.

### 6.5 Package rollback

The installer transaction is the **only** rollback authority in this design. Neither
section 7 primitive restores anything; they publish and report, and section 6.5 decides.
That single-owner rule is what keeps reverse-order restoration correct, because only the
installer knows which members advanced and in what order.

Any failure after the first destination mutation and before package acceptance triggers
package rollback. Rollback proceeds in **reverse publication order** over the **touched
set**, using the recorded transaction state from Phase 2, and it covers the manifest
exactly as it covers the executable members.

**Membership of the touched set is decided by `PublicationOccurred`, not by which member
failed.** A member enters the touched set when its publication advanced the destination.
The member whose failure ended the transaction is therefore included only if it actually
advanced:

- Current member, Case A of section 7.2.1 (threw before publication): it did not advance.
  Do not attempt to restore it. Verify it still hashes to its recorded preimage, then roll
  back only the earlier members that did advance.
- Current member, Case B (returned, postimage verification failed): it advanced. It is the
  most recent entry in the touched set, so it is restored first, then the earlier members
  in reverse order.

- **Preimage `Existing`.** Restore the exact retained backup over the destination using
  the explicit-backup replacement semantics of section 7.2, then re-hash the restored
  destination and confirm it equals the recorded preimage hash. A restoration that
  cannot be positively verified is not treated as successful.
- **Preimage `Absent`.** Remove exactly the destination this transaction created, then
  positively confirm it is absent again. The installer never removes a file it did not
  create in this transaction, and never removes a destination whose current content no
  longer matches what it published, since that means something else has taken ownership.

After rollback the installer re-verifies that the **entire** installed package equals its
pre-transaction state: every previously present member restored to its preimage hash, and
every member that was absent beforehand absent again.

- Rollback verified complete: exit `72`, the bounded installation-failed-and-rolled-back
  status.
- Rollback not completely verifiable: exit `73`, manual owner action required. The
  installer stops rather than attempting further repair, and every artefact is retained
  for inspection.

None of these outcomes is permitted to leave a new launcher with an old library, an old
launcher with a new library, executable files that disagree with the manifest, or a
manifest describing bytes that are not installed. `EGRT-T42` through `EGRT-T44` assert
those four states are unreachable.

### 6.6 Backup cleanup and post-commit residue

Reaping a backup is a transaction-commit action, never a per-file one. The split of
authority is deliberate:

- The section 7 primitives prove one publication and always return with the backup
  retained. They have no view of the package and therefore no authority to reap.
- The installer holds every retained preimage until Phase 4 acceptance, because until the
  manifest has been read back and the package re-verified, any of those preimages might
  still be needed by section 6.5.
- Only the Phase 4 commit step authorises cleanup, and only for backups this transaction
  created.

**If cleanup itself fails after acceptance**, the package is already committed, verified,
and correct on disk. Rolling back a fully accepted installation because a now-redundant
backup file could not be deleted would replace a good outcome with a worse one, so the
installer does not do that. Instead it:

- keeps the accepted installation;
- retains the undeleted backup as inert residue;
- reports a bounded, operator-visible status
  (`EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE`) naming how many backups remain;
- exits `0`, because the installation genuinely succeeded.

The failure is never swallowed. A committed installation with residue is reported as
exactly that, and removing the residue is a separate, safe operator action. This is a
narrow, visible exception to the fail-closed default, permitted because it concerns a
redundant artefact after a verified success rather than any part of the installed
package.

Any residue the installer leaves, on this path or after a verified rollback, must carry a
name that satisfies the reserved contract in section 6.7. The installer never leaves an
arbitrary filename in the launcher root and expects the launcher to tolerate it.

### 6.7 Launcher-root entry classes

Two rules in this document would otherwise collide. Section 6.6 permits an accepted
installation to leave an inert retained backup, exiting `0`. Section 6.4 requires an
unexpected runtime file to fail closed. Read as "every file in the launcher root must be a
manifest member", the second turns the first into an outage: a valid installed package
whose backup cleanup failed would fail every subsequent launcher preflight, including the
unattended daily job, over a file the installer itself deliberately left and called inert.

The resolution is not to tolerate extra files. It is to classify them. Every entry in the
launcher root belongs to exactly one of three classes, and the classification is
deterministic.

#### Class A - deployed package members

Exactly three, at fixed names, joined to the launcher root:

| Member | Fixed name |
| --- | --- |
| Entry script | `launcher.ps1` |
| Pure library | `launcher_lib.ps1` |
| Integrity manifest | `installation_manifest.json` |

The set is exact. A missing member fails closed, an extra member is impossible by
definition, and no substitution is tolerated. The manifest describes exactly the two
executable members, per section 6.4.

#### Class B - recognised installer-owned transaction residue

Residue is **not** a package member and is **not** described by the manifest. It is
recognised only by an exact reserved-name contract, deliberately narrow enough that an
ordinary file cannot drift into it:

```text
.eglauncher-<kind>--<member>--<operation-id>
```

- The entry name begins with the literal reserved prefix `.eglauncher-`.
- The remainder splits on the two-hyphen delimiter `--` into **exactly three** fields.
  Package member names contain `.` but never `--`, so the split is unambiguous.
- `<kind>` is exactly one of `staging`, `backup`, or `rollback`.
- `<member>` is exactly one of the three Class A fixed names.
- `<operation-id>` is a canonical lowercase GUID matching
  `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`, the transaction
  identifier the installer already owns under section 6.3.

Every field must parse. A name that fails any one of them is not residue; it is Class C.

A generic extension rule such as `*.bak` or `*.tmp` is explicitly **not** sufficient and
must never be used. Those patterns would let any file a user or attacker drops into the
launcher root be waved through, which is the opposite of the intent. The reserved name
binds residue to a specific member and a specific transaction, and it carries no private
path, host name, principal, or other private value: the member names are public, the kinds
are a fixed vocabulary, and the GUID is random.

The name also cannot be executed by accident. It begins with a dot and does not end in
`.ps1`, `.psm1`, or any executable extension.

#### Class C - everything else

Any entry that is neither an exact Class A member nor a valid Class B residue name fails
closed. This preserves the integrity intent in full. Each of the following is Class C and
terminal:

- another `.ps1` script, whatever it is called, including `launcher-old.ps1`;
- a copy of the library under a different name;
- a `.bak` or `.tmp` file that does not satisfy the Class B parser;
- a lookalike residue name with a malformed prefix, wrong field count, unknown kind,
  unrecognised member, or non-canonical GUID;
- any unexpected executable, script, or other file;
- any unexpected directory, since the locked architecture requires none.

The rule is never relaxed to "ignore extra files".

#### Security boundary

- Residue can never influence which code runs. Package-member paths are fixed by joining
  the launcher root with the three Class A names.
- The launcher never enumerates the root to *find* a script or library. It enumerates only
  to *classify*, and Class C is fatal.
- Recognised residue is never dot-sourced, invoked, imported, or searched as an alternate
  launcher or library, and never acts as a fallback.
- Recognised residue can never satisfy or replace a missing Class A member. A missing or
  invalid member fails regardless of what residue is present.
- The launcher-root ACL, reparse-point, and read-only expectations in section 17.2 remain
  in force and are unaffected by classification.

`EGRT-T51` through `EGRT-T57` assert each of these properties.

## 7. Atomic Replacement And Rollback Contract

This section is the direct, reusable consequence of the Run119 `File.Replace` defect
(section 18.2). It is a contract on a library function, not a patch to one host.

### 7.1 Two publish primitives, chosen by preimage state

`[System.IO.File]::Replace` requires the destination to already exist. It is a
replacement primitive, not a creation primitive, and calling it against a missing
destination fails. A design that routed every publication through it could therefore
never perform a clean first installation, which is precisely the disaster-rebuild case
this document exists to make reproducible.

There are consequently two primitives, and the installer selects between them by
positively establishing the destination's preimage state first. There is no third path,
no overwrite-style fallback, and no copy-over-existing fallback.

```text
Invoke-AtomicFileReplace          # preimage EXISTS
  -SourcePath        # the fully written staging file, mandatory
  -DestinationPath   # the file being replaced, mandatory
  -BackupPath        # explicit backup destination, mandatory, non-empty
  -ExpectedSha256    # hash the destination must have after replacement, mandatory

Invoke-PublishToAbsentDestination # preimage ABSENT, contract in section 7.4
  -SourcePath        # the fully written, parse-clean, hash-verified staging file
  -DestinationPath   # the destination, which must be absent, mandatory
  -ExpectedSha256    # hash the destination must have after publication, mandatory
```

Both return a structured result object carrying `Success`, `SupportRef`,
`ExceptionTypeName`, `HResult`, `PreimageState` (`Existing` or `Absent`),
`PreimageSha256` (empty when absent), `PostimageSha256`, `PublicationOccurred`,
`BackupCreated`, and `BackupRetained`.

There is no `RolledBack` field, because neither primitive ever rolls back. The fields
that matter to a failed caller are `PublicationOccurred`, which says whether the
destination advanced and therefore whether the member joins the transaction's touched
set, and `BackupCreated`, which says whether a preimage backup actually exists to restore
from. Section 7.2.1 defines both precisely.

### 7.2 Mandatory rules

1. **The backup path is always explicit.** `-BackupPath` is mandatory and validated as
   non-null, non-empty, and resolving to the same directory as `-DestinationPath`.
   `[System.IO.File]::Replace` is never called with `$null` or `''` as the third
   argument, anywhere, on any code path. The same-directory rule is stronger than the
   same-volume requirement `ReplaceFileW` imposes, and is enforced because it is
   mechanically checkable.
2. **Success is positively verified, never assumed.** After the call returns, the
   destination is re-hashed and compared to `-ExpectedSha256`. A returned call is not
   treated as a successful replacement until that comparison passes.
3. **The primitive never deletes a backup, and never fabricates one.** Per-file
   verification proves one publication; it does not prove the package is consistent, so
   authority to reap belongs to the installer's transaction commit phase (section 6.6)
   and to nothing else. Retention is therefore unconditional but existence is not:
   whenever the destination actually advanced, `ReplaceFileW` created the preimage
   backup, and that backup is left in place and reported. When the call threw before the
   destination advanced, Windows may never have created a backup at all, and the
   primitive does not pretend otherwise. `BackupCreated` reports what was observed on
   disk, not what `-BackupPath` requested; supplying a backup path is not evidence that a
   backup file exists. A caller publishing a single file outside a package transaction
   owns the reap decision explicitly rather than inheriting it silently.
4. **The primitive never rolls back.** It publishes and reports; it does not restore. On
   any failure it leaves the destination exactly as the failure left it, leaves any
   created backup in place, and returns the state the caller needs to decide what to do.
   Restoring a preimage is the installer transaction's exclusive responsibility under
   section 6.5, because only the installer knows which other package members have already
   advanced and in what order they must be undone. A primitive that quietly restored
   itself would race the installer's own rollback for the same member and could leave the
   package half-undone.
5. **Exceptions are classified, never swallowed.** Every failure records the exception
   type name and the HRESULT formatted as `0x%08X`, mapped to a bounded support
   reference (section 11). No exception message text reaches any output surface, and no
   control-flow decision anywhere is made by reading exception text.

### 7.2.1 The two existing-destination failure states

A failed replacement is not one condition but two, and conflating them is what makes a
package transaction incoherent. The distinction is drawn from explicit execution and
observed filesystem state, never from an exception message.

**Precondition that makes this decidable.** A member reaches publication only when its
source hash differs from its installed preimage hash; Phase 1 classifies anything else as
already current and publishes nothing. Preimage and postimage hashes are therefore always
distinguishable for any member that is actually published.

**Determination.** `PublicationOccurred` is true when the `File.Replace` call returned
normally, a flag set immediately after the call and before any verification work. It is
also true when the call threw but the destination's observed hash no longer equals the
recorded preimage, which means the destination advanced regardless of the exception. It
is false only when the call did not return normally **and** the destination still hashes
to its recorded preimage.

**Case A, threw before publication.** The sharing-violation, access-denied, and
invalid-argument classes in section 7.3 all behave this way.

| Field | Value |
| --- | --- |
| `Success` | false |
| `PublicationOccurred` | false |
| Destination | still the recorded preimage, verified by hash |
| `BackupCreated` | whatever was observed, normally false |
| `BackupRetained` | equals `BackupCreated`; nothing is created to satisfy the contract |

The member did **not** advance, so the installer does not add it to the touched set and
does not attempt to restore it. The installer still rolls back every earlier member that
did advance.

**Case B, returned but postimage verification failed.**

| Field | Value |
| --- | --- |
| `Success` | false |
| `PublicationOccurred` | true |
| Destination | advanced; no longer the preimage |
| `BackupCreated` | true |
| `BackupRetained` | true; the primitive does not restore or reap it |

The member **did** advance, so the installer adds it to the touched set and section 6.5
restores it first, then walks the earlier touched members in reverse publication order.

**The one unrecoverable combination.** If `PublicationOccurred` is true but the preimage
backup is not present on disk, the destination has advanced with no way back. That is
reported as `EG_LAUNCHER_REPLACE_PREIMAGE_UNRECOVERABLE` and drives exit `73`, manual
owner action required. It is called out rather than left implicit because it is the only
state in which the design cannot restore what it changed.

### 7.3 Observed failure classes the contract must handle

The Run119 synthetic proof established these deterministic behaviours on the production
compatibility boundary, Windows PowerShell 5.1 on .NET Framework 4.x:

| Condition | Observed result | Support reference |
| --- | --- | --- |
| Explicit same-directory backup path | Succeeds | not applicable |
| `$null` backup argument | `System.ArgumentException`, `0x80070057`, rejected before `ReplaceFileW` reaches the filesystem | `EG_LAUNCHER_REPLACE_ARGUMENT_INVALID` |
| Empty-string backup argument | Rejected | `EG_LAUNCHER_REPLACE_ARGUMENT_INVALID` |
| Destination held with an exclusive share | `System.IO.IOException`, `0x80070020` | `EG_LAUNCHER_REPLACE_SHARING_VIOLATION` |
| Read-only destination | `System.UnauthorizedAccessException`, `0x80070005` | `EG_LAUNCHER_REPLACE_ACCESS_DENIED` |

Because the invalid-argument rejection happens before the filesystem is touched, the
destination is byte-identical afterwards. The design turns that observation into an
asserted post-condition, scoped precisely: every failure class in the table above throws
before publication, so each is a Case A failure under section 7.2.1, and for each the
destination hash equals the recorded preimage hash and `PublicationOccurred` is false.

That post-condition belongs to these throw-before-publication classes only. It is
explicitly **not** claimed for a post-publication verification failure, where
`File.Replace` returned and the destination has already advanced. Asserting it there
would be false, and the earlier wording of `EGRT-T10` came close to implying it.

A compatibility note that the implementation must respect: the null-backup rejection is
an observed behaviour of the Windows PowerShell 5.1 and .NET Framework 4.x boundary.
Other runtimes may accept a null backup argument. The design therefore does not rely on
any runtime rejecting it. The prohibition is enforced structurally by the mandatory
parameter and by a static guard over the committed call sites, so the contract holds
regardless of which runtime executes it.

### 7.4 Publishing to an absent destination

This is the clean-first-install path. It is reached only when the destination's absence
has been positively established, and it never calls `[System.IO.File]::Replace`.

1. **Absence is established first, not assumed.** The destination is confirmed absent
   before anything is published. An unexpectedly present destination means the caller's
   preimage classification was wrong, and that is terminal
   (`EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT`) rather than something to
   recover from by overwriting.
2. **The staging file is complete before publication.** It is fully written, flushed,
   parse-clean, and hash-verified in the destination directory before the move begins.
   Nothing partially written is ever published.
3. **Publication is a same-directory move that will not clobber.** The staging file is
   moved to the destination with a primitive that fails rather than overwrites if the
   destination has appeared in the meantime. Windows `MoveFileExW` without
   `MOVEFILE_REPLACE_EXISTING` provides exactly that, and it is the same no-replace move
   discipline the application already uses when publishing a validated PDF. A losing race
   is reported (`EG_LAUNCHER_PUBLISH_RACE_LOST`), never resolved by replacing.
4. **The postimage is verified.** After the move, the destination is re-hashed and must
   equal `-ExpectedSha256`. Publication is not treated as successful until it does.
5. **The result records `PreimageState = Absent`.** There is no backup, because there was
   nothing to back up. `PreimageSha256` is empty and `BackupRetained` is false.
6. **Rollback means returning to absence.** If package verification later fails, undoing
   this publication is the removal of exactly the destination this transaction created,
   after which the destination is positively confirmed absent again. The installer never
   removes a file it did not create in this transaction, and never removes a destination
   whose content no longer matches what it published, since that would mean something
   else has taken ownership.

**A deliberate limit on what is claimed.** A no-replace move is atomic with respect to
concurrent observers on the same volume: the destination is either absent or the complete
file, never a partial one. It is not a crash-consistency guarantee. This design does not
claim that an operating-system crash mid-transaction leaves the package in a committed or
fully rolled-back state, because the primitive does not provide that and asserting it
would be false. What the design does provide is that a crash leaves the installed
package's prior members untouched, leaves retained backups in place, and leaves any
partially advanced transaction detectable on the next run through the manifest
verification in section 6.4. Recovery from that state is an operator action.

## 8. ValidateOnly Contract

`-ValidateOnly` exists so that every preflight, binding, security, and installation
check can be exercised on the production host without any production mutation, and
without owner approval for a mutating action.

- It runs every check the corresponding real path would run.
- It creates, modifies, deletes, and renames nothing. No staging file, no backup file,
  no log file, no directory, no scheduler entry, no environment change that outlives the
  process.
- It starts no child process other than the read-only interpreter version probe and the
  governed Git reads, and it never invokes `run` or `list`.
- It launches no browser, installs or updates no browser cache, and contacts no portal.
- It may import the private DPAPI credential artefact in-process to prove viability,
  because an artefact that cannot be imported is exactly the failure an operator needs
  to find before scheduling. The import is read-only, the imported object is discarded
  immediately after the three booleans below are derived, and no value survives it.
- It records only `credential_import_ok`, `username_nonempty`, and `password_nonempty`
  as booleans. It never emits, logs, hashes, measures, or otherwise derives a reportable
  quantity from either credential value.
- It never emits the value supplied on `-AuthorisedLauncherRootWriteSid`, any other SID,
  any trustee or owner name, or any count derived from them. The two launcher-root write
  checks reach the output as a check name and a pass or fail outcome, nothing more.
- `launcher_root_not_writable_by_run_principal` is evaluated against the token of the
  process actually running the launcher. A `-ValidateOnly` run performed from an elevated
  prompt, or under any account other than the one the unattended job uses, therefore
  tests a principal the job will not use; it may legitimately fail, and a pass obtained
  that way is not evidence about the job. Section 14 step 6 states the operator
  requirement that follows.
- It emits exactly one JSON object on standard output: a `checks` map of check name to
  outcome, an overall `status`, and a `support_ref` when the overall status is not a
  pass. The object contains no path, no environment value, no credential value, no
  account identity, and no Git output text.
- Its output is deterministic. It carries no timestamp and no generated identifier, so
  two consecutive runs against unchanged host state produce byte-identical output. This
  is what makes the idempotency assertion in section 12 a strict byte comparison rather
  than a fuzzy one.
- Exit `0` means every check passed. A failure exits `70` and names the first failing
  check by its stable name.

## 9. Private Configuration And Credential Boundary

### 9.1 Host-supplied values

- The launcher receives every host-specific value as an explicit parameter. There is no
  default that encodes a private path, a private identity, or a host name.
- `launcher.settings.example.json` may be committed as a shape, carrying
  `REPLACE_WITH_...` placeholders only, following the precedent set by
  `config/energygrid.example.json`.
- `account_identity` stays where it already is: in the external private JSON, never on
  a command line, never in a log, never in the repository.
- The launcher writes only to the private diagnostics root supplied by `-LogRoot`, and
  only the single terminal event described in section 11.
- The exhaustive set of trustees permitted to hold write-capable access on the launcher
  root is host-specific, so it is supplied explicitly, on
  `-AuthorisedLauncherRootWriteSid`. The operator who performs the installation supplies
  it, from the launcher root's intended administrative ownership on that host. Its
  accepted representation is one or more SID strings in the standard `S-R-I-S-S...`
  textual form. Account names are not accepted: names are ambiguous, locale-dependent,
  and accepting them would make a security check depend on a name-resolution lookup
  performed at check time. The parameter is the only route by which the value reaches
  the launcher; there is no default, no committed example value, no environment-variable
  form, and no file the launcher reads it from. Any private operator record of the value
  lives with the other private deployment state, outside Git. The value is never written
  to a log, to the terminal event, or to `-ValidateOnly` output.

### 9.2 DPAPI credential loading, injection, and cleanup

The production launcher already loads credentials itself, from a Windows-user-bound
DPAPI `PSCredential` CLIXML artefact. That is accepted runtime architecture, and it is
reusable, secret-free logic. Leaving it on the server would defeat the whole point of
this design, so Git becomes canonical for the behaviour while the artefact stays
private.

**What stays private.** The DPAPI artefact itself, its absolute path, the Windows user
identity it is bound to, and every value it yields. The artefact is never committed,
never copied into the checkout, and never reconstructed by the repository. Its location
reaches the launcher only through `-CredentialPath`, supplied from private host
settings.

**What Git owns.** The import, injection, and cleanup behaviour, expressed without any
private value:

1. Resolve `-CredentialPath`. A missing or unreadable artefact is terminal
   (`EG_LAUNCHER_CREDENTIAL_ARTEFACT_MISSING`).
2. Import it locally with the platform CLIXML import. DPAPI binds the artefact to the
   Windows user, so an import under any other user fails by construction rather than by
   a check the launcher has to write. A failed or non-`PSCredential` import is terminal
   (`EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED`).
3. Require a non-empty username and a non-empty password. Either being empty is terminal
   (`EG_LAUNCHER_CREDENTIAL_INCOMPLETE`).
4. Every failure above happens before the Python child starts. There is no path on which
   the application is launched with absent, partial, or unverified credentials.
5. Values exist in memory only. They are never written to disk, never passed as
   command-line arguments, never placed in a result object, never logged, and never
   emitted in any derived form, including length, prefix, suffix, or hash.

**Injection scope.** Immediately before the child starts, the launcher captures the
prior process-scope state of `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD`, then sets
both at **process scope only** for the bounded child execution. It never writes User-scope
or Machine-scope environment variables, so no persistent EnergyGrid credential variable
is ever created on the host and the existing absence of those persistent variables is
preserved.

**Restoration.** In a finally-equivalent path, reached whether the child succeeded,
failed, or never started, the launcher restores the exact prior process state: a variable
that was absent beforehand is removed rather than left set to an empty string, and a
variable that was present is restored to its exact original value. A failed restoration
is terminal (`EG_LAUNCHER_CREDENTIAL_RESTORE_FAILED`) rather than silent.

**Cleanup.** The contract is bounded lifetime and reference removal, stated in terms the
platform can actually honour:

- The `PSCredential` reference is held for the minimum lifetime required, and is removed
  or nulled as soon as the child no longer needs it.
- Plaintext-bearing temporaries are removed from scope promptly, and no credential-derived
  reference is retained past the finally block.
- The process environment is restored exactly, on the finally-equivalent path described
  above.
- `Dispose` is called only on an object that genuinely implements `IDisposable` and whose
  lifetime the launcher owns.

Two things this design explicitly does **not** require, because requiring them would
produce code that cannot work:

`System.Management.Automation.PSCredential` does **not** implement `IDisposable`. The
launcher must never call `$credential.Dispose()` or any equivalent; doing so would throw
at runtime. `EGRT-T49` asserts that no committed cleanup path attempts it. Disposing the
`SecureString` reached through `.Password` is permitted only where the launcher genuinely
owns that instance and no still-live object depends on it; where ownership is not clear,
reference removal is the correct and sufficient action.

Nor does this design claim cryptographic erasure of managed memory. .NET string interning
and garbage collection make that claim false, and a false guarantee is worse than a
bounded one. What is guaranteed is bounded lifetime, exact environment restoration, and
no persistence to disk or to any durable environment scope.

**Application interface is unchanged.** The application still reads only
`ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` from its environment, exactly as
`energygrid-bill-downloader/README.md` documents. The DPAPI import is the "approved host
mechanism" that README refers to, now written down rather than assumed, so no
application change is implied.

### 9.3 Private Playwright browser-cache binding

The launcher must positively bind the approved private browser cache and fail closed. It
must never silently fall through to an ambient or default Playwright cache, because a
run that quietly uses an unreviewed browser cache is a run whose behaviour nobody
approved.

- The cache location arrives through `-BrowserCachePath`, from private host settings. No
  cache path is committed, and there is no default.
- Before the child starts, the path must resolve
  (`EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED`) and must pass a positive readiness check that
  confirms a provisioned Chromium is actually present, not merely that a directory exists
  (`EG_LAUNCHER_BROWSER_CACHE_NOT_READY`). Both failures are terminal.
- The launcher captures the prior process-scope value of `PLAYWRIGHT_BROWSERS_PATH`, then
  sets it explicitly to `-BrowserCachePath` for the child. It never relies on whatever
  the PowerShell process inherited, and an ambient value pointing elsewhere is overridden
  rather than honoured (`EG_LAUNCHER_BROWSER_CACHE_BIND_FAILED` if the binding cannot be
  established).
- The prior process-scope value is restored exactly afterwards, on the same
  finally-equivalent path as the credential variables, with absent restored as absent.
- There is no fallback. A cache that is missing, unreadable, or not provisioned fails the
  run; it never degrades to the default location.
- The launcher never installs, updates, repairs, or downloads into the cache. Provisioning
  stays an operator action, exactly as
  `energygrid-bill-downloader/docs/runbook.md` already requires.

## 10. Git And Environment Hermeticity

This section is the direct, reusable consequence of the Run119 output-shape defect
(section 18.1).

### 10.1 Structured result contract

Every Git read goes through one library function that returns a structured object:

```text
Invoke-GovernedGit -RepositoryRootPath <path> -Arguments <string[]>
  -> [pscustomobject]@{
       Success    = [bool]     # $true only when the process exited 0
       ExitCode   = [int]      # always the real process exit code
       Lines      = [string[]] # always a collection, never a bare string, never $null
       SupportRef = [string]   # bounded reference on failure, empty on success
     }
```

Required behaviours, each of which is a regression against the observed defect:

- A one-line success yields `Lines.Count -eq 1` and `Lines[0] -eq 'main'`. Indexing
  `[0]` must never yield `'m'`. The implementation forces collection shape with the
  array subexpression `@(...)` rather than relying on the pipeline's scalar collapse.
- A zero-line success yields `Success -eq $true` and `Lines.Count -eq 0`. It must never
  yield `$null`, and it must remain distinguishable from a failure.
- A non-zero exit yields `Success -eq $false` with the real exit code preserved, and is
  distinguishable from a successful empty read even when both carry zero lines.
- Standard output and standard error are captured through `System.Diagnostics.Process`
  with redirected streams. Native stderr is never merged inline with `2>&1`, because on
  Windows PowerShell 5.1 that wraps each line in an `ErrorRecord` and falsifies `$?`
  even for a process that exited `0`.
- Raw Git output text never reaches a log, a console summary, or a result object field
  other than `Lines`, and `Lines` is consumed by the library rather than emitted.

### 10.2 Ambient environment neutralisation

Git binding is security-sensitive: an ambient variable can silently redirect a
governed read to a different repository, which would let a runtime-binding check pass
against the wrong tree. Before each governed read the function neutralises, and in a
`finally` block exactly restores:

`GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_CONFIG`, `GIT_CONFIG_GLOBAL`,
`GIT_CONFIG_SYSTEM`, `GIT_CONFIG_NOSYSTEM`, `GIT_CEILING_DIRECTORIES`, `GIT_NAMESPACE`,
`GIT_ATTR_NOSYSTEM`, `GIT_PAGER`, `GIT_EDITOR`, `GIT_ASKPASS`, `GIT_SSH`,
`GIT_SSH_COMMAND`, and `GIT_TERMINAL_PROMPT`.

Restoration is exact: a variable that was absent before the call is absent after it,
not present and empty. The repository is selected explicitly with `-C <path>`, and
reads use `--no-optional-locks` so a governed read never mutates the repository.

### 10.3 Path-scoped source integrity for unattended execution

This section reconciles the design with `DL-XB-141-SCHEDULER-005`.

**Why not a fixed commit.** `x-boundaries/automation` is a monorepo. Requiring the
deployed checkout to sit at one permanently fixed whole-repository HEAD would mean any
accepted, unrelated merge stops the daily EnergyGrid job, and any unrelated dirty file
elsewhere in the tree does the same. That converts routine repository activity into an
outage. The correct unit of protection is the EnergyGrid runtime-critical surface, not
the repository.

**Governed surface.** Exactly two paths, relative to the checkout root:

- `energygrid-bill-downloader/energygrid_bill_downloader`
- `energygrid-bill-downloader/requirements.txt`

The first is the code the child process actually executes. The second is the pinned
dependency contract the approved environment was built from. Nothing else is added.
`runtime/` is deliberately excluded: the launcher executes from the launcher root
outside the checkout, and its integrity is covered by the installation manifest in
section 6.4, so the checkout copy is install-source rather than execution surface.
`tests/`, `docs/`, `task-scheduler/`, and `config/energygrid.example.json` are excluded
because a normal run does not execute or read them. Broadening to the whole monorepo
because it is easier is exactly the failure this section exists to prevent.

**Required checks**, all read-only, all through `Invoke-GovernedGit` from section 10.1
with the neutralisation from section 10.2:

1. Repository binding: the resolved top level equals the supplied `-CheckoutRoot`. This
   also proves ambient `GIT_DIR` or `GIT_WORK_TREE` redirection did not silently move
   the checks to another tree (`EG_LAUNCHER_SOURCE_BINDING_FAILED`).
2. Branch binding: unless `-ExpectedBranch` is the literal `ANY_BRANCH`, the current
   branch equals it. The sentinel is explicit, so binding is never disabled by omission.
3. Each governed path exists (`EG_LAUNCHER_SOURCE_PATH_MISSING`).
4. Each governed path is tracked, proven by a non-empty tracked-file listing scoped to
   that path (`EG_LAUNCHER_SOURCE_PATH_UNTRACKED`).
5. No staged modification under the governed paths
   (`EG_LAUNCHER_SOURCE_STAGED_MODIFICATION`).
6. No unstaged modification under the governed paths
   (`EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION`).
7. No deletion of a tracked file under the governed paths
   (`EG_LAUNCHER_SOURCE_DELETED`).
8. No untracked substitution or overlay inside the governed executable surface
   (`EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY`).

**Scope is the point.** Every check above is scoped to the governed paths with an
explicit Git pathspec. A dirty file anywhere else in the monorepo does not fail the run,
and a HEAD that has moved to a newer accepted commit does not fail the run so long as
the governed surface is still clean and tracked. Both tolerances are asserted by tests,
not left implicit.

**Overlay detection and the one legitimate exception.** An untracked `.py` file dropped
into the package directory would be imported by Python ahead of nothing at all, and an
untracked file that shadows a module name is a real substitution risk, so the overlay
check must see untracked files even when `.gitignore` would hide them. That means the
listing cannot rely on the standard exclusions alone. The single sanctioned exception is
Python bytecode: `__pycache__` directories and `.pyc` files are created by normal
execution and must not fail the run. Every other untracked entry inside the governed
executable surface, ignored or not, is a substitution and is terminal.

**Read-only, always.** The launcher may invoke only this allowlist of Git subcommands:
`rev-parse`, `symbolic-ref`, `ls-files`, `diff`, and `status`. Any Git operation that
fetches, updates, moves, or rewrites state is prohibited outright:

`fetch`, `pull`, `clone`, `remote update`, `reset`, `rebase`, `merge`, `checkout`,
`switch`, `restore`, `cherry-pick`, `revert`, `stash`, `clean`, `add`, `rm`, `commit`,
`tag`, `push`, `gc`, and `worktree`.

The launcher performs zero Git network operations and zero Git state mutations.
Bringing the deployed checkout to a newer source revision is a separately controlled
operator or repository action, never something the unattended job does to itself. A
static guard test enforces the allowlist over the committed runtime files, so the
prohibition cannot erode through a later edit.

## 11. Exception And Diagnostic Contract

The application already establishes the repository's diagnostic pattern: a bounded
ASCII `support_ref` vocabulary, a single terminal evidence event, and no raw exception
text on any surface. The runtime layer adopts the same pattern with an `EG_LAUNCHER_*`
prefix so the two vocabularies cannot collide.

### 11.1 What may be reported

`support_ref`; the phase that failed; the exception type name; the HRESULT as
`0x%08X`; and boolean or integer outcome fields.

### 11.2 What may never be reported

Any path; any file name; any environment variable value; any credential value, length,
prefix, or hash; any account identity; any raw Git output; any exception message text;
any host or principal identity.

### 11.3 Terminal event

When `-LogRoot` is supplied and resolvable, a failing run appends exactly one
`launcher_failed` JSONL event carrying the run identifier, the phase, the status, and
the support reference. The event is evidence, not a result: a failure to write it never
changes the exit code, and a failure raised before the log root is resolved simply has
no event. This mirrors `log_terminal_failure` in `energygrid_bill_downloader/cli.py`.

### 11.4 Vocabulary discipline

The vocabulary is bounded and closed. Retiring a reference means moving it to a
retired set rather than deleting it, so evidence written by an earlier build stays
readable. A test asserts that every live reference is reachable and every retired
reference is unreachable, matching the `SUPPORT_REFS_BY_MESSAGE` and
`RETIRED_SUPPORT_REFS` treatment already in `cli.py`. An unrecognised failure records
`EG_LAUNCHER_UNCLASSIFIED` rather than leaking detail.

## 12. Test Strategy

All tests are offline and deterministic. None contacts the Energy@Grid portal, requires
a real credential, reads the private configuration, or touches the live launcher root.
Filesystem behaviour is exercised in a per-test scratch directory.

The driver is `energygrid-bill-downloader/tests/test_runtime_launcher.py`, a Python
`unittest` module that invokes PowerShell through `subprocess`, following the existing
`tests/test_member_create_uat_runner_ps.py` and
`tests/test_ac2_member_expiry_capability_probe.py` pattern.

### 12.1 Three tiers

**Tier A, portable library tests.** Dot-source `launcher_lib.ps1` and exercise pure
functions under whichever PowerShell the host provides.

**Tier B, compatibility-boundary tests.** Pinned to Windows PowerShell 5.1
(`powershell.exe`, `$PSVersionTable.PSEdition -eq 'Desktop'`), because that is the
production runtime and the runtime on which the `File.Replace` behaviour in section 7.3
was observed. A different edition may behave differently, so running these tests
elsewhere would prove nothing. Where the boundary interpreter is absent the test is
skipped with a message naming the missing interpreter, and a companion test fails
outright when the interpreter is missing while the `CI` environment variable is set.
The CI runner is `windows-latest`, which always provides it, so the boundary can never
be silently unexercised in the gate.

**Tier C, static guard tests.** Read the committed runtime files as text and as a
parsed AST, and assert structural properties that no dynamic test can guarantee across
runtimes.

### 12.2 Required assertions

| ID | Assertion | Tier |
| --- | --- | --- |
| `EGRT-T01` | One-line Git output preserved: `Lines.Count` is 1 and `Lines[0]` is `main` | A |
| `EGRT-T02` | Successful zero-line Git output preserved: `Success` is true, `Lines.Count` is 0, result is not `$null` | A |
| `EGRT-T03` | Non-zero Git exit distinguished: `Success` false, real exit code preserved, distinguishable from `EGRT-T02` | A |
| `EGRT-T04` | Ambient `GIT_DIR`, `GIT_WORK_TREE`, and `GIT_CONFIG_GLOBAL` pointing at a decoy scratch repository cannot redirect a governed read | A |
| `EGRT-T05` | Environment restored exactly after helper execution, including variables that were absent staying absent | A |
| `EGRT-T06` | Valid explicit-backup replacement succeeds in a synthetic scratch directory and the destination carries the source content | A and B |
| `EGRT-T07` | `$null` and `''` backup arguments are refused by the mandatory-parameter contract, on every runtime | A and B |
| `EGRT-T08` | No committed `File.Replace` call site passes a literal `$null`, a literal `''`, or a variable not proven non-empty as the third argument | C |
| `EGRT-T09` | Replacement exception type name and HRESULT are surfaced for the sharing-violation (`0x80070020`) and access-denied (`0x80070005`) classes | B |
| `EGRT-T10` | Every throw-before-publication failure class leaves the destination hash equal to the recorded preimage and reports `PublicationOccurred` false; the assertion is scoped to those classes and is not claimed for a post-publication verification failure | A and B |
| `EGRT-T11` | Backup semantics are conditional and the primitive never reaps: after a verified publication the preimage backup is retained, after a post-publication verification failure it is retained, and after a throw before publication the destination remains at its preimage with no backup required to exist | A |
| `EGRT-T12` | A deliberately wrong `-ExpectedSha256` after a returned `File.Replace` fails with `PublicationOccurred` true and the preimage backup retained, the primitive performs no self-rollback, and the installer transaction then restores the destination to the exact recorded preimage and reports the verified rolled-back result with exit `72` | A |
| `EGRT-T13` | No credential value, account identity, private absolute path (`^[A-Za-z]:\\`), or UNC path (`^\\\\`) appears in any committed runtime file, example settings file, or test | C |
| `EGRT-T14` | `-ValidateOnly` mutates nothing: a full recursive snapshot of path, size, modification time, and hash across the scratch launcher root, config path, and runtime roots is identical before and after, with no file created or removed | A |
| `EGRT-T15` | Repeated validation is idempotent: two consecutive `-ValidateOnly` runs produce byte-identical standard output and identical filesystem snapshots | A |
| `EGRT-T16` | Installation is idempotent by hash: a second install against an already-current destination reports `ALREADY_CURRENT` and performs no replacement | A |
| `EGRT-T17` | Every committed runtime `.ps1` parses cleanly via `Parser::ParseFile` with zero errors | C |
| `EGRT-T18` | Every live `EG_LAUNCHER_*` reference is reachable and every retired reference is unreachable | A and C |
| `EGRT-T19` | The launcher exit band `70` to `73` is disjoint from the application's `0`, `10`, `20`, `64` | C |
| `EGRT-T20` | No test path invokes the application's `run` or `list` against anything but a stubbed child executable, and no committed runtime file contains a portal URL literal | C |
| `EGRT-T21` | An ephemeral synthetic same-user DPAPI `PSCredential` CLIXML imports successfully through the launcher helper, and neither value is printed | A and B |
| `EGRT-T22` | A corrupted, truncated, or unreadable credential artefact fails closed before the child stub is started | A and B |
| `EGRT-T23` | The child stub observes `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` during its execution and only then | A |
| `EGRT-T24` | Both credential variables are restored exactly afterwards: previously absent stays absent, previously present is restored to the original value | A |
| `EGRT-T25` | No User-scope or Machine-scope environment variable is written for either credential name, on any path including failure | A and C |
| `EGRT-T26` | No credential value appears in stdout, stderr, the result object, the JSONL event, or any exception surface, on success or failure | A and C |
| `EGRT-T27` | An explicit private scratch cache path reaches the child stub as the Playwright browser-cache variable | A |
| `EGRT-T28` | A conflicting ambient `PLAYWRIGHT_BROWSERS_PATH` cannot override the supplied binding; the child stub observes the supplied path | A |
| `EGRT-T29` | A missing, unreadable, or unprovisioned supplied cache fails closed with no child start and no fallback to the default location | A |
| `EGRT-T30` | The prior process-scope browser-cache value is restored exactly afterwards, with absent restored as absent | A |
| `EGRT-T31` | `-ValidateOnly` launches no browser and performs no cache install, update, or download | A and C |
| `EGRT-T32` | A scratch repository with clean, tracked governed paths passes the integrity contract | A |
| `EGRT-T33` | A staged modification under a governed path fails the run | A |
| `EGRT-T34` | An unstaged modification under a governed path fails the run | A |
| `EGRT-T35` | A deletion of a tracked file under a governed path fails the run | A |
| `EGRT-T36` | An untracked overlay inside the governed executable surface fails the run, including a `.gitignore`-ignored one, while a `__pycache__` directory or `.pyc` artefact does not | A |
| `EGRT-T37` | A dirty file outside the governed paths does not fail the run | A |
| `EGRT-T38` | A local HEAD moved to a newer commit that leaves the governed surface clean and tracked does not fail the run | A |
| `EGRT-T39` | No Git subcommand outside the read-only allowlist appears in any committed runtime file, and no network or state-mutating Git command is invoked at runtime | A and C |
| `EGRT-T40` | Ambient `GIT_DIR`, `GIT_WORK_TREE`, and `GIT_CONFIG_GLOBAL` cannot redirect the governed-path integrity checks to another repository | A |
| `EGRT-T41` | A clean first install, with launcher, library, and manifest all absent, succeeds through the publish-to-absent path and yields a package whose bytes and manifest verify | A and B |
| `EGRT-T42` | A first-install failure after publication returns every newly created owned destination to absent, confirmed absent afterwards | A |
| `EGRT-T43` | A failure while publishing a later deployed member restores every earlier changed member to its exact pre-transaction state, byte for byte | A |
| `EGRT-T44` | A manifest publication or manifest read-back verification failure rolls back every already published executable member and restores or removes the prior manifest according to its recorded preimage state | A |
| `EGRT-T45` | Existing-file backups remain present through every per-file verification and are reaped only after whole-package acceptance | A |
| `EGRT-T46` | After a successful commit the installed executable bytes and the manifest are mutually consistent, with no extra or missing member | A |
| `EGRT-T47` | Only the enumerated deployed set reaches the launcher root; `install_or_update_launcher.ps1`, `launcher.settings.example.json`, and `runtime/README.md` are not deployed | A and C |
| `EGRT-T48` | A deliberately failing non-secret preflight check, including the launcher-root security checks, causes zero credential import attempt | A and C |
| `EGRT-T49` | No committed cleanup path invokes `Dispose` on a `PSCredential`, and the bounded-lifetime and no-persistence contract is preserved | C |
| `EGRT-T50` | An unexpectedly present destination on the publish-to-absent path, and a destination that appears mid-publication, both fail rather than overwrite | A |
| `EGRT-T51` | A valid installed package plus one correctly named Class B retained preimage backup passes launcher-root classification and manifest completeness | A |
| `EGRT-T52` | A valid installed package plus a correctly named Class B staging or rollback residue entry passes, does not substitute for a package member, and does not break manifest completeness | A |
| `EGRT-T53` | An arbitrary extra `.ps1` file in the launcher root, including one named `launcher-old.ps1`, fails closed | A |
| `EGRT-T54` | A generic `*.bak` or `*.tmp` entry that does not satisfy the exact Class B parser fails closed | A |
| `EGRT-T55` | A lookalike residue name fails closed for each malformation independently: wrong prefix, wrong field count, unknown kind, unrecognised member, and non-canonical GUID | A |
| `EGRT-T56` | If `launcher.ps1`, `launcher_lib.ps1`, or the manifest is missing or invalid, recognised residue cannot satisfy the missing member and preflight fails | A |
| `EGRT-T57` | No recognised residue path is ever dot-sourced, invoked, imported, or selected as a fallback, and the launcher never enumerates the root to locate a script or library | A and C |
| `EGRT-T58` | An authorised write-capable trustee, supplied as an exact SID, passes `launcher_root_write_trustees_authorised` against a synthetic scratch launcher root and each of its package members | A and B |
| `EGRT-T59` | `launcher_root_not_writable_by_run_principal` reports writable whenever the checking token is granted **exactly one** write-capable right, proven separately for a file-specific right such as `FILE_WRITE_DATA` and for a directory-specific right such as `FILE_DELETE_CHILD`, and reports non-writable only where the granted mask intersects the write-capable mask in no bit. This is the union/all-rights false-negative guard: an implementation that requests the union of every write-capable right and reads a denied access status as safe fails this assertion | A and B |
| `EGRT-T60` | A write-capable access-allowed entry for a trustee outside the supplied set fails closed, on the directory and on a package member independently | A |
| `EGRT-T61` | A write-capable entry for an unrelated service SID beneath `S-1-5-80-` fails closed, and no prefix, pattern, range, or wildcard form is accepted anywhere in the supplied set | A and C |
| `EGRT-T62` | Same-account separation is proven rather than assumed: against one scratch root whose only write-capable grant is a group identity, a token in which that identity is deny-only is reported non-writable while the same token before restriction is reported writable | A and B |
| `EGRT-T63` | No SID string, trustee name, owner identity, or count derived from them appears in `-ValidateOnly` output, the terminal event, the result object, or any committed runtime file, example settings file, or test | A and C |
| `EGRT-T64` | An inherited write-capable access-allowed entry is evaluated exactly as an explicit one, and fails closed when its trustee is outside the supplied set | A |
| `EGRT-T65` | An access-denied entry never authorises a trustee: a root carrying both a write-capable allow entry for an unauthorised trustee and a deny entry for that same trustee still fails `launcher_root_write_trustees_authorised`, in either entry order | A |
| `EGRT-T66` | `WRITE_DAC`, `WRITE_OWNER`, and `DELETE` are each treated as write-capable independently by both launcher-root write checks, so a token granted exactly one of those standard rights and nothing else is reported writable; and an examined object whose owner is outside the supplied set fails closed even where no explicit write-capable entry exists | A |
| `EGRT-T67` | An object carrying no discretionary access control list fails both launcher-root write checks rather than passing them | A |
| `EGRT-T68` | An inheritable write-capable `CREATOR OWNER` entry fails closed, and `S-1-3-0` is refused if it is supplied in the authorised set | A |
| `EGRT-T69` | A checking token holding `SeTakeOwnershipPrivilege` or `SeRestorePrivilege` fails `launcher_root_not_writable_by_run_principal` even where the access control list grants it nothing | A |
| `EGRT-T70` | The access check is performed against an impersonation token duplicated from the primary token of the process actually running the launcher, at `SecurityIdentification` level, and the launcher never impersonates with it, never passes the primary token to the access check, and never derives the context from an account name or a SID | A and C |
| `EGRT-T71` | The write-capable mask is passed through generic mapping before it is intersected with the granted mask, so a descriptor expressed only in generic rights is still detected as write-capable | A and C |

`EGRT-T58` to `EGRT-T71` are offline synthetic tests. They construct scratch directories
carrying deliberately shaped security descriptors, and where a case needs a token with a
deny-only group identity they derive it from the test process's own token by restricting
it, which is an operation any process may perform on itself. That is what makes the
deny-only case in `EGRT-T62` provable in hosted CI without an elevated host, a second
account, or the production launcher root.

What hosted CI cannot prove is that the *production* launcher root and the *production*
run principal satisfy the two checks. That is host state. It is proven only by
`launcher.ps1 -ValidateOnly` executed under section 14 step 6, in a context equivalent to
the unattended job, and it is recorded separately as `EGRT-I31` rather than being claimed
here.

These fourteen identifiers are appended rather than folded into existing ones because no
existing assertion evaluates a security descriptor or an access token at all. `EGRT-T48`
asserts only that a failing non-secret check stops the run before the credential import;
it says nothing about whether the security check itself is correct. `EGRT-T70` and
`EGRT-T71` are separate from `EGRT-T59` because they constrain how the access check is
constructed rather than what it concludes, and a conforming conclusion reached by a
non-conforming construction would still be a defect. `EGRT-T01` to `EGRT-T57` are
unchanged and are not renumbered.

### 12.3 Testability without production fallbacks

`-ExpectedSha256` is a mandatory production parameter, not a test hook. Passing a
deliberately wrong value is a legitimate caller error, and it is what lets `EGRT-T12`
drive a genuine Case B failure, a `File.Replace` that returned while verification fails,
without adding a test-only branch, a mock seam, or a compatibility fallback to production
code. That is the only way to exercise installer-owned rollback against a member that
really did advance.

`EGRT-T11` is likewise asserted against observable post-conditions, the presence or
absence of the backup file and the reported `BackupCreated` and `PublicationOccurred`
state, rather than against injected instrumentation. Because those two booleans come from
explicit execution state and observed filesystem state, `EGRT-T10` and `EGRT-T12` together
distinguish the two failure cases deterministically, which is why this amendment needs no
additional test identifier.

### 12.4 The DPAPI testability boundary, stated honestly

Some of the credential contract is provable in hosted CI and some is not. Claiming
otherwise would be worse than the gap itself, so the split is recorded here.

**What is faithfully testable.** A `windows-latest` job runs as a real Windows user, so
the test can create a synthetic `PSCredential` from throwaway values, export it to a
scratch CLIXML with the same user-bound mechanism the launcher uses, and import it back
in the same job. That exercises the real DPAPI path end to end and covers `EGRT-T21`,
`EGRT-T22`, and the injection, restoration, and non-disclosure assertions `EGRT-T23`
through `EGRT-T26`. The synthetic values are generated per run, are never real
credentials, and the scratch artefact is confined to the runner's temporary directory.

**What is not testable in hosted CI.** Cross-user rejection. Proving that the artefact
fails to import under a *different* Windows user needs a second interactive account,
which a hosted runner does not provide, and creating one would be a system-settings
change this design has no authority for. The same applies to cross-machine rejection.

**How that gap is covered instead.** Two ways, neither of which pretends to be the
missing test. First, the property is supplied by the platform rather than by our code:
DPAPI `CurrentUser` protection is what binds the artefact, so cross-user failure is a
construction guarantee, not a behaviour the launcher implements and could get wrong.
Second, a static guard asserts that the committed import path uses the user-bound
mechanism only, and passes no `-Key` or `-SecureKey` argument, since a keyed export
would silently convert the artefact into something portable between users and quietly
void the binding. That guard is the part we can actually regress against.

**Non-Windows hosts.** SecureString export is not encrypted outside Windows, so the
credential tests are Tier B and pinned to the Windows boundary. Where the boundary
interpreter is absent they skip explicitly, and the companion CI assertion from section
12.1 turns that skip into a hard failure inside the gate.

## 13. CI Strategy

The implementation change is expected to require no workflow edit, which is a deliberate
property of the structure chosen in section 4:

- `.github/workflows/energygrid-bill-downloader-tests.yml` already triggers on
  `energygrid-bill-downloader/**`, which covers `runtime/**` and the new test module.
- Its `Project synthetic test suite` step already runs
  `python -m unittest discover -s tests -v` from `energygrid-bill-downloader`, and
  default discovery matches `test*.py`.
- Its `Scope and whitespace check` step already permits `^energygrid-bill-downloader/`.
- The job runs on `windows-latest`, which provides both `powershell.exe` (5.1) and
  `pwsh`, so Tier A, B, and C all execute in the gate.

The workflow's existing PowerShell parse-only step names
`task-scheduler/register_task.example.ps1` explicitly. The runtime files are parse-checked
by `EGRT-T17` inside the Python suite instead of by extending that step, so parse coverage
grows with the directory without a workflow edit. If a future need genuinely requires a
workflow change, moving the check into the suite is the preferred resolution.

This design document itself lives under `energygrid-bill-downloader/docs/`, so it matches
the `energygrid-bill-downloader/**` trigger above. The design pull request therefore runs
the same EnergyGrid job an implementation change would: the project synthetic suite plus
the scope and whitespace guard, which accepts the changed path because it is under
`^energygrid-bill-downloader/`. No workflow was edited to obtain that coverage.

## 14. Deployment And Update Flow

1. The implementation pull request is reviewed and merged.
2. On the production host, under owner control, the deployed checkout is updated to the
   reviewed commit.
3. `install_or_update_launcher.ps1 -ValidateOnly` is run and its JSON is inspected.
4. The owner gives explicit current-turn approval for the mutating install, naming the
   launcher root and the operation. This is a live-system action and is gated by
   `AGENTS.md`.
5. The installer is run for real, as the package transaction of section 6.2. It prepares
   and verifies every staging file with no destination mutation, classifies each
   destination as `Existing` or `Absent`, publishes existing members through
   explicit-backup `File.Replace` and absent members through the publish-to-absent path,
   publishes and reads back the manifest inside the same transaction, re-verifies the
   complete installed package, and only then accepts the transaction. Backup cleanup
   happens after acceptance and never before it. Any pre-acceptance failure triggers
   installer-owned reverse-order package rollback under section 6.5. Not every install
   uses `File.Replace`: on a clean host every member is `Absent` and none of them do.
6. `launcher.ps1 -ValidateOnly` is run to confirm runtime binding, security
   expectations, and configuration reachability on the host. It is run in a context
   equivalent to the one the unattended job will use: the same account, and the same
   elevation state. `launcher_root_not_writable_by_run_principal` is evaluated against
   the token of the process running the launcher, so validating from an elevated prompt
   exercises a principal the scheduled job will not use, and a pass obtained that way
   proves nothing about the scheduled job.
7. Scheduling remains blocked. Task Scheduler registration, headed validation, and any
   live run each require their own separate approval and are out of scope here.

Every subsequent update repeats steps 2 to 6. There is no in-place edit path, and no
step in which a human edits the installed launcher directly.

## 15. Migration From The Current Server-Only Launcher

The current live state is not modified by this design, and nothing here authorises a
second replacement attempt, a cleanup, or a retry.

Known current state, restated so the migration starts from a recorded baseline: the
installed launcher remains the accepted preimage; the Run119 A1 invocation is consumed;
the retired bridge remains prohibited; the
`.energygrid-launcher.run119a1.rollback` artefact remains present beside the launcher
and hashes to the accepted preimage; the headed list, the live run, the scheduler, and
n8n all remain blocked; and R2 remains no-retry.

Migration proceeds in this order.

1. **Record the baseline.** Under owner control, record the SHA-256 of the installed
   launcher and of the `.energygrid-launcher.run119a1.rollback` artefact, privately on
   the host. Both are expected to be the accepted preimage. That recorded hash is the
   reference the recovery copy is verified against in step 7, so recording it is a
   precondition of the sequence rather than a formality.
2. **Reconcile behaviour.** Compare the installed launcher's behaviour to the
   source-controlled implementation, and classify every difference as one of: reusable
   and non-secret, so it moves into the repository; host-specific, so it moves into the
   private configuration or a parameter; or obsolete, so it is dropped with the reason
   recorded. Any residue that fits none of the three is a blocker and stops the
   migration.
3. **Publish.** Merge the implementation, which by then encodes every reusable
   behaviour from step 2.
4. **Validate the installer.** Run `install_or_update_launcher.ps1 -ValidateOnly` on the
   host and confirm its checks pass against the real deployment. The launcher's own
   `-ValidateOnly` is deferred to step 9, for the reason in section 15.4.
5. **Install under approval.** Perform the first source-controlled installation through
   the sanctioned installer, per section 14.
6. **Verify the installed bytes.** Confirm each installed member's hash matches the
   reviewed source hash and that the manifest reads back correctly. This is a direct
   hash comparison and does not depend on the launcher running.
7. **Hold the recovery copy outside the launcher root.** Under a separate explicit
   approval, place a copy of the historical artefact at the owner-controlled recovery
   holding location of section 15.1, and positively verify it as section 15.2 requires.
   Nothing inside the launcher root changes in this step.
8. **Retire the in-root artefact.** Only after that external copy has been positively
   verified, and only under a separate explicit approval naming the artefact, may
   `.energygrid-launcher.run119a1.rollback` be removed from the launcher root.
   Afterwards, positively confirm it is absent.
9. **Verify the launcher.** Run `launcher.ps1 -ValidateOnly` and confirm every check
   passes. The verified external recovery copy still exists throughout this step, and
   that is the point of the ordering.
10. **Retire the recovery copy.** Only after step 9 has passed does the external copy
    become eligible for retirement. Removing it is a separate owner-controlled cleanup
    action, never an automatic consequence of a passing validation.

### 15.1 The migration recovery holding location

The recovery copy lives in a location this repository describes but never names. Git owns
the procedure; the server owns the path.

- It is owner-controlled private deployment state on the host.
- It resolves outside the launcher root, and outside the deployed Git checkout.
- It is never committed. This document records no default, no example value, and no
  hard-coded absolute path for it, in keeping with section 3.3 and section 17.1.
- It is selected and supplied only at the later, separately authorised live migration. It
  does not exist as a standing host location.
- It is outside the classification domain of section 6.7, because that domain is the
  launcher root and nothing else. Holding the copy outside the root is precisely what
  lets it survive without becoming a Class C entry, and it is why no Class B exception is
  needed for it.
- No committed script gains a parameter for it. The installer parameter surface in
  section 6.1 is unchanged and still has no cleanup or recovery parameter; the transfer is
  an approved owner action, not an installer feature.

### 15.2 Safe transfer, verified before removal

The order of operations matters more than the mechanism.

1. The accepted-preimage SHA-256 is already recorded under owner control, from step 1.
2. Under the separate live approval, create the copy at the approved private holding path.
3. Never overwrite an existing destination. An unexpectedly present destination means the
   holding path was not what the operator believed it was, and that is terminal rather
   than something to resolve by overwriting.
4. Re-read the external copy from disk and hash it.
5. That hash must equal the recorded accepted-preimage SHA-256 exactly.
6. Only after that positive byte-for-byte verification may the in-root artefact be
   removed.
7. Positively confirm the in-root Class C artefact is absent afterwards.

If the copy cannot be created, or cannot be verified, the migration fails closed: the
original in-root artefact is left untouched, `launcher.ps1 -ValidateOnly` is not run, and
the sequence stops for owner attention. The design never depends on an unverified copy,
and a copy that cannot be verified is treated as no copy at all.

### 15.3 What the recovery copy is, and what a failed validation does

The copy is a byte-identical copy of the accepted pre-migration launcher preimage, held as
bounded migration recovery material. Stating what it is not matters just as much:

- it is not Class B transaction residue, and the Class B parser is not involved in
  recognising it;
- it is not a member of the new three-member installed package;
- it is never executed, dot-sourced, imported, or selected as a fallback from the holding
  location, or from anywhere else;
- it does not alter, relax, or participate in launcher-root classification;
- it remains private deployment state and never enters Git.

**Retaining a copy is not a rollback.** Holding a verified copy preserves the option to
recover; it performs no restoration and asserts no automatic recovery. Claiming otherwise
would be the same category of error as treating a hash comparison as functional proof.

If step 9 fails, the sequence stops with the external copy retained and nothing restored.
Recovery from that state is a new live mutation: it requires explicit current owner
authority, and that later gate must define the complete safe pre-migration topology it is
restoring, including the intended installed-launcher content, the intended launcher-root
state, and the scheduler position. This design change neither performs nor authorises that
restoration. A failed validation is never automatically converted into an unreviewed
rollback mutation.

### 15.4 Why the launcher check follows retirement

The Run119 artefact predates the reserved residue contract and does not satisfy it: its
name begins `.energygrid-launcher.` rather than the reserved `.eglauncher-` prefix, and it
carries no `--` delimited kind, member, and transaction fields. Section 6.7 therefore
classifies it as Class C, and the launcher fails closed while it is present. That is the
correct behaviour for an unrecognised file beside the launcher, and it is not a reason to
widen the Class B parser to accommodate one historical artefact.

What an earlier ordering of this section got wrong was treating step 6 as sufficient cover
for retiring the artefact. Byte verification proves the installed members are the reviewed
bytes; it does not prove the launcher works against the real host. Section 5.2
additionally gates on the private configuration, the interpreter version, governed source
integrity over the runtime-critical surface, browser-cache readiness, the launcher-root
ACL, reparse-point, and read-only expectations, and the DPAPI credential import. None of
those is detectable by a hash comparison, so retiring the last accepted-preimage copy
before `launcher.ps1 -ValidateOnly` removed the recovery asset before the first evidence
that the new package actually functions.

Steps 7 to 10 close that window without weakening anything. The copy moves out of the
classification domain instead of being destroyed, so all three properties hold together:
the launcher still fails closed on the in-root Class C artefact, the launcher still proves
its own preflight against the real host, and a verified way back still exists while that
proof is being obtained.

## 16. Recovery And Disaster-Rebuild Procedure

The acceptance test for this design is that the following sequence needs the reviewed
commit plus separately provisioned private host state, and nothing else. Specifically it
needs the reviewed Git commit, a provisioned private DPAPI credential artefact, the
private launcher and settings paths, the private application configuration, and an
approved Python and browser installation. It needs no GitHub issue comment, no chat
history, no retired bridge, no `%TEMP%` script, and no recollection of the original
server setup.

The migration recovery copy of section 15.1 is deliberately not part of this procedure.
It is bounded migration material with a defined end of life at section 15 step 10, and the
steady-state rebuild must never acquire a dependency on it, on the Run119 artefact it
copies, or on any other retired bridge residue. Once migration is final, a clean-host
rebuild still requires only the reviewed Git source plus separately provisioned private
host state, exactly as stated above.

Everything reusable and secret-free that the rebuild depends on comes from the
repository: importing the credential, injecting it at process scope, restoring and
clearing it, binding the private browser cache, validating the governed source surface,
and launching the application. The host supplies only values and artefacts, never
behaviour.

1. Clone `x-boundaries/automation` at the reviewed commit onto a clean Windows host.
2. Install Python 3.14.x and the pinned dependencies with
   `python -m pip install --requirement requirements.txt` from
   `energygrid-bill-downloader`.
3. Provision Chromium into the private browser cache with the official mechanism,
   `python -m playwright install chromium`, with the cache location set to the private
   path the launcher will later bind (section 9.3).
4. Create the private runtime roots (archive, SQLite state parent, temp, logs, browser
   cache) per the path rules the application enforces and the project README documents.
5. Copy `config/energygrid.example.json` to an external private path and set
   `account_identity` and every path to the real private values.
6. Provision the private DPAPI `PSCredential` CLIXML artefact as the Windows user the
   job will run as, and record its path in the private launcher settings. The artefact
   is user-bound, so it must be created on the target host under that account; it cannot
   be copied from another machine or another user. The repository supplies the import
   behaviour, not the artefact.
7. Run `install_or_update_launcher.ps1 -ValidateOnly` with the reviewed commit as
   `-AdmissionCommit`, then the approved install. On a clean host the launcher root
   contains no `launcher.ps1`, no `launcher_lib.ps1`, and no manifest, so all three
   members classify as `Absent` in Phase 1 and publish through the publish-to-absent
   path in section 7.4. No destination preimage is assumed to exist anywhere in this
   procedure, and `EGRT-T41` asserts the bare-root case end to end.
8. Run `launcher.ps1 -ValidateOnly` and confirm every check passes, including
   `credential_import_ok`, the browser-cache readiness check, and the governed
   source-integrity checks.
9. Perform the controlled first validation already documented in
   `energygrid-bill-downloader/docs/runbook.md`: a headed `list`, then a controlled
   `run`, then an idempotency re-run, each under its own approval.
10. Register the Scheduled Task only after that review is complete and separately
    approved.

Steps 1 to 8 are fully determined by the repository. Steps 9 and 10 are live-system
gates by design and are expected to require a human.

## 17. Security And Privacy Constraints

### 17.1 Never committed

Username; password; credential blob or DPAPI material; connection string; private
account identity; private absolute server path; UNC path; host or principal identity;
any host-specific secret value. `EGRT-T13` enforces the mechanically checkable subset
of this list over the committed runtime files, the example settings file, and the tests.

### 17.2 Expressed without private identities

Security expectations are recorded as named checks with expected outcomes. The paths
they are evaluated against, and the authorised write-trustee set the launcher-root
checks compare against, are supplied at deployment: the paths on their existing
parameters, and the trustee set on `-AuthorisedLauncherRootWriteSid`. No principal or
SID is committed, defaulted, or inferred from a name. Section 17.2.1 states exactly
how the two launcher-root write checks are evaluated.

| Check name | Expected outcome |
| --- | --- |
| `launcher_root_not_writable_by_run_principal` | The token the launcher is actually running under has no write-capable effective access to the launcher root or to any package member, so a compromised run cannot rewrite its own launcher |
| `launcher_root_write_trustees_authorised` | Every trustee holding a write-capable access-allowed grant on the launcher root or on a package member, and every examined object's owner, is inside the exhaustive set supplied on `-AuthorisedLauncherRootWriteSid`; any other write-capable trustee is terminal |
| `launcher_root_entries_classified` | Every launcher-root entry is an exact Class A package member or a valid Class B reserved residue name; any Class C entry is terminal, per section 6.7 |
| `launcher_files_not_reparse_points` | Neither installed file, nor the destination directory, is a symlink, junction, or other reparse point |
| `launcher_files_not_unexpectedly_readonly` | No installed file carries an unexplained read-only attribute |
| `config_path_outside_checkout` | The private configuration resolves outside the deployed checkout |
| `launcher_root_outside_checkout` | The launcher root resolves outside the deployed checkout |

A failed security check is a terminal preflight failure. It is never downgraded to a
warning and never bypassed by a switch.

`launcher_root_write_trustees_authorised` supersedes the earlier check name
`launcher_root_write_restricted_to_install_principal`, which is retired vocabulary and
must not be emitted. The replacement occupies the same position in the ordered preflight.
The old name was narrowed rather than kept because it was misleading twice over: the
check cannot observe which principal actually performed an installation, and on a real
host the authorised write set is not necessarily a single principal.

### 17.2.1 Launcher-root write authority, exactly

Two different questions are being asked about the launcher root, and Windows answers them
by two different mechanisms. Conflating them is the defect this subsection exists to
prevent.

**Security objects examined.** Both checks are evaluated against the launcher-root
directory *and* each Class A package member individually. A member's own discretionary
access control list can differ from the directory's, and directory-level authority to add
or delete children is by itself sufficient to replace a member, so neither object alone is
enough.

**Rights treated as write-capable.** After generic mapping, any of `FILE_WRITE_DATA` /
`FILE_ADD_FILE`, `FILE_APPEND_DATA` / `FILE_ADD_SUBDIRECTORY`, `FILE_WRITE_EA`,
`FILE_WRITE_ATTRIBUTES`, `FILE_DELETE_CHILD`, `DELETE`, `WRITE_DAC`, and `WRITE_OWNER`.
`FILE_ADD_FILE` and `FILE_ADD_SUBDIRECTORY` are the directory readings of the same bits as
`FILE_WRITE_DATA` and `FILE_APPEND_DATA`, so the mask is bit-identical for both object
kinds and the object type changes only how a granted bit is described;
`FILE_DELETE_CHILD` is meaningful on the directory.
`WRITE_DAC` and `WRITE_OWNER` are included deliberately. Windows defines them as the right
to modify the discretionary access control list and the right to change the owner, so a
trustee holding either can grant itself every other right at will; treating them as
read-level rights would make both checks decorative.

**`launcher_root_write_trustees_authorised`.**

- *Inputs.* The set supplied on `-AuthorisedLauncherRootWriteSid`, and the security
  descriptor of each examined object.
- *Evaluation.* Enumerate every access-allowed entry whose access mask intersects the
  write-capable set. Each such entry's trustee SID must be a member of the supplied set.
  Separately, each examined object's owner SID must also be a member of the supplied set,
  because an owner implicitly holds `WRITE_DAC` and can therefore restore write access to
  itself whenever it chooses.
- *Inherited entries.* An inherited access-allowed write-capable entry is treated exactly
  as an explicit one. Inheritance describes where an entry came from, not how much access
  it grants.
- *Access-denied entries.* Ignored by this check. A deny entry can only reduce access,
  never authorise it, and whether a given deny entry actually neutralises a given allow
  entry depends on their order in the list, which Windows walks in sequence. Cancelling an
  allow against a deny here would let a badly ordered list conceal a real grant.
- *`CREATOR OWNER` (`S-1-3-0`).* Windows defines this as a placeholder replaced, when an
  inheritable entry is inherited, by the SID of whoever created the new object. An
  inheritable write-capable `CREATOR OWNER` entry therefore describes an unbounded future
  write set rather than a principal. It is terminal, and `S-1-3-0` may not be placed in
  the supplied set.
- *No wildcards.* The supplied set is an exhaustive list of exact SID strings. No prefix,
  pattern, range, or wildcard form is accepted. A rule of the form "any SID beginning
  `S-1-5-80-`" is specifically prohibited: `S-1-5-80` is the service-account SID prefix,
  so such a rule would authorise every service configured on the host rather than a named
  authority.
- *Well-known SIDs are not implicitly authorised.* `S-1-5-32-544` (Administrators),
  `S-1-5-18` (SYSTEM, which Windows documents as a hidden member of Administrators), and
  every other well-known SID are accepted only where the operator has supplied them
  explicitly. A principal that merely happens to be an administrator is not a principal
  this design trusts by default.
- *PASS.* Every write-capable allow trustee, and every examined object's owner, is in the
  supplied set.
- *FAIL.* Any other write-capable allow trustee; any examined owner outside the set; a
  write-capable `CREATOR OWNER` entry; an object with no discretionary access control list
  at all, because Windows grants all access in that case; or a security descriptor that
  cannot be read.
- *Support reference.* `EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED`.

**`launcher_root_not_writable_by_run_principal`.**

- *Inputs.* The access token of the process actually running the launcher, and the
  security descriptor of each examined object.
- *Evaluation, exactly.* One algorithm is prescribed. An implementation that substitutes a
  different formulation of the requested access is non-conforming. For the launcher-root
  directory, and then for each Class A package member:

  1. Open the primary access token of the process actually running `launcher.ps1`, with at
     least `TOKEN_DUPLICATE` and `TOKEN_QUERY` access.
  2. Duplicate it with `DuplicateTokenEx`, passing `TokenType` = `TokenImpersonation` and
     `ImpersonationLevel` = `SecurityIdentification`. `AccessCheck` is documented to take
     an impersonation token, so the primary token is never passed to it directly.
     `SecurityIdentification` is the least-privilege level Windows documents as sufficient
     for a server to make access-validation decisions from a client's security
     information. The duplicate exists to be evaluated; the launcher never impersonates
     with it and never starts anything under it.
  3. Retrieve the object's security descriptor including its owner, its group, and its
     discretionary access control list. All three are required, because `AccessCheck`
     fails with `ERROR_INVALID_SECURITY_DESCR` when the descriptor carries no owner and
     group SIDs.
  4. Build the `GENERIC_MAPPING` for file-system objects from the documented
     `FILE_GENERIC_READ`, `FILE_GENERIC_WRITE`, `FILE_GENERIC_EXECUTE`, and
     `FILE_ALL_ACCESS` values.
  5. Call `AccessCheck` with the duplicated token and `DesiredAccess` = `MAXIMUM_ALLOWED`.
     Windows then returns, in `GrantedAccess`, the maximum access rights the security
     descriptor allows that token.
  6. Any failure of any call in steps 1 to 5, an unreadable token, or an unreadable
     security descriptor is terminal. The check never falls back to another method, never
     retries at a different impersonation level, and never treats an error as a pass.
  7. Apply `MapGenericMask` to the write-capable mask defined above, so that the mask being
     compared contains no generic rights, and then compute

     `any_write_granted = (GrantedAccess AND write_capable_mask) != 0`

     `AreAnyAccessesGranted(GrantedAccess, write_capable_mask)` is the documented
     equivalent of that intersection and may be used in its place. Nothing else may be.
  8. `any_write_granted` true on any examined object fails the check immediately.

  The check passes only where the intersection is zero on the launcher-root directory *and*
  on every Class A package member.

- *Why `MAXIMUM_ALLOWED` and an intersection, and not a requested write mask.* This is the
  one place the algorithm must not be left to the implementer, because the obvious
  formulation is wrong in the unsafe direction. Windows grants an access check only when
  the descriptor allows *all* of the requested rights: it walks entries until every
  requested right has been allowed, or until a requested right is denied or is simply never
  granted, in which case access is denied. Passing the union of every write-capable bit as
  `DesiredAccess` and reading `AccessStatus` false as "not writable" is therefore a false
  negative by construction. A token holding exactly one of those rights — enough to append
  to, delete, or re-permission the launcher — yields `AccessStatus` false under that
  formulation, and the run would proceed. Requesting `MAXIMUM_ALLOWED` and intersecting the
  returned mask is what makes a single granted write right observable. An equivalent
  algorithm that checks each write-capable right individually would also be correct, but
  the design deliberately prescribes one rather than leaving the choice open.

- *Null discretionary access control list.* Windows grants access when an object has no
  such list, so `GrantedAccess` comes back carrying every right and the intersection is
  non-zero. The fail-closed outcome required below therefore falls out of this algorithm
  rather than needing to be handled as a special case.
- *Why a token and not a trustee.* The token is evaluated as a token and is never
  reconstructed from an account name or from a SID. A security
  context rebuilt from a SID recomposes group membership as enabled, which is precisely
  the information a filtered token does not carry. The trustee-based effective-rights
  helper additionally ignores the owner's implicit rights, ignores privileges, ignores
  logon-session identities such as `Batch` and `Interactive`, is documented as superseded,
  and fails outright when the list contains an inherited access-denied entry. Only a
  token-based access check observes deny-only group attributes, access-denied entries,
  entry order, and logon-session identities together.
- *Privilege bypass.* A token holding `SeTakeOwnershipPrivilege` or `SeRestorePrivilege`
  can reach the object whatever the list says, so the check also fails when either is
  present in the running token. This is a bounded rule, not an exhaustive one: no
  discretionary-access-list check can fully constrain a principal that has been granted
  list-bypassing privileges, and claiming otherwise would be dishonest.
- *PASS.* No write-capable right is granted on the launcher-root directory or on any
  package member, and neither bypass privilege is present.
- *FAIL.* Any write-capable right granted on any examined object; either bypass privilege
  present; an object with no discretionary access control list; or a token or security
  descriptor that cannot be read.
- *Support reference.* `EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE`.

Neither check emits a SID, a trustee name, an owner identity, a path, or any count derived
from them. Only the check name, the pass or fail outcome, and the bounded support
reference reach any surface, per sections 8 and 11.2.

**The same-account case, which is why this subsection exists.**

On this host the human who installs and the identity the unattended job runs as may be the
same Windows account. That case has to be reasoned about directly, because the obvious
binding is wrong.

When a member of the local Administrators group signs in, Windows builds two tokens: a
full administrator token, and a filtered standard-user token that contains the same
user-specific information but from which the administrative privileges and SIDs have been
removed. In the filtered token the Administrators group identity is present as a deny-only
identity, and Windows ignores access-allowed entries for a deny-only identity while still
honouring access-denied entries for it. The account's own *user* SID is not among what that
filtering removes: Windows documents the standard user token as carrying the same
user-specific information as the administrator token, and `AdjustTokenGroups` cannot
disable a token's user SID, so both of that account's ordinary running contexts carry it
enabled.

This design needs no claim stronger than that, and no stronger claim would be true.
`CreateRestrictedToken` can mark any SID deny-only, the user SID included. A deliberately
restricted token is a different construction from the split token UAC produces, and the
reasoning below is scoped to normal UAC split-token behaviour rather than to the platform
in general.

Three consequences follow, and together they decide the design.

1. Binding write authority to the installing account's **user SID** does not separate the
   two contexts at all. Under normal UAC split-token behaviour the same user SID is enabled
   in the elevated installer token and in the unattended token, so an allow entry keyed to
   it grants write to the unattended run as well. Where installer and run principal are
   the same account, that binding alone fails the very property it was meant to
   establish.
2. Binding write authority to an **administrative group identity** does separate them, but
   only while the host keeps the conditions that produce a filtered token. A scheduled task
   set to run with highest privileges receives the full token; the run-level setting is
   documented as ignored entirely when User Account Control is turned off; and the
   local-account token-filter policy can be set to build an elevated token instead. None of
   that is visible in the launcher root's access control list.
3. The trustee binding therefore cannot be trusted on its own, whichever principal form the
   host uses. The design keeps it, because it is what stops an unexpected writer, and pairs
   it with a check that asks Windows directly whether *this* token can write. Where the
   host is configured such that the unattended run really can write the launcher root, that
   second check fails and the run stops, however defensible the access control list looked.

This also means the design does not force a dedicated run account. A host may separate
installer and run principals by account, or may rely on elevation separation within one
account. Both are permitted deployments, neither is assumed, and the run-principal check is
what makes the difference observable rather than asserted.

### 17.3 Diagnosability without disclosure

Error reporting must stay useful. The combination of a bounded support reference, the
failing phase, the exception type name, and the HRESULT is sufficient to distinguish
every failure class in section 7.3 from every other, without emitting a path, a value,
or a message. This mirrors the application's existing behaviour, where a
`PORTAL_LAYOUT_CHANGED` run is diagnosed from its support reference rather than from
exception text.

### 17.4 Fallback discipline

No broad fallback, silent compatibility path, synthetic-data fallback, fake success
state, or catch-and-continue behaviour is permitted in the runtime layer. A failed
check fails the run. A failed replacement rolls back and reports. A missing
configuration value is an error, not a default.

## 18. Relationship To Run119

Two defects were independently proven during the Run119 sequence. Both are represented
here as reusable, regression-tested requirements rather than as historical notes,
because both were properties of the technique rather than of the host.

### 18.1 PowerShell Git output-shape defect

A helper returned native command output without preserving collection shape. Single-line
output collapsed to a scalar string, so indexing `[0]` on the branch name `main`
produced `m`; and successful zero-line output collapsed to `$null`, making a successful
empty read indistinguishable from a failure.

Reusable requirement: section 10.1's structured result contract, asserted by `EGRT-T01`
through `EGRT-T03`. Because Git binding is security-sensitive, the same section adds the
ambient-variable neutralisation and exact restoration required by `EGRT-T04` and
`EGRT-T05`.

### 18.2 File.Replace null-backup defect

Both Run119 bridges called
`[System.IO.File]::Replace($source, $destination, $null)`. On Windows PowerShell 5.1
with .NET Framework 4.x this is deterministically rejected with
`System.ArgumentException` and HRESULT `0x80070057`, before `ReplaceFileW` reaches the
filesystem. The synthetic proof also established the behaviour of the explicit-backup,
empty-string, sharing-violation, and read-only cases recorded in section 7.3.

Reusable requirement: section 7's replacement contract together with the installer-owned
rollback contract in section 6.5, asserted by `EGRT-T06` through `EGRT-T12`, with the
structural prohibition on null and empty backup arguments asserted by `EGRT-T08` so that
the rule holds on runtimes that would accept them.

### 18.3 What Run119 does not contribute

The one-shot owner bridges and diagnostic probes themselves are not repository
artefacts and are not to be reconstructed as such. The retired bridge remains
prohibited. Only the behaviour they proved is carried forward, in the form above.

## 19. Completion And Acceptance Criteria

### 19.1 For this design change

| ID | Criterion |
| --- | --- |
| `EGRT-D01` | The design document is the only file added or changed |
| `EGRT-D02` | No implementation file, test, workflow, or application module is changed |
| `EGRT-D03` | No live server, launcher, scheduler, portal, credential, or ACL is touched |
| `EGRT-D04` | No secret, credential, private absolute path, or private identity appears in the document |
| `EGRT-D05` | No unresolved `TBD`, `TODO`, or placeholder remains |
| `EGRT-D06` | Option 2 is preserved exactly: Git canonical for reusable non-secret runtime behaviour, the server canonical for private deployment state |
| `EGRT-D07` | Both Run119 defects appear as reusable requirements with named regression assertions, not as historical notes |
| `EGRT-D08` | The migration sequence retains a positively SHA-256-verified accepted-preimage recovery copy outside the launcher-root verification domain until `launcher.ps1 -ValidateOnly` passes; the in-root historical Class C artefact is removed only after that external copy is verified, and a failed `-ValidateOnly` retains the external copy and grants no automatic restore |

### 19.2 For the later implementation change

| ID | Criterion |
| --- | --- |
| `EGRT-I01` | `runtime/launcher_lib.ps1` is pure and dot-sourceable, with no side effect at load |
| `EGRT-I02` | `runtime/launcher.ps1` implements the section 5 parameter surface, preflight order, and exit bands |
| `EGRT-I03` | `runtime/install_or_update_launcher.ps1` implements the section 6 sequence, including hash idempotency |
| `EGRT-I04` | `Invoke-AtomicFileReplace` implements every rule in section 7.2, with a mandatory explicit backup path |
| `EGRT-I05` | `Invoke-GovernedGit` implements the section 10 result contract and environment neutralisation |
| `EGRT-I06` | `-ValidateOnly` satisfies section 8, including deterministic output and zero mutation |
| `EGRT-I07` | All seventy-one assertions `EGRT-T01` to `EGRT-T71` are implemented and pass |
| `EGRT-I08` | The full project suite passes on Windows via `python -m unittest discover -s tests -v` |
| `EGRT-I09` | No GitHub Actions workflow is modified |
| `EGRT-I10` | No secret, credential, private absolute path, or private identity is committed |
| `EGRT-I11` | The `EG_LAUNCHER_*` vocabulary is bounded, closed, and reachability-tested |
| `EGRT-I12` | The implementation performs no live server, scheduler, portal, or credential action |
| `EGRT-I13` | The DPAPI import, process-scope injection, exact restoration, and cleanup contract in section 9.2 is implemented in source-controlled, secret-free logic |
| `EGRT-I14` | The private credential artefact, its path, and its bound identity remain outside Git, and no credential value reaches any output surface |
| `EGRT-I15` | The private browser cache is positively bound and fails closed, with no fallback to an ambient or default Playwright cache |
| `EGRT-I16` | Unattended execution uses the path-scoped governed integrity contract in section 10.3 and requires no fixed whole-repository commit |
| `EGRT-I17` | Unrelated dirty paths elsewhere in the monorepo, and unrelated accepted repository movement, do not block the daily run |
| `EGRT-I18` | The launcher performs zero Git network operations and zero Git state mutations, enforced by the read-only allowlist and its static guard |
| `EGRT-I19` | Exact-commit admission exists only in the operator-controlled install lane as `-AdmissionCommit`, and never in the unattended path |
| `EGRT-I20` | Installed-launcher integrity is reproducible from reviewed Git source plus the installer-generated private manifest, with no dependence on issue comments or retired bridges |
| `EGRT-I21` | Clean first installation is supported without calling `File.Replace` against a missing destination, using the publish-to-absent primitive in section 7.4 |
| `EGRT-I22` | The launcher, library, and manifest form one package transaction, with every preimage retained until package acceptance |
| `EGRT-I23` | Any pre-commit later-member or manifest failure restores the exact complete pre-transaction package state, verified before the failure status is returned |
| `EGRT-I24` | Manifest publication and full package re-verification occur inside the transaction, before any backup cleanup is authorised |
| `EGRT-I25` | Every non-secret and security preflight check precedes the DPAPI import, with no credential import attempted when an earlier check fails |
| `EGRT-I26` | Credential cleanup uses bounded lifetime and reference removal, never requires `PSCredential.Dispose()`, and makes no memory-erasure claim |
| `EGRT-I27` | Launcher-root entries are deterministically classified as exact package members, recognised installer-owned residue, or unexpected, per section 6.7 |
| `EGRT-I28` | Manifest completeness is scoped to the deployed package while arbitrary unexpected root entries still fail closed |
| `EGRT-I29` | Recognised installer residue never participates in execution, import, fallback, or package membership |
| `EGRT-I30` | The implementation and runbook migration procedure encodes the `EGRT-D08` ordering and keeps four concerns clearly separate: installer transaction backup cleanup, migration recovery holding, launcher functional validation, and later separately authorised recovery-copy retirement or restoration |
| `EGRT-I31` | Launcher-root write authority is bound as section 17.2.1 requires: the authorised write-trustee set arrives only on `-AuthorisedLauncherRootWriteSid`, the run-principal check is evaluated against the running token rather than a reconstructed trustee, and both checks pass on the production host under `launcher.ps1 -ValidateOnly` executed in a context equivalent to the unattended job. The host half of this criterion is host state and is not claimed by hosted CI |

Acceptance of this document is an architectural decision only. It does not approve the
implementation change, the first installation, the scheduler, or any live run. Each of
those remains a separate gate.
