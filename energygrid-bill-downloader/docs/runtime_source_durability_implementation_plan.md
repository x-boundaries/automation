# Energy@Grid Runtime Source-Durability Implementation Plan

Status: plan only. This change adds no launcher, no installer, no runtime
implementation, no test, and no CI change. It is the task-by-task execution plan that a
later, separately approved implementation change must follow.

Design lock: `DL-XB-141-RUNTIME-005-SOURCE-DURABILITY`.
Controlling specification: `energygrid-bill-downloader/docs/runtime_source_durability_design.md`.
Canonical base at authoring time: `main` at
`0e3d53c57682e345d2889b7f9e860c658ee450c0`, tree
`d306d0162ea217fd1f97cb4c86c785af6a025909`, sole parent
`e43efacfbd2016873ac78720c3a6874b3268910d`.

## Goal

Implement the accepted Option 2 runtime source-durability architecture so that the
reusable, non-secret Energy@Grid launcher, installer, and runtime-binding behaviour is
canonical in Git, is offline-regression-tested, and satisfies every acceptance criterion
`EGRT-I01` through `EGRT-I30` and every assertion `EGRT-T01` through `EGRT-T57`, while
every private value stays on the server.

The work is complete when `python -m unittest discover -s tests -v` run from
`energygrid-bill-downloader` on Windows passes with all fifty-seven assertions present,
and no GitHub Actions workflow has been modified.

## Architecture

Three source-controlled PowerShell files under `energygrid-bill-downloader/runtime/`,
driven by one Python `unittest` module inside the already-discovered project suite.

- `launcher_lib.ps1` is a **pure, dot-sourceable library**. No filesystem mutation at
  import time, no network, no Git invocation at load, and no live action. Every function
  is deterministic given its inputs and its explicitly supplied paths. This is what makes
  the offline tests possible and it mirrors `scripts/member_create_uat_runner_lib.ps1`.
- `launcher.ps1` is the **impure entry script**: argument surface, ordered preflight,
  process-scope environment injection, child invocation, exit codes, terminal event.
- `install_or_update_launcher.ps1` is the **impure installer entry script**: the
  four-phase package transaction and the sole rollback authority.
- `tests/test_runtime_launcher.py` is the offline driver. It invokes PowerShell through
  `subprocess`, following `tests/test_member_create_uat_runner_ps.py`.

The dependency direction is one-way. Entry scripts dot-source the library; the library
never dot-sources an entry script and never reads a caller's `$PSScriptRoot`.

## Tech Stack

| Component | Pinned contract | Evidence in repository |
| --- | --- | --- |
| Application runtime | Python 3.14.x | `energygrid-bill-downloader/README.md`; workflow `python-version: "3.14"` |
| Browser automation | `playwright==1.61.0` | `energygrid-bill-downloader/requirements.txt` |
| Production shell boundary | Windows PowerShell 5.1 on .NET Framework 4.x, `PSEdition` `Desktop` | design section 12.1 |
| Portable shell tier | whichever host PowerShell is available (`pwsh` or `powershell`) | `tests/test_member_create_uat_runner_ps.py` |
| Test framework | standard-library `unittest`, discovery pattern `test*.py` | workflow `python -m unittest discover -s tests -v` |
| CI runner | `windows-latest`, provides both `powershell.exe` and `pwsh` | `.github/workflows/energygrid-bill-downloader-tests.yml` |

## Spec

The single controlling specification is
`energygrid-bill-downloader/docs/runtime_source_durability_design.md` at canonical `main`
`0e3d53c57682e345d2889b7f9e860c658ee450c0`. No task may weaken, broaden, reinterpret, or
redesign it. Where this plan resolves an implementation choice the design left open, that
resolution is recorded in **Derived decisions** below with the design sections it follows
from.

## Global Constraints

### Authority and scope

1. Implementation only. No task registers, alters, starts, or removes a Scheduled Task.
2. No task contacts the live Energy@Grid portal, the production server, the production
   launcher root, or any live n8n instance.
3. No task creates, rotates, relocates, or inspects a real DPAPI credential artefact. The
   only credential material any test may touch is a synthetic `PSCredential` built from
   throwaway values inside the runner's temporary directory.
4. No task provisions, installs, updates, or repairs a browser cache. The runtime binds
   and validates a supplied cache; it never writes into it.
5. No GitHub Actions workflow is modified (`EGRT-I09`). Task 22 verifies this as a diff
   assertion, not as an intention.
6. No task promotes a retired Run119 bridge or diagnostic probe into a repository
   artefact.

### Committed-content constraints

7. No username, password, credential blob, DPAPI material, connection string, private
   account identity, private absolute path matching `^[A-Za-z]:\\`, UNC path matching
   `^\\\\`, host identity, or principal identity may appear in any committed runtime
   file, the example settings file, or any test (`EGRT-T13`, `EGRT-I10`).
8. Committed example configuration carries `REPLACE_WITH_...` placeholders only,
   following the precedent in `config/energygrid.example.json`.
9. Plain ASCII punctuation in all committed PowerShell, JSON, and Python added by this
   plan, per root `AGENTS.md`.
10. Repository PowerShell naming stays snake_case for file names
    (`install_or_update_launcher.ps1`, not a hyphenated form) and `Verb-Noun` for
    function names.

### Fallback discipline

11. No broad fallback, silent compatibility path, synthetic-data fallback, fake success
    state, or catch-and-continue behaviour. A failed check fails the run. The single
    sanctioned narrow, visible exception is post-acceptance backup-cleanup failure
    (design section 6.6), which reports `EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE`
    with a `backups_remaining` count and exits `0`. Task 12 implements exactly that and
    nothing wider.
12. No test-only branch, mock seam, or compatibility fallback is added to production
    code. `-ExpectedSha256` is a mandatory production parameter, not a test hook
    (design section 12.3).

### Windows PowerShell 5.1 compatibility rules

Every committed runtime `.ps1` must execute correctly under `powershell.exe`
(`PSEdition` `Desktop`). The following are prohibited because they are parse or runtime
errors on that boundary. Task 1 installs the static guard for the mechanically checkable
subset.

| Prohibited construct | Required 5.1-safe form |
| --- | --- |
| Pipeline chain operators (and/or forms) | `;` plus an explicit `if` on the prior result |
| Ternary conditional operator | `if` / `else` |
| Null-coalescing and null-conditional operators | explicit `$null -eq` / `$null -ne` tests |
| Native-command stderr merge redirection | `System.Diagnostics.Process` with redirected streams (design section 10.1) |
| `ConvertFrom-Json -AsHashtable` | `ConvertFrom-Json` to `PSCustomObject`, then an explicit property walk |
| `Join-Path` with three or more path arguments | nested two-argument `Join-Path` calls |
| `$IsWindows` automatic variable | `[System.Environment]::OSVersion.Platform` |
| `New-Item -Force` against an existing file | explicit `Test-Path`, then a deliberate write |
| `Set-Content` / `Add-Content` without `-Encoding` | `[System.IO.File]::WriteAllText($path, $text, $utf8NoBom)` |
| `ConvertTo-Json` without `-Depth` | `ConvertTo-Json -Depth 8 -Compress` over `[ordered]@{}` inputs |

Two further 5.1 behaviours are load-bearing and must be respected rather than merely
avoided.

- `ConvertFrom-Json` on PowerShell 7 coerces ISO-date-shaped strings to `[datetime]`
  while 5.1 keeps them as strings. The installation manifest therefore carries **no
  date-shaped field**. Its only fields are file name, lowercase hex SHA-256, integer byte
  length, and the 40-character hex admission commit, so manifest parsing is identical on
  both editions.
- Pipeline scalar collapse is the direct cause of the Run119 output-shape defect. Every
  collection produced from a pipeline or a native command must be forced with the array
  subexpression before it is returned or indexed.

### Determinism

13. `-ValidateOnly` output on both entry scripts carries no timestamp and no generated
    identifier, so two consecutive runs against unchanged host state produce
    byte-identical standard output (`EGRT-T15`). Every emitted object is built from
    `[ordered]@{}` and serialised with `ConvertTo-Json -Depth 8 -Compress`.
14. Every hash in this plan is a lowercase hexadecimal SHA-256 string produced by
    `Get-EgFileSha256`, which normalises `Get-FileHash` output with `ToLowerInvariant()`.

## Derived decisions

These resolve implementation choices the accepted design deliberately left open. Each is
derived from the design, adds no new contract, and weakens nothing. They are recorded here
so the executing engineer does not have to invent them.

| ID | Decision | Derived from |
| --- | --- | --- |
| `DD-01` | The launcher root is the `$PSScriptRoot` of `launcher.ps1`. There is no launcher-root parameter on the launcher, and package-member paths are fixed joins of that root with the three Class A names. | 5.1 parameter table is exhaustive; 6.7 security boundary requires fixed joins |
| `DD-02` | `launcher_root_not_writable_by_run_principal` is evaluated against the currently running identity and its group SIDs via `[System.Security.Principal.WindowsIdentity]::GetCurrent()`. No principal parameter is added. | 5.1 has no principal parameter; 17.2 says the principal is supplied at deployment, and at run time the deployed run principal is the current identity |
| `DD-03` | `launcher_root_write_restricted_to_install_principal` asserts that every write, modify, or full-control ACE on the launcher root resolves to a well-known administrative SID: `S-1-5-32-544` (Administrators), `S-1-5-18` (SYSTEM), `S-1-3-0` (CREATOR OWNER), or a SID beginning `S-1-5-80-` (service/TrustedInstaller class). Well-known SIDs are universal, not host-specific, so nothing private is committed. | 17.2 heading "Expressed without private identities"; 3.3 host-specific values are never committed; 9.1 no private-identity default |
| `DD-04` | `launcher_root_entries_classified` is not re-evaluated inside the security group. It reports the result already computed at preflight step 1, so classification runs exactly once per run. | 5.2 step 1 runs first and is terminal; 17.2 records the same property as a named check |
| `DD-05` | The application-required configuration keys the launcher checks for are exactly `portal_url`, `account_identity`, `archive_root`, `state_path`, `temp_root`, and `log_root`. The launcher checks presence and non-empty string only; it does not re-validate the application's path rules. | 5.2 step 4; `energygrid_bill_downloader/config.py` `load_runtime_config` required set plus `_validate_url` and `_validate_account_identity` |
| `DD-06` | The installer's real-path stdout and its `-ValidateOnly` stdout are two distinct bounded JSON shapes. `ALREADY_CURRENT` is a real-path `status`, not a validation check, so it never appears in the checks map. | 6.2 Phase 1 step 5 is a real-install outcome; section 8 defines the validation shape |
| `DD-07` | `HResult` is emitted as a string in exact `0x%08X` form, and is the empty string when no exception occurred. `ExceptionTypeName` is likewise the empty string when no exception occurred. Both fields are always present on every result object. | 7.2 rule 5; 11.1 |
| `DD-08` | `RETIRED_EG_LAUNCHER_SUPPORT_REFS` is introduced as an empty closed set at first implementation. The reachability test asserts both halves; the retired half is vacuously satisfied until a reference is retired. | 11.4; `cli.py` `RETIRED_SUPPORT_REFS` precedent |
| `DD-09` | The installer transaction identifier used in the Class B `operation-id` field is generated once per installer invocation with `[guid]::NewGuid().ToString('D').ToLowerInvariant()`. `-ValidateOnly` generates none, because it creates no residue and must stay byte-deterministic. | 6.3, 6.7, section 8 determinism |
| `DD-10` | Publication order within Phase 2 is the fixed recorded order `launcher_lib.ps1` first, then `launcher.ps1`. The library is published before the entry script that dot-sources it, so a mid-transaction crash leaves an old entry script with a new library rather than a new entry script calling a missing library function. Reverse publication order for rollback is therefore `launcher.ps1` first, then `launcher_lib.ps1`. | 6.2 Phase 2 "in a recorded order"; 6.5 reverse publication order |
| `DD-11` | `Test-EgGovernedSourceIntegrity` detects a `.gitignore`-hidden untracked overlay by running `ls-files --others --exclude-standard` and `ls-files --others --ignored --exclude-standard` over the governed executable pathspec, then subtracting the sanctioned Python bytecode exception. Both are read-only allowlisted `ls-files` invocations. | 10.3 overlay detection and the one legitimate exception; 10.3 read-only allowlist |

## Support-reference vocabulary

This is the complete live `EG_LAUNCHER_*` set the implementation introduces. The
**Source** column marks `design` where the design names the reference literally, and
`derived` where the design mandates a terminal failure and this plan names the bounded
reference for it. No reference outside this table may be emitted; an unrecognised failure
records `EG_LAUNCHER_UNCLASSIFIED`.

