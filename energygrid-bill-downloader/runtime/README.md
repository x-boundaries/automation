# Energy@Grid runtime layer

Design lock: `DL-XB-141-RUNTIME-005-SOURCE-DURABILITY`.
Controlling specification: `../docs/runtime_source_durability_design.md`.

This directory is the source of truth for the operational layer that starts the reviewed
Energy@Grid application on the production Windows host. Every statement below is a
contract, not a description of intent.

## What is canonical here and what is not

Git is canonical for the reusable, non-secret launcher and runtime behaviour: the launcher
contract, installation and update mechanics, `ValidateOnly` behaviour, runtime binding and
validation rules, atomic replacement semantics, rollback semantics, the bounded
`EG_LAUNCHER_*` diagnostic vocabulary, the security expectations expressed as named checks,
governed Git invocation, the credential import and cleanup logic, the browser-cache binding
logic, the manifest shape and comparison rules, and the tests.

The server remains canonical for private deployment state: credentials, DPAPI credential
material, the private JSON configuration and its `account_identity`, every private absolute
path, Python and Chromium installation state, downloaded PDFs, the operational manifest
database, logs, temporary and runtime data, and every host-specific secret, principal, and
identity.

Three rules keep the boundary from drifting.

1. A host-specific value is a parameter or a private configuration key. It is never a
   default, a fallback, or a literal in a committed file.
2. A behaviour that would be identical on a second Energy@Grid host is reusable and belongs
   in Git, even if it was first discovered on this host.
3. Committed example configuration carries placeholders only.

## The three-member deployed package

The installed package is exactly three members, at fixed names, and the installer publishes
nothing else into the launcher root:

| Member | Role |
| --- | --- |
| `launcher.ps1` | The entry script the Scheduled Task invokes |
| `launcher_lib.ps1` | The pure library the entry script dot-sources |
| `installation_manifest.json` | Generated; describes the other two |

Sharing this directory does not make a file deployable. These stay checkout, source, or
operator material and are never copied to the launcher root:

- `install_or_update_launcher.ps1` — the operator runs it from the checkout, and deploying
  an installer inside the surface it installs would be self-referential.
- `launcher.settings.example.json` — a placeholder shape, superseded on the host by real
  private settings. No committed script reads it at runtime.
- `README.md` — this file.

The deployable set is enumerated explicitly in source rather than derived from a directory
listing, so a file added here later cannot become deployable by accident.

## Launcher parameter surface

Eleven parameters, and no more. There is no parameter that accepts a credential value, no
portal parameter, no browser-install parameter, no commit-pin parameter, and no headed
switch. There is no launcher-root parameter either: the launcher root is the entry script's
own directory.

| Parameter | Required | Meaning |
| --- | --- | --- |
| `-ConfigPath` | yes | Absolute path to the external private JSON configuration |
| `-PythonExe` | yes | Absolute path to the approved Python 3.14.x interpreter |
| `-CheckoutRoot` | yes | Absolute path to the deployed checkout root |
| `-CredentialPath` | yes | Absolute path to the private DPAPI PSCredential CLIXML artefact |
| `-BrowserCachePath` | yes | Absolute path to the approved private Playwright browser cache |
| `-ExpectedBranch` | yes | Branch the deployed checkout must be on, or the literal `ANY_BRANCH` |
| `-AuthorisedLauncherRootWriteSid` | yes | One or more exact security identifier strings naming the exhaustive set of trustees permitted to hold write-capable access on the launcher root |
| `-Command` | no | `run` (default), `list`, `login-diagnostic`, or `download-preflight-diagnostic` |
| `-LogRoot` | no | Private diagnostics root for the launcher's own terminal event |
| `-ValidateOnly` | no | Switch; see below |
| `-RunId` | no | Correlation identifier for the terminal event only |

`-Command` is a closed allowlist of four fixed operation names and is the only thing that
varies in the invocation. The child is always started as
`<PythonExe> -m energygrid_bill_downloader <Command> --config <ConfigPath>` -- exactly five
arguments, in that order, for every admitted command. Nothing is appended conditionally, and
no caller-supplied script, module, path, portal address, credential value, or arbitrary
child argument can reach it.

