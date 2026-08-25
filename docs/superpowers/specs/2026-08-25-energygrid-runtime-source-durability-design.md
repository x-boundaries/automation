# Energy@Grid Runtime Source-Durability Design

Status: design only. This change adds no launcher, no installer, no runtime
implementation, no test, and no CI change. It records the reviewed contract that a
later, separately approved implementation change must satisfy.

Design lock: `DL-XB-141-RUNTIME-005-SOURCE-DURABILITY`.
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
- implementing or changing a credential or DPAPI mechanism;
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
| Rollback semantics | Same function's failure path, contract in section 7 |
| Exception and diagnostic reporting | Bounded `EG_LAUNCHER_*` vocabulary, section 11 |
| Security and ACL expectations | Named checks with expected outcomes, section 17 |
| Reusable environment hardening | Governed Git invocation, section 10 |
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
| `-Command` | no | `run` (default) or `list` |
| `-ExpectedCommit` | no | Full 40-character commit the deployed checkout must be at |
| `-LogRoot` | no | Private diagnostics root for the launcher's own terminal event |
| `-ValidateOnly` | no | Switch, contract in section 8 |
| `-RunId` | no | Correlation identifier for the terminal event only |

There is no credential parameter, no portal parameter, no browser-install parameter,
and no headed switch. `--headed` remains an application concern reached through a
separately approved manual invocation, not through the launcher.

### 5.2 Preflight, in order

Every check runs before any child process is started. A failure is terminal; the
launcher never continues past a failed check, and never downgrades one to a warning.

1. Both `launcher.ps1` and `launcher_lib.ps1` in the launcher root parse cleanly and
   hash-match the values recorded by the last accepted installation.
2. `-CheckoutRoot`, `-ConfigPath`, and `-PythonExe` exist and are absolute.
3. `-ConfigPath` resolves outside `-CheckoutRoot`.
4. The private configuration parses as JSON and carries every key the application
   requires. The launcher does not re-validate the application's own path rules; the
   application already enforces them in `energygrid_bill_downloader/config.py` and
   duplicating them here would create two sources of truth.
5. The interpreter reports a 3.14.x version.
6. `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` are present and non-empty in the
   process environment. Presence only, per section 9.
7. Runtime binding: if `-ExpectedCommit` is supplied, a governed Git read (section 10)
   confirms the deployed checkout is at exactly that commit with a clean worktree.
8. Security expectations on the launcher root hold (section 17).

### 5.3 Invocation and exit codes

On success the launcher invokes
`<PythonExe> -m energygrid_bill_downloader <Command> --config <ConfigPath>` with the
`energygrid-bill-downloader` directory beneath `-CheckoutRoot` as the working
directory, passes the process environment through unchanged, and propagates the child's
exit code verbatim.

The application returns `0`, `10`, `20`, or `64`. The launcher's own failures therefore
use a disjoint band, so a launcher failure can never be mistaken for an application
status:

| Code | Meaning |
| --- | --- |
| `0`, `10`, `20`, `64` | Propagated unchanged from the application child process |
| `70` | Launcher preflight or validation failed; no child process was started |
| `71` | Installation or update failed; destination left at its accepted preimage |
| `72` | Post-replacement verification failed; rollback performed and verified |
| `73` | Rollback failed; manual owner action required |

The disjointness of the two bands is asserted by a test, not left to convention.

## 6. Installation And Update Contract

`runtime/install_or_update_launcher.ps1` publishes the reviewed launcher from the
deployed checkout into the launcher root. It is the only sanctioned way the installed
launcher ever changes.

### 6.1 Parameters

`-CheckoutRoot` (source, required), `-LauncherRoot` (destination, required),
`-ValidateOnly` (switch), `-LogRoot` (optional), `-RunId` (optional). No credential,
scheduler, ACL-mutation, or cleanup parameter exists.

### 6.2 Sequence

1. Resolve the source files under the `energygrid-bill-downloader/runtime/` directory
   beneath `-CheckoutRoot`.
2. Assert each source file parses cleanly with
   `[System.Management.Automation.Language.Parser]::ParseFile`, and compute its
   SHA-256.