| Reference | Source | Raised by |
| --- | --- | --- |
| `EG_LAUNCHER_REPLACE_ARGUMENT_INVALID` | design 7.3 | `Invoke-AtomicFileReplace` |
| `EG_LAUNCHER_REPLACE_SHARING_VIOLATION` | design 7.3 | `Invoke-AtomicFileReplace` |
| `EG_LAUNCHER_REPLACE_ACCESS_DENIED` | design 7.3 | `Invoke-AtomicFileReplace` |
| `EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH` | derived (7.2 rule 2) | `Invoke-AtomicFileReplace` |
| `EG_LAUNCHER_REPLACE_PREIMAGE_UNRECOVERABLE` | design 7.2.1 | installer rollback |
| `EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT` | design 7.4 | `Invoke-PublishToAbsentDestination` |
| `EG_LAUNCHER_PUBLISH_RACE_LOST` | design 7.4 | `Invoke-PublishToAbsentDestination` |
| `EG_LAUNCHER_PUBLISH_POSTIMAGE_MISMATCH` | derived (7.4 step 4) | `Invoke-PublishToAbsentDestination` |
| `EG_LAUNCHER_CREDENTIAL_ARTEFACT_MISSING` | design 9.2 | `Import-EgLauncherCredential` |
| `EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED` | design 9.2 | `Import-EgLauncherCredential` |
| `EG_LAUNCHER_CREDENTIAL_INCOMPLETE` | design 9.2 | `Test-EgCredentialViability` |
| `EG_LAUNCHER_CREDENTIAL_RESTORE_FAILED` | design 9.2 | `Restore-EgProcessEnvironmentSnapshot` |
| `EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED` | design 9.3 | `Test-EgBrowserCacheReady` |
| `EG_LAUNCHER_BROWSER_CACHE_NOT_READY` | design 9.3 | `Test-EgBrowserCacheReady` |
| `EG_LAUNCHER_BROWSER_CACHE_BIND_FAILED` | design 9.3 | `launcher.ps1` injection |
| `EG_LAUNCHER_SOURCE_BINDING_FAILED` | design 10.3 | `Test-EgGovernedSourceIntegrity` |
| `EG_LAUNCHER_SOURCE_BRANCH_MISMATCH` | derived (10.3 check 2) | `Test-EgGovernedSourceIntegrity` |
| `EG_LAUNCHER_SOURCE_PATH_MISSING` | design 10.3 | `Test-EgGovernedSourceIntegrity` |
| `EG_LAUNCHER_SOURCE_PATH_UNTRACKED` | design 10.3 | `Test-EgGovernedSourceIntegrity` |
| `EG_LAUNCHER_SOURCE_STAGED_MODIFICATION` | design 10.3 | `Test-EgGovernedSourceIntegrity` |
| `EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION` | design 10.3 | `Test-EgGovernedSourceIntegrity` |
| `EG_LAUNCHER_SOURCE_DELETED` | design 10.3 | `Test-EgGovernedSourceIntegrity` |
| `EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY` | design 10.3 | `Test-EgGovernedSourceIntegrity` |
| `EG_LAUNCHER_GIT_INVOCATION_FAILED` | derived (10.1 `SupportRef`) | `Invoke-GovernedGit` |
| `EG_LAUNCHER_GIT_SUBCOMMAND_FORBIDDEN` | derived (10.3 allowlist) | `Invoke-GovernedGit` |
| `EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY` | derived (6.7 Class C) | `Get-EgLauncherRootClassification` |
| `EG_LAUNCHER_PACKAGE_MEMBER_MISSING` | derived (6.7 Class A) | `Get-EgLauncherRootClassification` |
| `EG_LAUNCHER_PACKAGE_PARSE_FAILED` | derived (5.2 step 1d) | `launcher.ps1` preflight |
| `EG_LAUNCHER_MANIFEST_MISSING` | derived (6.4 terminal) | `Compare-EgInstalledPackageToManifest` |
| `EG_LAUNCHER_MANIFEST_UNPARSABLE` | derived (6.4 terminal) | `Compare-EgInstalledPackageToManifest` |
| `EG_LAUNCHER_MANIFEST_MISMATCH` | derived (6.4 terminal) | `Compare-EgInstalledPackageToManifest` |
| `EG_LAUNCHER_PATH_NOT_ABSOLUTE` | derived (5.2 step 2) | `launcher.ps1` preflight |
| `EG_LAUNCHER_PATH_MISSING` | derived (5.2 step 2) | `launcher.ps1` preflight |
| `EG_LAUNCHER_CONFIG_INSIDE_CHECKOUT` | derived (5.2 step 3, 17.2) | `launcher.ps1` preflight |
| `EG_LAUNCHER_CONFIG_UNPARSABLE` | derived (5.2 step 4) | `launcher.ps1` preflight |
| `EG_LAUNCHER_CONFIG_KEY_MISSING` | derived (5.2 step 4) | `launcher.ps1` preflight |
| `EG_LAUNCHER_PYTHON_VERSION_UNSUPPORTED` | derived (5.2 step 5) | `launcher.ps1` preflight |
| `EG_LAUNCHER_ROOT_INSIDE_CHECKOUT` | derived (17.2) | `Test-EgLauncherRootSecurity` |
| `EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE` | derived (17.2) | `Test-EgLauncherRootSecurity` |
| `EG_LAUNCHER_ROOT_ACL_WRITE_NOT_RESTRICTED` | derived (17.2) | `Test-EgLauncherRootSecurity` |
| `EG_LAUNCHER_ROOT_REPARSE_POINT` | derived (17.2) | `Test-EgLauncherRootSecurity` |
| `EG_LAUNCHER_FILE_UNEXPECTEDLY_READONLY` | derived (17.2) | `Test-EgLauncherRootSecurity` |
| `EG_LAUNCHER_INSTALL_ADMISSION_INVALID` | derived (6.1 mandatory) | installer Phase 1 |
| `EG_LAUNCHER_INSTALL_STAGING_FAILED` | derived (6.2 Phase 1 step 6) | installer Phase 1 |
| `EG_LAUNCHER_INSTALL_MANIFEST_VERIFY_FAILED` | derived (6.2 Phase 3) | installer Phase 3 |
| `EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE` | derived (6.5 exit 73) | installer rollback |
| `EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE` | design 6.6 | installer Phase 4 |
| `EG_LAUNCHER_UNCLASSIFIED` | design 11.4 | any unrecognised failure |

## Shared object shapes

Every task below produces or consumes these shapes. They are defined once here and
referenced by name.

### `PublicationResult`

Returned by both publish primitives. All ten fields are always present (design section
7.1, run-instruction contract family J).

```text
[pscustomobject]@{
    Success             = [bool]    # positively verified publication
    SupportRef          = [string]  # bounded EG_LAUNCHER_* reference, '' on success
    ExceptionTypeName   = [string]  # exception type name, '' when none (DD-07)
    HResult             = [string]  # '0x%08X' form, '' when none (DD-07)
    PreimageState       = [string]  # 'Existing' or 'Absent'
    PreimageSha256      = [string]  # lowercase hex, '' when PreimageState is 'Absent'
    PostimageSha256     = [string]  # lowercase hex, '' when the destination is absent
    PublicationOccurred = [bool]    # the destination advanced (design 7.2.1)
    BackupCreated       = [bool]    # a backup file was observed on disk
    BackupRetained      = [bool]    # equals BackupCreated; the primitive never reaps
}
```

### `GovernedGitResult`

```text
[pscustomobject]@{
    Success    = [bool]     # $true only when the process exited 0
    ExitCode   = [int]      # always the real process exit code
    Lines      = [string[]] # always a collection, never a bare string, never $null
    SupportRef = [string]   # bounded reference on failure, '' on success
}
```

### `CheckResult`

Returned by every predicate that participates in preflight or validation.

```text
[pscustomobject]@{
    Pass       = [bool]
    SupportRef = [string]                 # '' when Pass is $true
    Checks     = [System.Collections.Specialized.OrderedDictionary]
                                          # stable check name -> 'PASS' or 'FAIL'
}
```

### `InstallationManifest`

Written to the launcher root as `installation_manifest.json`. Never committed.

```text
{
  "schema_version": "eg_launcher_installation_manifest/v1",
  "admission_commit": "<40 lowercase hex characters>",
  "members": [
    { "name": "launcher.ps1",     "sha256": "<64 lowercase hex>", "byte_length": <int> },
    { "name": "launcher_lib.ps1", "sha256": "<64 lowercase hex>", "byte_length": <int> }
  ]
}
```

The `members` array is sorted by `name` ascending and describes exactly the two executable
members, never the manifest itself and never residue (design section 6.4).

### Launcher `-ValidateOnly` stdout

One JSON object, no timestamp, no generated identifier (design section 8).

```text
{"checks":{"<stable_name>":"PASS|FAIL", ...},"status":"PASS|FAIL","support_ref":"<ref or omitted on PASS>"}
```

### Installer real-path stdout (`DD-06`)

```text
{"status":"INSTALLED|ALREADY_CURRENT|FAILED_PREFLIGHT|FAILED_ROLLED_BACK|FAILED_ROLLBACK_INCOMPLETE","support_ref":"<ref or empty>","backups_remaining":<int>}
```

## Ordered preflight check names

The launcher evaluates these once each, in this exact order. `EGRT-T48` asserts that
positions 1 to 18 all complete before position 19 is attempted.

| # | Stable check name | Design source |
| --- | --- | --- |
| 1 | `launcher_root_entries_classified` | 5.2 step 1a-1b, 6.7, 17.2 |
| 2 | `launcher_package_members_present` | 5.2 step 1c |
| 3 | `launcher_package_parse_clean` | 5.2 step 1d |
| 4 | `launcher_package_manifest_match` | 5.2 step 1d, 6.4 |
| 5 | `checkout_root_exists_absolute` | 5.2 step 2 |
| 6 | `config_path_exists_absolute` | 5.2 step 2 |
| 7 | `python_exe_exists_absolute` | 5.2 step 2 |
| 8 | `config_path_outside_checkout` | 5.2 step 3, 17.2 |
| 9 | `config_parses_json` | 5.2 step 4 |
| 10 | `config_required_keys_present` | 5.2 step 4, `DD-05` |
| 11 | `python_version_is_3_14` | 5.2 step 5 |
| 12 | `governed_source_integrity` | 5.2 step 6, 10.3 |
| 13 | `browser_cache_ready` | 5.2 step 7, 9.3 |
| 14 | `launcher_root_outside_checkout` | 5.2 step 8, 17.2 |
| 15 | `launcher_root_not_writable_by_run_principal` | 17.2, `DD-02` |
| 16 | `launcher_root_write_restricted_to_install_principal` | 17.2, `DD-03` |
| 17 | `launcher_files_not_reparse_points` | 17.2 |
| 18 | `launcher_files_not_unexpectedly_readonly` | 17.2 |
| 19 | `credential_import_ok` | 5.2 step 9, 9.2, section 8 |
| 20 | `username_nonempty` | 5.2 step 9, section 8 |
| 21 | `password_nonempty` | 5.2 step 9, section 8 |

## Task index and assertion coverage

| Task | Subject | Assertions closed |
| --- | --- | --- |
| 1 | Test harness, tiering, parse and 5.1-compatibility guards | `EGRT-T17` |
| 2 | `Invoke-GovernedGit` result contract | `EGRT-T01`, `EGRT-T02`, `EGRT-T03` |
| 3 | Ambient Git environment neutralisation and restoration | `EGRT-T04`, `EGRT-T05` |
| 4 | Read-only Git subcommand allowlist and static guard | `EGRT-T39` |
| 5 | `Invoke-AtomicFileReplace` | `EGRT-T06`, `EGRT-T07`, `EGRT-T09`, `EGRT-T10`, `EGRT-T11` |
| 6 | `Invoke-PublishToAbsentDestination` | `EGRT-T50` |
| 7 | Null and empty backup-argument static guard | `EGRT-T08` |
| 8 | Launcher-root Class A/B/C classification | `EGRT-T51` to `EGRT-T55` |
| 9 | Installation manifest shape and comparison | `EGRT-T46` |
| 10 | Installer transaction Phases 1 to 4 | `EGRT-T16`, `EGRT-T41`, `EGRT-T45`, `EGRT-T47` |
| 11 | Installer-owned package rollback | `EGRT-T12`, `EGRT-T42`, `EGRT-T43`, `EGRT-T44` |
| 12 | Post-acceptance backup cleanup and visible residue status | design 6.6 |
| 13 | DPAPI credential import, injection, restoration, cleanup | `EGRT-T21` to `EGRT-T26`, `EGRT-T49` |
| 14 | Private Playwright browser-cache binding | `EGRT-T27` to `EGRT-T30` |
| 15 | Path-scoped governed source integrity | `EGRT-T32` to `EGRT-T38`, `EGRT-T40` |
| 16 | Launcher entry script, preflight order, exit bands, terminal event | `EGRT-T19`, `EGRT-T20`, `EGRT-T48`, `EGRT-T56`, `EGRT-T57` |
| 17 | `-ValidateOnly` on both entry scripts | `EGRT-T14`, `EGRT-T15`, `EGRT-T31` |
| 18 | Bounded support-reference vocabulary and reachability | `EGRT-T18` |
| 19 | Privacy static guard over committed files | `EGRT-T13` |
| 20 | `launcher.settings.example.json` placeholder shape | supports `EGRT-T13` |
| 21 | Runtime README, runbook, project README, scheduler example correction | `EGRT-I30`, run-instruction items P and Q |
| 22 | Full-suite validation, CI verification, no-workflow-change proof | `EGRT-I07`, `EGRT-I08`, `EGRT-I09` |

Every assertion `EGRT-T01` through `EGRT-T57` appears exactly once as a closing task
above. Task 22 re-runs the whole set.

---

## Task 1 - Test harness, tiering, and structural guards

Establishes the driver, the three tiers, and the guards every later task depends on.

### Files

- Create: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Create: `energygrid-bill-downloader/tests/test_runtime_launcher.py`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

Python module-level, in `test_runtime_launcher.py`:

```python
RUNTIME_DIR: Path            # <project>/runtime
LIB: Path                    # RUNTIME_DIR / "launcher_lib.ps1"
LAUNCHER: Path               # RUNTIME_DIR / "launcher.ps1"
INSTALLER: Path              # RUNTIME_DIR / "install_or_update_launcher.ps1"
RUNTIME_PS1_FILES: tuple[Path, ...]   # (LIB, LAUNCHER, INSTALLER)

def find_any_powershell() -> str | None:
    """Tier A host: pwsh, then powershell, then powershell.exe. None when absent."""

def find_desktop_powershell() -> str | None:
    """Tier B host: powershell.exe whose $PSVersionTable.PSEdition is 'Desktop'.

    Returns the resolved path only after a probe confirms the Desktop edition, so a
    pwsh shim named powershell.exe cannot masquerade as the 5.1 boundary.
    """

ANY_PS: str | None = find_any_powershell()
DESKTOP_PS: str | None = find_desktop_powershell()

def run_ps(exe: str, script_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke a scratch .ps1 with -NoProfile -NonInteractive -ExecutionPolicy Bypass -File."""

def probe(exe: str, op: str, **kwargs: str) -> subprocess.CompletedProcess[str]:
    """Run the shared PROBE scratch script with -Lib <LIB> -Op <op> and named arguments."""

def probe_json(exe: str, op: str, **kwargs: str) -> dict:
    """probe() plus returncode assertion plus json.loads of stdout."""
```

Two base classes:

```python
class TierABase(unittest.TestCase):
    """Portable library tests. Skips when ANY_PS is None."""

class TierBBase(unittest.TestCase):
    """Compatibility-boundary tests pinned to Windows PowerShell 5.1.

    Skips with a message naming powershell.exe when DESKTOP_PS is None.
    """
```

PowerShell, in `launcher_lib.ps1` (this task adds only the header and one function):

```powershell
Set-StrictMode -Version Latest

function Get-EgLauncherLibraryContract {
    # Returns the library's own bounded self-description. Pure; no side effect.
    [CmdletBinding()]
    param()
    [pscustomobject]@{
        SchemaVersion        = 'eg_launcher_lib/v1'
        DeployedMemberNames  = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
    }
}
```

### Steps

1. Write the failing test module with three tests:
   `test_runtime_library_exists_and_parses_cleanly` (`EGRT-T17`, Tier C: parse every path
   in `RUNTIME_PS1_FILES` that exists via `Parser::ParseFile` and assert zero errors, and
   assert `LIB` exists), `test_ci_requires_the_desktop_boundary_interpreter` (fails when
   `DESKTOP_PS is None` and `os.environ.get("CI")` is truthy, otherwise passes), and
   `test_committed_runtime_files_avoid_powershell_7_only_syntax` (Tier C: for each
   existing runtime file, assert the source contains none of the prohibited tokens
   listed in the Global Constraints compatibility table, matched as regular expressions
   over non-comment lines).
2. Run `python -m unittest tests.test_runtime_launcher -v` from
   `energygrid-bill-downloader`. Prove RED: `launcher_lib.ps1` does not exist.