`login-diagnostic` runs the bounded login diagnostic: the canonical login sequence up to and
including exactly one real Login submit, then a bounded read-only observation of fixed
public-safe counts and booleans, then stop. It reaches no inventory, download, archive,
state, or publication behaviour. There is still **no generic headed switch**: headed
execution is an implicit and non-overridable property of that one fixed operation, it cannot
be selected, suppressed, or applied to `run` or `list`, and no launcher parameter exposes
it. `run` and `list` are unchanged.

`download-preflight-diagnostic` (DL-XB-199 G2-083 / G3-084) runs the fixed headless
no-Download pre-dispatch diagnostic: canonical login, the production inventory, then the
one shared production pre-dispatch proof over every row in order. It never clicks
Download, creates no state, log, temp or archive artefact, and emits one
`energygrid.download_preflight_diagnostic.v1` document. It reuses the existing credential
import and injection unchanged and adds no parameter: the child vector is the same fixed
five elements. The installed launcher on a host keeps its previously admitted allowlist
until a separately authorised republish, re-admission and `ValidateOnly` accept the new
launcher bytes; changing this source file deploys nothing.

`-ExpectedBranch` is mandatory with an explicit `ANY_BRANCH` sentinel rather than optional,
so branch binding is never disabled by omitting an argument.

`-AuthorisedLauncherRootWriteSid` is mandatory for the same reason: omitting an argument
must never silently disable a security expectation. It carries exact identifier strings
rather than principal names, because names are ambiguous, locale-dependent, and accepting
them would make a security check depend on a name-resolution lookup performed at check
time. It has no default, no committed example value, no environment-variable form, and no
file the launcher reads it from. This file states the parameter and its contract; it
carries no identifier. Any private operator record of the value lives with the other
private deployment state, outside Git, and the value is never written to a log, to the
terminal event, or to validation output.

## Exit bands

The application returns `0`, `10`, `20`, or `64`. The runtime layer's own failures use a
disjoint band, so a launcher or installer failure can never be mistaken for an application
status. The disjointness is asserted by a test, not left to convention.

| Code | Meaning |
| --- | --- |
| `0`, `10`, `20`, `64` | Propagated unchanged from the application child process |
| `70` | Launcher preflight or validation failed; no child process was started |
| `71` | Installation failed before any destination was mutated; the installed package is unchanged |
| `72` | Installation or package verification failed before acceptance; installer-owned package rollback completed and was positively verified |
| `73` | Package rollback failed, could not be verified, or an advanced destination had no recoverable preimage; manual owner action required |

## Launcher-root entry classes

Every entry in the launcher root belongs to exactly one of three classes, and the
classification is deterministic.

**Class A — deployed package members.** Exactly three, at the fixed names
`launcher.ps1`, `launcher_lib.ps1`, and `installation_manifest.json`. The set is exact. A
missing member fails closed and no substitution is tolerated. A Class A name is satisfied
only by a file, so a directory cannot impersonate a member.

**Class B — recognised installer-owned transaction residue.** Residue is not a package
member and is not described by the manifest. It is recognised only by an exact reserved-name
contract:

```text
.eglauncher-<kind>--<member>--<operation-id>
```

All five rules must hold. The name begins with the literal reserved prefix
`.eglauncher-`; the remainder splits on the two-hyphen delimiter `--` into exactly three
fields; `<kind>` is exactly one of `staging`, `backup`, or `rollback`; `<member>` is exactly
one of the three Class A fixed names; and `<operation-id>` is a canonical lowercase
identifier matching `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`.

A generic extension rule such as `*.bak` or `*.tmp` is explicitly not sufficient and is
never used: it would let any file dropped into the launcher root be waved through, which is
the opposite of the intent. The reserved name binds residue to a specific member and a
specific transaction, and it carries no private path, host name, principal, or other
private value. It also cannot be executed by accident, because it begins with a dot and
ends in no executable extension.