3. Compute the SHA-256 of each currently installed file, when present.
4. If every installed file already matches its source hash, report `ALREADY_CURRENT`,
   perform no replacement, and exit `0`. Installation is idempotent by hash, not by
   timestamp.
5. Otherwise, for each file that differs: record the installed preimage hash, write the
   source bytes to an owned staging file in the destination directory, and publish it
   with the atomic replacement contract in section 7.
6. Report a per-file result object and exit `0` only when every file verified.

### 6.3 Constraints

- The installer never writes into the Git checkout, never reads private configuration,
  never reads or decrypts credentials, and never touches the archive, the SQLite state,
  the logs, or the Scheduled Task.
- Staging and backup files are created in the destination directory only, are owned by
  the operation (a GUID-shaped component in the name), and are never created in a
  shared temporary directory.
- A file the installer did not create is never deleted. On any failure path, artefacts
  are retained for inspection rather than cleaned up.

## 7. Atomic Replacement And Rollback Contract

This section is the direct, reusable consequence of the Run119 `File.Replace` defect
(section 18.2). It is a contract on a library function, not a patch to one host.

### 7.1 Function shape

```text
Invoke-AtomicFileReplace
  -SourcePath        # the fully written staging file, mandatory
  -DestinationPath   # the file being replaced, mandatory
  -BackupPath        # explicit backup destination, mandatory, non-empty
  -ExpectedSha256    # hash the destination must have after replacement, mandatory
```

Returns a structured result object carrying `Success`, `SupportRef`,
`ExceptionTypeName`, `HResult`, `PreimageSha256`, `PostimageSha256`, `BackupRetained`,
and `RolledBack`.

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
3. **Rollback capability is retained until verification passes.** The backup is removed
   only after the post-replacement hash comparison succeeds. On any failure the backup
   is retained and reported.
4. **Rollback restores the accepted preimage.** If verification fails, the backup is
   restored over the destination using the same explicit-backup mechanism (a second,
   distinct same-directory backup path), the restored destination is re-hashed and
   compared to the recorded preimage hash, and the result is `72` on a verified
   restore or `73` when the restore itself cannot be verified.
5. **Exceptions are classified, never swallowed.** Every failure records the exception
   type name and the HRESULT formatted as `0x%08X`, mapped to a bounded support
   reference (section 11). No exception message text reaches any output surface.

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
asserted post-condition: on every failure class above, the destination hash equals the
recorded preimage hash.

A compatibility note that the implementation must respect: the null-backup rejection is
an observed behaviour of the Windows PowerShell 5.1 and .NET Framework 4.x boundary.
Other runtimes may accept a null backup argument. The design therefore does not rely on
any runtime rejecting it. The prohibition is enforced structurally by the mandatory
parameter and by a static guard over the committed call sites, so the contract holds
regardless of which runtime executes it.

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
- It emits exactly one JSON object on standard output: a `checks` map of check name to
  outcome, an overall `status`, and a `support_ref` when the overall status is not a
  pass. The object contains no path, no environment value, no account identity, and no
  Git output text.
- Its output is deterministic. It carries no timestamp and no generated identifier, so
  two consecutive runs against unchanged host state produce byte-identical output. This
  is what makes the idempotency assertion in section 12 a strict byte comparison rather
  than a fuzzy one.
- Exit `0` means every check passed. A failure exits `70` and names the first failing
  check by its stable name.

## 9. Private Configuration Boundary

- The launcher receives every host-specific value as an explicit parameter. There is no
  default that encodes a private path, a private identity, or a host name.
- `launcher.settings.example.json` may be committed as a shape, carrying
  `REPLACE_WITH_...` placeholders only, following the precedent set by
  `config/energygrid.example.json`.
- `account_identity` stays where it already is: in the external private JSON, never on
  a command line, never in a log, never in the repository.
- Credentials are read from `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` in the
  process environment by the application. The launcher asserts that each is present and
  non-empty and does nothing else with them. It never accepts them as parameters, never
  writes them, never includes them in a result object, never passes them as command-line
  arguments, and never emits their length, prefix, or hash.