3. Create `runtime/launcher_lib.ps1` with the header comment block, `Set-StrictMode
   -Version Latest`, and `Get-EgLauncherLibraryContract` exactly as above.
4. Re-run the module. Prove GREEN.
5. Run the neighbouring regression suite: `python -m unittest discover -s tests -v` from
   `energygrid-bill-downloader`. Confirm `test_cli.py` scope-guard tests still pass,
   because the new paths are under `energygrid-bill-downloader/` and the committed guard
   already accepts that prefix.
6. Commit: `Add EnergyGrid runtime test harness and structural guards`.

---

## Task 2 - `Invoke-GovernedGit` structured result contract

Closes the Run119 output-shape defect (design sections 10.1 and 18.1).

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
function Invoke-GovernedGit {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRootPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]]$Arguments
    )
    # Returns GovernedGitResult. Never throws for a non-zero Git exit.
}
```

Consumed: `System.Diagnostics.ProcessStartInfo` with `RedirectStandardOutput` and
`RedirectStandardError` both `$true`, `UseShellExecute` `$false`. Produced:
`GovernedGitResult`.

Mandatory implementation properties:

- The argument list passed to Git is always `@('-C', $RepositoryRootPath,
  '--no-optional-locks') + $Arguments`.
- Standard output is split on `"`r`n"` and `"`n"`, trailing empty entries are dropped,
  and the result is forced with the array subexpression before assignment to `Lines`.
- `Success` is `$true` only when `ExitCode -eq 0`.
- On `ExitCode -ne 0`, `SupportRef` is `EG_LAUNCHER_GIT_INVOCATION_FAILED`.
- Neither captured stream ever reaches a log, a console surface, or any field other than
  `Lines`, and `Lines` is consumed by the library rather than emitted.

### Steps

1. Add the `PROBE` operation `git` to the shared probe script: it creates a scratch Git
   repository at `-Dir`, makes one commit on branch `main`, then emits a JSON object with
   `oneLineCount`, `oneLineFirst`, `zeroLineSuccess`, `zeroLineCount`, `zeroLineIsNull`,
   `failExit`, `failSuccess`, and `failLineCount`, derived from three calls:
   `@('rev-parse','--abbrev-ref','HEAD')`, `@('ls-files','--','no_such_pathspec')`, and
   `@('rev-parse','--verify','refs/heads/definitely_absent')`.
2. Write failing tests `test_governed_git_preserves_one_line_output` (`EGRT-T01`:
   `oneLineCount == 1` and `oneLineFirst == "main"`, explicitly asserting the value is not
   `"m"`), `test_governed_git_preserves_successful_zero_line_output` (`EGRT-T02`:
   `zeroLineSuccess` true, `zeroLineCount == 0`, `zeroLineIsNull` false), and
   `test_governed_git_distinguishes_a_non_zero_exit` (`EGRT-T03`: `failSuccess` false,
   `failExit != 0`, and the failure is distinguishable from the `EGRT-T02` case even
   though both carry zero lines).
3. Run the module. Prove RED: `Invoke-GovernedGit` is not defined.
4. Implement `Invoke-GovernedGit` in `launcher_lib.ps1` exactly as specified.
5. Re-run the module. Prove GREEN on all three.
6. Regression: `python -m unittest discover -s tests -v` from
   `energygrid-bill-downloader`.
7. Commit: `Add governed Git structured result contract`.

---

## Task 3 - Ambient Git environment neutralisation and exact restoration

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
function Get-EgGovernedGitEnvironmentNames {
    # The exact nineteen names from design section 10.2, as a fixed ordered array.
    [CmdletBinding()]
    param()
    @(
        'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
        'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CONFIG',
        'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM',
        'GIT_CEILING_DIRECTORIES', 'GIT_NAMESPACE', 'GIT_ATTR_NOSYSTEM', 'GIT_PAGER',
        'GIT_EDITOR', 'GIT_ASKPASS', 'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_TERMINAL_PROMPT'
    )
}

function Get-EgProcessEnvironmentSnapshot {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Names)
    # Returns [ordered]@{ <name> = [pscustomobject]@{ Present = [bool]; Value = [string] } }
}

function Restore-EgProcessEnvironmentSnapshot {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Snapshot)
    # Returns CheckResult. A name recorded Present=$false is REMOVED, not set to ''.
    # SupportRef on failure is EG_LAUNCHER_CREDENTIAL_RESTORE_FAILED when the snapshot
    # carries a credential name, otherwise EG_LAUNCHER_UNCLASSIFIED.
}
```

`Invoke-GovernedGit` is modified to snapshot the nineteen names, remove every one of
them, run the read, and restore the snapshot in a `finally` block. Restoration is exact:
a variable absent beforehand is absent afterwards, never present and empty.

### Steps

1. Extend the probe with operation `gitambient`: it creates a real scratch repository and
   a separate decoy scratch repository, sets `GIT_DIR`, `GIT_WORK_TREE`, and
   `GIT_CONFIG_GLOBAL` to the decoy, calls `Invoke-GovernedGit` against the real
   repository, and emits `boundTopLevel`, plus a `restored` map recording for each of the
   nineteen names whether it is present afterwards and its value.
2. Write failing tests `test_ambient_git_variables_cannot_redirect_a_governed_read`
   (`EGRT-T04`: `boundTopLevel` resolves to the real repository, never the decoy) and
   `test_git_environment_is_restored_exactly_including_absent_names` (`EGRT-T05`: each of
   the three seeded names is restored to its exact seeded value, and every name that was
   absent before the call is absent after it, not present and empty).
3. Run the module. Prove RED.
4. Implement the three functions and wire the snapshot/neutralise/restore cycle into
   `Invoke-GovernedGit`.
5. Re-run. Prove GREEN. Re-run Task 2's three tests unchanged to prove the result contract
   still holds under neutralisation.
6. Regression: full project suite.
7. Commit: `Neutralise and exactly restore ambient Git environment`.

---

## Task 4 - Read-only Git subcommand allowlist and its static guard

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
$script:EgGitAllowedSubcommands = @('rev-parse', 'symbolic-ref', 'ls-files', 'diff', 'status')

function Test-EgGitSubcommandAllowed {
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Subcommand)
    # Returns [bool]. Exact, case-sensitive membership of $script:EgGitAllowedSubcommands.
}
```

`Invoke-GovernedGit` gains a first-argument gate: when
`Test-EgGitSubcommandAllowed $Arguments[0]` is `$false`, it returns a
`GovernedGitResult` with `Success` `$false`, `ExitCode` `-1`, empty `Lines`, and
`SupportRef` `EG_LAUNCHER_GIT_SUBCOMMAND_FORBIDDEN`, and starts no process.

The prohibited list asserted by the static guard is exactly design section 10.3's:
`fetch`, `pull`, `clone`, `remote`, `reset`, `rebase`, `merge`, `checkout`, `switch`,
`restore`, `cherry-pick`, `revert`, `stash`, `clean`, `add`, `rm`, `commit`, `tag`,
`push`, `gc`, `worktree`.

### Steps

1. Write the failing Tier A test `test_forbidden_git_subcommand_is_refused_without_a_process`
   (probe operation `gitforbidden` calls `Invoke-GovernedGit` with `@('fetch','--all')`
   and asserts `Success` false, `SupportRef` `EG_LAUNCHER_GIT_SUBCOMMAND_FORBIDDEN`, and
   `ExitCode -1`).
2. Write the failing Tier C test `test_no_committed_runtime_file_invokes_a_forbidden_git_subcommand`
   (`EGRT-T39`). Its mechanism, driven by a scratch AST inspector:
   - assert no `CommandAst` in any committed runtime file has `GetCommandName()` equal to
     `git`, `git.exe`, or resolves through `&` to either;
   - assert every `Invoke-GovernedGit` call site passes `-Arguments` as an array literal
     whose first element is a `StringConstantExpressionAst`;
   - assert every such first element is a member of the allowlist;
   - assert none of the twenty-one prohibited subcommand strings appears as a first
     element anywhere.
3. Run the module. Prove RED on both.
4. Implement `$script:EgGitAllowedSubcommands`, `Test-EgGitSubcommandAllowed`, and the
   gate inside `Invoke-GovernedGit`.
5. Re-run. Prove GREEN.
6. Regression: full project suite.
7. Commit: `Enforce the read-only Git subcommand allowlist`.

---

## Task 5 - `Invoke-AtomicFileReplace`

The direct, reusable consequence of the Run119 `File.Replace` null-backup defect
(design sections 7.2, 7.2.1, 7.3, 18.2).

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
function Get-EgFileSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)
    # Returns lowercase hex SHA-256, or '' when the path does not exist as a file.
}

function New-EgPublicationResult {
    # Internal factory. Guarantees all ten PublicationResult fields are always present.
    [CmdletBinding()]
    param(
        [bool]$Success, [string]$SupportRef, [string]$ExceptionTypeName, [string]$HResult,
        [string]$PreimageState, [string]$PreimageSha256, [string]$PostimageSha256,
        [bool]$PublicationOccurred, [bool]$BackupCreated, [bool]$BackupRetained
    )
}

function Get-EgExceptionHResultString {
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)
    # Returns the HRESULT as '0x%08X' using [string]::Format('0x{0:X8}', $Exception.HResult).
}

function Get-EgReplaceSupportRef {
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)
    # Maps by exception TYPE and HRESULT ONLY. Never reads exception message text.
    #   System.ArgumentException            -> EG_LAUNCHER_REPLACE_ARGUMENT_INVALID
    #   System.IO.IOException, 0x80070020   -> EG_LAUNCHER_REPLACE_SHARING_VIOLATION
    #   System.UnauthorizedAccessException  -> EG_LAUNCHER_REPLACE_ACCESS_DENIED
    #   anything else                       -> EG_LAUNCHER_UNCLASSIFIED
}

function Invoke-AtomicFileReplace {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$BackupPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedSha256
    )
    # Returns PublicationResult. Never rolls back. Never deletes a backup.
}
```

Required body sequence:

1. Validate `$BackupPath` resolves to the same directory as `$DestinationPath`. A
   different directory returns `EG_LAUNCHER_REPLACE_ARGUMENT_INVALID` before any call.
2. Record `$preimageSha = Get-EgFileSha256 $DestinationPath`. `PreimageState` is
   `Existing`.
3. Set `$publicationOccurred = $false`. Call
   `[System.IO.File]::Replace($SourcePath, $DestinationPath, $BackupPath)` inside `try`.
   Immediately after the call returns and before any verification work, set
   `$publicationOccurred = $true`.
4. In `catch`: record `ExceptionTypeName` and `HResult`, map the support reference, then
   re-hash the destination. When the observed hash no longer equals `$preimageSha`, set
   `$publicationOccurred = $true` (design section 7.2.1 determination). Otherwise leave it
   `$false`.
5. `BackupCreated` is `Test-Path -LiteralPath $BackupPath -PathType Leaf`, observed on
   disk, never inferred from the requested path. `BackupRetained` equals `BackupCreated`.
6. On a returned call, re-hash the destination and compare to `$ExpectedSha256`. A
   mismatch yields `Success` `$false` with `EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH`,
   `PublicationOccurred` `$true`, and the backup retained. This is Case B.
7. The function never deletes `$BackupPath` and never writes to `$DestinationPath` outside
   the single `File.Replace` call.

### Steps

1. Extend the probe with operations `replaceok`, `replacenullbackup`,
   `replaceemptybackup`, `replacesharing` (opens the destination with an exclusive share
   via `[System.IO.File]::Open($dest,'Open','ReadWrite','None')` before the call),
   `replacereadonly` (sets `IsReadOnly` on the destination), and `replacewronghash`.
   Each emits the full `PublicationResult` as JSON plus an observed `destSha` and
   `backupExists`.
2. Write failing tests, each run under Tier A and repeated under Tier B where the design
   marks it "A and B":
   - `test_valid_explicit_backup_replacement_succeeds` (`EGRT-T06`, A and B): `Success`
     true, destination content equals the source content, `PublicationOccurred` true,
     `BackupCreated` true.
   - `test_null_and_empty_backup_arguments_are_refused` (`EGRT-T07`, A and B): both probe
     operations fail at the mandatory-parameter contract, on every runtime, and no
     `File.Replace` call is reached.
   - `test_replacement_exception_type_and_hresult_are_surfaced` (`EGRT-T09`, B):
     sharing violation reports `System.IO.IOException` with `0x80070020` and
     `EG_LAUNCHER_REPLACE_SHARING_VIOLATION`; read-only destination reports
     `System.UnauthorizedAccessException` with `0x80070005` and
     `EG_LAUNCHER_REPLACE_ACCESS_DENIED`.
   - `test_throw_before_publication_leaves_the_preimage_intact` (`EGRT-T10`, A and B): for
     each throw-before-publication class, `PublicationOccurred` is false and `destSha`
     equals the recorded preimage. The test asserts this **only** for those classes and
     explicitly does not assert it for the wrong-hash case.
   - `test_backup_semantics_are_conditional_and_never_reaped` (`EGRT-T11`, A): after a
     verified publication the backup is present; after a post-publication verification
     failure it is present; after a throw before publication the destination is still the
     preimage and no backup is required to exist.
3. Run the module. Prove RED.
4. Implement the five functions.
5. Re-run. Prove GREEN. Confirm the Tier B cases actually executed rather than skipped by
   asserting `DESKTOP_PS is not None` on this development host, or by recording the skip
   reason if the boundary interpreter is genuinely absent.
6. Regression: full project suite.
7. Commit: `Add the explicit-backup atomic replacement primitive`.

---

## Task 6 - `Invoke-PublishToAbsentDestination`

The clean-first-install path (design section 7.4).

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
function Invoke-EgNoReplaceMove {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath
    )
    # Windows MoveFileExW WITHOUT MOVEFILE_REPLACE_EXISTING and WITH
    # MOVEFILE_WRITE_THROUGH (0x00000008), reached through Add-Type P/Invoke.
    # Mirrors scripts/member_expiry_capability_probe_lib.ps1 and the application's
    # publish_no_replace in energygrid_bill_downloader/publication.py.
    # Returns [pscustomobject]@{ Ok = [bool]; LastError = [int] }.
}