**Class C — everything else.** Any entry that is neither an exact Class A member nor a valid
Class B residue name fails closed. That includes another script whatever it is called, a
copy of the library under a different name, a `.bak` or `.tmp` file that does not satisfy
the Class B parser, a lookalike residue name with any single malformed field, and any
unexpected directory. The rule is never relaxed to "ignore extra files".

Residue can never influence which code runs. Package-member paths are fixed joins of the
launcher root with the three Class A names. The launcher enumerates the root only to
classify, never to find a script or library, and recognised residue is never dot-sourced,
invoked, imported, or selected as a fallback, and can never satisfy a missing member.

## ValidateOnly

`-ValidateOnly` exists so that every preflight, binding, security, and installation check
can be exercised on the production host without any production mutation, and without owner
approval for a mutating action.

It creates, modifies, deletes, and renames nothing: no staging file, no backup file, no log
file, no directory, no scheduler entry, and no environment change that outlives the process.
It starts no child process other than the read-only interpreter version probe and the
governed Git reads, and it never invokes `run`, `list`, or `login-diagnostic`. It launches
no browser, installs or updates no browser cache, and contacts no portal.

It may import the private credential artefact in-process to prove viability, because an
artefact that cannot be imported is exactly the failure an operator needs to find before
scheduling. The import is read-only, the imported object is discarded immediately, and only
three booleans are recorded. No credential value, length, prefix, suffix, or hash is ever
emitted, logged, measured, or otherwise turned into a reportable quantity.

Output is exactly one JSON object carrying a `checks` map of check name to outcome, an
overall `status`, and a `support_ref` when the status is not a pass. It contains no path, no
environment value, no credential value, no account identity, no security identifier, no
trustee or owner name, and no Git output text. It carries no timestamp and no generated
identifier, so two consecutive runs against unchanged host state produce byte-identical
output.

The launcher's real path stops at the first failed check. A validation run evaluates the
whole non-secret block so the operator sees every outcome at once, then reports the first
failing check by its stable name. A position that was not evaluated reports `FAIL`, never
`PASS`: an unevaluated security expectation is never reported as satisfied. Credential
positions are reached only when every non-secret position passed, so a run that will fail
for any other reason never opens the credential artefact at all.

Exit `0` means every check passed. A failure exits `70`.

## Installation

`install_or_update_launcher.ps1` is the only sanctioned way the installed launcher ever
changes. `-AdmissionCommit` is the operator-controlled admission lane and is mandatory: it
is verified against the deployed checkout's head through a governed read-only Git
invocation. That requirement stops at this lane and is not inherited by normal unattended
execution, which uses the path-scoped governed integrity contract instead, so an unrelated
merge elsewhere in the repository never blocks the daily job.

The three members are one transaction, in four phases.

1. **Admission and prepare.** No destination mutation whatsoever. Verify the admission
   commit and the source checkout, resolve the deployable set, parse-check and hash every
   source file, classify every destination as existing or absent, report an already-current
   package without mutating anything, write and fully verify every staging file before
   publishing any of them, then construct and shape-validate the candidate manifest. Any
   failure here exits `71` with the destination byte-identical.
2. **Publish the executable members**, in a fixed recorded order: the library before the
   entry script that dot-sources it. An existing destination publishes through
   explicit-backup replacement; an absent one publishes through a no-replace move. No backup
   is deleted in this phase.
3. **Publish and verify the manifest inside the same transaction**, only after every
   executable member has individually verified, then read it back and re-verify the whole
   package against it.
4. **Commit.** Only after acceptance may retained backups be reaped.

Rollback authority belongs to the installer transaction and to nothing else. Neither publish
primitive restores anything: they publish and report. Rollback walks the touched set in
reverse publication order, restores each existing preimage from its retained backup and
re-hashes to confirm it, returns each created destination to absence, and then re-verifies
the entire package against its pre-transaction state. A verified rollback exits `72`; one
that cannot be positively verified exits `73` with every artefact retained and no further
repair attempted.