- DPAPI material and any host credential store stay on the host. The launcher never
  decrypts, never enumerates, and never persists credential material.
- The launcher writes only to the private diagnostics root supplied by `-LogRoot`, and
  only the single terminal event described in section 11.

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
| `EGRT-T10` | Every replacement failure class leaves the destination hash equal to the recorded preimage hash | A and B |
| `EGRT-T11` | On success the post-replacement hash is verified before the backup is removed: the backup is absent after success and present after any failure | A |
| `EGRT-T12` | A deliberately wrong `-ExpectedSha256` triggers rollback, the destination is restored to the recorded preimage hash, and the result is `72` | A |
| `EGRT-T13` | No credential value, account identity, private absolute path (`^[A-Za-z]:\\`), or UNC path (`^\\\\`) appears in any committed runtime file, example settings file, or test | C |
| `EGRT-T14` | `-ValidateOnly` mutates nothing: a full recursive snapshot of path, size, modification time, and hash across the scratch launcher root, config path, and runtime roots is identical before and after, with no file created or removed | A |
| `EGRT-T15` | Repeated validation is idempotent: two consecutive `-ValidateOnly` runs produce byte-identical standard output and identical filesystem snapshots | A |
| `EGRT-T16` | Installation is idempotent by hash: a second install against an already-current destination reports `ALREADY_CURRENT` and performs no replacement | A |
| `EGRT-T17` | Every committed runtime `.ps1` parses cleanly via `Parser::ParseFile` with zero errors | C |
| `EGRT-T18` | Every live `EG_LAUNCHER_*` reference is reachable and every retired reference is unreachable | A and C |
| `EGRT-T19` | The launcher exit band `70` to `73` is disjoint from the application's `0`, `10`, `20`, `64` | C |
| `EGRT-T20` | No test path invokes the application's `run` or `list` against anything but a stubbed child executable, and no committed runtime file contains a portal URL literal | C |

### 12.3 Testability without production fallbacks

`-ExpectedSha256` is a mandatory production parameter, not a test hook. Passing a
deliberately wrong value is a legitimate caller error, and it is what lets `EGRT-T12`
exercise the rollback path without adding a test-only branch, a mock seam, or a
compatibility fallback to production code. Similarly, the ordering assertion in
`EGRT-T11` is made against observable post-conditions (backup present or absent) rather
than against injected instrumentation.

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

This design change itself touches only `docs/superpowers/specs/`, which matches no
workflow trigger path. No GitHub Actions run is therefore expected for the design pull
request, and its absence is not a failure.

## 14. Deployment And Update Flow

1. The implementation pull request is reviewed and merged.
2. On the production host, under owner control, the deployed checkout is updated to the
   reviewed commit.
3. `install_or_update_launcher.ps1 -ValidateOnly` is run and its JSON is inspected.
4. The owner gives explicit current-turn approval for the mutating install, naming the
   launcher root and the operation. This is a live-system action and is gated by
   `AGENTS.md`.
5. The installer is run for real. It stages, replaces atomically with an explicit
   backup, verifies the hash, and reaps the backup only on verified success.
6. `launcher.ps1 -ValidateOnly` is run to confirm runtime binding, security
   expectations, and configuration reachability on the host.
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
   the host. Both are expected to be the accepted preimage.
2. **Reconcile behaviour.** Compare the installed launcher's behaviour to the
   source-controlled implementation, and classify every difference as one of: reusable
   and non-secret, so it moves into the repository; host-specific, so it moves into the
   private configuration or a parameter; or obsolete, so it is dropped with the reason
   recorded. Any residue that fits none of the three is a blocker and stops the
   migration.
3. **Publish.** Merge the implementation, which by then encodes every reusable
   behaviour from step 2.
4. **Validate.** Run both `-ValidateOnly` paths on the host and confirm the checks pass
   against the real deployment.
5. **Install under approval.** Perform the first source-controlled installation through
   the sanctioned installer, per section 14.
6. **Verify.** Confirm the installed hash matches the reviewed source hash and that
   `launcher.ps1 -ValidateOnly` passes.