function Invoke-PublishToAbsentDestination {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedSha256
    )
    # Returns PublicationResult with PreimageState 'Absent', PreimageSha256 '',
    # BackupCreated $false, BackupRetained $false. Never calls File.Replace.
}
```

Required body sequence, in this order:

1. Confirm the destination is absent. A present destination returns
   `EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT` with `PublicationOccurred`
   `$false` and publishes nothing.
2. Confirm the staging source exists, parses cleanly when its extension is `.ps1`, and
   hashes to `$ExpectedSha256` before the move begins.
3. Move with `Invoke-EgNoReplaceMove`. A failure where the destination now exists returns
   `EG_LAUNCHER_PUBLISH_RACE_LOST`; the primitive never resolves a race by replacing.
4. Re-hash the destination and compare to `$ExpectedSha256`. A mismatch returns
   `EG_LAUNCHER_PUBLISH_POSTIMAGE_MISMATCH` with `PublicationOccurred` `$true`.
5. `PublicationOccurred` is `$true` whenever the move succeeded.

The `Add-Type` P/Invoke block is emitted once at library load into a uniquely named type
so a second dot-source in the same session does not throw a duplicate-type error. It is a
type definition only, performs no filesystem action at load, and therefore does not
violate the pure-library rule in `EGRT-I01`.

### Steps

1. Extend the probe with operations `publishabsent`, `publishpresent`, and
   `publishracelost` (the race case creates the destination between the absence check and
   the move by pre-seeding it inside the probe immediately before invoking the move
   helper directly).
2. Write failing tests:
   - `test_publish_to_absent_destination_succeeds_and_verifies` (Tier A and B): `Success`
     true, `PreimageState` `Absent`, `PreimageSha256` empty, `BackupRetained` false.
   - `test_unexpectedly_present_destination_fails_rather_than_overwriting` (`EGRT-T50`,
     Tier A): `Success` false, `SupportRef`
     `EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT`, and the pre-existing
     destination content is byte-identical afterwards.
   - `test_destination_appearing_mid_publication_fails_rather_than_overwriting`
     (`EGRT-T50`, Tier A): `SupportRef` `EG_LAUNCHER_PUBLISH_RACE_LOST` and the occupying
     content survives unchanged.
3. Run the module. Prove RED.
4. Implement both functions.
5. Re-run. Prove GREEN.
6. Regression: full project suite, plus a re-run of Task 5's tests to confirm the two
   primitives do not interfere.
7. Commit: `Add the publish-to-absent-destination primitive`.

---

## Task 7 - Null and empty backup-argument static guard

### Files

- Modify: `energygrid-bill-downloader/tests/test_runtime_launcher.py`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

A scratch AST inspector, `BACKUP_GUARD_INSPECTOR`, that for a given runtime file emits:

```text
[pscustomobject]@{
    parseErrors        = [int]
    replaceCallCount   = [int]   # InvokeMemberExpressionAst where Member.Value -eq 'Replace'
                                 # and Expression.Extent.Text matches '\[System\.IO\.File\]'
    literalNullThird   = [int]   # third argument is VariableExpressionAst named 'null'
    literalEmptyThird  = [int]   # third argument is a StringConstantExpressionAst of ''
    unprovenThird      = [int]   # third argument is neither a parameter declared
                                 # [Parameter(Mandatory)][ValidateNotNullOrEmpty()]
                                 # nor a variable assigned from such a parameter
} | ConvertTo-Json -Compress
```

### Steps

1. Write the failing Tier C test
   `test_no_committed_file_replace_call_site_passes_a_null_or_unproven_backup`
   (`EGRT-T08`): across every committed runtime `.ps1`, assert `parseErrors == 0`,
   `replaceCallCount == 1` (the single call site inside `Invoke-AtomicFileReplace`),
   `literalNullThird == 0`, `literalEmptyThird == 0`, and `unprovenThird == 0`.
2. Run the module. Prove RED if the inspector or the assertion is not yet present; if the
   Task 5 implementation already satisfies it, deliberately introduce the failure by
   temporarily adding a second call site passing `$null` in a scratch copy of the library
   and confirm the inspector reports it, then discard the scratch copy. The guard must be
   proven to fail on the defect it exists to catch, not merely to pass.
3. Add the inspector and the assertion.
4. Re-run. Prove GREEN.
5. Regression: full project suite.
6. Commit: `Guard against null and unproven File.Replace backup arguments`.

---

## Task 8 - Launcher-root Class A, B, and C classification

Design section 6.7. The exact reserved-name syntax and the canonical lowercase GUID rule
are preserved verbatim from the accepted design.

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
$script:EgDeployedPackageMemberNames = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
$script:EgResidueKinds               = @('staging', 'backup', 'rollback')
$script:EgResiduePrefix              = '.eglauncher-'
$script:EgCanonicalGuidPattern       = '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

function Get-EgDeployedPackageMemberNames {
    [CmdletBinding()]
    param()
    # Returns the exact three fixed names, enumerated explicitly, never derived from a
    # directory listing (design 6.2, asserted by EGRT-T47).
}

function Test-EgResidueName {
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Name)
    # Returns [pscustomobject]@{ IsResidue = [bool]; Kind = [string]; Member = [string]; OperationId = [string] }
    #
    # Parse rules, ALL of which must hold:
    #   1. $Name starts with the literal reserved prefix '.eglauncher-'.
    #   2. The remainder splits on the two-hyphen delimiter '--' into EXACTLY three fields.
    #   3. Field 1 is exactly one of $script:EgResidueKinds.
    #   4. Field 2 is exactly one of $script:EgDeployedPackageMemberNames.
    #   5. Field 3 matches $script:EgCanonicalGuidPattern (canonical LOWERCASE GUID).
    # Any failure yields IsResidue $false. A generic *.bak or *.tmp rule is NEVER used.
}

function New-EgResidueName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('staging', 'backup', 'rollback')][string]$Kind,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Member,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')][string]$OperationId
    )
    # Returns the reserved name. The installer's ONLY sanctioned residue-name source.
}

function Get-EgLauncherRootClassification {
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath)
    # Returns:
    # [pscustomobject]@{
    #     ClassA            = [string[]]  # exact member names found
    #     ClassB            = [string[]]  # valid residue names found
    #     ClassC            = [string[]]  # every other entry, files AND directories
    #     MissingMembers    = [string[]]  # Class A names not present
    #     Pass              = [bool]      # ClassC empty AND MissingMembers empty
    #     SupportRef        = [string]    # EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY when ClassC
    #                                     # is non-empty, else
    #                                     # EG_LAUNCHER_PACKAGE_MEMBER_MISSING when a
    #                                     # member is missing, else ''
    # }
    #
    # Enumeration is for CLASSIFICATION ONLY. This function never returns a path to be
    # dot-sourced, invoked, or imported, and never selects a substitute for a member.
}
```

Security boundary, enforced structurally: package-member paths anywhere in the codebase
are produced only by `Join-Path $LauncherRootPath <fixed name>`, never by selecting an
entry from an enumeration result.

### Steps

1. Extend the probe with operation `classify`, which builds a scratch launcher root from
   a caller-supplied JSON list of entry names and emits the classification result.
2. Write failing tests:
   - `test_valid_package_plus_a_named_backup_residue_classifies_and_passes` (`EGRT-T51`):
     three members plus
     `.eglauncher-backup--launcher.ps1--3f2504e0-4f89-11d3-9a0c-0305e82c3301` yields
     `Pass` true, one Class B entry, no Class C.
   - `test_valid_package_plus_staging_or_rollback_residue_passes_and_never_substitutes`
     (`EGRT-T52`): staging and rollback residue each classify Class B, `ClassA` still
     lists exactly the three fixed names, and residue never appears in `ClassA`.
   - `test_an_arbitrary_extra_ps1_fails_closed` (`EGRT-T53`): `launcher-old.ps1` and a
     differently named library copy each produce a non-empty `ClassC` and `Pass` false
     with `EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY`.
   - `test_generic_bak_or_tmp_entries_fail_closed` (`EGRT-T54`): `launcher.ps1.bak` and
     `staging.tmp` are Class C.
   - `test_each_residue_name_malformation_fails_closed_independently` (`EGRT-T55`): five
     separate sub-cases, one per malformation, each asserted alone:
     wrong prefix (`.eg-launcher-backup--launcher.ps1--<guid>`), wrong field count
     (`.eglauncher-backup--launcher.ps1`), unknown kind
     (`.eglauncher-archive--launcher.ps1--<guid>`), unrecognised member
     (`.eglauncher-backup--notamember.ps1--<guid>`), and non-canonical GUID
     (uppercase-hex and braced forms).
   - `test_an_unexpected_directory_fails_closed` (`EGRT-T53` support): a subdirectory in
     the launcher root is Class C, because the locked architecture requires none.
3. Run the module. Prove RED.
4. Implement the four functions and the four script-scope constants.
5. Re-run. Prove GREEN on all six.
6. Regression: full project suite.
7. Commit: `Add deterministic launcher-root entry classification`.

---

## Task 9 - Installation manifest shape and comparison

Design section 6.4.

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
$script:EgManifestFileName    = 'installation_manifest.json'
$script:EgManifestSchema      = 'eg_launcher_installation_manifest/v1'
$script:EgManifestMemberNames = @('launcher.ps1', 'launcher_lib.ps1')

function New-EgInstallationManifestObject {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$MemberEntries,   # array of @{ name; sha256; byte_length }
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$AdmissionCommit
    )
    # Returns [ordered]@{ schema_version; admission_commit; members } with members sorted
    # by name ascending. No date-shaped field, so 5.1 and 7 parse identically.
}

function ConvertTo-EgManifestJson {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$ManifestObject)
    # ConvertTo-Json -Depth 8 over the ordered dictionary. Deterministic byte output for
    # identical input, which is what makes hash idempotency (EGRT-T16) decidable.
}

function Test-EgInstallationManifestShape {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$ManifestObject)
    # Returns CheckResult. Fails with EG_LAUNCHER_MANIFEST_MISMATCH when:
    #   schema_version is not $script:EgManifestSchema;
    #   admission_commit is not 40 lowercase hex characters;
    #   the members name set is not exactly $script:EgManifestMemberNames;
    #   any sha256 is not 64 lowercase hex characters;
    #   any byte_length is not a non-negative integer.
}

function Compare-EgInstalledPackageToManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath
    )
    # Reads the manifest from the launcher root and compares it to the two installed
    # executable members. Returns CheckResult.
    #
    # Completeness is evaluated over the DEPLOYED PACKAGE-MEMBER DOMAIN ONLY, never over
    # every file in the launcher root (design 6.4). Residue is excluded by design and its
    # absence from the manifest is NOT a completeness failure.
    #
    # Support references:
    #   manifest file absent           -> EG_LAUNCHER_MANIFEST_MISSING
    #   manifest unparsable JSON       -> EG_LAUNCHER_MANIFEST_UNPARSABLE
    #   shape invalid                  -> EG_LAUNCHER_MANIFEST_MISMATCH
    #   hash or byte-length mismatch   -> EG_LAUNCHER_MANIFEST_MISMATCH
    #   manifest entry with no member  -> EG_LAUNCHER_MANIFEST_MISMATCH
    #   member with no manifest entry  -> EG_LAUNCHER_MANIFEST_MISMATCH
}
```

### Steps

1. Extend the probe with operation `manifest`, which builds a scratch launcher root with
   caller-controlled member bytes and manifest content and emits the comparison
   `CheckResult`.
2. Write failing tests:
   - `test_manifest_matching_the_installed_members_passes` (`EGRT-T46`).
   - `test_missing_manifest_is_terminal` (support ref `EG_LAUNCHER_MANIFEST_MISSING`).
   - `test_unparsable_manifest_is_terminal` (`EG_LAUNCHER_MANIFEST_UNPARSABLE`).
   - `test_hash_or_length_mismatch_is_terminal` (`EG_LAUNCHER_MANIFEST_MISMATCH`).
   - `test_manifest_entry_without_a_member_is_terminal`.
   - `test_member_without_a_manifest_entry_is_terminal`.
   - `test_class_b_residue_does_not_break_manifest_completeness` (`EGRT-T51` and
     `EGRT-T52` support): a launcher root carrying valid residue still passes the
     comparison, proving completeness is scoped to the package domain.
3. Run the module. Prove RED.
4. Implement the four functions and three constants.
5. Re-run. Prove GREEN.
6. Regression: full project suite, including Task 8's classification tests, to confirm the
   two concerns stay separate.
7. Commit: `Add the installation manifest shape and comparison contract`.

---

## Task 10 - Installer transaction Phases 1 to 4

Design sections 6.1, 6.2, 6.3.

### Files

- Create: `energygrid-bill-downloader/runtime/install_or_update_launcher.ps1`
- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

Entry script parameter surface, exactly design section 6.1 and nothing more:

```powershell
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$AdmissionCommit,
    [switch]$ValidateOnly,
    [string]$LogRoot,
    [string]$RunId
)
```

There is no credential, browser-cache, scheduler, ACL-mutation, cleanup, or recovery
parameter, and none may be added.

Library additions:

```powershell
function Get-EgDeployableSourceSet {
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RuntimeSourceDirectory)
    # Returns an ORDERED array of [pscustomobject]@{ Name; SourcePath } in the fixed
    # publication order of DD-10: launcher_lib.ps1 first, then launcher.ps1.
    #
    # The set is ENUMERATED EXPLICITLY from $script:EgManifestMemberNames, never derived
    # from a directory listing, so a file later added to runtime/ cannot become deployable
    # by accident (design 6.2, asserted by EGRT-T47).
}

function Get-EgDestinationPreimage {
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath)
    # Returns [pscustomobject]@{ PreimageState; PreimageSha256; ByteLength }
    # PreimageState is 'Existing' or 'Absent'. Established BEFORE anything is written.
}