If backup cleanup fails after acceptance, the installation is kept, the undeleted backup is
retained as inert residue that still satisfies the Class B contract, the count is reported
under a bounded reference, and the exit code stays `0`, because the installation genuinely
succeeded. That is the single narrow, visible exception to the fail-closed default, and it
concerns a redundant artefact after a verified success rather than any part of the installed
package.

## What the runtime never does

- No Scheduled Task action of any kind: it never registers, alters, starts, or removes one.
- No Git network operation and no Git state mutation. Only a read-only subcommand allowlist
  is reachable, and a static guard enforces it over the committed files.
- No browser provisioning: it never installs, updates, repairs, or downloads into the
  browser cache. Provisioning is an operator action.
- No credential creation, rotation, relocation, or inspection beyond the read-only import.
- No portal contact and no headed run of its own. The launcher never opens a browser; a
  headed browser exists only inside the application child started for the fixed
  `login-diagnostic` command, and no launcher parameter can request one.
- No access-control mutation. The runtime observes launcher-root security and fails closed;
  it never grants, revokes, or repairs a permission.
- No broad fallback, silent compatibility path, synthetic-data fallback, fake success state,
  or catch-and-continue behaviour. A failed check fails the run.

## Launcher-root write authority

Two independent expectations are checked, and neither implies the other. Both are terminal
and neither is ever downgraded to a warning or bypassed by a switch.

`launcher_root_not_writable_by_run_principal` asks Windows to evaluate the access token the
launcher process is actually running under, against the launcher root and against every
Class A member individually. The token is duplicated to an impersonation token at
identification level purely to be evaluated; the launcher never impersonates with it. The
access check requests the maximum allowed access and intersects the returned mask with the
write-capable mask after generic mapping, so a token holding even one write-capable right is
observable. Requesting the union of every write-capable right and reading a denied status as
safe would be a false negative by construction and is never done. Any failure to read the
token or a security descriptor is terminal and is never treated as a pass.

`launcher_root_write_trustees_authorised` inspects the discretionary access control list of
the same object set. Every trustee holding a write-capable access-allowed grant, and every
examined object's owner, must be a member of the exhaustive set supplied on
`-AuthorisedLauncherRootWriteSid`. Comparison is exact identifier equality: no prefix,
pattern, range, or wildcard form is accepted, and a well-known administrative or service
identifier carries no implicit authority. An inherited allow entry counts exactly as an
explicit one. Access-denied entries are ignored, because a deny can only reduce access and
whether it neutralises a given allow depends on list order. A write-capable `CREATOR OWNER`
entry is terminal, and that placeholder may not be supplied in the authorised set. An object
with no discretionary access control list fails, because Windows grants all access in that
case.

A run principal holding `SeTakeOwnershipPrivilege` or `SeRestorePrivilege` fails the
run-principal check by construction, whether the privilege is enabled or disabled, because
such a token can reach the object whatever the access list says. `LocalSystem` holds those
privileges by construction and is therefore not a valid unattended run principal for
`launcher.ps1`. This is a bounded rule, not an exhaustive one: no discretionary-access-list
check can fully constrain a principal granted list-bypassing privileges.

The design does not force a dedicated run account. A host may separate installer and run
principals by account, or may rely on elevation separation within one account. Both are
permitted deployments, neither is assumed, and the run-principal check is what makes the
difference observable rather than asserted.

Neither check emits a security identifier, a trustee name, an owner identity, a path, or any
count derived from them. Only the check name, the pass or fail outcome, and a bounded support
reference reach any surface.

An honest limitation, stated because a reader will otherwise assume more: the installation
manifest sits beside the files it describes, so a writer who can modify the launcher can
also modify the manifest. It is tamper-evident only to the extent that these launcher-root
expectations hold. The manifest defends against accidental drift, partial installation, and
unreviewed hand-editing; it does not by itself defend against an attacker who already has
write access to the launcher root.