7. **Retire the bridge artefact.** Only after step 6, and only under a separate explicit
   approval naming the artefact, may `.energygrid-launcher.run119a1.rollback` be
   removed. It is the last remaining rollback to the accepted preimage until then, so
   it is retained through every earlier step.

## 16. Recovery And Disaster-Rebuild Procedure

The acceptance test for this design is that the following sequence needs the reviewed
commit and separately supplied private host configuration, and nothing else. It needs
no GitHub issue comment, no chat history, no retired bridge, no `%TEMP%` script, and no
recollection of the original server setup.

1. Clone `x-boundaries/automation` at the reviewed commit onto a clean Windows host.
2. Install Python 3.14.x and the pinned dependencies with
   `python -m pip install --requirement requirements.txt` from
   `energygrid-bill-downloader`.
3. Provision Chromium with the official mechanism, `python -m playwright install chromium`.
4. Create the private runtime roots (archive, SQLite state parent, temp, logs, browser
   cache) per the path rules the application enforces and the project README documents.
5. Copy `config/energygrid.example.json` to an external private path and set
   `account_identity` and every path to the real private values.
6. Supply `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` through the approved host
   mechanism.
7. Run `install_or_update_launcher.ps1 -ValidateOnly`, then the approved install.
8. Run `launcher.ps1 -ValidateOnly` and confirm every check passes.
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

Security expectations are recorded as named checks with expected outcomes. The
principal, SID, and paths they are evaluated against are supplied at deployment.

| Check name | Expected outcome |
| --- | --- |
| `launcher_root_not_writable_by_run_principal` | The principal the scheduled job runs as has no write, modify, or full-control grant on the launcher root, so a compromised run cannot rewrite its own launcher |
| `launcher_root_write_restricted_to_install_principal` | Write access is limited to the administrative principal that performs installation |
| `launcher_files_not_reparse_points` | Neither installed file, nor the destination directory, is a symlink, junction, or other reparse point |
| `launcher_files_not_unexpectedly_readonly` | No installed file carries an unexplained read-only attribute |
| `config_path_outside_checkout` | The private configuration resolves outside the deployed checkout |
| `launcher_root_outside_checkout` | The launcher root resolves outside the deployed checkout |

A failed security check is a terminal preflight failure. It is never downgraded to a
warning and never bypassed by a switch.

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

Reusable requirement: section 7's replacement and rollback contract, asserted by
`EGRT-T06` through `EGRT-T12`, with the structural prohibition on null and empty backup
arguments asserted by `EGRT-T08` so that the rule holds on runtimes that would accept
them.

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

### 19.2 For the later implementation change

| ID | Criterion |
| --- | --- |
| `EGRT-I01` | `runtime/launcher_lib.ps1` is pure and dot-sourceable, with no side effect at load |
| `EGRT-I02` | `runtime/launcher.ps1` implements the section 5 parameter surface, preflight order, and exit bands |
| `EGRT-I03` | `runtime/install_or_update_launcher.ps1` implements the section 6 sequence, including hash idempotency |
| `EGRT-I04` | `Invoke-AtomicFileReplace` implements every rule in section 7.2, with a mandatory explicit backup path |
| `EGRT-I05` | `Invoke-GovernedGit` implements the section 10 result contract and environment neutralisation |
| `EGRT-I06` | `-ValidateOnly` satisfies section 8, including deterministic output and zero mutation |
| `EGRT-I07` | All twenty assertions `EGRT-T01` to `EGRT-T20` are implemented and pass |
| `EGRT-I08` | The full project suite passes on Windows via `python -m unittest discover -s tests -v` |
| `EGRT-I09` | No GitHub Actions workflow is modified |
| `EGRT-I10` | No secret, credential, private absolute path, or private identity is committed |
| `EGRT-I11` | The `EG_LAUNCHER_*` vocabulary is bounded, closed, and reachability-tested |
| `EGRT-I12` | The implementation performs no live server, scheduler, portal, or credential action |

Acceptance of this document is an architectural decision only. It does not approve the
implementation change, the first installation, the scheduler, or any live run. Each of
those remains a separate gate.