function New-EgTransactionState {
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')][string]$OperationId)
    # Returns a mutable record: OperationId plus an ordered list of member entries, each
    # [pscustomobject]@{ Name; DestinationPath; PreimageState; PreimageSha256;
    #                    StagingPath; BackupPath; PublicationOccurred; BackupCreated }
    # This record is the transaction state that Phase 4 and rollback both depend on.
}
```

Phase sequence in the entry script, exactly design section 6.2:

- **Phase 1 (no destination mutation whatsoever).** Verify `-AdmissionCommit` shape and
  that the source checkout resolves; resolve the deployable set; parse-check and SHA-256
  every source file; classify every destination as `Existing` or `Absent`; if every
  deployed member already matches its source hash and the manifest already agrees, emit
  `ALREADY_CURRENT`, mutate nothing, exit `0`; write and fully verify every staging file
  for all changed members before publishing any of them; construct the candidate manifest;
  validate its shape. Any Phase 1 failure exits `71` with zero destination mutation.
- **Phase 2.** For each changed executable member in `DD-10` order: `Existing` publishes
  through `Invoke-AtomicFileReplace` with an explicit same-directory backup path produced
  by `New-EgResidueName -Kind backup`; `Absent` publishes through
  `Invoke-PublishToAbsentDestination`. Verify the destination hash immediately after each
  publication. **No backup is deleted in this phase.** Record preimage state, preimage
  hash, backup path, `PublicationOccurred`, and `BackupCreated` for every touched
  destination.
- **Phase 3.** Publish the manifest only after every executable member has individually
  verified, through the same two primitives selected by its own preimage state, retaining
  its backup exactly like any other member. Re-read the published manifest from disk and
  verify the exact expected entry set, hashes, byte lengths, admitted commit, and no extra
  or missing deployed member. Then re-verify every deployed executable file against the
  manifest just read back.
- **Phase 4.** Accept only when every executable member verified, the manifest published
  and read back correctly, and the Phase 3 step 4 re-verification passed. Only then may
  retained backups be reaped (Task 12).

Staging files are created in the destination directory only, never in a shared temporary
directory, and are named exclusively by `New-EgResidueName -Kind staging`.

### Steps

1. Write failing tests:
   - `test_clean_first_install_publishes_all_three_members_through_the_absent_path`
     (`EGRT-T41`, Tier A and B): a bare scratch launcher root yields `status`
     `INSTALLED`, all three members present, every member's `PreimageState` recorded
     `Absent`, no `File.Replace` invoked, and the manifest verifies against the installed
     bytes.
   - `test_second_install_against_a_current_destination_reports_already_current`
     (`EGRT-T16`, Tier A): `status` `ALREADY_CURRENT`, and the destination file
     modification times and hashes are identical before and after, proving idempotency is
     by hash and not by timestamp.
   - `test_existing_member_backups_survive_every_per_file_verification` (`EGRT-T45`, Tier
     A): during an update over an existing installation, after Phase 2 completes and
     before Phase 4 acceptance, a backup file exists for every `Existing` member.
   - `test_only_the_enumerated_deployed_set_reaches_the_launcher_root` (`EGRT-T47`, Tier A
     and C): after a successful install, `install_or_update_launcher.ps1`,
     `launcher.settings.example.json`, and `runtime/README.md` are absent from the
     launcher root; and the static half asserts `Get-EgDeployableSourceSet` derives its
     set from the explicit constant rather than from `Get-ChildItem`, by AST-asserting
     that the function body contains no `Get-ChildItem` command.
   - `test_installed_bytes_and_manifest_are_mutually_consistent_after_commit`
     (`EGRT-T46`, Tier A): no extra or missing member, hashes agree in both directions.
2. Run the module. Prove RED: the installer script does not exist.
3. Implement `Get-EgDeployableSourceSet`, `Get-EgDestinationPreimage`, and
   `New-EgTransactionState` in the library, then the four phases in the entry script.
4. Re-run. Prove GREEN.
5. Regression: full project suite, plus Task 8 and Task 9 tests, because the installer is
   the first consumer of both.
6. Commit: `Add the installer package transaction`.

---

## Task 11 - Installer-owned package rollback

Design sections 6.5 and 7.2.1. The installer transaction is the **only** rollback
authority; neither section 7 primitive restores anything.

### Files

- Modify: `energygrid-bill-downloader/runtime/install_or_update_launcher.ps1`
- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
function Get-EgTouchedSet {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$TransactionState)
    # Returns the member entries whose PublicationOccurred is $true, in REVERSE
    # publication order. Membership is decided by PublicationOccurred, NOT by which
    # member failed (design 6.5).
}

function Invoke-EgPackageRollback {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$TransactionState)
    # Returns [pscustomobject]@{ Verified = [bool]; SupportRef = [string] }
    #
    # Walks Get-EgTouchedSet in reverse publication order:
    #   PreimageState 'Existing':
    #     restore the exact retained backup over the destination using the
    #     explicit-backup replacement semantics of section 7.2, then re-hash the restored
    #     destination and confirm it equals the recorded PreimageSha256. A restoration
    #     that cannot be positively verified is NOT treated as successful.
    #     PublicationOccurred true with no backup on disk yields
    #     EG_LAUNCHER_REPLACE_PREIMAGE_UNRECOVERABLE.
    #   PreimageState 'Absent':
    #     remove exactly the destination THIS transaction created, then positively confirm
    #     it is absent again. Never remove a file this transaction did not create, and
    #     never remove a destination whose current content no longer matches what was
    #     published.
    #
    # After the walk, re-verify the ENTIRE installed package equals its pre-transaction
    # state: every previously present member restored to its preimage hash, and every
    # member that was absent beforehand absent again.
    #
    # Verified $true  -> caller exits 72 with status FAILED_ROLLED_BACK.
    # Verified $false -> caller exits 73 with EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE and
    #                    status FAILED_ROLLBACK_INCOMPLETE; every artefact is retained and
    #                    no further repair is attempted.
}
```

Case handling for the member whose failure ended the transaction:

- Case A (`PublicationOccurred` false, threw before publication): it did not advance. Do
  not attempt to restore it. Verify it still hashes to its recorded preimage, then roll
  back only the earlier members that did advance.
- Case B (`PublicationOccurred` true, returned but postimage verification failed): it
  advanced. It is the most recent entry in the touched set, so it is restored first, then
  the earlier members in reverse order.

### Steps

1. Extend the probe and the test module with fault injection that uses no production
   test-only branch. The three injection mechanisms, each a legitimate caller-side or
   host-side condition:
   - **Wrong expected hash** for a specific member, driven by a scratch installer harness
     that dot-sources the library and calls the primitives with a deliberately wrong
     `-ExpectedSha256`. This is a legitimate caller error and is what produces a genuine
     Case B (design section 12.3).
   - **Read-only destination** on a later member, producing a genuine Case A mid-package.
   - **Manifest destination held with an exclusive share**, producing a Phase 3 failure.
2. Write failing tests:
   - `test_a_returned_replace_with_a_wrong_expected_hash_rolls_back_and_exits_72`
     (`EGRT-T12`, Tier A): `PublicationOccurred` true, backup retained, the primitive
     performed no self-rollback, the installer restored the destination to the exact
     recorded preimage byte for byte, and the reported exit code is `72`.
   - `test_first_install_failure_returns_created_destinations_to_absent` (`EGRT-T42`,
     Tier A): every newly created owned destination is removed and positively confirmed
     absent afterwards; none is kept behind for diagnosis.
   - `test_a_later_member_failure_restores_every_earlier_member_byte_for_byte`
     (`EGRT-T43`, Tier A): the earlier member's post-rollback bytes equal its
     pre-transaction bytes exactly.
   - `test_manifest_failure_rolls_back_executables_and_restores_the_prior_manifest`
     (`EGRT-T44`, Tier A): with a pre-existing manifest, it is restored to its preimage;
     with no pre-existing manifest, the destination is confirmed absent again.
   - `test_the_four_forbidden_end_states_are_unreachable` (`EGRT-T42` to `EGRT-T44`
     support, Tier A): after every failure case above, assert none of the four states
     holds - new launcher with old library, old launcher with new library, executables
     disagreeing with the manifest, or a manifest describing bytes that are not installed.
   - `test_an_unrecoverable_advanced_member_exits_73_and_retains_everything` (Tier A):
     when `PublicationOccurred` is true and the backup is deleted out from under the
     transaction, the result is `EG_LAUNCHER_REPLACE_PREIMAGE_UNRECOVERABLE`, exit `73`,
     and no further repair is attempted.
3. Run the module. Prove RED.
4. Implement `Get-EgTouchedSet` and `Invoke-EgPackageRollback`, and wire every
   pre-acceptance failure path in the installer to them.
5. Re-run. Prove GREEN on all six.
6. Regression: full project suite, plus Task 5, 6, and 10 tests, to confirm the primitives
   still never roll back themselves.
7. Commit: `Add installer-owned reverse-order package rollback`.

---

## Task 12 - Post-acceptance backup cleanup and visible residue status

Design section 6.6. This is the single sanctioned narrow exception to the fail-closed
default, and it is bounded exactly as written.

### Files

- Modify: `energygrid-bill-downloader/runtime/install_or_update_launcher.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
function Invoke-EgPostAcceptanceBackupCleanup {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$TransactionState)
    # Returns [pscustomobject]@{ BackupsRemaining = [int]; SupportRef = [string] }
    #
    # Reaps ONLY backups THIS transaction created, identified by the exact
    # New-EgResidueName value recorded in the transaction state. A file this transaction
    # did not create is never deleted.
    #
    # When a delete fails, the accepted installation is KEPT. The undeleted backup is
    # retained as inert residue, SupportRef is
    # EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE, BackupsRemaining names how many
    # remain, and the caller exits 0 because the installation genuinely succeeded.
    #
    # Rolling back a fully accepted installation because a redundant backup could not be
    # deleted is explicitly NOT done.
}
```

### Steps

1. Write failing tests:
   - `test_backups_are_reaped_only_after_whole_package_acceptance` (Tier A): a successful
     update leaves zero Class B backup entries in the launcher root, and the reap happens
     after, never before, the Phase 3 re-verification.
   - `test_a_failed_cleanup_keeps_the_accepted_install_and_reports_it_visibly` (Tier A):
     with a backup held by an exclusive share, `status` is `INSTALLED`, `support_ref` is
     `EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE`, `backups_remaining` is `1`, the exit
     code is `0`, and every installed member still verifies against the manifest.
   - `test_retained_cleanup_residue_still_satisfies_the_class_b_contract` (Tier A): the
     retained name parses as valid Class B, so the next launcher preflight passes rather
     than failing closed on a file the installer itself deliberately left.
   - `test_cleanup_never_deletes_a_file_this_transaction_did_not_create` (Tier A): a
     foreign valid-looking residue name from a different operation identifier survives
     the reap untouched.
2. Run the module. Prove RED.
3. Implement `Invoke-EgPostAcceptanceBackupCleanup` and call it only from the Phase 4
   commit step.
4. Re-run. Prove GREEN.
5. Regression: full project suite, plus Task 8 (classification) and Task 11 (rollback), to
   confirm cleanup and rollback authority stay separate.
6. Commit: `Reap installer backups only after whole-package acceptance`.

---

## Task 13 - DPAPI credential import, injection, restoration, and cleanup

Design section 9.2. Git owns the behaviour; the artefact, its path, its bound identity,
and every value it yields stay private.

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
$script:EgCredentialVariableNames = @('ENERGYGRID_USERNAME', 'ENERGYGRID_PASSWORD')

function Import-EgLauncherCredential {
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CredentialPath)
    # Returns [pscustomobject]@{ Success = [bool]; SupportRef = [string]; Credential = [pscredential] }
    #
    #   path absent or unreadable        -> EG_LAUNCHER_CREDENTIAL_ARTEFACT_MISSING
    #   Import-Clixml throws, or the
    #   imported object is not a
    #   [System.Management.Automation.PSCredential]
    #                                    -> EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED
    #
    # Uses Import-Clixml ONLY. Passes NO -Key and NO -SecureKey argument, because a keyed
    # export would silently convert the artefact into something portable between users and
    # void the DPAPI CurrentUser binding (design 12.4). EGRT-T49 guards this statically.
    #
    # On failure Credential is $null. No exception message text is recorded anywhere.
}

function Test-EgCredentialViability {
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowNull()]$Credential)
    # Returns [pscustomobject]@{
    #     CredentialImportOk = [bool]
    #     UsernameNonEmpty   = [bool]
    #     PasswordNonEmpty   = [bool]
    #     SupportRef         = [string]   # EG_LAUNCHER_CREDENTIAL_INCOMPLETE when either
    #                                     # value is empty, else ''
    # }
    #
    # Password non-emptiness is derived by converting the SecureString through
    # [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR / PtrToStringBSTR
    # inside a try/finally that ALWAYS calls ZeroFreeBSTR, testing only .Length -gt 0, and
    # letting the plaintext temporary leave scope immediately. The length itself is never
    # recorded, returned, logged, or emitted in any form - only the boolean.
}

function Set-EgProcessEnvironmentVariable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name,
        [Parameter(Mandatory)][AllowEmptyString()][AllowNull()][string]$Value
    )
    # [System.Environment]::SetEnvironmentVariable($Name, $Value, 'Process') ONLY.
    # A $null Value REMOVES the variable. The 'User' and 'Machine' scopes are never used
    # anywhere in the committed runtime (EGRT-T25).
}
```

Entry-script injection contract in `launcher.ps1`:

- Immediately before the child starts, capture the prior process-scope state of
  `ENERGYGRID_USERNAME` and `ENERGYGRID_PASSWORD` with
  `Get-EgProcessEnvironmentSnapshot`, then set both at **process scope only**.
- In a `finally`-equivalent path reached whether the child succeeded, failed, or never
  started, call `Restore-EgProcessEnvironmentSnapshot`. A variable absent beforehand is
  removed, not set to an empty string. A failed restoration is terminal with
  `EG_LAUNCHER_CREDENTIAL_RESTORE_FAILED`.
- The `PSCredential` reference is nulled as soon as the child no longer needs it. No
  credential-derived reference is retained past the `finally` block.
- `Dispose` is **never** called on the `PSCredential`, because
  `System.Management.Automation.PSCredential` does not implement `IDisposable` and the
  call would throw at runtime.
- No credential value, length, prefix, suffix, or hash reaches stdout, stderr, a result
  object, the JSONL event, or any exception surface.

### Steps

1. Add a synthetic-credential fixture helper to the probe: operation `credmake` builds a
   `PSCredential` from per-run throwaway values generated inside the runner temporary
   directory, exports it with `Export-Clixml` under the same user-bound mechanism the
   launcher imports with, and writes it to a scratch path. The values are never real
   credentials and never leave the runner's temporary directory.
2. Write failing tests:
   - `test_a_synthetic_same_user_dpapi_credential_imports_successfully` (`EGRT-T21`, Tier
     A and B): `Success` true, `CredentialImportOk` true, and neither value appears in
     stdout or stderr.
   - `test_a_corrupted_or_unreadable_artefact_fails_closed_before_the_child_stub`
     (`EGRT-T22`, Tier A and B): three sub-cases - absent path, truncated CLIXML, and a
     CLIXML holding a non-`PSCredential` object - each yielding the correct support
     reference and zero child-stub executions.
   - `test_the_child_stub_observes_both_credential_variables_only_during_execution`
     (`EGRT-T23`, Tier A): a scratch child stub writes the two variables' presence to a
     scratch JSON file; both are present during, and neither is present in the parent
     process before or after.
   - `test_both_credential_variables_are_restored_exactly_afterwards` (`EGRT-T24`, Tier
     A): two sub-cases - previously absent stays absent (not present-and-empty), and
     previously present is restored to its exact original value.
   - `test_no_user_or_machine_scope_credential_variable_is_ever_written` (`EGRT-T25`, Tier
     A and C): the dynamic half reads the `User` and `Machine` scopes for both names
     before and after a successful run and a failing run and asserts both stay absent; the
     static half AST-asserts that no committed runtime file passes `'User'` or `'Machine'`
     to `SetEnvironmentVariable`.
   - `test_no_credential_value_reaches_any_output_surface` (`EGRT-T26`, Tier A and C): on
     both success and failure, assert the synthetic username and password substrings
     appear in neither stdout, stderr, the emitted JSON, nor the JSONL event; the static
     half asserts no committed runtime file writes a credential variable's value into a
     result object or a log field.
   - `test_no_committed_cleanup_path_disposes_a_pscredential` (`EGRT-T49`, Tier C):
     AST-assert that no `InvokeMemberExpressionAst` with `Member.Value -eq 'Dispose'` has
     an expression that resolves to a variable assigned from `Import-EgLauncherCredential`
     or typed `[pscredential]`, and text-assert the absence of the `.Dispose()` call on
     any variable named with the credential prefix.
   - `test_the_committed_import_path_uses_the_user_bound_mechanism_only` (`EGRT-T49`
     support, Tier C): AST-assert that every `Import-Clixml` call site passes neither
     `-Key` nor `-SecureKey`.
3. Run the module. Prove RED.
4. Implement the three functions and the constant.
5. Re-run. Prove GREEN on all eight.
6. Regression: full project suite.
7. Commit: `Add DPAPI credential import, injection, and restoration`.

### Testability boundary, recorded honestly

Cross-user and cross-machine rejection are **not** testable in hosted CI: proving the
artefact fails to import under a different Windows user needs a second interactive
account, which a hosted runner does not provide, and creating one is a system-settings
change this plan has no authority for. That gap is covered two ways, neither of which
pretends to be the missing test. First, DPAPI `CurrentUser` protection supplies the
property by construction rather than through code the launcher could get wrong. Second,
the `-Key`/`-SecureKey` static guard above regresses the part that is actually ours. No
task may claim the missing test exists.

---

## Task 14 - Private Playwright browser-cache binding

Design section 9.3. Positive binding, no ambient fallback, and the launcher never writes
into the cache.

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
$script:EgBrowserCacheVariableName = 'PLAYWRIGHT_BROWSERS_PATH'

function Test-EgBrowserCacheReady {
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$BrowserCachePath)
    # Returns CheckResult with the single check name 'browser_cache_ready'.
    #
    #   path does not resolve to an existing directory
    #                                  -> EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED
    #   resolves, but no provisioned Chromium is present
    #                                  -> EG_LAUNCHER_BROWSER_CACHE_NOT_READY
    #
    # READINESS RULE (positive, not merely directory existence): the path contains at
    # least one immediate child directory whose name matches '^chromium(-|_).+' AND that
    # child contains a file named 'chrome.exe' or 'headless_shell.exe' at any depth. The
    # probe is READ-ONLY: Get-ChildItem only. Nothing is created, downloaded, or repaired.
}
```

Entry-script binding contract in `launcher.ps1`:

- Capture the prior process-scope value of `PLAYWRIGHT_BROWSERS_PATH` in the **same**
  snapshot used for the credential variables, so restoration is one operation on one
  `finally`-equivalent path.
- Set it explicitly to `-BrowserCachePath`. An ambient value pointing elsewhere is
  overridden, never honoured. If the binding cannot be established, that is terminal with
  `EG_LAUNCHER_BROWSER_CACHE_BIND_FAILED`.
- Restore the prior process-scope value exactly afterwards, with absent restored as
  absent.
- There is no fallback. A missing, unreadable, or unprovisioned cache fails the run and
  never degrades to the default location.

### Steps

1. Extend the probe with operation `browsercache`, and add a scratch cache builder in the
   test module that creates `<scratch>/chromium-1234/chrome-win/chrome.exe` as an empty
   file to represent a provisioned cache without downloading anything.
2. Write failing tests:
   - `test_an_explicit_private_cache_path_reaches_the_child_stub` (`EGRT-T27`, Tier A):
     the child stub records the variable's value and it equals the supplied scratch path.
   - `test_a_conflicting_ambient_value_cannot_override_the_supplied_binding` (`EGRT-T28`,
     Tier A): with `PLAYWRIGHT_BROWSERS_PATH` pre-set to a second scratch path, the child
     stub still observes the supplied path.
   - `test_a_missing_unreadable_or_unprovisioned_cache_fails_closed` (`EGRT-T29`, Tier
     A): three sub-cases - absent path (`EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED`), an empty
     existing directory, and a directory holding a `chromium-1234` child with no browser
     executable (both `EG_LAUNCHER_BROWSER_CACHE_NOT_READY`) - each with zero child-stub
     executions and no fallback to the default location.
   - `test_the_prior_browser_cache_value_is_restored_exactly` (`EGRT-T30`, Tier A): two
     sub-cases - previously absent stays absent, previously present is restored exactly.
   - `test_the_launcher_never_writes_into_the_browser_cache` (Tier A and C): a recursive
     snapshot of the scratch cache is identical before and after a full run; the static
     half AST-asserts no committed runtime file invokes `playwright install`,
     `New-Item`, `Copy-Item`, `Remove-Item`, or `Invoke-WebRequest` against a path derived
     from `-BrowserCachePath`.
3. Run the module. Prove RED.
4. Implement `Test-EgBrowserCacheReady` and the constant, and extend the entry-script
   snapshot to cover the third variable name.
5. Re-run. Prove GREEN.
6. Regression: full project suite, plus Task 13, to confirm all three variables restore on
   the same path.
7. Commit: `Bind and validate the private Playwright browser cache`.

---

## Task 15 - Path-scoped governed source integrity

Design section 10.3, reconciling with `DL-XB-141-SCHEDULER-005`. The unit of protection is
the EnergyGrid runtime-critical surface, never the whole monorepo.

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
$script:EgGovernedSourcePaths = @(
    'energygrid-bill-downloader/energygrid_bill_downloader',
    'energygrid-bill-downloader/requirements.txt'
)
$script:EgGovernedExecutableSurface = 'energygrid-bill-downloader/energygrid_bill_downloader'
$script:EgAnyBranchSentinel = 'ANY_BRANCH'

function Get-EgGovernedSourcePaths {
    [CmdletBinding()]
    param()
    # Returns the exact two paths above. runtime/, tests/, docs/, task-scheduler/, and
    # config/energygrid.example.json are DELIBERATELY EXCLUDED (design 10.3).
}

function Test-EgGovernedSourceIntegrity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRootPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedBranch
    )
    # Returns CheckResult whose Checks map carries these eight stable names in order:
    #   source_repository_binding      rev-parse --show-toplevel equals $CheckoutRootPath
    #                                    -> EG_LAUNCHER_SOURCE_BINDING_FAILED
    #   source_branch_binding          unless $ExpectedBranch is ANY_BRANCH, symbolic-ref
    #                                  --short HEAD equals it
    #                                    -> EG_LAUNCHER_SOURCE_BRANCH_MISMATCH
    #   source_paths_exist             each governed path exists on disk
    #                                    -> EG_LAUNCHER_SOURCE_PATH_MISSING
    #   source_paths_tracked           ls-files -- <path> is non-empty for each
    #                                    -> EG_LAUNCHER_SOURCE_PATH_UNTRACKED
    #   source_no_staged_modification  diff --cached --name-only -- <paths> is empty
    #                                    -> EG_LAUNCHER_SOURCE_STAGED_MODIFICATION
    #   source_no_unstaged_modification diff --name-only -- <paths> is empty
    #                                    -> EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION
    #   source_no_tracked_deletion     diff --name-only --diff-filter=D -- <paths> is
    #                                  empty, and the staged equivalent is empty
    #                                    -> EG_LAUNCHER_SOURCE_DELETED
    #   source_no_untracked_overlay    per DD-11
    #                                    -> EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY
    #
    # EVERY check is scoped with an explicit Git pathspec. A dirty file anywhere else in
    # the monorepo does not fail the run, and a HEAD that has moved to a newer accepted
    # commit does not fail the run so long as the governed surface is still clean and
    # tracked. Every read goes through Invoke-GovernedGit, so ambient GIT_* neutralisation
    # from Task 3 and the allowlist from Task 4 both apply.
}

function Test-EgIsSanctionedBytecodeArtefact {
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$RelativePath)
    # The ONE sanctioned overlay exception (design 10.3): a path is sanctioned when any
    # segment is exactly '__pycache__', or the path ends in '.pyc'. Every OTHER untracked
    # entry inside the governed executable surface, ignored or not, is a substitution and
    # is terminal.
}
```

### Steps

1. Add a scratch-repository builder to the test module. It creates a temporary Git
   repository containing `energygrid-bill-downloader/energygrid_bill_downloader/cli.py`,
   `energygrid-bill-downloader/requirements.txt`, an unrelated
   `scripts/unrelated_tool.py`, and a `.gitignore` that ignores `*.ignored`, commits on
   branch `main`, and returns the path. Extend the probe with operation `sourceintegrity`
   that calls `Test-EgGovernedSourceIntegrity` and emits the full `CheckResult`.
2. Write failing tests:
   - `test_a_clean_tracked_governed_surface_passes` (`EGRT-T32`).
   - `test_a_staged_modification_under_a_governed_path_fails` (`EGRT-T33`):
     `EG_LAUNCHER_SOURCE_STAGED_MODIFICATION`.
   - `test_an_unstaged_modification_under_a_governed_path_fails` (`EGRT-T34`):
     `EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION`.
   - `test_a_deletion_of_a_tracked_governed_file_fails` (`EGRT-T35`):
     `EG_LAUNCHER_SOURCE_DELETED`.
   - `test_untracked_overlay_rules_including_the_bytecode_exception` (`EGRT-T36`): four
     sub-cases - a plain untracked `overlay.py` inside the package fails; an untracked
     `overlay.ignored` inside the package that `.gitignore` hides **also** fails; a
     `__pycache__` directory does **not** fail; a stray `.pyc` does **not** fail.
   - `test_a_dirty_file_outside_the_governed_paths_does_not_fail_the_run` (`EGRT-T37`):
     modifying `scripts/unrelated_tool.py` and adding an untracked file at the repository
     root both leave `Pass` true.
   - `test_a_moved_head_with_a_clean_governed_surface_does_not_fail_the_run`
     (`EGRT-T38`): a second commit touching only `scripts/unrelated_tool.py` leaves `Pass`
     true, proving no fixed whole-repository HEAD is required.
   - `test_ambient_git_variables_cannot_redirect_the_integrity_checks` (`EGRT-T40`): with
     `GIT_DIR`, `GIT_WORK_TREE`, and `GIT_CONFIG_GLOBAL` pointing at a decoy repository
     whose governed surface is dirty, the checks still evaluate the real repository and
     pass.
   - `test_branch_binding_and_the_any_branch_sentinel` (Tier A): a mismatched
     `-ExpectedBranch` yields `EG_LAUNCHER_SOURCE_BRANCH_MISMATCH`, and the literal
     `ANY_BRANCH` disables only branch binding while every other check still runs.
3. Run the module. Prove RED.
4. Implement the three functions and the three constants.
5. Re-run. Prove GREEN on all nine.
6. Regression: full project suite, plus Tasks 2, 3, and 4, because every read here flows
   through the governed Git path.
7. Commit: `Add path-scoped governed source integrity for unattended execution`.

---

## Task 16 - Launcher entry script, preflight order, exit bands, terminal event

Design sections 5.1, 5.2, 5.3, 11.3.

### Files

- Create: `energygrid-bill-downloader/runtime/launcher.ps1`
- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

Parameter surface, exactly design section 5.1 and nothing more:

```powershell
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ConfigPath,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$PythonExe,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CredentialPath,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$BrowserCachePath,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedBranch,
    [ValidateSet('run', 'list')][string]$Command = 'run',
    [string]$LogRoot,
    [switch]$ValidateOnly,
    [string]$RunId
)
```

There is no parameter accepting a credential value, no portal parameter, no
browser-install parameter, no commit-pin parameter, and no headed switch. `-ExpectedBranch`
is mandatory with the explicit `ANY_BRANCH` sentinel, so branch binding can never be
disabled by omitting an argument.

Library additions:

```powershell
$script:EgLauncherExitCodes = [ordered]@{
    PreflightFailed          = 70
    InstallPreMutationFailed = 71
    InstallRolledBack        = 72
    InstallRollbackIncomplete = 73
}
$script:EgApplicationExitCodes = @(0, 10, 20, 64)

function Test-EgPythonVersionSupported {
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$PythonExe)
    # Runs "<PythonExe> --version" through System.Diagnostics.Process with redirected
    # streams and matches '^Python 3\.14\.' on the captured output. Read-only probe.
    # Returns CheckResult with check name 'python_version_is_3_14'.
    #   -> EG_LAUNCHER_PYTHON_VERSION_UNSUPPORTED
}

function Test-EgLauncherConfigContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ConfigPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRootPath
    )
    # Returns CheckResult with check names 'config_path_outside_checkout',
    # 'config_parses_json', and 'config_required_keys_present'.
    #
    # Required keys, presence and non-empty string only (DD-05):
    #   portal_url, account_identity, archive_root, state_path, temp_root, log_root
    # The launcher does NOT re-validate the application's own path rules; config.py
    # already enforces them and duplicating them would create two sources of truth.
    # No configuration VALUE is ever emitted, logged, or placed in a result object.
}

function Test-EgLauncherRootSecurity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRootPath
    )
    # Returns CheckResult with check names, in order:
    #   launcher_root_outside_checkout                      -> EG_LAUNCHER_ROOT_INSIDE_CHECKOUT
    #   launcher_root_not_writable_by_run_principal (DD-02) -> EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE
    #   launcher_root_write_restricted_to_install_principal (DD-03)
    #                                                       -> EG_LAUNCHER_ROOT_ACL_WRITE_NOT_RESTRICTED
    #   launcher_files_not_reparse_points                   -> EG_LAUNCHER_ROOT_REPARSE_POINT
    #   launcher_files_not_unexpectedly_readonly            -> EG_LAUNCHER_FILE_UNEXPECTEDLY_READONLY
    #
    # No principal name, SID, or path is ever emitted. Only the check name and the bounded
    # support reference reach any surface.
}

function Write-EgLauncherTerminalEvent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$LogRoot,
        [Parameter(Mandatory)][AllowEmptyString()][string]$RunId,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Phase,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Status,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SupportRef
    )
    # Appends EXACTLY ONE launcher_failed JSONL event carrying run_id, phase, status, and
    # support_ref, and nothing else. Mirrors log_terminal_failure in
    # energygrid_bill_downloader/cli.py: the event is evidence, not a result. A failure to
    # write it NEVER changes the exit code, and a failure raised before the log root is
    # resolved simply has no event. Does nothing when $LogRoot is empty or unresolvable.
}
```

Invocation contract, on success only:

- Working directory is the `energygrid-bill-downloader` directory beneath `-CheckoutRoot`.
- The child is `<PythonExe> -m energygrid_bill_downloader <Command> --config <ConfigPath>`.
- The child's exit code is propagated verbatim.
- Exactly three process-scope variables are set immediately before the child starts and
  restored on the `finally`-equivalent path: the two credential names and the browser-cache
  name. No other environment change is made.

### Steps

1. Add a child-stub builder to the test module: a scratch `.ps1` or `.cmd` that records the
   three environment variables plus its working directory to a scratch JSON file and exits
   with a caller-chosen code. The application is never invoked; only the stub is.
2. Write failing tests:
   - `test_the_launcher_exit_band_is_disjoint_from_the_application_band` (`EGRT-T19`,
     Tier C): assert `{70,71,72,73}` and `{0,10,20,64}` are disjoint as read from the
     committed constants, not from convention.
   - `test_no_test_path_invokes_the_real_application_and_no_portal_literal_is_committed`
     (`EGRT-T20`, Tier C): AST-and-text assert that no committed runtime file contains a
     portal URL literal (no `http://` or `https://` string constant), and that every test
     that starts a child does so against the scratch stub.
   - `test_a_failing_non_secret_preflight_causes_zero_credential_import_attempt`
     (`EGRT-T48`, Tier A and C): a sentinel-instrumented scratch credential path that
     records any open attempt shows zero opens when any of checks 1 to 18 fails; run once
     per failing check, including each of the four launcher-root security checks. The
     static half AST-asserts that the single `Import-EgLauncherCredential` call site in
     `launcher.ps1` lexically follows every non-secret check call site.
   - `test_a_missing_or_invalid_class_a_member_cannot_be_satisfied_by_residue`
     (`EGRT-T56`, Tier A): with `launcher_lib.ps1` absent but a
     `.eglauncher-backup--launcher_lib.ps1--<guid>` residue present, preflight fails with
     `EG_LAUNCHER_PACKAGE_MEMBER_MISSING`; repeat for `launcher.ps1` and for the manifest.
   - `test_recognised_residue_is_never_executed_imported_or_selected` (`EGRT-T57`, Tier A
     and C): the dynamic half places an executable-content residue file whose execution
     would create a marker and asserts the marker never appears; the static half
     AST-asserts that no committed runtime file dot-sources, invokes, or imports a path
     derived from a directory enumeration, and that the only dot-source in `launcher.ps1`
     is a fixed `Join-Path $PSScriptRoot 'launcher_lib.ps1'`.
   - `test_the_preflight_check_order_matches_the_committed_contract` (Tier A): the
     `checks` map key order from a `-ValidateOnly` run equals the twenty-one names in the
     Ordered preflight check names table, exactly.
   - `test_the_terminal_event_is_written_once_and_carries_no_private_data` (Tier A): a
     failing run with `-LogRoot` set appends exactly one `launcher_failed` line whose keys
     are exactly `run_id`, `phase`, `status`, and `support_ref`; a failing run without
     `-LogRoot` writes nothing and returns the same exit code; a run whose log root is
     unwritable returns the same exit code as one whose log root is writable.
   - `test_the_child_exit_code_is_propagated_verbatim` (Tier A): stub exit codes `0`,
     `10`, `20`, and `64` each reach the caller unchanged, and a preflight failure yields
     `70`.
3. Run the module. Prove RED.
4. Implement the four library functions, the two constants, and `launcher.ps1`.
5. Re-run. Prove GREEN on all eight.
6. Regression: full project suite, plus Tasks 8, 9, 13, 14, and 15, because the launcher
   is the first consumer of all five.
7. Commit: `Add the launcher entry script and ordered preflight`.

---

## Task 17 - `-ValidateOnly` on both entry scripts

Design section 8.

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher.ps1`
- Modify: `energygrid-bill-downloader/runtime/install_or_update_launcher.ps1`
- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
function ConvertTo-EgValidationJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Checks,                     # [ordered] name -> 'PASS'|'FAIL'
        [Parameter(Mandatory)][ValidateSet('PASS','FAIL')][string]$Status,
        [Parameter(Mandatory)][AllowEmptyString()][string]$SupportRef
    )
    # Emits EXACTLY ONE JSON object on standard output:
    #   { "checks": { ... }, "status": "...", "support_ref": "..." }
    # support_ref is OMITTED when Status is PASS.
    #
    # Deterministic: no timestamp, no generated identifier, no path, no environment value,
    # no credential value, no account identity, and no Git output text.
    # Built from [ordered]@{} and serialised with ConvertTo-Json -Depth 8 -Compress.
}
```

`-ValidateOnly` semantics, on both entry scripts:

- Runs every check the corresponding real path would run.
- Creates, modifies, deletes, and renames nothing: no staging file, no backup file, no log
  file, no directory, no scheduler entry, no environment change that outlives the process.
- Starts no child process other than the read-only interpreter version probe and the
  governed Git reads, and never invokes `run` or `list`.
- Launches no browser, installs or updates no browser cache, and contacts no portal.
- May import the private DPAPI credential artefact in-process to prove viability. The
  import is read-only, the imported object is discarded immediately after the three
  booleans are derived, and no value survives it.
- Records only `credential_import_ok`, `username_nonempty`, and `password_nonempty` as
  booleans. It never emits, logs, hashes, measures, or otherwise derives a reportable
  quantity from either credential value.
- Exit `0` means every check passed. A failure exits `70` and names the first failing
  check by its stable name.
- The installer's `-ValidateOnly` generates no transaction identifier (`DD-09`) and emits
  no `ALREADY_CURRENT` check (`DD-06`).

### Steps

1. Add a recursive-snapshot helper to the test module:
   `snapshot_tree(root) -> dict[str, tuple[int, float, str]]` mapping each relative path
   to its size, modification time, and SHA-256.
2. Write failing tests:
   - `test_validate_only_mutates_nothing` (`EGRT-T14`, Tier A): snapshots of the scratch
     launcher root, the config path's directory, the checkout root, the browser cache, and
     the log root are identical before and after, on both entry scripts, with no file
     created or removed.
   - `test_repeated_validation_is_byte_identical_and_idempotent` (`EGRT-T15`, Tier A): two
     consecutive `-ValidateOnly` runs on each entry script produce byte-identical standard
     output and identical filesystem snapshots.
   - `test_validate_only_launches_no_browser_and_performs_no_cache_write` (`EGRT-T31`,
     Tier A and C): the browser-cache snapshot is unchanged and no browser process was
     started; the static half asserts no committed runtime file reachable from the
     `-ValidateOnly` path invokes a browser or a cache-provisioning command.
   - `test_validate_only_emits_exactly_one_json_object_with_no_private_content` (Tier A):
     stdout parses as exactly one JSON object; its keys are exactly `checks`, `status`,
     and optionally `support_ref`; and it contains no `^[A-Za-z]:\\` substring, no `\\\\`
     substring, no environment value, and no account identity.
   - `test_validate_only_failure_exits_70_and_names_the_first_failing_check` (Tier A).
   - `test_installer_validate_only_generates_no_transaction_identifier` (Tier A and C):
     no Class B residue name appears in the output and the launcher root gains no entry;
     the static half asserts the `NewGuid` call site is not reachable from the
     `-ValidateOnly` branch.
3. Run the module. Prove RED.
4. Implement `ConvertTo-EgValidationJson` and the `-ValidateOnly` branches on both entry
   scripts.
5. Re-run. Prove GREEN.
6. Regression: full project suite.
7. Commit: `Add deterministic zero-mutation ValidateOnly to both entry scripts`.

---

## Task 18 - Bounded support-reference vocabulary and reachability

Design section 11.

### Files

- Modify: `energygrid-bill-downloader/runtime/launcher_lib.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```powershell
$script:EgLauncherSupportRefs = @( <the 48 live references from the vocabulary table> )
$script:EgLauncherRetiredSupportRefs = @()   # empty at first implementation (DD-08)

function Get-EgLauncherSupportRefs {
    [CmdletBinding()]
    param()
    # Returns the live set. The vocabulary is BOUNDED and CLOSED.
}

function Get-EgLauncherRetiredSupportRefs {
    [CmdletBinding()]
    param()
    # Retiring a reference means MOVING it here, never deleting it, so evidence written by
    # an earlier build stays readable. Mirrors RETIRED_SUPPORT_REFS in cli.py.
}

function Test-EgLauncherSupportRefLive {
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$SupportRef)
    # Returns [bool]. An unrecognised reference is NEVER emitted; callers substitute
    # EG_LAUNCHER_UNCLASSIFIED rather than leaking detail.
}
```

### Steps

1. Write failing tests:
   - `test_every_live_support_reference_is_reachable_in_committed_source` (`EGRT-T18`,
     Tier A and C): for each member of `Get-EgLauncherSupportRefs`, assert the literal
     appears at least once in a committed runtime file at a raising site, not only in the
     constant declaration itself.
   - `test_every_retired_support_reference_is_unreachable` (`EGRT-T18`, Tier C): for each
     member of `Get-EgLauncherRetiredSupportRefs`, assert the literal appears nowhere
     outside the retired-set declaration. Vacuously satisfied while the set is empty
     (`DD-08`); the assertion is still present so the first retirement is regressed.
   - `test_no_support_reference_outside_the_bounded_vocabulary_is_emitted` (Tier C):
     regex-extract every `EG_LAUNCHER_[A-Z0-9_]+` literal from every committed runtime
     file and assert the set equals the live set exactly.
   - `test_the_two_vocabularies_cannot_collide` (Tier C): assert no `EG_LAUNCHER_*`
     reference appears in `energygrid_bill_downloader/cli.py`, and no `EG_LOGIN_*` or
     `APP_ERROR_*` reference appears in any committed runtime file.
   - `test_an_unrecognised_failure_records_the_unclassified_reference` (Tier A): a probe
     that raises an exception class outside the mapped set yields
     `EG_LAUNCHER_UNCLASSIFIED` and no message text on any surface.
2. Run the module. Prove RED.
3. Implement the two constants and three functions, and replace every inline reference
   literal at a raising site with a reference to the constant where that does not obscure
   the raising site.
4. Re-run. Prove GREEN.
5. Regression: full project suite. Expect this task to surface any reference declared in
   this plan but never actually raised; resolve by implementing the raising site, never by
   deleting the reference from the vocabulary.
6. Commit: `Bound and close the launcher support-reference vocabulary`.

---

## Task 19 - Privacy static guard over committed files

Design section 17.1 and run-instruction secret discipline.

### Files

- Modify: `energygrid-bill-downloader/tests/test_runtime_launcher.py`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

```python
PRIVACY_SCANNED_FILES: tuple[Path, ...]
# Every committed runtime .ps1, runtime/launcher.settings.example.json,
# runtime/README.md, and this test module itself.

FORBIDDEN_PATTERNS: dict[str, str] = {
    "windows_absolute_path": r"[A-Za-z]:\\\\",
    "unc_path":             r"^\\\\\\\\|[^\\\\]\\\\\\\\[A-Za-z0-9]",
    "credential_assignment": r"(?i)(password|passwd|pwd|secret|token|apikey|api_key)\s*=\s*['\"][^'\"]+['\"]",
    "http_url":             r"https?://",
}

ALLOWED_PLACEHOLDER_PREFIX = "REPLACE_WITH_"
```

### Steps

1. Write the failing Tier C test
   `test_no_committed_runtime_file_carries_private_or_secret_material` (`EGRT-T13`): for
   every file in `PRIVACY_SCANNED_FILES`, assert no `FORBIDDEN_PATTERNS` entry matches,
   with the single exemption that a matched line consisting only of a
   `REPLACE_WITH_...` placeholder inside the example settings file is permitted, and that
   the regular-expression pattern literals inside this test module itself are excluded
   from their own scan by anchoring the scan to lines outside the `FORBIDDEN_PATTERNS`
   declaration block.
2. Add companion assertions in the same test:
   `test_no_account_identity_or_host_identity_literal_is_committed` (assert the literal
   token `account_identity` appears only as a JSON key name in the config-contract check
   and never with a value), and
   `test_the_example_settings_file_carries_placeholders_only`.
3. Run the module. Prove RED by temporarily adding a scratch copy of a runtime file
   carrying a `C:\\` literal and confirming the guard reports it, then discarding the
   scratch copy. The guard must be proven to fail on the defect it exists to catch.
4. Implement the guard.
5. Re-run. Prove GREEN.
6. Regression: full project suite.
7. Commit: `Guard committed runtime files against private material`.

---

## Task 20 - `launcher.settings.example.json` placeholder shape

Design section 9.1.

### Files

- Create: `energygrid-bill-downloader/runtime/launcher.settings.example.json`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

Exact committed content, placeholders only, following the precedent set by
`config/energygrid.example.json`:

```json
{
  "config_path": "REPLACE_WITH_PRIVATE_CONFIG_JSON_PATH",
  "python_exe": "REPLACE_WITH_PYTHON_314_EXECUTABLE_PATH",
  "checkout_root": "REPLACE_WITH_DEPLOYED_CHECKOUT_ROOT",
  "credential_path": "REPLACE_WITH_PRIVATE_DPAPI_PSCREDENTIAL_CLIXML_PATH",
  "browser_cache_path": "REPLACE_WITH_PRIVATE_PLAYWRIGHT_BROWSER_CACHE_PATH",
  "expected_branch": "REPLACE_WITH_EXPECTED_BRANCH_OR_ANY_BRANCH",
  "log_root": "REPLACE_WITH_PRIVATE_DIAGNOSTICS_ROOT"
}
```

The file is a **shape**, superseded on the host by real private settings. It is not
deployed to the launcher root (`EGRT-T47`), no committed script reads it at runtime, and
its key set corresponds one-to-one with the launcher's host-supplied parameters.

### Steps

1. Write failing tests `test_the_example_settings_file_parses_and_is_placeholder_only`
   (every value starts with `REPLACE_WITH_`, no value matches a Windows absolute path or
   a UNC path) and
   `test_the_example_settings_keys_match_the_launcher_host_supplied_parameters` (the key
   set equals the seven host-supplied launcher parameters lowercased and snake_cased, so a
   parameter added later without a settings key fails the test).
2. Run the module. Prove RED.
3. Create the file with exactly the content above.
4. Re-run. Prove GREEN.
5. Regression: full project suite, plus Task 19's privacy guard, which now scans this file.
6. Commit: `Add the placeholder-only launcher settings example`.

---

## Task 21 - Documentation, runbook, and the stale Python 3.12 correction

Design sections 4, 14, 15, 16, and run-instruction items P and Q.

### Files

- Create: `energygrid-bill-downloader/runtime/README.md`
- Modify: `energygrid-bill-downloader/docs/runbook.md`
- Modify: `energygrid-bill-downloader/README.md`
- Modify: `energygrid-bill-downloader/task-scheduler/register_task.example.ps1`
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py`

### Interfaces

`runtime/README.md` is the directory-level runtime contract and source of truth. Required
sections, each stating a contract rather than a narrative:

1. **What is canonical here and what is not** - the section 3 boundary in short form.
2. **The three-member deployed package** - the exact Class A names, and the explicit
   statement that `install_or_update_launcher.ps1`, `launcher.settings.example.json`, and
   this README are never deployed.
3. **Launcher parameter surface** - the ten parameters, with the note that
   `-ExpectedBranch` is mandatory and takes the literal `ANY_BRANCH` sentinel.
4. **Exit bands** - `0`, `10`, `20`, `64` propagated from the application; `70` to `73`
   owned by the runtime layer, with the meaning of each.
5. **Launcher-root entry classes** - Class A, the exact Class B reserved-name syntax
   including the canonical lowercase GUID rule, and Class C fails closed.
6. **ValidateOnly** - what it runs, what it never mutates, and the deterministic output
   shape.
7. **Installation** - the four-phase transaction, `-AdmissionCommit` as the
   operator-controlled admission lane, and installer-only rollback authority.
8. **What the runtime never does** - no Scheduler action, no Git network or state
   mutation, no browser provisioning, no credential creation or rotation, no portal
   contact.

`docs/runbook.md` gains one new section, **Runtime launcher installation and validation**,
placed after "Controlled first validation" and before "Recovery guidance". It encodes the
design section 14 deployment flow and the design section 15 migration ordering, and it
must keep the four `EGRT-I30` concerns clearly separate and explicitly named:

- installer transaction backup cleanup (Task 12, automatic, post-acceptance);
- migration recovery holding (design 15.1, owner action, outside both the launcher root
  and the deployed checkout, never a committed path and never an installer parameter);
- launcher functional validation (`launcher.ps1 -ValidateOnly`, design 15 step 9);
- later separately authorised recovery-copy retirement or restoration (design 15 step 10).

The runbook section must state, without naming any private path:

- the historical in-root rollback artefact remains Class C and the launcher fails closed
  while it is present;
- the recovery copy is created and positively SHA-256-verified against the recorded
  accepted-preimage hash **before** the in-root artefact is removed;
- an unverifiable copy is treated as no copy at all and the migration fails closed with
  the in-root artefact untouched and `launcher.ps1 -ValidateOnly` not run;
- the recovery copy is retained through the first functional `launcher.ps1 -ValidateOnly`;
- a failed validation retains the copy, restores nothing automatically, and any
  restoration requires separate owner authority that must first define the complete safe
  pre-migration topology it is restoring;
- every mutating step is a live-system action requiring separate explicit current-turn
  approval.

`energygrid-bill-downloader/README.md` gains a short **Runtime layer** paragraph pointing
at `runtime/README.md` and stating that the approved host mechanism the existing
credential paragraph refers to is now the source-controlled DPAPI import in
`runtime/launcher_lib.ps1`. No application behaviour claim changes.

`task-scheduler/register_task.example.ps1` is corrected in place. The file stays
**inert** - it still registers, starts, alters, and removes nothing - and its role in the
design's structure table is unchanged. The correction is documentation only: the
placeholder token `<PYTHON_3_12_EXE>` is stale against the accepted Python 3.14 contract
(design section 5.2 step 5 requires a 3.14.x interpreter, `README.md` and `runbook.md`
already say 3.14.x, and the CI workflow pins `python-version: "3.14"`). It becomes
`<PYTHON_3_14_EXE>`. The intended-action comment block additionally gains one line naming
`runtime/launcher.ps1` as the eventual scheduled executable shape, because the design
makes the launcher "the only thing the Scheduled Task will ever invoke".

### Steps

1. Write failing Tier C tests:
   - `test_the_scheduler_example_names_the_accepted_python_314_contract` (item Q): assert
     `<PYTHON_3_12_EXE>` appears nowhere in
     `task-scheduler/register_task.example.ps1` and `<PYTHON_3_14_EXE>` appears exactly
     once.
   - `test_the_scheduler_example_remains_inert`: assert the file contains no
     `Register-ScheduledTask`, `New-ScheduledTask`, `Start-ScheduledTask`,
     `Set-ScheduledTask`, or `Unregister-ScheduledTask` command, and still parses cleanly.
   - `test_the_runtime_readme_documents_every_required_contract_section`: assert
     `runtime/README.md` exists and contains each of the eight required section headings.
   - `test_the_runtime_readme_states_the_exact_class_b_syntax`: assert the reserved prefix
     `.eglauncher-`, the three kinds, and the canonical lowercase GUID pattern all appear
     verbatim, so the documented contract cannot drift from `Test-EgResidueName`.
   - `test_the_runbook_keeps_the_four_migration_concerns_separate` (`EGRT-I30`): assert
     the runbook contains a heading for the new section and names all four concerns as
     distinct items.
   - `test_no_documentation_file_carries_a_private_path` : extend
     `PRIVACY_SCANNED_FILES` from Task 19 to include `runtime/README.md` and assert the
     runbook's new section adds no `^[A-Za-z]:\\` literal beyond the pre-existing
     owner-controlled archive example the runbook already carries.
2. Run the module. Prove RED.
3. Write `runtime/README.md`, add the runbook section, add the project README paragraph,
   and correct the scheduler example.
4. Re-run. Prove GREEN.
5. Regression: full project suite. The `test_cli.py` scope-guard tests must still pass,
   because every changed path is under `energygrid-bill-downloader/`.
6. Commit: `Document the runtime layer and correct the stale scheduler interpreter token`.

---

## Task 22 - Full-suite validation, CI verification, and no-workflow-change proof

### Files

- Modify: none. This task changes no file unless a validation failure requires a targeted
  repair, in which case the repair belongs to the task that owns the failing contract.
- Test: `energygrid-bill-downloader/tests/test_runtime_launcher.py` (execution only)

### Interfaces

Consumed: the committed workflow
`.github/workflows/energygrid-bill-downloader-tests.yml`, unchanged.

### Steps

1. Run the full project suite on Windows from `energygrid-bill-downloader`:

   ```powershell
   python -m unittest discover -s tests -v
   ```

   Confirm every `EGRT-T01` to `EGRT-T57` assertion is present and passing (`EGRT-I07`,
   `EGRT-I08`). Confirm Tier B tests **executed** rather than skipped; a skip on the
   development host is acceptable only if `powershell.exe` is genuinely absent, and the
   `CI` companion test guarantees they cannot be silently unexercised in the gate.
2. Run the root focused test that shares the workflow trigger, so the n8n error-handler
   boundary is confirmed untouched:

   ```powershell
   python -m unittest discover -s tests -p "test_energygrid_n8n_error_handler.py" -v
   ```

   The handler consumes `support_ref` and `exit_code` as opaque pass-through strings and
   enumerates no valid code set, so the new `70` to `73` band and the `EG_LAUNCHER_*`
   vocabulary are purely additive and require no workflow or handler change.
3. Prove the CI claim rather than asserting it:
   - `git diff --name-only <base>..HEAD` contains no path under `.github/` (`EGRT-I09`).
   - The workflow's existing `paths` trigger `energygrid-bill-downloader/**` already
     covers `runtime/**` and `tests/test_runtime_launcher.py`; confirm with
     `git check-ignore -v` that no new path is ignored, and confirm each new path matches
     the trigger prefix.
   - The workflow's `Scope and whitespace check` already permits
     `^energygrid-bill-downloader/`; confirm every changed path matches it.
   - The workflow's `PowerShell parse-only check` still names only
     `task-scheduler/register_task.example.ps1`; runtime parse coverage comes from
     `EGRT-T17` inside the Python suite instead, so parse coverage grows with the
     directory without a workflow edit (design section 13).
4. Run `git diff --check <base>..HEAD` and confirm no whitespace error.
5. Push the branch and read the exact-head CI result. Report it as read; never claim CI
   passed without reading it.
6. Commit: no commit unless step 1 or 2 required a targeted repair.

---

## Plan self-review

Performed against the writing-plans self-review before this plan was committed.

### 1. Spec coverage

Every material design section maps to at least one task, and every acceptance criterion
`EGRT-I01` to `EGRT-I30` maps to the task that satisfies it.

| Design section | Tasks |
| --- | --- |
| 3 source-of-truth boundary | 19, 20, 21 |
| 4 repository structure | 1, 10, 16, 20, 21 |
| 5.1 parameter surface | 16 |
| 5.2 ordered preflight | 16 |
| 5.3 invocation and exit codes | 16 |
| 6.1 installer parameters | 10 |
| 6.2 package set and phases | 10 |
| 6.3 installer constraints | 10, 12 |
| 6.4 integrity manifest | 9 |
| 6.5 package rollback | 11 |
| 6.6 backup cleanup and residue | 12 |
| 6.7 entry classes | 8 |
| 7.1 two primitives | 5, 6 |
| 7.2 mandatory replacement rules | 5 |
| 7.2.1 the two failure states | 5, 11 |
| 7.3 observed failure classes | 5 |
| 7.4 publish to absent | 6 |
| 8 ValidateOnly | 17 |
| 9.1 host-supplied values | 20 |
| 9.2 credential contract | 13 |
| 9.3 browser-cache binding | 14 |
| 10.1 governed Git result | 2 |
| 10.2 environment neutralisation | 3 |
| 10.3 path-scoped integrity and allowlist | 4, 15 |
| 11 exception and diagnostic contract | 16, 18 |
| 12 test strategy and tiering | 1, 22 |
| 13 CI strategy | 22 |
| 14 deployment flow | 21 |
| 15 migration and recovery ordering | 21 |
| 16 disaster rebuild | 21 |
| 17 security and privacy | 16, 19 |
| 18 Run119 defects | 2, 3, 5, 7 |

| Criterion | Task |
| --- | --- |
| `EGRT-I01` pure library | 1 |
| `EGRT-I02` launcher surface and order | 16 |
| `EGRT-I03` installer sequence and idempotency | 10 |
| `EGRT-I04` atomic replace rules | 5 |
| `EGRT-I05` governed Git contract | 2, 3 |
| `EGRT-I06` ValidateOnly | 17 |
| `EGRT-I07` all fifty-seven assertions | 22 |
| `EGRT-I08` full suite on Windows | 22 |
| `EGRT-I09` no workflow modified | 22 |
| `EGRT-I10` nothing private committed | 19 |
| `EGRT-I11` bounded vocabulary | 18 |
| `EGRT-I12` no live action | Global Constraints 1 to 4, verified in 22 |
| `EGRT-I13` credential logic source-controlled | 13 |
| `EGRT-I14` credential artefact outside Git | 13, 19 |
| `EGRT-I15` browser cache fails closed | 14 |
| `EGRT-I16` path-scoped integrity | 15 |
| `EGRT-I17` unrelated movement does not block | 15 |
| `EGRT-I18` zero Git network or mutation | 4 |
| `EGRT-I19` admission only in the install lane | 10 |
| `EGRT-I20` reproducible installed integrity | 9, 10 |
| `EGRT-I21` clean first install without `File.Replace` | 6, 10 |
| `EGRT-I22` one package transaction | 10 |
| `EGRT-I23` exact pre-transaction restoration | 11 |
| `EGRT-I24` manifest and re-verification before cleanup | 10, 12 |
| `EGRT-I25` non-secret checks precede import | 16 |
| `EGRT-I26` bounded credential cleanup, no `Dispose` | 13 |
| `EGRT-I27` deterministic classification | 8 |
| `EGRT-I28` scoped completeness, extras fail closed | 8, 9 |
| `EGRT-I29` residue never participates | 8, 16 |
| `EGRT-I30` four migration concerns separate | 21 |

Run-instruction contract families A to Q all map: A to Tasks 3, 15, 19, 20; B to Task 10;
C to Global Constraints and Task 1; D to Tasks 2 and 3; E to Task 13; F to Task 14; G to
Task 15; H to Tasks 10, 11, 12; I to Tasks 5 and 7; J to the `PublicationResult` shape and
Task 5; K to Task 8; L to Task 17; M to Task 21; N preserved throughout (no `EGRT-T` or
`EGRT-I` identifier is renumbered anywhere in this plan); O to Task 22; P to Task 21; Q to
Task 21.

**Result: PASS.** All fifty-seven `EGRT-T` assertions, all thirty `EGRT-I` criteria, all
thirty-two design sections listed, and all seventeen run-instruction contract families are
covered.

### 2. Placeholder scan

No `TODO`, `TBD`, `FIXME`, `similar to above`, generic `add tests`, unspecified error
handling, or unbound file or function name appears in this plan. Every task names exact
Create, Modify, and Test paths; every function is given an exact signature with its
parameter attributes; every failure path names its bounded support reference; and every
test is named. The `REPLACE_WITH_...` tokens in Task 20 are the required committed
placeholder content mandated by design section 9.1, not plan placeholders.

**Result: PASS.**

### 3. Type and interface consistency

- `PublicationResult` carries the same ten fields wherever it appears: defined once in
  Shared object shapes, produced by `Invoke-AtomicFileReplace` (Task 5) and
  `Invoke-PublishToAbsentDestination` (Task 6), consumed by `New-EgTransactionState`
  (Task 10) and `Invoke-EgPackageRollback` (Task 11).
- `GovernedGitResult` is produced only by `Invoke-GovernedGit` (Tasks 2, 3, 4) and
  consumed only by `Test-EgGovernedSourceIntegrity` (Task 15).
- `CheckResult` is returned by `Restore-EgProcessEnvironmentSnapshot` (Task 3),
  `Test-EgInstallationManifestShape` and `Compare-EgInstalledPackageToManifest` (Task 9),
  `Test-EgBrowserCacheReady` (Task 14), `Test-EgGovernedSourceIntegrity` (Task 15), and
  `Test-EgPythonVersionSupported`, `Test-EgLauncherConfigContract`, and
  `Test-EgLauncherRootSecurity` (Task 16). Its `Checks` ordered dictionary feeds
  `ConvertTo-EgValidationJson` (Task 17).
- The three Class A fixed names are declared once as
  `$script:EgDeployedPackageMemberNames` (Task 8) and are the sole source for
  `Get-EgDeployedPackageMemberNames` (Task 8), the `Member` field of `Test-EgResidueName`
  and `New-EgResidueName` (Task 8), and `Get-EgDeployableSourceSet` (Task 10). The two
  executable names are declared once as `$script:EgManifestMemberNames` (Task 9).
- The twenty-one stable preflight check names in the Ordered preflight check names table
  are the same strings produced by Task 16's `CheckResult` objects and asserted by Task
  16's order test and Task 17's determinism tests.
- The forty-eight live support references in the vocabulary table are the same set
  declared as `$script:EgLauncherSupportRefs` (Task 18) and raised by the functions named
  in that table's third column.
- Publication order `DD-10` is used identically by Task 10 Phase 2 and, reversed, by Task
  11 `Get-EgTouchedSet`.
- The environment-snapshot pair `Get-EgProcessEnvironmentSnapshot` and
  `Restore-EgProcessEnvironmentSnapshot` is introduced once in Task 3 and reused unchanged
  for the credential variables (Task 13) and the browser-cache variable (Task 14), so all
  three restore on one `finally`-equivalent path.

**Result: PASS.** No function is referenced before it is defined by an earlier task, no
signature differs between its definition and its uses, and no return shape is consumed as
a different shape anywhere.

### 4. Change-scope verification

- No implementation file is changed by this planning run.
- Exactly one document is added:
  `energygrid-bill-downloader/docs/runtime_source_durability_implementation_plan.md`.
- No secret, credential, private absolute path, UNC path, account identity, host identity,
  or principal identity appears in this document.
- `git diff --check` passes.

**Result: PASS.**

## What this plan does not authorise

Acceptance of this plan is a planning decision only. It does not approve the
implementation change, the first installation, the migration sequence, the scheduler, or
any live run. Each remains a separate gate requiring its own explicit current-turn owner
approval, and no task above may be started against a live system on the strength of this
document.
