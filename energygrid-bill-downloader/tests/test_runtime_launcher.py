"""Offline tests for the source-controlled Energy@Grid runtime layer.

Design lock: DL-XB-141-RUNTIME-005-SOURCE-DURABILITY.
Controlling specification: docs/runtime_source_durability_design.md.
Controlling plan: docs/runtime_source_durability_implementation_plan.md.

No test in this module contacts the Energy@Grid portal, the production server, the
production launcher root, a real DPAPI credential artefact, Task Scheduler, or n8n. Every
filesystem behaviour is exercised in a per-test scratch directory, every credential is a
per-run synthetic throwaway, and the application itself is never invoked: only a scratch
child stub is.

Three tiers, per design section 12.1:

* Tier A, portable library tests, run under whichever PowerShell the host provides.
* Tier B, compatibility-boundary tests, pinned to Windows PowerShell 5.1 (PSEdition
  Desktop), because that is the production runtime and the runtime on which the
  File.Replace behaviour in design section 7.3 was observed.
* Tier C, static guard tests, which read the committed runtime files as text and as a
  parsed abstract syntax tree and assert structural properties no dynamic test can
  guarantee across runtimes.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / "runtime"
LIB = RUNTIME_DIR / "launcher_lib.ps1"
LAUNCHER = RUNTIME_DIR / "launcher.ps1"
INSTALLER = RUNTIME_DIR / "install_or_update_launcher.ps1"
RUNTIME_PS1_FILES = (LIB, LAUNCHER, INSTALLER)

EXAMPLE_SETTINGS = RUNTIME_DIR / "launcher.settings.example.json"
RUNTIME_README = RUNTIME_DIR / "README.md"


def existing_runtime_ps1_files():
    """Every runtime .ps1 that exists right now, so early tasks run before later ones."""
    return tuple(path for path in RUNTIME_PS1_FILES if path.is_file())


# --------------------------------------------------------------------------------------
# Scratch directory helper
# --------------------------------------------------------------------------------------

def _force_remove_tree(root):
    """Remove a scratch tree, clearing read-only attributes a test deliberately set."""
    def _onerror(func, path, _excinfo):
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass

    for child in sorted(Path(root).rglob("*"), reverse=True):
        try:
            if child.is_file() or child.is_symlink():
                os.chmod(child, 0o700)
        except OSError:
            pass
    shutil.rmtree(root, onerror=_onerror)


class TemporaryScratch:
    """A per-test scratch directory that is always removed, even when marked read-only."""

    def __init__(self):
        self._path = None

    def __enter__(self):
        self._path = Path(tempfile.mkdtemp(prefix="eg_runtime_"))
        return self._path

    def __exit__(self, exc_type, exc, tb):
        if self._path is None:
            return False
        _force_remove_tree(self._path)
        self._path = None
        return False


# --------------------------------------------------------------------------------------
# Scratch Git repository construction
# --------------------------------------------------------------------------------------
# Scratch repositories are built with raw git from Python, deliberately never through the
# runtime library. The library's Git surface is a READ-ONLY allowlist (design section
# 10.3), so init, add, and commit are not reachable from it and must not be.

GIT_EXE = shutil.which("git")


def run_git(repo, *args, check=True):
    """Run raw git inside a scratch repository. Test-side setup only."""
    if GIT_EXE is None:
        raise unittest.SkipTest("git is not available on this host")
    completed = subprocess.run(
        [GIT_EXE, "-C", str(repo)] + [str(arg) for arg in args],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            "git %s exited %d\nstdout:\n%s\nstderr:\n%s"
            % (" ".join(str(a) for a in args), completed.returncode,
               completed.stdout, completed.stderr)
        )
    return completed


def init_scratch_repo(root, branch="main"):
    """Create a scratch Git repository with one commit on the named branch."""
    if GIT_EXE is None:
        raise unittest.SkipTest("git is not available on this host")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [GIT_EXE, "init", "-b", branch, str(root)],
        capture_output=True, text=True, timeout=300, check=True,
    )
    run_git(root, "config", "user.email", "runtime-tests@invalid.test")
    run_git(root, "config", "user.name", "EnergyGrid Runtime Tests")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    run_git(root, "add", "seed.txt")
    run_git(root, "commit", "-m", "seed")
    return root


# --------------------------------------------------------------------------------------
# Interpreter discovery
# --------------------------------------------------------------------------------------

def _probe_edition(exe):
    """Return the PSEdition string an interpreter reports, or None when it cannot run."""
    try:
        completed = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSEdition"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def find_any_powershell():
    """Tier A host: pwsh, then powershell, then powershell.exe. None when absent."""
    for candidate in ("pwsh", "powershell", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved and _probe_edition(resolved) is not None:
            return resolved
    return None


def find_desktop_powershell():
    """Tier B host: powershell.exe whose $PSVersionTable.PSEdition is 'Desktop'.

    The path is returned only after a probe confirms the Desktop edition, so a pwsh shim
    named powershell.exe cannot masquerade as the Windows PowerShell 5.1 boundary.
    """
    for candidate in ("powershell.exe", "powershell"):
        resolved = shutil.which(candidate)
        if resolved and _probe_edition(resolved) == "Desktop":
            return resolved
    return None


ANY_PS = find_any_powershell()
DESKTOP_PS = find_desktop_powershell()


# --------------------------------------------------------------------------------------
# Shared PowerShell invocation helpers
# --------------------------------------------------------------------------------------

def run_ps(exe, script_path, *args):
    """Invoke a scratch .ps1 with a non-interactive, profile-free, bypassed host."""
    command = [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    command.extend(str(arg) for arg in args)
    return subprocess.run(command, capture_output=True, text=True, timeout=900)


PROBE_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Lib,
    [Parameter(Mandatory)][string]$Op,
    [string]$Dir = '',
    [string]$Dir2 = '',
    [string]$Path = '',
    [string]$Path2 = '',
    [string]$Path3 = '',
    [string]$Value = '',
    [string]$Value2 = '',
    [string]$Json = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. $Lib

switch ($Op) {
    'contract' {
        Get-EgLauncherLibraryContract | ConvertTo-Json -Depth 8 -Compress
    }
    'git' {
        # Three governed reads against the scratch repository the caller prepared:
        # a one-line success, a zero-line success, and a non-zero exit.
        $oneLine  = Invoke-GovernedGit -RepositoryRootPath $Dir -Arguments @('rev-parse', '--abbrev-ref', 'HEAD')
        $zeroLine = Invoke-GovernedGit -RepositoryRootPath $Dir -Arguments @('ls-files', '--', 'no_such_pathspec')
        $failing  = Invoke-GovernedGit -RepositoryRootPath $Dir -Arguments @('rev-parse', '--verify', 'refs/heads/definitely_absent')

        $oneLineFirst = ''
        if ($oneLine.Lines.Count -ge 1) { $oneLineFirst = $oneLine.Lines[0] }

        [ordered]@{
            oneLineSuccess    = $oneLine.Success
            oneLineCount      = $oneLine.Lines.Count
            oneLineFirst      = $oneLineFirst
            oneLineIsBareString = ($oneLine.Lines -is [string])
            oneLineSupportRef = $oneLine.SupportRef
            zeroLineSuccess   = $zeroLine.Success
            zeroLineCount     = $zeroLine.Lines.Count
            zeroLineIsNull    = ($null -eq $zeroLine.Lines)
            zeroLineExit      = $zeroLine.ExitCode
            failExit          = $failing.ExitCode
            failSuccess       = $failing.Success
            failLineCount     = $failing.Lines.Count
            failSupportRef    = $failing.SupportRef
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'gitambient' {
        # Seed ambient Git variables at the decoy repository, then take a governed read
        # against the real one. Neutralisation must make the decoy unreachable, and the
        # finally block must restore the process environment exactly.
        $names = @(Get-EgGovernedGitEnvironmentNames)
        $seeded = [ordered]@{}
        $seeded['GIT_DIR'] = (Join-Path $Dir2 '.git')
        $seeded['GIT_WORK_TREE'] = $Dir2
        $seeded['GIT_CONFIG_GLOBAL'] = (Join-Path $Dir2 'decoy.gitconfig')
        foreach ($seededName in @($seeded.Keys)) {
            [System.Environment]::SetEnvironmentVariable($seededName, $seeded[$seededName], 'Process')
        }

        $before = [ordered]@{}
        foreach ($name in $names) {
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            $before[$name] = [ordered]@{
                present = ($null -ne $observed)
                value   = ([string]$observed)
            }
        }

        $bound = Invoke-GovernedGit -RepositoryRootPath $Dir -Arguments @('rev-parse', '--show-toplevel')

        $after = [ordered]@{}
        foreach ($name in $names) {
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            $after[$name] = [ordered]@{
                present = ($null -ne $observed)
                value   = ([string]$observed)
            }
        }

        $topLevel = ''
        if ($bound.Lines.Count -ge 1) { $topLevel = $bound.Lines[0] }

        [ordered]@{
            boundSuccess  = $bound.Success
            boundTopLevel = $topLevel
            names         = $names
            nameCount     = $names.Count
            before        = $before
            after         = $after
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'envsnapshot' {
        # Restoration of a snapshot must REMOVE a name recorded absent rather than set it
        # to an empty string.
        $names = @('EG_PROBE_ABSENT_NAME', 'EG_PROBE_PRESENT_NAME')
        [System.Environment]::SetEnvironmentVariable('EG_PROBE_ABSENT_NAME', $null, 'Process')
        [System.Environment]::SetEnvironmentVariable('EG_PROBE_PRESENT_NAME', 'original', 'Process')

        $snapshot = Get-EgProcessEnvironmentSnapshot -Names $names

        # Disturb both names in opposite directions.
        [System.Environment]::SetEnvironmentVariable('EG_PROBE_ABSENT_NAME', 'appeared', 'Process')
        [System.Environment]::SetEnvironmentVariable('EG_PROBE_PRESENT_NAME', 'overwritten', 'Process')

        $restore = Restore-EgProcessEnvironmentSnapshot -Snapshot $snapshot

        $absentAfter = [System.Environment]::GetEnvironmentVariable('EG_PROBE_ABSENT_NAME', 'Process')
        $presentAfter = [System.Environment]::GetEnvironmentVariable('EG_PROBE_PRESENT_NAME', 'Process')

        [ordered]@{
            restorePass       = $restore.Pass
            restoreSupportRef = $restore.SupportRef
            absentPresent     = ($null -ne $absentAfter)
            absentValue       = ([string]$absentAfter)
            presentValue      = ([string]$presentAfter)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'gitforbidden' {
        # A forbidden subcommand must be refused BEFORE any process is started.
        $forbidden = Invoke-GovernedGit -RepositoryRootPath $Dir -Arguments @('fetch', '--all')
        $allowed = @(Get-EgGitAllowedSubcommands)
        [ordered]@{
            forbiddenSuccess    = $forbidden.Success
            forbiddenExit       = $forbidden.ExitCode
            forbiddenLineCount  = $forbidden.Lines.Count
            forbiddenSupportRef = $forbidden.SupportRef
            allowed             = $allowed
            allowedCount        = $allowed.Count
            revParseAllowed     = (Test-EgGitSubcommandAllowed -Subcommand 'rev-parse')
            fetchAllowed        = (Test-EgGitSubcommandAllowed -Subcommand 'fetch')
            emptyAllowed        = (Test-EgGitSubcommandAllowed -Subcommand '')
            upperCaseAllowed    = (Test-EgGitSubcommandAllowed -Subcommand 'REV-PARSE')
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'replace' {
        # One fixture, one publication attempt, one emitted observation. -Value selects
        # the failure class to induce; every case shares the same fixture shape so the
        # emitted observation is directly comparable across cases.
        $sourceText = 'SOURCE-CONTENT'
        $destText = 'DESTINATION-PREIMAGE'
        $source = Join-Path $Dir 'staging.txt'
        $destination = Join-Path $Dir 'destination.txt'
        $backup = Join-Path $Dir 'destination.backup'
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($source, $sourceText, $utf8)
        [System.IO.File]::WriteAllText($destination, $destText, $utf8)

        $preimageSha = Get-EgFileSha256 -Path $destination
        $sourceSha = Get-EgFileSha256 -Path $source
        $expected = $sourceSha
        if ($Value -eq 'wronghash') {
            $expected = '0000000000000000000000000000000000000000000000000000000000000000'
        }

        $refused = $false
        $refusedType = ''
        $result = $null
        $readOnlyItem = $null
        try {
            if ($Value -eq 'readonly') {
                $readOnlyItem = Get-Item -LiteralPath $destination
                $readOnlyItem.IsReadOnly = $true
            }

            if ($Value -eq 'nullbackup') {
                $result = Invoke-AtomicFileReplace -SourcePath $source -DestinationPath $destination -BackupPath $null -ExpectedSha256 $expected
            }
            elseif ($Value -eq 'emptybackup') {
                $result = Invoke-AtomicFileReplace -SourcePath $source -DestinationPath $destination -BackupPath '' -ExpectedSha256 $expected
            }
            elseif ($Value -eq 'otherdirbackup') {
                $elsewhere = Join-Path $Dir 'elsewhere'
                if (-not (Test-Path -LiteralPath $elsewhere -PathType Container)) {
                    [void](New-Item -ItemType Directory -Path $elsewhere)
                }
                $result = Invoke-AtomicFileReplace -SourcePath $source -DestinationPath $destination -BackupPath (Join-Path $elsewhere 'destination.backup') -ExpectedSha256 $expected
            }
            else {
                $result = Invoke-AtomicFileReplace -SourcePath $source -DestinationPath $destination -BackupPath $backup -ExpectedSha256 $expected
            }
        }
        catch {
            $refused = $true
            $refusedType = $_.Exception.GetType().FullName
        }
        finally {
            if ($null -ne $readOnlyItem) {
                $readOnlyItem.Refresh()
                if (Test-Path -LiteralPath $destination -PathType Leaf) {
                    $clear = Get-Item -LiteralPath $destination
                    $clear.IsReadOnly = $false
                }
            }
        }

        $emitted = [ordered]@{
            refused          = $refused
            refusedType      = $refusedType
            sourceSha        = $sourceSha
            preimageShaSetup = $preimageSha
            destShaAfter     = (Get-EgFileSha256 -Path $destination)
            destTextAfter    = ''
            backupExists     = (Test-Path -LiteralPath $backup -PathType Leaf)
            backupShaAfter   = (Get-EgFileSha256 -Path $backup)
            sourceStillThere = (Test-Path -LiteralPath $source -PathType Leaf)
        }
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $emitted['destTextAfter'] = [System.IO.File]::ReadAllText($destination)
        }
        if ($null -ne $result) {
            $emitted['success'] = $result.Success
            $emitted['supportRef'] = $result.SupportRef
            $emitted['exceptionTypeName'] = $result.ExceptionTypeName
            $emitted['hresult'] = $result.HResult
            $emitted['preimageState'] = $result.PreimageState
            $emitted['preimageSha256'] = $result.PreimageSha256
            $emitted['postimageSha256'] = $result.PostimageSha256
            $emitted['publicationOccurred'] = $result.PublicationOccurred
            $emitted['backupCreated'] = $result.BackupCreated
            $emitted['backupRetained'] = $result.BackupRetained
            $emitted['fieldNames'] = @($result.PSObject.Properties.Name)
        }
        $emitted | ConvertTo-Json -Depth 8 -Compress
    }
    'replaceheld' {
        # The sharing-violation case. The fixture is prepared by the caller and the
        # destination is held with an exclusive share by a SEPARATE process, which is how
        # a real sharing violation arises. This operation therefore writes no fixture file
        # and observes no destination state of its own: the caller does that after the
        # holder has released.
        $source = Join-Path $Dir 'staging.txt'
        $destination = Join-Path $Dir 'destination.txt'
        $backup = Join-Path $Dir 'destination.backup'
        $expected = Get-EgFileSha256 -Path $source
        $result = Invoke-AtomicFileReplace -SourcePath $source -DestinationPath $destination -BackupPath $backup -ExpectedSha256 $expected
        [ordered]@{
            success             = $result.Success
            supportRef          = $result.SupportRef
            exceptionTypeName   = $result.ExceptionTypeName
            hresult             = $result.HResult
            preimageState       = $result.PreimageState
            preimageSha256      = $result.PreimageSha256
            postimageSha256     = $result.PostimageSha256
            publicationOccurred = $result.PublicationOccurred
            backupCreated       = $result.BackupCreated
            backupRetained      = $result.BackupRetained
            fieldNames          = @($result.PSObject.Properties.Name)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'publish' {
        # The publish-to-absent primitive. -Value selects the destination precondition.
        $source = Join-Path $Dir 'staging.ps1'
        $destination = Join-Path $Dir 'published.ps1'
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($source, "# staging content`r`nGet-Date | Out-Null`r`n", $utf8)
        $sourceSha = Get-EgFileSha256 -Path $source
        $expected = $sourceSha
        if ($Value -eq 'stagingbad') {
            $expected = '1111111111111111111111111111111111111111111111111111111111111111'
        }
        if ($Value -eq 'presentfile') {
            [System.IO.File]::WriteAllText($destination, '# OCCUPYING FILE', $utf8)
        }
        if ($Value -eq 'occupieddirectory') {
            [void](New-Item -ItemType Directory -Path $destination)
            [System.IO.File]::WriteAllText((Join-Path $destination 'occupant.txt'), 'OCCUPANT', $utf8)
        }

        $result = Invoke-PublishToAbsentDestination -SourcePath $source -DestinationPath $destination -ExpectedSha256 $expected

        $emitted = [ordered]@{
            sourceSha           = $sourceSha
            success             = $result.Success
            supportRef          = $result.SupportRef
            exceptionTypeName   = $result.ExceptionTypeName
            hresult             = $result.HResult
            preimageState       = $result.PreimageState
            preimageSha256      = $result.PreimageSha256
            postimageSha256     = $result.PostimageSha256
            publicationOccurred = $result.PublicationOccurred
            backupCreated       = $result.BackupCreated
            backupRetained      = $result.BackupRetained
            fieldNames          = @($result.PSObject.Properties.Name)
            sourceStillThere    = (Test-Path -LiteralPath $source -PathType Leaf)
            destinationIsFile   = (Test-Path -LiteralPath $destination -PathType Leaf)
            destinationIsDir    = (Test-Path -LiteralPath $destination -PathType Container)
            destinationText     = ''
            occupantText        = ''
        }
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $emitted['destinationText'] = [System.IO.File]::ReadAllText($destination)
        }
        $occupant = Join-Path $destination 'occupant.txt'
        if (Test-Path -LiteralPath $occupant -PathType Leaf) {
            $emitted['occupantText'] = [System.IO.File]::ReadAllText($occupant)
        }
        $emitted | ConvertTo-Json -Depth 8 -Compress
    }
    'movenoreplace' {
        # The no-replace move helper, driven DIRECTLY against a pre-seeded destination.
        $source = Join-Path $Dir 'staging.txt'
        $destination = Join-Path $Dir 'published.txt'
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($source, 'STAGING', $utf8)
        [System.IO.File]::WriteAllText($destination, 'OCCUPANT', $utf8)
        $moved = Invoke-EgNoReplaceMove -SourcePath $source -DestinationPath $destination
        [ordered]@{
            ok               = $moved.Ok
            lastError        = $moved.LastError
            destinationText  = [System.IO.File]::ReadAllText($destination)
            sourceStillThere = (Test-Path -LiteralPath $source -PathType Leaf)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'classify' {
        # Build a scratch launcher root from the caller's entry manifest, then classify it.
        # -Json is the PATH to a JSON file carrying { files: [...], dirs: [...] }.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        foreach ($fileName in @($spec.files)) {
            [System.IO.File]::WriteAllText((Join-Path $Dir $fileName), ('# ' + $fileName), $utf8)
        }
        foreach ($dirName in @($spec.dirs)) {
            [void](New-Item -ItemType Directory -Path (Join-Path $Dir $dirName))
        }

        $classification = Get-EgLauncherRootClassification -LauncherRootPath $Dir
        [ordered]@{
            classA         = @($classification.ClassA)
            classB         = @($classification.ClassB)
            classC         = @($classification.ClassC)
            missingMembers = @($classification.MissingMembers)
            pass           = $classification.Pass
            supportRef     = $classification.SupportRef
            memberNames    = @(Get-EgDeployedPackageMemberNames)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'residuename' {
        # Parse and construct reserved residue names. -Json is the PATH to a JSON file
        # carrying { names: [...] }.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $parsed = @()
        foreach ($name in @($spec.names)) {
            $verdict = Test-EgResidueName -Name $name
            $parsed = $parsed + ([ordered]@{
                name        = $name
                isResidue   = $verdict.IsResidue
                kind        = $verdict.Kind
                member      = $verdict.Member
                operationId = $verdict.OperationId
            })
        }
        [ordered]@{
            parsed      = @($parsed)
            constructed = (New-EgResidueName -Kind 'backup' -Member 'launcher.ps1' -OperationId $Value)
            kinds       = @(Get-EgResidueKinds)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'manifest' {
        # Build a scratch launcher root with caller-controlled member bytes and manifest
        # text, then compare the installed package to the manifest.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        foreach ($member in @($spec.members)) {
            [System.IO.File]::WriteAllText((Join-Path $Dir $member.name), $member.content, $utf8)
        }
        foreach ($residueName in @($spec.residue)) {
            [System.IO.File]::WriteAllText((Join-Path $Dir $residueName), 'RESIDUE', $utf8)
        }
        if ($spec.writeManifest) {
            [System.IO.File]::WriteAllText(
                (Join-Path $Dir $script:EgManifestFileName), $spec.manifestRaw, $utf8)
        }

        $result = Compare-EgInstalledPackageToManifest -LauncherRootPath $Dir
        [ordered]@{
            pass             = $result.Pass
            supportRef       = $result.SupportRef
            checkNames       = @($result.Checks.Keys)
            checkOutcomes    = @($result.Checks.Values)
            manifestFileName = $script:EgManifestFileName
            manifestSchema   = $script:EgManifestSchema
            manifestMembers  = @($script:EgManifestMemberNames)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'manifestbuild' {
        # Construct and serialise a manifest from member entries, then validate its shape.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $entries = @()
        foreach ($member in @($spec.members)) {
            $entries = $entries + ([ordered]@{
                name        = $member.name
                sha256      = $member.sha256
                byte_length = [int]$member.byte_length
            })
        }
        $manifest = New-EgInstallationManifestObject -MemberEntries $entries -AdmissionCommit $Value
        $shape = Test-EgInstallationManifestShape -ManifestObject $manifest
        $serialised = ConvertTo-EgManifestJson -ManifestObject $manifest
        $again = ConvertTo-EgManifestJson -ManifestObject $manifest
        [ordered]@{
            shapePass       = $shape.Pass
            shapeSupportRef = $shape.SupportRef
            shapeCheckNames = @($shape.Checks.Keys)
            memberOrder     = @($manifest.members | ForEach-Object { $_.name })
            schemaVersion   = $manifest.schema_version
            admissionCommit = $manifest.admission_commit
            serialised      = $serialised
            deterministic   = ($serialised -ceq $again)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'rollback' {
        # A scratch installer harness. It dot-sources the library and drives the publish
        # primitives with a deliberately WRONG -ExpectedSha256, which is a legitimate
        # caller error and the only way to produce a genuine Case B (a File.Replace that
        # returned while verification failed) without adding a test-only branch, a mock
        # seam, or a compatibility fallback to production code.
        #
        # -Value selects the variant: 'caseb' drives Case B and then rolls back;
        # 'unrecoverable' deletes the retained backup out from under the transaction first.
        $memberName = 'launcher_lib.ps1'
        $destination = Join-Path $Dir $memberName
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($destination, 'PREIMAGE-BYTES', $utf8)
        $preimageSha = Get-EgFileSha256 -Path $destination

        $operationId = New-EgOperationId
        $stagingPath = Join-Path $Dir (New-EgResidueName -Kind 'staging' -Member $memberName -OperationId $operationId)
        $backupPath = Join-Path $Dir (New-EgResidueName -Kind 'backup' -Member $memberName -OperationId $operationId)
        [System.IO.File]::WriteAllText($stagingPath, 'ADVANCED-BYTES', $utf8)
        $stagingSha = Get-EgFileSha256 -Path $stagingPath

        $published = Invoke-AtomicFileReplace -SourcePath $stagingPath `
            -DestinationPath $destination -BackupPath $backupPath `
            -ExpectedSha256 '2222222222222222222222222222222222222222222222222222222222222222'

        $transaction = New-EgTransactionState -OperationId $operationId
        $entry = New-EgTransactionMember -Name $memberName -DestinationPath $destination `
            -PreimageState 'Existing' -PreimageSha256 $preimageSha `
            -StagingPath $stagingPath -BackupPath $backupPath -SourceSha256 $stagingSha
        $entry.PublicationOccurred = $published.PublicationOccurred
        $entry.BackupCreated = $published.BackupCreated
        $transaction.Members = $transaction.Members + $entry

        if ($Value -eq 'unrecoverable') {
            if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
                Remove-Item -LiteralPath $backupPath -Force
            }
        }

        $touched = @(Get-EgTouchedSet -TransactionState $transaction)
        $rollback = Invoke-EgPackageRollback -TransactionState $transaction

        $rollbackResidue = @()
        foreach ($item in @(Get-ChildItem -LiteralPath $Dir -Force)) {
            if ((Test-EgResidueName -Name $item.Name).Kind -ceq 'rollback') {
                $rollbackResidue = $rollbackResidue + $item.Name
            }
        }

        [ordered]@{
            publishSuccess       = $published.Success
            publishSupportRef    = $published.SupportRef
            publicationOccurred  = $published.PublicationOccurred
            backupCreated        = $published.BackupCreated
            touchedCount         = $touched.Count
            touchedNames         = @($touched | ForEach-Object { $_.Name })
            rollbackVerified     = $rollback.Verified
            rollbackSupportRef   = $rollback.SupportRef
            preimageSha          = $preimageSha
            destinationSha       = (Get-EgFileSha256 -Path $destination)
            destinationText      = ''
            backupStillThere     = (Test-Path -LiteralPath $backupPath -PathType Leaf)
            rollbackResidueCount = @($rollbackResidue).Count
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'touchedorder' {
        # Membership of the touched set is decided by PublicationOccurred, NOT by which
        # member failed, and the walk order is REVERSE publication order.
        $operationId = New-EgOperationId
        $transaction = New-EgTransactionState -OperationId $operationId
        foreach ($name in (Get-EgPublicationOrder)) {
            $entry = New-EgTransactionMember -Name $name `
                -DestinationPath (Join-Path $Dir $name) -PreimageState 'Absent'
            $entry.PublicationOccurred = $true
            $transaction.Members = $transaction.Members + $entry
        }
        $notAdvanced = New-EgTransactionMember -Name $script:EgManifestFileName `
            -DestinationPath (Join-Path $Dir $script:EgManifestFileName) -PreimageState 'Absent'
        $notAdvanced.PublicationOccurred = $false
        $transaction.Members = $transaction.Members + $notAdvanced

        $touched = @(Get-EgTouchedSet -TransactionState $transaction)
        [ordered]@{
            publicationOrder = @(Get-EgPublicationOrder)
            touchedNames     = @($touched | ForEach-Object { $_.Name })
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'cleanup' {
        # Post-acceptance backup cleanup, driven directly against a scratch transaction
        # record. -Value 'locked' holds the first recorded backup with a share mode that
        # forbids deletion, which makes the reap of that one file fail deterministically.
        $operationId = New-EgOperationId
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        $transaction = New-EgTransactionState -OperationId $operationId
        $backupPaths = @()
        foreach ($name in (Get-EgPublicationOrder)) {
            $backupPath = Join-Path $Dir (New-EgResidueName -Kind 'backup' -Member $name -OperationId $operationId)
            [System.IO.File]::WriteAllText($backupPath, ('PREIMAGE-OF-' + $name), $utf8)
            $entry = New-EgTransactionMember -Name $name `
                -DestinationPath (Join-Path $Dir $name) -PreimageState 'Existing' `
                -PreimageSha256 ('0' * 64) -BackupPath $backupPath
            $transaction.Members = $transaction.Members + $entry
            $backupPaths = $backupPaths + $backupPath
        }

        # A validly named backup from a DIFFERENT transaction. Cleanup must never touch it.
        $foreignName = New-EgResidueName -Kind 'backup' -Member 'launcher.ps1' `
            -OperationId '11111111-2222-3333-4444-555555555555'
        $foreignPath = Join-Path $Dir $foreignName
        [System.IO.File]::WriteAllText($foreignPath, 'FOREIGN-RESIDUE', $utf8)

        $held = $null
        $backupsRemaining = -1
        $supportRef = ''
        $firstStillThere = $false
        $secondStillThere = $false
        $foreignStillThere = $false
        $foreignText = ''
        try {
            if ($Value -eq 'locked') {
                $held = [System.IO.File]::Open($backupPaths[0], [System.IO.FileMode]::Open,
                    [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
            }
            $cleanup = Invoke-EgPostAcceptanceBackupCleanup -TransactionState $transaction
            $backupsRemaining = $cleanup.BackupsRemaining
            $supportRef = $cleanup.SupportRef
            $firstStillThere = (Test-Path -LiteralPath $backupPaths[0] -PathType Leaf)
            $secondStillThere = (Test-Path -LiteralPath $backupPaths[1] -PathType Leaf)
            $foreignStillThere = (Test-Path -LiteralPath $foreignPath -PathType Leaf)
            if ($foreignStillThere) {
                $foreignText = [System.IO.File]::ReadAllText($foreignPath)
            }
        }
        finally {
            if ($null -ne $held) { $held.Dispose() }
        }

        [ordered]@{
            backupsRemaining  = $backupsRemaining
            supportRef        = $supportRef
            firstStillThere   = $firstStillThere
            secondStillThere  = $secondStillThere
            foreignStillThere = $foreignStillThere
            foreignText       = $foreignText
            retainedName      = [System.IO.Path]::GetFileName($backupPaths[0])
            retainedIsClassB  = (Test-EgResidueName -Name ([System.IO.Path]::GetFileName($backupPaths[0]))).IsResidue
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'credmake' {
        # Build a synthetic PSCredential from per-run throwaway values and export it with
        # the SAME user-bound mechanism the launcher imports with. -Json is the PATH to a
        # JSON file carrying { username, password }. The values are never real credentials
        # and never leave the runner's temporary directory.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $secure = ConvertTo-SecureString -String $spec.password -AsPlainText -Force
        $credential = New-Object System.Management.Automation.PSCredential($spec.username, $secure)
        $credential | Export-Clixml -LiteralPath $Path
        [ordered]@{ exported = (Test-Path -LiteralPath $Path -PathType Leaf) } |
            ConvertTo-Json -Depth 4 -Compress
    }
    'credimport' {
        # Import the artefact at -Path and derive the three viability booleans. No
        # credential value, length, prefix, suffix, or hash is ever emitted.
        $imported = Import-EgLauncherCredential -CredentialPath $Path
        $viability = Test-EgCredentialViability -Credential $imported.Credential
        [ordered]@{
            importSuccess       = $imported.Success
            importSupportRef    = $imported.SupportRef
            credentialIsNull    = ($null -eq $imported.Credential)
            credentialImportOk  = $viability.CredentialImportOk
            usernameNonEmpty    = $viability.UsernameNonEmpty
            passwordNonEmpty    = $viability.PasswordNonEmpty
            viabilitySupportRef = $viability.SupportRef
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'credinject' {
        # The full injection sequence: snapshot, set at PROCESS SCOPE ONLY, run a child
        # stub, then restore exactly on the finally-equivalent path. -Path is the credential
        # artefact, -Path2 the PowerShell host to run the stub with, -Path3 the stub,
        # -Dir the scratch directory, and -Value2 an optional pre-set username value.
        $names = @($script:EgCredentialVariableNames)

        if ($Value2 -ne '') {
            [System.Environment]::SetEnvironmentVariable($names[0], $Value2, 'Process')
        }
        else {
            [System.Environment]::SetEnvironmentVariable($names[0], $null, 'Process')
        }
        [System.Environment]::SetEnvironmentVariable($names[1], $null, 'Process')

        $before = [ordered]@{}
        foreach ($name in $names) {
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            $before[$name] = [ordered]@{ present = ($null -ne $observed); value = ([string]$observed) }
        }

        $imported = Import-EgLauncherCredential -CredentialPath $Path
        $viability = Test-EgCredentialViability -Credential $imported.Credential

        $childOutPath = Join-Path $Dir 'child_observed.json'
        $childExit = -1
        $restorePass = $false
        $restoreSupportRef = ''

        if ($viability.CredentialImportOk -and $viability.UsernameNonEmpty -and $viability.PasswordNonEmpty) {
            $plainUser = $imported.Credential.UserName
            $bstr = [System.IntPtr]::Zero
            $plainPassword = ''
            try {
                $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($imported.Credential.Password)
                $plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            }
            finally {
                if ($bstr -ne [System.IntPtr]::Zero) {
                    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
                }
            }

            $variables = [ordered]@{}
            $variables[$names[0]] = $plainUser
            $variables[$names[1]] = $plainPassword

            $stubArguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-File', $Path3, '-OutPath', $childOutPath)
            $injected = Invoke-EgWithInjectedProcessEnvironment -Variables $variables -Body {
                $started = Start-Process -FilePath $Path2 -ArgumentList $stubArguments `
                    -NoNewWindow -Wait -PassThru
                $started.ExitCode
            }
            $childExit = $injected.BodyResult
            $restorePass = $injected.Restore.Pass
            $restoreSupportRef = $injected.Restore.SupportRef
            $plainPassword = ''
        }

        $after = [ordered]@{}
        foreach ($name in $names) {
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            $after[$name] = [ordered]@{ present = ($null -ne $observed); value = ([string]$observed) }
        }

        $userScope = [ordered]@{}
        $machineScope = [ordered]@{}
        foreach ($name in $names) {
            $userScope[$name] = ($null -ne [System.Environment]::GetEnvironmentVariable($name, 'User'))
            $machineScope[$name] = ($null -ne [System.Environment]::GetEnvironmentVariable($name, 'Machine'))
        }

        $childObserved = ''
        if (Test-Path -LiteralPath $childOutPath -PathType Leaf) {
            $childObserved = [System.IO.File]::ReadAllText($childOutPath)
        }

        [ordered]@{
            importSuccess     = $imported.Success
            importSupportRef  = $imported.SupportRef
            childExit         = $childExit
            childObserved     = $childObserved
            childStubRan      = (Test-Path -LiteralPath $childOutPath -PathType Leaf)
            restorePass       = $restorePass
            restoreSupportRef = $restoreSupportRef
            before            = $before
            after             = $after
            userScope         = $userScope
            machineScope      = $machineScope
        } | ConvertTo-Json -Depth 8 -Compress
    }
    default {
        throw ('unknown probe operation: ' + $Op)
    }
}
"""


def _named_args(kwargs):
    """Map python keyword arguments to PowerShell -Name value pairs."""
    args = []
    for name, value in kwargs.items():
        args.extend(["-" + name[0].upper() + name[1:], str(value)])
    return args


def write_probe(tmp):
    """Materialise the shared probe script inside a scratch directory."""
    path = Path(tmp) / "eg_probe.ps1"
    path.write_text(PROBE_SCRIPT, encoding="utf-8")
    return path


def probe(exe, op, tmp, **kwargs):
    """Run the shared probe script with -Lib <LIB> -Op <op> and named arguments."""
    script = write_probe(tmp)
    args = ["-Lib", str(LIB), "-Op", op]
    args.extend(_named_args(kwargs))
    return run_ps(exe, script, *args)


def write_json(tmp, name, payload):
    """Write a JSON payload to a scratch file and return its path.

    Complex probe inputs travel as a FILE path rather than as a command-line argument, so
    no test depends on how a shell happens to quote embedded double quotes.
    """
    path = Path(tmp) / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def probe_json(exe, op, tmp, **kwargs):
    """probe() plus a return-code assertion plus json.loads of standard output."""
    completed = probe(exe, op, tmp, **kwargs)
    if completed.returncode != 0:
        raise AssertionError(
            "probe %r exited %d\nstdout:\n%s\nstderr:\n%s"
            % (op, completed.returncode, completed.stdout, completed.stderr)
        )
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------------------
# Separate-process exclusive lock holder
# --------------------------------------------------------------------------------------
# The sharing-violation class must be induced from ANOTHER process. Holding the exclusive
# share in the same process that then calls the library is not a faithful fixture: the
# library's own preimage hash of the locked destination was observed to disturb the
# in-process handle, so the replacement could succeed against a destination the fixture
# believed it had locked. A separate holder process also models production, where a
# sharing violation comes from a different process altogether.

LOCK_HOLDER_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$ReadyPath,
    [Parameter(Mandatory)][string]$ReleasePath,
    [int]$TimeoutSeconds = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$utf8 = New-Object System.Text.UTF8Encoding($false)
$stream = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
try {
    [System.IO.File]::WriteAllText($ReadyPath, 'ready', $utf8)
    $deadline = [System.DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ((-not (Test-Path -LiteralPath $ReleasePath)) -and ([System.DateTime]::UtcNow -lt $deadline)) {
        Start-Sleep -Milliseconds 40
    }
}
finally {
    $stream.Dispose()
}
"""


class ExclusiveLockHolder:
    """Hold an exclusive share on a path from a separate PowerShell process."""

    def __init__(self, exe, tmp, target):
        self.exe = exe
        self.tmp = Path(tmp)
        self.target = Path(target)
        self.ready = self.tmp / "lock_ready.sentinel"
        self.release = self.tmp / "lock_release.sentinel"
        self.process = None

    def __enter__(self):
        script = self.tmp / "eg_lock_holder.ps1"
        script.write_text(LOCK_HOLDER_SCRIPT, encoding="utf-8")
        for sentinel in (self.ready, self.release):
            if sentinel.exists():
                sentinel.unlink()
        self.process = subprocess.Popen(
            [
                self.exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script),
                "-Path", str(self.target),
                "-ReadyPath", str(self.ready),
                "-ReleasePath", str(self.release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.ready.exists():
                return self
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                raise AssertionError(
                    "the lock holder exited before signalling ready\nstdout:\n%s\nstderr:\n%s"
                    % (out, err)
                )
            time.sleep(0.02)
        raise AssertionError("the lock holder did not signal ready in time")

    def __exit__(self, exc_type, exc, tb):
        try:
            self.release.write_text("release", encoding="utf-8")
        finally:
            if self.process is not None:
                try:
                    self.process.communicate(timeout=120)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.communicate(timeout=60)
        return False


# --------------------------------------------------------------------------------------
# PowerShell abstract-syntax-tree inspection, used by the Tier C guards
# --------------------------------------------------------------------------------------

PARSE_INSPECTOR = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Target)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$errors = $null
$tokens = $null
[System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$tokens, [ref]$errors) | Out-Null
$count = 0
if ($null -ne $errors) { $count = @($errors).Count }
[pscustomobject]@{ parseErrors = $count } | ConvertTo-Json -Depth 4 -Compress
"""


GIT_GUARD_INSPECTOR = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Target)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$tokens, [ref]$errors)
$parseErrors = 0
if ($null -ne $errors) { $parseErrors = @($errors).Count }

$commands = @($ast.FindAll(
    { param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true))

# Plain arrays, deliberately not System.Collections.Generic.List: on Windows PowerShell
# 5.1 the array subexpression over an empty List[object] breaks the [ordered] cast that
# builds the emitted result.
$gitCommandNames = @()
$callSites = @()

foreach ($command in $commands) {
    $commandName = $command.GetCommandName()
    if ($null -ne $commandName) {
        $lowered = $commandName.ToLowerInvariant()
        $leaf = $lowered
        $normalised = $lowered.Replace('/', '\')
        $separator = $normalised.LastIndexOf('\')
        if ($separator -ge 0) { $leaf = $normalised.Substring($separator + 1) }
        if ($leaf -eq 'git' -or $leaf -eq 'git.exe') {
            $gitCommandNames = $gitCommandNames + $commandName
        }
    }

    if ($null -ne $commandName -and $commandName -eq 'Invoke-GovernedGit') {
        $elements = @($command.CommandElements)
        $argumentsValue = $null
        for ($index = 0; $index -lt $elements.Count; $index++) {
            $element = $elements[$index]
            if ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
                if ($element.ParameterName -eq 'Arguments') {
                    if ($null -ne $element.Argument) {
                        $argumentsValue = $element.Argument
                    }
                    elseif ($index + 1 -lt $elements.Count) {
                        $argumentsValue = $elements[$index + 1]
                    }
                }
            }
        }

        $isArrayShaped = $false
        $firstElement = ''
        $hasFirstElement = $false
        if ($null -ne $argumentsValue) {
            $candidate = $argumentsValue
            if ($candidate -is [System.Management.Automation.Language.BinaryExpressionAst]) {
                $candidate = $candidate.Left
            }
            if ($candidate -is [System.Management.Automation.Language.ArrayExpressionAst] -or
                $candidate -is [System.Management.Automation.Language.ArrayLiteralAst]) {
                $isArrayShaped = $true
            }
            $constants = @($argumentsValue.FindAll(
                { param($node) $node -is [System.Management.Automation.Language.StringConstantExpressionAst] },
                $true))
            if ($constants.Count -ge 1) {
                $ordered = @($constants | Sort-Object { $_.Extent.StartOffset })
                $firstElement = $ordered[0].Value
                $hasFirstElement = $true
            }
        }

        $siteEntry = [ordered]@{
            isArrayShaped   = $isArrayShaped
            hasFirstElement = $hasFirstElement
            firstElement    = $firstElement
        }
        $callSites = $callSites + $siteEntry
    }
}

[ordered]@{
    parseErrors     = $parseErrors
    gitCommandCount = @($gitCommandNames).Count
    gitCommandNames = @($gitCommandNames)
    callSiteCount   = @($callSites).Count
    callSites       = @($callSites)
} | ConvertTo-Json -Depth 8 -Compress
"""


BACKUP_GUARD_INSPECTOR = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Target)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$tokens, [ref]$errors)
$parseErrors = 0
if ($null -ne $errors) { $parseErrors = @($errors).Count }

# A backup argument is PROVEN when it is a parameter declared both [Parameter(Mandatory)]
# and [ValidateNotNullOrEmpty()], or a variable assigned from such a parameter, or a
# non-empty string literal. Anything else is unproven and is a defect.
$proven = @()
$parameters = @($ast.FindAll(
    { param($node) $node -is [System.Management.Automation.Language.ParameterAst] }, $true))
foreach ($parameter in $parameters) {
    $hasMandatory = $false
    $hasNotNullOrEmpty = $false
    foreach ($attribute in @($parameter.Attributes)) {
        if ($attribute -is [System.Management.Automation.Language.AttributeAst]) {
            $attributeName = $attribute.TypeName.Name
            if ($attributeName -eq 'ValidateNotNullOrEmpty') { $hasNotNullOrEmpty = $true }
            if ($attributeName -eq 'Parameter') {
                foreach ($named in @($attribute.NamedArguments)) {
                    if ($named.ArgumentName -eq 'Mandatory') { $hasMandatory = $true }
                }
            }
        }
    }
    if ($hasMandatory) {
        if ($hasNotNullOrEmpty) {
            $proven = $proven + $parameter.Name.VariablePath.UserPath
        }
    }
}

$assignments = @($ast.FindAll(
    { param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true))
foreach ($assignment in $assignments) {
    if ($assignment.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
        $rightText = $assignment.Right.Extent.Text.TrimStart('$')
        foreach ($provenName in $proven) {
            if ($rightText -eq $provenName) {
                $proven = $proven + $assignment.Left.VariablePath.UserPath
            }
        }
    }
}

$replaceCallCount = 0
$literalNullThird = 0
$literalEmptyThird = 0
$unprovenThird = 0

$invocations = @($ast.FindAll(
    { param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true))
foreach ($invocation in $invocations) {
    $memberName = ''
    if ($invocation.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        $memberName = $invocation.Member.Value
    }
    if ($memberName -ne 'Replace') { continue }
    if ($invocation.Expression.Extent.Text -notmatch '\[System\.IO\.File\]') { continue }

    $replaceCallCount++
    $arguments = @($invocation.Arguments)
    if ($arguments.Count -lt 3) {
        $unprovenThird++
        continue
    }
    $third = $arguments[2]
    if ($third -is [System.Management.Automation.Language.VariableExpressionAst]) {
        $name = $third.VariablePath.UserPath
        if ($name -eq 'null') {
            $literalNullThird++
            continue
        }
        if ($proven -contains $name) { continue }
        $unprovenThird++
        continue
    }
    if ($third -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        if ($third.Value -eq '') {
            $literalEmptyThird++
            continue
        }
        continue
    }
    $unprovenThird++
}

[ordered]@{
    parseErrors       = $parseErrors
    provenNames       = @($proven)
    replaceCallCount  = $replaceCallCount
    literalNullThird  = $literalNullThird
    literalEmptyThird = $literalEmptyThird
    unprovenThird     = $unprovenThird
} | ConvertTo-Json -Depth 8 -Compress
"""


FUNCTION_BODY_COMMAND_INSPECTOR = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Target,
    [Parameter(Mandatory)][string]$FunctionName,
    [Parameter(Mandatory)][string]$ForbiddenCommand
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$tokens, [ref]$errors)
$parseErrors = 0
if ($null -ne $errors) { $parseErrors = @($errors).Count }

$functions = @($ast.FindAll(
    { param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))

$found = $false
$forbiddenCount = 0
foreach ($function in $functions) {
    if ($function.Name -ne $FunctionName) { continue }
    $found = $true
    $commands = @($function.Body.FindAll(
        { param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true))
    foreach ($command in $commands) {
        $name = $command.GetCommandName()
        if ($null -ne $name) {
            if ($name -ieq $ForbiddenCommand) { $forbiddenCount++ }
        }
    }
}

[ordered]@{
    parseErrors     = $parseErrors
    functionFound   = $found
    forbiddenCount  = $forbiddenCount
} | ConvertTo-Json -Depth 4 -Compress
"""


def run_inspector(exe, tmp, source, **kwargs):
    """Write an inspector script into a scratch directory, run it, and parse its JSON."""
    script = Path(tmp) / "eg_inspector.ps1"
    script.write_text(source, encoding="utf-8")
    completed = run_ps(exe, script, *_named_args(kwargs))
    if completed.returncode != 0:
        raise AssertionError(
            "inspector exited %d\nstdout:\n%s\nstderr:\n%s"
            % (completed.returncode, completed.stdout, completed.stderr)
        )
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------------------
# Tier base classes
# --------------------------------------------------------------------------------------

class TierABase(unittest.TestCase):
    """Portable library tests. Skips when no PowerShell host is available."""

    @classmethod
    def setUpClass(cls):
        if ANY_PS is None:
            raise unittest.SkipTest("no PowerShell host found (pwsh or powershell)")


class TierBBase(unittest.TestCase):
    """Compatibility-boundary tests pinned to Windows PowerShell 5.1."""

    @classmethod
    def setUpClass(cls):
        if DESKTOP_PS is None:
            raise unittest.SkipTest(
                "Windows PowerShell 5.1 boundary interpreter powershell.exe "
                "(PSEdition Desktop) is not available on this host"
            )


class TierCBase(unittest.TestCase):
    """Static guard tests. Text-only assertions need no interpreter."""


# --------------------------------------------------------------------------------------
# Windows PowerShell 5.1 compatibility guard
# --------------------------------------------------------------------------------------
# Each entry is a prohibited construct from the plan's Global Constraints compatibility
# table, expressed as a regular expression matched over non-comment lines of the committed
# runtime files. The plan records the required 5.1-safe form for each.
POWERSHELL_7_ONLY_PATTERNS = {
    "pipeline_chain_and": r"&&",
    "pipeline_chain_or": r"\|\|",
    "ternary_conditional": r"\s\?\s",
    "null_coalescing": r"\?\?",
    "null_conditional_member": r"\?\.",
    "null_conditional_index": r"\?\[",
    "native_stderr_merge": r"2>&1",
    "convertfrom_json_as_hashtable": r"-AsHashtable",
    "join_path_additional_child_path": r"-AdditionalChildPath",
    "is_windows_automatic_variable": r"\$IsWindows\b",
    "new_item_force": r"New-Item[^\r\n]*-Force",
    "set_or_add_content": r"\b(?:Set-Content|Add-Content)\b",
}


def non_comment_lines(text):
    """Yield (line_number, line) for every line that is not a whole-line comment."""
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        yield number, line


class RuntimeHarnessAndStructuralGuards(TierCBase):
    """Task 1: the harness, the tiering, and the guards every later task depends on."""

    def test_runtime_library_exists_and_parses_cleanly(self):
        """EGRT-T17: every committed runtime .ps1 parses with zero errors."""
        self.assertTrue(LIB.is_file(), "runtime/launcher_lib.ps1 must exist")
        if ANY_PS is None:
            self.skipTest("no PowerShell host found (pwsh or powershell)")
        with TemporaryScratch() as tmp:
            for target in existing_runtime_ps1_files():
                with self.subTest(runtime_file=target.name):
                    result = run_inspector(ANY_PS, tmp, PARSE_INSPECTOR, target=target)
                    self.assertEqual(
                        0,
                        result["parseErrors"],
                        "%s must parse with zero errors" % target.name,
                    )

    def test_ci_requires_the_desktop_boundary_interpreter(self):
        """The Tier B boundary can never be silently unexercised inside the gate."""
        if os.environ.get("CI"):
            self.assertIsNotNone(
                DESKTOP_PS,
                "CI is set but the Windows PowerShell 5.1 boundary interpreter "
                "powershell.exe (PSEdition Desktop) was not found; Tier B must not be "
                "silently skipped in the gate",
            )
        else:
            self.assertTrue(True)

    def test_committed_runtime_files_avoid_powershell_7_only_syntax(self):
        """Every committed runtime .ps1 must execute correctly under PSEdition Desktop."""
        targets = existing_runtime_ps1_files()
        self.assertTrue(targets, "at least runtime/launcher_lib.ps1 must exist")
        for target in targets:
            text = target.read_text(encoding="utf-8")
            for name, pattern in POWERSHELL_7_ONLY_PATTERNS.items():
                compiled = re.compile(pattern)
                for number, line in non_comment_lines(text):
                    with self.subTest(runtime_file=target.name, construct=name):
                        self.assertIsNone(
                            compiled.search(line),
                            "%s line %d uses the PowerShell 7 only construct %r: %s"
                            % (target.name, number, name, line.strip()),
                        )


class GovernedGitResultContract(TierABase):
    """Task 2: the structured Git result contract that closes the Run119 output-shape defect.

    Design sections 10.1 and 18.1. A helper that returns native command output without
    preserving collection shape collapsed single-line output to a scalar string, so
    indexing [0] on the branch name main produced m, and collapsed successful zero-line
    output to $null, making a successful empty read indistinguishable from a failure.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._scratch = TemporaryScratch()
        tmp = cls._scratch.__enter__()
        cls.tmp = tmp
        cls.repo = init_scratch_repo(tmp / "repo")
        cls.result = probe_json(ANY_PS, "git", tmp, dir=cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls._scratch.__exit__(None, None, None)

    def test_governed_git_preserves_one_line_output(self):
        """EGRT-T01: Lines.Count is 1 and Lines[0] is main, never the character m."""
        self.assertTrue(self.result["oneLineSuccess"])
        self.assertEqual(1, self.result["oneLineCount"])
        self.assertEqual("main", self.result["oneLineFirst"])
        self.assertNotEqual(
            "m",
            self.result["oneLineFirst"],
            "indexing [0] must not yield the first character of a collapsed scalar",
        )
        self.assertFalse(
            self.result["oneLineIsBareString"],
            "Lines must always be a collection, never a bare string",
        )
        self.assertEqual("", self.result["oneLineSupportRef"])

    def test_governed_git_preserves_successful_zero_line_output(self):
        """EGRT-T02: a zero-line success stays a success and is never $null."""
        self.assertTrue(self.result["zeroLineSuccess"])
        self.assertEqual(0, self.result["zeroLineCount"])
        self.assertFalse(self.result["zeroLineIsNull"], "Lines must never be $null")
        self.assertEqual(0, self.result["zeroLineExit"])

    def test_governed_git_distinguishes_a_non_zero_exit(self):
        """EGRT-T03: a non-zero exit is distinguishable from a successful empty read."""
        self.assertFalse(self.result["failSuccess"])
        self.assertNotEqual(0, self.result["failExit"])
        self.assertEqual(0, self.result["failLineCount"])
        self.assertEqual(
            "EG_LAUNCHER_GIT_INVOCATION_FAILED", self.result["failSupportRef"]
        )
        # Both carry zero lines, so Success and SupportRef are what separate them.
        self.assertNotEqual(
            self.result["zeroLineSuccess"],
            self.result["failSuccess"],
            "a successful empty read must remain distinguishable from a failure",
        )


GOVERNED_GIT_ENVIRONMENT_NAMES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CEILING_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_ATTR_NOSYSTEM",
    "GIT_PAGER",
    "GIT_EDITOR",
    "GIT_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_TERMINAL_PROMPT",
)

SEEDED_AMBIENT_NAMES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL")


def same_path(left, right):
    """Compare two filesystem paths for identity, tolerating separator and case form."""
    return Path(str(left)).resolve() == Path(str(right)).resolve()


class AmbientGitEnvironmentIsolation(TierABase):
    """Task 3: ambient Git variables cannot redirect a governed read, and are restored exactly.

    Git binding is security-sensitive: an ambient variable can silently redirect a
    governed read to a different repository, which would let a runtime-binding check pass
    against the wrong tree (design section 10.2).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._scratch = TemporaryScratch()
        tmp = cls._scratch.__enter__()
        cls.tmp = tmp
        cls.real = init_scratch_repo(tmp / "real")
        cls.decoy = init_scratch_repo(tmp / "decoy")
        (cls.decoy / "decoy.gitconfig").write_text("[core]\n\tquotepath = false\n", encoding="utf-8")
        cls.result = probe_json(ANY_PS, "gitambient", tmp, dir=cls.real, dir2=cls.decoy)

    @classmethod
    def tearDownClass(cls):
        cls._scratch.__exit__(None, None, None)

    def test_the_governed_environment_name_set_matches_the_design(self):
        """Design section 10.2 names exactly nineteen variables, in a fixed order."""
        self.assertEqual(19, self.result["nameCount"])
        self.assertEqual(list(GOVERNED_GIT_ENVIRONMENT_NAMES), self.result["names"])

    def test_ambient_git_variables_cannot_redirect_a_governed_read(self):
        """EGRT-T04: GIT_DIR, GIT_WORK_TREE, and GIT_CONFIG_GLOBAL at a decoy are inert."""
        self.assertTrue(self.result["boundSuccess"])
        self.assertTrue(
            same_path(self.result["boundTopLevel"], self.real),
            "the governed read resolved to %r rather than the real repository %r"
            % (self.result["boundTopLevel"], str(self.real)),
        )
        self.assertFalse(
            same_path(self.result["boundTopLevel"], self.decoy),
            "the governed read must never resolve to the decoy repository",
        )

    def test_git_environment_is_restored_exactly_including_absent_names(self):
        """EGRT-T05: exact restoration, with absent names staying absent."""
        before = self.result["before"]
        after = self.result["after"]
        for name in GOVERNED_GIT_ENVIRONMENT_NAMES:
            with self.subTest(variable=name):
                self.assertEqual(
                    before[name]["present"],
                    after[name]["present"],
                    "%s presence changed across the governed read" % name,
                )
                self.assertEqual(
                    before[name]["value"],
                    after[name]["value"],
                    "%s value changed across the governed read" % name,
                )
        for name in SEEDED_AMBIENT_NAMES:
            with self.subTest(seeded=name):
                self.assertTrue(after[name]["present"], "%s must be restored" % name)
                self.assertNotEqual("", after[name]["value"])
        absent_before = [
            name for name in GOVERNED_GIT_ENVIRONMENT_NAMES if not before[name]["present"]
        ]
        self.assertTrue(
            absent_before,
            "the fixture must include names that were absent before the call",
        )
        for name in absent_before:
            with self.subTest(absent=name):
                self.assertFalse(
                    after[name]["present"],
                    "%s was absent beforehand and must not be present and empty" % name,
                )

    def test_a_snapshot_restores_an_absent_name_by_removing_it(self):
        """EGRT-T05 support: restoration removes rather than blanks an absent name."""
        with TemporaryScratch() as tmp:
            result = probe_json(ANY_PS, "envsnapshot", tmp)
        self.assertTrue(result["restorePass"])
        self.assertEqual("", result["restoreSupportRef"])
        self.assertFalse(
            result["absentPresent"],
            "a name recorded absent must be REMOVED, never set to an empty string",
        )
        self.assertEqual("original", result["presentValue"])


GIT_ALLOWED_SUBCOMMANDS = ("rev-parse", "symbolic-ref", "ls-files", "diff", "status")

# Design section 10.3: every Git operation that fetches, updates, moves, or rewrites state
# is prohibited outright from the runtime layer.
GIT_PROHIBITED_SUBCOMMANDS = (
    "fetch", "pull", "clone", "remote", "reset", "rebase", "merge", "checkout",
    "switch", "restore", "cherry-pick", "revert", "stash", "clean", "add", "rm",
    "commit", "tag", "push", "gc", "worktree",
)


class ReadOnlyGitAllowlist(TierABase):
    """Task 4: the runtime layer performs zero Git network operations and zero mutations."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._scratch = TemporaryScratch()
        tmp = cls._scratch.__enter__()
        cls.tmp = tmp
        cls.repo = init_scratch_repo(tmp / "repo")
        cls.result = probe_json(ANY_PS, "gitforbidden", tmp, dir=cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls._scratch.__exit__(None, None, None)

    def test_forbidden_git_subcommand_is_refused_without_a_process(self):
        """A prohibited subcommand is refused by the gate before any process starts."""
        self.assertFalse(self.result["forbiddenSuccess"])
        self.assertEqual(-1, self.result["forbiddenExit"])
        self.assertEqual(0, self.result["forbiddenLineCount"])
        self.assertEqual(
            "EG_LAUNCHER_GIT_SUBCOMMAND_FORBIDDEN", self.result["forbiddenSupportRef"]
        )

    def test_the_allowlist_is_exactly_the_five_read_only_subcommands(self):
        """Design section 10.3 permits only rev-parse, symbolic-ref, ls-files, diff, status."""
        self.assertEqual(5, self.result["allowedCount"])
        self.assertEqual(list(GIT_ALLOWED_SUBCOMMANDS), self.result["allowed"])
        self.assertTrue(self.result["revParseAllowed"])
        self.assertFalse(self.result["fetchAllowed"])
        self.assertFalse(self.result["emptyAllowed"])
        self.assertFalse(
            self.result["upperCaseAllowed"],
            "membership must be exact and case-sensitive",
        )


class ReadOnlyGitAllowlistStaticGuard(TierCBase):
    """Task 4, Tier C: EGRT-T39 over the committed runtime files."""

    def _inspect(self):
        if ANY_PS is None:
            self.skipTest("no PowerShell host found (pwsh or powershell)")
        results = {}
        with TemporaryScratch() as tmp:
            for target in existing_runtime_ps1_files():
                results[target.name] = run_inspector(
                    ANY_PS, tmp, GIT_GUARD_INSPECTOR, target=target
                )
        return results

    def test_no_committed_runtime_file_invokes_a_forbidden_git_subcommand(self):
        """EGRT-T39: git is reachable only through the governed function and its allowlist."""
        results = self._inspect()
        self.assertTrue(results, "at least runtime/launcher_lib.ps1 must exist")
        for name, result in results.items():
            with self.subTest(runtime_file=name):
                self.assertEqual(0, result["parseErrors"])
                self.assertEqual(
                    0,
                    result["gitCommandCount"],
                    "%s invokes git directly as a command: %r"
                    % (name, result["gitCommandNames"]),
                )
                for site in result["callSites"]:
                    self.assertTrue(
                        site["isArrayShaped"],
                        "%s passes -Arguments in a shape that is not an array" % name,
                    )
                    self.assertTrue(
                        site["hasFirstElement"],
                        "%s passes -Arguments whose first element is not a string constant"
                        % name,
                    )
                    self.assertIn(
                        site["firstElement"],
                        GIT_ALLOWED_SUBCOMMANDS,
                        "%s passes the non-allowlisted subcommand %r"
                        % (name, site["firstElement"]),
                    )
                    self.assertNotIn(
                        site["firstElement"],
                        GIT_PROHIBITED_SUBCOMMANDS,
                        "%s passes the prohibited subcommand %r"
                        % (name, site["firstElement"]),
                    )

    def test_the_prohibited_subcommand_list_is_disjoint_from_the_allowlist(self):
        """The two lists are read from the design and must not overlap."""
        self.assertEqual(21, len(GIT_PROHIBITED_SUBCOMMANDS))
        self.assertEqual(
            set(),
            set(GIT_ALLOWED_SUBCOMMANDS) & set(GIT_PROHIBITED_SUBCOMMANDS),
        )


PUBLICATION_RESULT_FIELDS = (
    "Success",
    "SupportRef",
    "ExceptionTypeName",
    "HResult",
    "PreimageState",
    "PreimageSha256",
    "PostimageSha256",
    "PublicationOccurred",
    "BackupCreated",
    "BackupRetained",
)

# Design section 7.3: every one of these classes throws BEFORE publication, so each is a
# Case A failure under design section 7.2.1 and each leaves the destination byte-identical.
# The sharing-violation class is induced separately, because it needs a holder process.
THROW_BEFORE_PUBLICATION_CASES = ("readonly", "otherdirbackup")

SOURCE_TEXT = "SOURCE-CONTENT"
DESTINATION_PREIMAGE_TEXT = "DESTINATION-PREIMAGE"


def sha256_of(path):
    """Lowercase hexadecimal SHA-256 of a file, or the empty string when it is absent."""
    import hashlib

    path = Path(path)
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_probe(exe, case):
    """Run one atomic-replacement fixture case and return its observation."""
    with TemporaryScratch() as tmp:
        work = tmp / "work"
        work.mkdir()
        return probe_json(exe, "replace", tmp, dir=work, value=case)


def replace_probe_with_held_destination(exe):
    """Induce the sharing-violation class with the destination held by another process."""
    with TemporaryScratch() as tmp:
        work = tmp / "work"
        work.mkdir()
        source = work / "staging.txt"
        destination = work / "destination.txt"
        backup = work / "destination.backup"
        source.write_text(SOURCE_TEXT, encoding="utf-8")
        destination.write_text(DESTINATION_PREIMAGE_TEXT, encoding="utf-8")
        preimage_sha = sha256_of(destination)
        source_sha = sha256_of(source)

        with ExclusiveLockHolder(exe, tmp, destination):
            observed = probe_json(exe, "replaceheld", tmp, dir=work)

        # Disk state is observed only after the holder has released, so the observation
        # itself never contends with the lock it is describing.
        observed["preimageShaSetup"] = preimage_sha
        observed["sourceSha"] = source_sha
        observed["destShaAfter"] = sha256_of(destination)
        observed["destTextAfter"] = (
            destination.read_text(encoding="utf-8") if destination.is_file() else ""
        )
        observed["backupExists"] = backup.is_file()
        observed["backupShaAfter"] = sha256_of(backup)
        observed["refused"] = False
        return observed


class AtomicFileReplaceContractMixin:
    """The design section 7.2 replacement contract, asserted on one interpreter."""

    exe = None

    def test_valid_explicit_backup_replacement_succeeds(self):
        """EGRT-T06: a valid explicit-backup replacement succeeds and carries the source."""
        observed = replace_probe(self.exe, "ok")
        self.assertFalse(observed["refused"])
        self.assertTrue(observed["success"], observed.get("supportRef"))
        self.assertEqual("", observed["supportRef"])
        self.assertEqual("", observed["exceptionTypeName"])
        self.assertEqual("", observed["hresult"])
        self.assertEqual("Existing", observed["preimageState"])
        self.assertEqual(SOURCE_TEXT, observed["destTextAfter"])
        self.assertEqual(observed["sourceSha"], observed["destShaAfter"])
        self.assertTrue(observed["publicationOccurred"])
        self.assertTrue(observed["backupCreated"])
        self.assertTrue(observed["backupRetained"])
        self.assertTrue(observed["backupExists"])
        self.assertEqual(
            observed["preimageShaSetup"],
            observed["backupShaAfter"],
            "the retained backup must carry the exact preimage bytes",
        )
        self.assertEqual(list(PUBLICATION_RESULT_FIELDS), observed["fieldNames"])

    def test_null_and_empty_backup_arguments_are_refused(self):
        """EGRT-T07: the mandatory-parameter contract refuses both, on every runtime."""
        for case in ("nullbackup", "emptybackup"):
            with self.subTest(backup_argument=case):
                observed = replace_probe(self.exe, case)
                self.assertTrue(
                    observed["refused"],
                    "a %s backup argument must be refused by the parameter contract" % case,
                )
                self.assertNotIn(
                    "success",
                    observed,
                    "no PublicationResult may be produced: File.Replace is never reached",
                )
                self.assertEqual(
                    observed["preimageShaSetup"],
                    observed["destShaAfter"],
                    "the destination must be byte-identical after a refused call",
                )
                self.assertFalse(observed["backupExists"])

    def test_throw_before_publication_leaves_the_preimage_intact(self):
        """EGRT-T10: scoped to the throw-before-publication classes only.

        The assertion is deliberately NOT claimed for a post-publication verification
        failure, where File.Replace returned and the destination has already advanced.
        """
        observations = [(case, replace_probe(self.exe, case))
                        for case in THROW_BEFORE_PUBLICATION_CASES]
        observations.append(
            ("sharing", replace_probe_with_held_destination(self.exe))
        )
        for case, observed in observations:
            with self.subTest(failure_class=case):
                self.assertFalse(observed["refused"])
                self.assertFalse(observed["success"])
                self.assertFalse(
                    observed["publicationOccurred"],
                    "%s must not advance the destination" % case,
                )
                self.assertEqual(
                    observed["preimageShaSetup"],
                    observed["destShaAfter"],
                    "%s must leave the destination at its recorded preimage" % case,
                )
                self.assertEqual(
                    DESTINATION_PREIMAGE_TEXT, observed["destTextAfter"]
                )
                self.assertEqual(
                    observed["backupCreated"],
                    observed["backupRetained"],
                    "nothing is created to satisfy the contract",
                )


class AtomicFileReplaceTierA(AtomicFileReplaceContractMixin, TierABase):
    """Task 5, Tier A: the portable half of the replacement contract."""

    @classmethod
    def setUpClass(cls):
        TierABase.setUpClass()
        cls.exe = ANY_PS


class AtomicFileReplaceTierB(AtomicFileReplaceContractMixin, TierBBase):
    """Task 5, Tier B: the same contract on the Windows PowerShell 5.1 boundary.

    This is the runtime on which the design section 7.3 failure classes were observed, so
    running these cases anywhere else would prove nothing about production.
    """

    @classmethod
    def setUpClass(cls):
        TierBBase.setUpClass()
        cls.exe = DESKTOP_PS

    def test_replacement_exception_type_and_hresult_are_surfaced(self):
        """EGRT-T09: the sharing-violation and access-denied classes, by type and HRESULT."""
        sharing = replace_probe_with_held_destination(self.exe)
        self.assertEqual("System.IO.IOException", sharing["exceptionTypeName"])
        self.assertEqual("0x80070020", sharing["hresult"])
        self.assertEqual(
            "EG_LAUNCHER_REPLACE_SHARING_VIOLATION", sharing["supportRef"]
        )

        readonly = replace_probe(self.exe, "readonly")
        self.assertEqual(
            "System.UnauthorizedAccessException", readonly["exceptionTypeName"]
        )
        self.assertEqual("0x80070005", readonly["hresult"])
        self.assertEqual("EG_LAUNCHER_REPLACE_ACCESS_DENIED", readonly["supportRef"])


class AtomicFileReplaceBackupSemantics(TierABase):
    """Task 5: EGRT-T11, conditional backup semantics with no reaping by the primitive."""

    def test_backup_semantics_are_conditional_and_never_reaped(self):
        """After a verified publication and after a Case B failure, the backup is retained."""
        verified = replace_probe(ANY_PS, "ok")
        self.assertTrue(verified["success"])
        self.assertTrue(
            verified["backupExists"],
            "the primitive proves one publication; it has no authority to reap",
        )

        case_b = replace_probe(ANY_PS, "wronghash")
        self.assertFalse(case_b["success"])
        self.assertEqual(
            "EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH", case_b["supportRef"]
        )
        self.assertTrue(
            case_b["publicationOccurred"],
            "a returned File.Replace advanced the destination: this is Case B",
        )
        self.assertTrue(case_b["backupCreated"])
        self.assertTrue(case_b["backupRetained"])
        self.assertTrue(
            case_b["backupExists"],
            "the primitive performs no self-rollback and no reap after Case B",
        )
        self.assertEqual(
            case_b["preimageShaSetup"],
            case_b["backupShaAfter"],
            "the retained backup is the exact preimage the installer will restore",
        )
        self.assertEqual(
            SOURCE_TEXT,
            case_b["destTextAfter"],
            "Case B means the destination advanced and was NOT restored by the primitive",
        )

        case_a = replace_probe_with_held_destination(ANY_PS)
        self.assertFalse(case_a["publicationOccurred"])
        self.assertEqual(case_a["preimageShaSetup"], case_a["destShaAfter"])
        self.assertFalse(
            case_a["backupCreated"],
            "BackupCreated reports what was observed on disk, not what was requested",
        )


ERROR_ALREADY_EXISTS = 183


def publish_probe(exe, case):
    """Run one publish-to-absent fixture case and return its observation."""
    with TemporaryScratch() as tmp:
        work = tmp / "work"
        work.mkdir()
        return probe_json(exe, "publish", tmp, dir=work, value=case)


class PublishToAbsentDestinationMixin:
    """Design section 7.4, the clean-first-install path, asserted on one interpreter."""

    exe = None

    def test_publish_to_absent_destination_succeeds_and_verifies(self):
        """A bare destination publishes through the no-replace move and verifies."""
        observed = publish_probe(self.exe, "absent")
        self.assertTrue(observed["success"], observed["supportRef"])
        self.assertEqual("", observed["supportRef"])
        self.assertEqual("Absent", observed["preimageState"])
        self.assertEqual(
            "",
            observed["preimageSha256"],
            "there was nothing to back up, so there is no preimage hash",
        )
        self.assertFalse(observed["backupCreated"])
        self.assertFalse(
            observed["backupRetained"],
            "there is no backup on the publish-to-absent path",
        )
        self.assertTrue(observed["publicationOccurred"])
        self.assertEqual(observed["sourceSha"], observed["postimageSha256"])
        self.assertTrue(observed["destinationIsFile"])
        self.assertFalse(
            observed["sourceStillThere"],
            "a move, not a copy: the staging file is consumed",
        )
        self.assertEqual(list(PUBLICATION_RESULT_FIELDS), observed["fieldNames"])


class PublishToAbsentDestinationTierA(PublishToAbsentDestinationMixin, TierABase):
    """Task 6, Tier A."""

    @classmethod
    def setUpClass(cls):
        TierABase.setUpClass()
        cls.exe = ANY_PS

    def test_unexpectedly_present_destination_fails_rather_than_overwriting(self):
        """EGRT-T50: an unexpectedly present destination is terminal, never overwritten."""
        observed = publish_probe(ANY_PS, "presentfile")
        self.assertFalse(observed["success"])
        self.assertEqual(
            "EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT",
            observed["supportRef"],
        )
        self.assertFalse(observed["publicationOccurred"])
        self.assertEqual(
            "# OCCUPYING FILE",
            observed["destinationText"],
            "the pre-existing destination must be byte-identical afterwards",
        )
        self.assertTrue(
            observed["sourceStillThere"],
            "nothing was published, so the staging file is untouched",
        )

    def test_destination_appearing_mid_publication_fails_rather_than_overwriting(self):
        """EGRT-T50: a move that cannot complete reports a lost race and clobbers nothing.

        Induced deterministically by an entry occupying the destination path that is not a
        published file, so the leaf-absence check passes and the no-replace move is what
        refuses. That exercises exactly the mid-publication-appearance branch, without a
        test-only hook in production code and without a timing-dependent fixture.
        """
        observed = publish_probe(ANY_PS, "occupieddirectory")
        self.assertFalse(observed["success"])
        self.assertEqual("EG_LAUNCHER_PUBLISH_RACE_LOST", observed["supportRef"])
        self.assertFalse(observed["publicationOccurred"])
        self.assertTrue(
            observed["destinationIsDir"],
            "the occupying entry must survive unchanged",
        )
        self.assertEqual("OCCUPANT", observed["occupantText"])
        self.assertTrue(observed["sourceStillThere"])

    def test_the_no_replace_move_helper_refuses_to_clobber(self):
        """EGRT-T50 support: MoveFileExW without MOVEFILE_REPLACE_EXISTING never replaces."""
        with TemporaryScratch() as tmp:
            work = tmp / "work"
            work.mkdir()
            observed = probe_json(ANY_PS, "movenoreplace", tmp, dir=work)
        self.assertFalse(observed["ok"])
        self.assertEqual(ERROR_ALREADY_EXISTS, observed["lastError"])
        self.assertEqual("OCCUPANT", observed["destinationText"])
        self.assertTrue(observed["sourceStillThere"])

    def test_an_unverified_staging_file_is_never_published(self):
        """Nothing partially written or unverified is ever published."""
        observed = publish_probe(ANY_PS, "stagingbad")
        self.assertFalse(observed["success"])
        self.assertEqual("EG_LAUNCHER_INSTALL_STAGING_FAILED", observed["supportRef"])
        self.assertFalse(observed["publicationOccurred"])
        self.assertFalse(observed["destinationIsFile"])
        self.assertTrue(observed["sourceStillThere"])


class PublishToAbsentDestinationTierB(PublishToAbsentDestinationMixin, TierBBase):
    """Task 6, Tier B: the same path on the Windows PowerShell 5.1 boundary."""

    @classmethod
    def setUpClass(cls):
        TierBBase.setUpClass()
        cls.exe = DESKTOP_PS


class NullBackupArgumentStaticGuard(TierCBase):
    """Task 7: EGRT-T08, the structural prohibition on a null or unproven backup argument.

    Design section 7.3 records that the null-backup rejection is an observed behaviour of
    the Windows PowerShell 5.1 and .NET Framework 4.x boundary. Other runtimes may accept
    a null backup argument, so the design deliberately does not rely on any runtime
    rejecting it: the prohibition is enforced structurally by the mandatory parameter and
    by this guard over the committed call sites.
    """

    def _inspect(self, target):
        if ANY_PS is None:
            self.skipTest("no PowerShell host found (pwsh or powershell)")
        with TemporaryScratch() as tmp:
            return run_inspector(ANY_PS, tmp, BACKUP_GUARD_INSPECTOR, target=target)

    def test_no_committed_file_replace_call_site_passes_a_null_or_unproven_backup(self):
        """EGRT-T08 over every committed runtime .ps1."""
        targets = existing_runtime_ps1_files()
        self.assertTrue(targets, "at least runtime/launcher_lib.ps1 must exist")
        total_call_sites = 0
        for target in targets:
            with self.subTest(runtime_file=target.name):
                result = self._inspect(target)
                self.assertEqual(0, result["parseErrors"])
                self.assertEqual(
                    0,
                    result["literalNullThird"],
                    "%s passes a literal $null as the backup argument" % target.name,
                )
                self.assertEqual(
                    0,
                    result["literalEmptyThird"],
                    "%s passes a literal empty string as the backup argument" % target.name,
                )
                self.assertEqual(
                    0,
                    result["unprovenThird"],
                    "%s passes a backup argument that is not proven non-empty"
                    % target.name,
                )
                total_call_sites += result["replaceCallCount"]
        self.assertEqual(
            1,
            total_call_sites,
            "there must be exactly one committed [System.IO.File]::Replace call site, "
            "inside Invoke-AtomicFileReplace",
        )

    def test_the_guard_detects_each_defect_it_exists_to_catch(self):
        """The guard is proven to FAIL on the defect, not merely to pass on clean source.

        Each defect is injected into a scratch copy of the library, never into the
        committed file, and the scratch copy is discarded with its scratch directory.
        """
        if ANY_PS is None:
            self.skipTest("no PowerShell host found (pwsh or powershell)")
        baseline = LIB.read_text(encoding="utf-8")
        injections = {
            "literalNullThird": (
                "\nfunction Test-EgScratchNullBackupDefect {\n"
                "    param($a, $b)\n"
                "    [System.IO.File]::Replace($a, $b, $null)\n"
                "}\n"
            ),
            "literalEmptyThird": (
                "\nfunction Test-EgScratchEmptyBackupDefect {\n"
                "    param($a, $b)\n"
                "    [System.IO.File]::Replace($a, $b, '')\n"
                "}\n"
            ),
            "unprovenThird": (
                "\nfunction Test-EgScratchUnprovenBackupDefect {\n"
                "    param($a, $b, $maybeEmpty)\n"
                "    [System.IO.File]::Replace($a, $b, $maybeEmpty)\n"
                "}\n"
            ),
        }
        with TemporaryScratch() as tmp:
            for field, injection in injections.items():
                with self.subTest(defect=field):
                    scratch = tmp / ("scratch_lib_%s.ps1" % field)
                    scratch.write_text(baseline + injection, encoding="utf-8")
                    result = run_inspector(
                        ANY_PS, tmp, BACKUP_GUARD_INSPECTOR, target=scratch
                    )
                    self.assertEqual(0, result["parseErrors"])
                    self.assertEqual(
                        2,
                        result["replaceCallCount"],
                        "the injected call site must be seen by the inspector",
                    )
                    self.assertGreaterEqual(
                        result[field],
                        1,
                        "the guard failed to report the %s defect" % field,
                    )

    def test_the_mandatory_backup_parameter_is_recognised_as_proven(self):
        """The one committed call site passes a parameter the guard can prove non-empty."""
        result = self._inspect(LIB)
        self.assertIn(
            "BackupPath",
            result["provenNames"],
            "-BackupPath must be declared [Parameter(Mandatory)][ValidateNotNullOrEmpty()]",
        )


CLASS_A_MEMBER_NAMES = ("launcher.ps1", "launcher_lib.ps1", "installation_manifest.json")
RESIDUE_PREFIX = ".eglauncher-"
RESIDUE_KINDS = ("staging", "backup", "rollback")
SAMPLE_OPERATION_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


def residue_name(kind, member, operation_id=SAMPLE_OPERATION_ID):
    """Compose a reserved Class B residue name from its three fields."""
    return "%s%s--%s--%s" % (RESIDUE_PREFIX, kind, member, operation_id)


def classify_probe(exe, files=(), dirs=(), include_package=True):
    """Classify a scratch launcher root built from the given entries."""
    entries = list(CLASS_A_MEMBER_NAMES) if include_package else []
    entries.extend(files)
    with TemporaryScratch() as tmp:
        root = tmp / "launcher_root"
        root.mkdir()
        spec = write_json(tmp, "classify_spec.json", {"files": entries, "dirs": list(dirs)})
        return probe_json(exe, "classify", tmp, dir=root, json=spec)


class LauncherRootClassification(TierABase):
    """Task 8: design section 6.7, deterministic Class A, B, and C classification.

    Read as "every file in the launcher root must be a manifest member", the integrity
    rule would turn a valid installed package whose backup cleanup failed into an outage.
    The resolution is not to tolerate extra files: it is to classify them.
    """

    def test_the_class_a_member_set_is_exactly_the_three_fixed_names(self):
        """The set is exact, enumerated explicitly, never derived from a directory listing."""
        observed = classify_probe(ANY_PS)
        self.assertEqual(list(CLASS_A_MEMBER_NAMES), observed["memberNames"])

    def test_valid_package_plus_a_named_backup_residue_classifies_and_passes(self):
        """EGRT-T51: a correctly named retained preimage backup is recognised residue."""
        backup = residue_name("backup", "launcher.ps1")
        observed = classify_probe(ANY_PS, files=[backup])
        self.assertTrue(observed["pass"], observed["supportRef"])
        self.assertEqual("", observed["supportRef"])
        self.assertEqual([backup], observed["classB"])
        self.assertEqual([], observed["classC"])
        self.assertEqual([], observed["missingMembers"])
        self.assertEqual(sorted(CLASS_A_MEMBER_NAMES), sorted(observed["classA"]))

    def test_valid_package_plus_staging_or_rollback_residue_passes_and_never_substitutes(self):
        """EGRT-T52: staging and rollback residue classify Class B and never join Class A."""
        for kind in ("staging", "rollback"):
            for member in CLASS_A_MEMBER_NAMES:
                name = residue_name(kind, member)
                with self.subTest(kind=kind, member=member):
                    observed = classify_probe(ANY_PS, files=[name])
                    self.assertTrue(observed["pass"], observed["supportRef"])
                    self.assertEqual([name], observed["classB"])
                    self.assertEqual([], observed["classC"])
                    self.assertEqual(
                        sorted(CLASS_A_MEMBER_NAMES),
                        sorted(observed["classA"]),
                        "residue must never appear in Class A",
                    )
                    self.assertNotIn(name, observed["classA"])

    def test_an_arbitrary_extra_ps1_fails_closed(self):
        """EGRT-T53: another .ps1 in the launcher root is Class C, whatever it is called."""
        for extra in ("launcher-old.ps1", "launcher_lib_copy.ps1", "anything.ps1"):
            with self.subTest(extra=extra):
                observed = classify_probe(ANY_PS, files=[extra])
                self.assertFalse(observed["pass"])
                self.assertEqual([extra], observed["classC"])
                self.assertEqual(
                    "EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY", observed["supportRef"]
                )

    def test_generic_bak_or_tmp_entries_fail_closed(self):
        """EGRT-T54: a generic extension rule is explicitly NOT sufficient and is not used."""
        for extra in ("launcher.ps1.bak", "staging.tmp", "launcher_lib.ps1.old"):
            with self.subTest(extra=extra):
                observed = classify_probe(ANY_PS, files=[extra])
                self.assertFalse(observed["pass"])
                self.assertEqual([extra], observed["classC"])
                self.assertEqual(
                    "EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY", observed["supportRef"]
                )

    def test_each_residue_name_malformation_fails_closed_independently(self):
        """EGRT-T55: one malformation per case, each asserted alone."""
        malformations = {
            "wrong_prefix": ".eg-launcher-backup--launcher.ps1--" + SAMPLE_OPERATION_ID,
            "wrong_field_count_short": RESIDUE_PREFIX + "backup--launcher.ps1",
            "wrong_field_count_long": residue_name("backup", "launcher.ps1") + "--extra",
            "unknown_kind": residue_name("archive", "launcher.ps1"),
            "unrecognised_member": residue_name("backup", "notamember.ps1"),
            "uppercase_guid": residue_name(
                "backup", "launcher.ps1", SAMPLE_OPERATION_ID.upper()
            ),
            "braced_guid": residue_name(
                "backup", "launcher.ps1", "{%s}" % SAMPLE_OPERATION_ID
            ),
            "truncated_guid": residue_name("backup", "launcher.ps1", "3f2504e0-4f89"),
        }
        for label, name in malformations.items():
            with self.subTest(malformation=label):
                observed = classify_probe(ANY_PS, files=[name])
                self.assertFalse(
                    observed["pass"], "%s must fail closed" % label
                )
                self.assertEqual(
                    [name],
                    observed["classC"],
                    "%s is not residue; it is Class C" % label,
                )
                self.assertEqual(
                    "EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY", observed["supportRef"]
                )

    def test_an_unexpected_directory_fails_closed(self):
        """EGRT-T53 support: the locked architecture requires no subdirectory."""
        observed = classify_probe(ANY_PS, dirs=["unexpected_subdirectory"])
        self.assertFalse(observed["pass"])
        self.assertEqual(["unexpected_subdirectory"], observed["classC"])
        self.assertEqual("EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY", observed["supportRef"])

    def test_a_directory_named_like_a_package_member_is_not_a_package_member(self):
        """A Class A name is satisfied only by a FILE, so a directory cannot impersonate one."""
        with TemporaryScratch() as tmp:
            root = tmp / "launcher_root"
            root.mkdir()
            spec = write_json(
                tmp,
                "classify_spec.json",
                {
                    "files": ["launcher_lib.ps1", "installation_manifest.json"],
                    "dirs": ["launcher.ps1"],
                },
            )
            observed = probe_json(ANY_PS, "classify", tmp, dir=root, json=spec)
        self.assertFalse(observed["pass"])
        self.assertIn("launcher.ps1", observed["classC"])
        self.assertIn("launcher.ps1", observed["missingMembers"])

    def test_a_missing_package_member_fails_closed(self):
        """EGRT-T56 support: a missing Class A member is terminal."""
        with TemporaryScratch() as tmp:
            root = tmp / "launcher_root"
            root.mkdir()
            spec = write_json(
                tmp,
                "classify_spec.json",
                {"files": ["launcher.ps1", "launcher_lib.ps1"], "dirs": []},
            )
            observed = probe_json(ANY_PS, "classify", tmp, dir=root, json=spec)
        self.assertFalse(observed["pass"])
        self.assertEqual(["installation_manifest.json"], observed["missingMembers"])
        self.assertEqual([], observed["classC"])
        self.assertEqual("EG_LAUNCHER_PACKAGE_MEMBER_MISSING", observed["supportRef"])

    def test_class_c_takes_precedence_in_the_reported_support_reference(self):
        """An unexpected entry is reported even when a member is also missing."""
        with TemporaryScratch() as tmp:
            root = tmp / "launcher_root"
            root.mkdir()
            spec = write_json(
                tmp,
                "classify_spec.json",
                {"files": ["launcher.ps1", "intruder.ps1"], "dirs": []},
            )
            observed = probe_json(ANY_PS, "classify", tmp, dir=root, json=spec)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY", observed["supportRef"])
        self.assertEqual(["intruder.ps1"], observed["classC"])


class ReservedResidueNameContract(TierABase):
    """Task 8: the exact reserved-name parser, deliberately narrow."""

    def _parse(self, names):
        with TemporaryScratch() as tmp:
            spec = write_json(tmp, "residue_spec.json", {"names": list(names)})
            return probe_json(
                ANY_PS, "residuename", tmp, json=spec, value=SAMPLE_OPERATION_ID
            )

    def test_a_valid_reserved_name_parses_into_its_three_fields(self):
        """Package member names contain a dot but never the two-hyphen delimiter."""
        names = [residue_name(kind, member)
                 for kind in RESIDUE_KINDS for member in CLASS_A_MEMBER_NAMES]
        observed = self._parse(names)
        self.assertEqual(list(RESIDUE_KINDS), observed["kinds"])
        for entry in observed["parsed"]:
            with self.subTest(name=entry["name"]):
                self.assertTrue(entry["isResidue"])
                self.assertIn(entry["kind"], RESIDUE_KINDS)
                self.assertIn(entry["member"], CLASS_A_MEMBER_NAMES)
                self.assertEqual(SAMPLE_OPERATION_ID, entry["operationId"])

    def test_the_constructor_is_the_only_sanctioned_residue_name_source(self):
        """New-EgResidueName composes exactly what Test-EgResidueName accepts."""
        observed = self._parse([])
        self.assertEqual(
            residue_name("backup", "launcher.ps1"), observed["constructed"]
        )

    def test_a_generic_extension_is_never_treated_as_residue(self):
        """A *.bak or *.tmp rule would wave through any dropped file. It is not used."""
        observed = self._parse(
            ["launcher.ps1.bak", "launcher.ps1.tmp", "backup.bak", "", "launcher.ps1"]
        )
        for entry in observed["parsed"]:
            with self.subTest(name=entry["name"]):
                self.assertFalse(entry["isResidue"])
                self.assertEqual("", entry["kind"])
                self.assertEqual("", entry["member"])
                self.assertEqual("", entry["operationId"])


MANIFEST_FILE_NAME = "installation_manifest.json"
MANIFEST_SCHEMA = "eg_launcher_installation_manifest/v1"
MANIFEST_MEMBER_NAMES = ("launcher.ps1", "launcher_lib.ps1")
SAMPLE_ADMISSION_COMMIT = "0123456789abcdef0123456789abcdef01234567"

DEFAULT_MEMBER_CONTENT = {
    "launcher.ps1": "# scratch launcher entry script\r\n",
    "launcher_lib.ps1": "# scratch launcher library\r\n",
}


def _sha256_of_text(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest_payload(content=None, overrides=None, omit=(), extra_entries=()):
    """Build a manifest object describing the given member content."""
    content = dict(DEFAULT_MEMBER_CONTENT if content is None else content)
    entries = []
    for name in sorted(MANIFEST_MEMBER_NAMES):
        if name in omit:
            continue
        text = content[name]
        entries.append(
            {
                "name": name,
                "sha256": _sha256_of_text(text),
                "byte_length": len(text.encode("utf-8")),
            }
        )
    entries.extend(dict(entry) for entry in extra_entries)
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "admission_commit": SAMPLE_ADMISSION_COMMIT,
        "members": entries,
    }
    if overrides:
        manifest.update(overrides)
    return manifest


def manifest_probe(
    exe,
    content=None,
    manifest=None,
    manifest_raw=None,
    write_manifest=True,
    residue=(),
    omit_members=(),
):
    """Compare a scratch installed package to a caller-controlled manifest."""
    content = dict(DEFAULT_MEMBER_CONTENT if content is None else content)
    members = [
        {"name": name, "content": text}
        for name, text in sorted(content.items())
        if name not in omit_members
    ]
    if manifest_raw is None:
        payload = build_manifest_payload(content) if manifest is None else manifest
        manifest_raw = json.dumps(payload, indent=2)
    with TemporaryScratch() as tmp:
        root = tmp / "launcher_root"
        root.mkdir()
        spec = write_json(
            tmp,
            "manifest_spec.json",
            {
                "members": members,
                "residue": list(residue),
                "writeManifest": bool(write_manifest),
                "manifestRaw": manifest_raw,
            },
        )
        return probe_json(exe, "manifest", tmp, dir=root, json=spec)


class InstallationManifestComparison(TierABase):
    """Task 9: design section 6.4, the installed-launcher integrity manifest.

    Git is canonical for the manifest's SHAPE and for the comparison rules. The manifest
    RECORD is private deployment state, generated by the installer from reviewed source,
    and is never committed.

    An honest limitation the design states and this test does not pretend to close: the
    manifest sits beside the files it describes, so a writer who can modify the launcher
    can also modify the manifest. It is tamper-evident only to the extent the launcher-root
    access-control expectations hold.
    """

    def test_the_manifest_constants_match_the_design(self):
        """The manifest describes exactly the two EXECUTABLE members, never itself."""
        observed = manifest_probe(ANY_PS)
        self.assertEqual(MANIFEST_FILE_NAME, observed["manifestFileName"])
        self.assertEqual(MANIFEST_SCHEMA, observed["manifestSchema"])
        self.assertEqual(list(MANIFEST_MEMBER_NAMES), observed["manifestMembers"])
        self.assertNotIn(
            MANIFEST_FILE_NAME,
            observed["manifestMembers"],
            "the manifest never describes itself",
        )

    def test_manifest_matching_the_installed_members_passes(self):
        """EGRT-T46: installed bytes and the record of them are mutually consistent."""
        observed = manifest_probe(ANY_PS)
        self.assertTrue(observed["pass"], observed["supportRef"])
        self.assertEqual("", observed["supportRef"])
        self.assertEqual(["launcher_package_manifest_match"], observed["checkNames"])
        self.assertEqual(["PASS"], observed["checkOutcomes"])

    def test_missing_manifest_is_terminal(self):
        """A missing manifest is terminal rather than a warning."""
        observed = manifest_probe(ANY_PS, write_manifest=False)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISSING", observed["supportRef"])
        self.assertEqual(["FAIL"], observed["checkOutcomes"])

    def test_unparsable_manifest_is_terminal(self):
        """An unparsable manifest is terminal and distinguishable from a missing one."""
        for raw in ("{ not json at all", "", "[1,2,3"):
            with self.subTest(raw=raw):
                observed = manifest_probe(ANY_PS, manifest_raw=raw)
                self.assertFalse(observed["pass"])
                self.assertEqual(
                    "EG_LAUNCHER_MANIFEST_UNPARSABLE", observed["supportRef"]
                )

    def test_hash_or_length_mismatch_is_terminal(self):
        """A hash or byte-length mismatch fails closed."""
        payload = build_manifest_payload()
        payload["members"][0]["sha256"] = "f" * 64
        observed = manifest_probe(ANY_PS, manifest=payload)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISMATCH", observed["supportRef"])

        payload = build_manifest_payload()
        payload["members"][0]["byte_length"] = payload["members"][0]["byte_length"] + 1
        observed = manifest_probe(ANY_PS, manifest=payload)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISMATCH", observed["supportRef"])

    def test_manifest_entry_without_a_member_is_terminal(self):
        """A manifest entry describing bytes that are not installed fails closed."""
        observed = manifest_probe(ANY_PS, omit_members=("launcher.ps1",))
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISMATCH", observed["supportRef"])

    def test_member_without_a_manifest_entry_is_terminal(self):
        """An installed executable member the manifest does not describe fails closed."""
        payload = build_manifest_payload(omit=("launcher_lib.ps1",))
        observed = manifest_probe(ANY_PS, manifest=payload)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISMATCH", observed["supportRef"])

    def test_an_unrecognised_manifest_entry_is_terminal(self):
        """The member name set must be exactly the two executable members."""
        payload = build_manifest_payload(
            extra_entries=[{"name": "intruder.ps1", "sha256": "a" * 64, "byte_length": 1}]
        )
        observed = manifest_probe(ANY_PS, manifest=payload)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISMATCH", observed["supportRef"])

    def test_an_invalid_shape_is_terminal(self):
        """Schema version, admission commit, and hash casing are all part of the shape."""
        cases = {
            "wrong_schema": {"schema_version": "something/else"},
            "short_commit": {"admission_commit": "0123abc"},
            "uppercase_commit": {"admission_commit": SAMPLE_ADMISSION_COMMIT.upper()},
        }
        for label, override in cases.items():
            with self.subTest(shape_defect=label):
                payload = build_manifest_payload(overrides=override)
                observed = manifest_probe(ANY_PS, manifest=payload)
                self.assertFalse(observed["pass"])
                self.assertEqual(
                    "EG_LAUNCHER_MANIFEST_MISMATCH", observed["supportRef"]
                )

        payload = build_manifest_payload()
        payload["members"][0]["sha256"] = payload["members"][0]["sha256"].upper()
        observed = manifest_probe(ANY_PS, manifest=payload)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISMATCH", observed["supportRef"])

    def test_class_b_residue_does_not_break_manifest_completeness(self):
        """EGRT-T51 and EGRT-T52 support: completeness is scoped to the package domain.

        Residue is not a package member and is not described by the manifest. Its absence
        from the manifest is therefore NOT a completeness failure.
        """
        residue = [
            residue_name("backup", "launcher.ps1"),
            residue_name("staging", "launcher_lib.ps1"),
            residue_name("rollback", MANIFEST_FILE_NAME),
        ]
        observed = manifest_probe(ANY_PS, residue=residue)
        self.assertTrue(observed["pass"], observed["supportRef"])
        self.assertEqual("", observed["supportRef"])


class InstallationManifestConstruction(TierABase):
    """Task 9: manifest construction, ordering, shape validation, and determinism."""

    def _build(self, members, commit=SAMPLE_ADMISSION_COMMIT):
        with TemporaryScratch() as tmp:
            spec = write_json(tmp, "build_spec.json", {"members": members})
            return probe_json(ANY_PS, "manifestbuild", tmp, json=spec, value=commit)

    def test_members_are_sorted_by_name_ascending(self):
        """The members array is sorted by name, so identical input yields identical bytes."""
        members = [
            {"name": "launcher_lib.ps1", "sha256": "b" * 64, "byte_length": 20},
            {"name": "launcher.ps1", "sha256": "a" * 64, "byte_length": 10},
        ]
        observed = self._build(members)
        self.assertTrue(observed["shapePass"], observed["shapeSupportRef"])
        self.assertEqual(["launcher.ps1", "launcher_lib.ps1"], observed["memberOrder"])
        self.assertEqual(MANIFEST_SCHEMA, observed["schemaVersion"])
        self.assertEqual(SAMPLE_ADMISSION_COMMIT, observed["admissionCommit"])
        self.assertEqual(
            ["installation_manifest_shape"], observed["shapeCheckNames"]
        )

    def test_serialisation_is_deterministic_for_identical_input(self):
        """Hash idempotency is only decidable if serialisation is deterministic."""
        members = [
            {"name": "launcher.ps1", "sha256": "a" * 64, "byte_length": 10},
            {"name": "launcher_lib.ps1", "sha256": "b" * 64, "byte_length": 20},
        ]
        observed = self._build(members)
        self.assertTrue(observed["deterministic"])

    def test_the_manifest_carries_no_date_shaped_field(self):
        """ConvertFrom-Json coerces date-shaped strings on PowerShell 7 but not on 5.1.

        The manifest therefore carries no date-shaped field, so parsing is identical on
        both editions.
        """
        members = [
            {"name": "launcher.ps1", "sha256": "a" * 64, "byte_length": 10},
            {"name": "launcher_lib.ps1", "sha256": "b" * 64, "byte_length": 20},
        ]
        observed = self._build(members)
        parsed = json.loads(observed["serialised"])
        self.assertEqual(
            {"schema_version", "admission_commit", "members"}, set(parsed.keys())
        )
        for entry in parsed["members"]:
            self.assertEqual({"name", "sha256", "byte_length"}, set(entry.keys()))
        self.assertNotRegex(
            observed["serialised"],
            r"\d{4}-\d{2}-\d{2}",
            "no field may be date-shaped",
        )

    def test_a_malformed_admission_commit_is_refused_by_the_shape_check(self):
        """The admission commit must be exactly forty lowercase hexadecimal characters."""
        members = [
            {"name": "launcher.ps1", "sha256": "a" * 64, "byte_length": 10},
            {"name": "launcher_lib.ps1", "sha256": "b" * 64, "byte_length": 20},
        ]
        observed = self._build(members, commit="not-a-commit")
        self.assertFalse(observed["shapePass"])
        self.assertEqual(
            "EG_LAUNCHER_MANIFEST_MISMATCH", observed["shapeSupportRef"]
        )


EXIT_INSTALL_PRE_MUTATION_FAILED = 71
EXIT_INSTALL_ROLLED_BACK = 72
EXIT_INSTALL_ROLLBACK_INCOMPLETE = 73

SCRATCH_LAUNCHER_SOURCE = (
    "# scratch launcher entry script for the installer transaction tests\r\n"
    "[CmdletBinding()]\r\n"
    "param()\r\n"
    "Set-StrictMode -Version Latest\r\n"
    "exit 0\r\n"
)
SCRATCH_LIB_SOURCE = (
    "# scratch launcher library for the installer transaction tests\r\n"
    "Set-StrictMode -Version Latest\r\n"
    "function Get-EgScratchMarker { 'scratch' }\r\n"
)

# Files that share the runtime directory but are NEVER deployed to the launcher root
# (design section 6.2). Sharing the directory does not make a file deployable.
NEVER_DEPLOYED_NAMES = (
    "install_or_update_launcher.ps1",
    "launcher.settings.example.json",
    "README.md",
)


def build_scratch_checkout(tmp, launcher_text=SCRATCH_LAUNCHER_SOURCE,
                           lib_text=SCRATCH_LIB_SOURCE, extra_runtime_files=True):
    """Create a scratch Git checkout carrying runtime source. Returns (root, head_commit)."""
    root = init_scratch_repo(tmp / "checkout")
    runtime = root / "energygrid-bill-downloader" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "launcher.ps1").write_text(launcher_text, encoding="utf-8", newline="")
    (runtime / "launcher_lib.ps1").write_text(lib_text, encoding="utf-8", newline="")
    if extra_runtime_files:
        # Present in the runtime directory on purpose: EGRT-T47 requires that none of
        # these reaches the launcher root.
        (runtime / "install_or_update_launcher.ps1").write_text(
            "# scratch installer, never deployed\r\n", encoding="utf-8", newline=""
        )
        (runtime / "launcher.settings.example.json").write_text(
            '{"config_path": "REPLACE_WITH_PRIVATE_CONFIG_JSON_PATH"}\r\n',
            encoding="utf-8", newline="",
        )
        (runtime / "README.md").write_text(
            "# scratch runtime readme, never deployed\r\n", encoding="utf-8", newline=""
        )
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "scratch runtime source")
    head = run_git(root, "rev-parse", "HEAD").stdout.strip()
    return root, head


def run_installer(exe, checkout, launcher_root, commit, *extra):
    """Invoke the committed installer entry script against scratch paths."""
    return run_ps(
        exe,
        INSTALLER,
        "-CheckoutRoot", str(checkout),
        "-LauncherRoot", str(launcher_root),
        "-AdmissionCommit", str(commit),
        *extra,
    )


def installer_status(completed):
    """Parse the installer's bounded real-path JSON from standard output."""
    text = completed.stdout.strip()
    if not text:
        raise AssertionError(
            "the installer emitted no status\nstdout:\n%s\nstderr:\n%s"
            % (completed.stdout, completed.stderr)
        )
    return json.loads(text.splitlines()[-1])


def launcher_root_entries(root):
    """Every entry name in a scratch launcher root."""
    return sorted(entry.name for entry in Path(root).iterdir())


def class_b_entries(root, kind=None):
    """Recognised residue names in a scratch launcher root, optionally filtered by kind."""
    prefix = RESIDUE_PREFIX if kind is None else "%s%s--" % (RESIDUE_PREFIX, kind)
    return sorted(
        name for name in launcher_root_entries(root) if name.startswith(prefix)
    )


class InstallerTransactionMixin:
    """Design sections 6.1, 6.2, and 6.3: the four-phase package transaction."""

    exe = None

    def test_clean_first_install_publishes_all_three_members_through_the_absent_path(self):
        """EGRT-T41: the bare-root case, end to end.

        On a clean host the launcher root contains no launcher.ps1, no launcher_lib.ps1,
        and no manifest, so all three members classify as Absent in Phase 1 and publish
        through the publish-to-absent path. Not every install uses File.Replace.
        """
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            completed = run_installer(self.exe, checkout, root, commit)
            self.assertEqual(
                0, completed.returncode,
                "installer failed\nstdout:\n%s\nstderr:\n%s"
                % (completed.stdout, completed.stderr),
            )
            status = installer_status(completed)
            self.assertEqual("INSTALLED", status["status"])
            self.assertEqual("", status["support_ref"])

            entries = launcher_root_entries(root)
            self.assertEqual(sorted(CLASS_A_MEMBER_NAMES), entries)
            self.assertEqual(
                [],
                class_b_entries(root, "backup"),
                "a clean install creates no preimage backup, because File.Replace was "
                "never called against a missing destination",
            )
            self.assertEqual([], class_b_entries(root, "staging"))

            self.assertEqual(
                SCRATCH_LAUNCHER_SOURCE,
                (root / "launcher.ps1").read_text(encoding="utf-8", newline=""),
            )
            self.assertEqual(
                SCRATCH_LIB_SOURCE,
                (root / "launcher_lib.ps1").read_text(encoding="utf-8", newline=""),
            )

            manifest = json.loads(
                (root / MANIFEST_FILE_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(MANIFEST_SCHEMA, manifest["schema_version"])
            self.assertEqual(commit, manifest["admission_commit"])
            self.assertEqual(
                list(MANIFEST_MEMBER_NAMES),
                sorted(entry["name"] for entry in manifest["members"]),
            )
            for entry in manifest["members"]:
                installed = root / entry["name"]
                self.assertEqual(sha256_of(installed), entry["sha256"])
                self.assertEqual(installed.stat().st_size, entry["byte_length"])


class InstallerTransactionTierA(InstallerTransactionMixin, TierABase):
    """Task 10, Tier A."""

    @classmethod
    def setUpClass(cls):
        TierABase.setUpClass()
        cls.exe = ANY_PS

    def test_second_install_against_a_current_destination_reports_already_current(self):
        """EGRT-T16: installation is idempotent by HASH, not by timestamp."""
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            first = run_installer(ANY_PS, checkout, root, commit)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual("INSTALLED", installer_status(first)["status"])

            before = {
                name: (
                    (root / name).stat().st_mtime_ns,
                    sha256_of(root / name),
                )
                for name in launcher_root_entries(root)
            }

            second = run_installer(ANY_PS, checkout, root, commit)
            self.assertEqual(0, second.returncode, second.stderr)
            status = installer_status(second)
            self.assertEqual("ALREADY_CURRENT", status["status"])
            self.assertEqual("", status["support_ref"])

            after = {
                name: (
                    (root / name).stat().st_mtime_ns,
                    sha256_of(root / name),
                )
                for name in launcher_root_entries(root)
            }
            self.assertEqual(
                before, after,
                "ALREADY_CURRENT must mutate nothing at all, including modification times",
            )

    def test_only_the_enumerated_deployed_set_reaches_the_launcher_root(self):
        """EGRT-T47: sharing the runtime directory does not make a file deployable."""
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            completed = run_installer(ANY_PS, checkout, root, commit)
            self.assertEqual(0, completed.returncode, completed.stderr)
            entries = launcher_root_entries(root)
        for name in NEVER_DEPLOYED_NAMES:
            with self.subTest(never_deployed=name):
                self.assertNotIn(name, entries)
        self.assertEqual(sorted(CLASS_A_MEMBER_NAMES), entries)

    def test_installed_bytes_and_manifest_are_mutually_consistent_after_commit(self):
        """EGRT-T46: no extra or missing member, and hashes agree in both directions."""
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            completed = run_installer(ANY_PS, checkout, root, commit)
            self.assertEqual(0, completed.returncode, completed.stderr)
            manifest = json.loads(
                (root / MANIFEST_FILE_NAME).read_text(encoding="utf-8")
            )
            described = {entry["name"]: entry for entry in manifest["members"]}
            installed = {
                name for name in launcher_root_entries(root)
                if name in MANIFEST_MEMBER_NAMES
            }
            self.assertEqual(set(described), installed)
            for name, entry in described.items():
                self.assertEqual(sha256_of(root / name), entry["sha256"])

    def test_an_admission_commit_that_is_not_the_checkout_head_is_refused(self):
        """-AdmissionCommit is the operator-controlled admission lane and is verified."""
        with TemporaryScratch() as tmp:
            checkout, _commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            completed = run_installer(ANY_PS, checkout, root, "0" * 40)
            self.assertEqual(EXIT_INSTALL_PRE_MUTATION_FAILED, completed.returncode)
            status = installer_status(completed)
            self.assertEqual("FAILED_PREFLIGHT", status["status"])
            self.assertEqual(
                "EG_LAUNCHER_INSTALL_ADMISSION_INVALID", status["support_ref"]
            )
            self.assertEqual(
                [], launcher_root_entries(root),
                "a Phase 1 failure leaves ZERO destination mutation",
            )

    def test_a_malformed_admission_commit_is_refused_by_the_parameter_contract(self):
        """The admission commit is mandatory and must be forty lowercase hex characters."""
        with TemporaryScratch() as tmp:
            checkout, _commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            completed = run_installer(ANY_PS, checkout, root, "not-a-commit")
            self.assertNotEqual(0, completed.returncode)
            self.assertEqual([], launcher_root_entries(root))


class InstallerTransactionTierB(InstallerTransactionMixin, TierBBase):
    """Task 10, Tier B: the clean first install on the Windows PowerShell 5.1 boundary."""

    @classmethod
    def setUpClass(cls):
        TierBBase.setUpClass()
        cls.exe = DESKTOP_PS


class DeployableSourceSetStaticGuard(TierCBase):
    """Task 10, Tier C: EGRT-T47's static half."""

    def test_the_deployable_set_is_not_derived_from_a_directory_listing(self):
        """The set is enumerated explicitly, so a file added later cannot become deployable."""
        if ANY_PS is None:
            self.skipTest("no PowerShell host found (pwsh or powershell)")
        with TemporaryScratch() as tmp:
            result = run_inspector(
                ANY_PS,
                tmp,
                FUNCTION_BODY_COMMAND_INSPECTOR,
                target=LIB,
                functionName="Get-EgDeployableSourceSet",
                forbiddenCommand="Get-ChildItem",
            )
        self.assertEqual(0, result["parseErrors"])
        self.assertTrue(
            result["functionFound"], "Get-EgDeployableSourceSet must exist in the library"
        )
        self.assertEqual(
            0,
            result["forbiddenCount"],
            "Get-EgDeployableSourceSet must not enumerate the runtime directory",
        )


def revise_scratch_source(checkout, suffix="# revised\r\n"):
    """Advance the scratch checkout to a new commit with different runtime source bytes."""
    runtime = checkout / "energygrid-bill-downloader" / "runtime"
    (runtime / "launcher.ps1").write_text(
        SCRATCH_LAUNCHER_SOURCE + suffix, encoding="utf-8", newline=""
    )
    (runtime / "launcher_lib.ps1").write_text(
        SCRATCH_LIB_SOURCE + suffix, encoding="utf-8", newline=""
    )
    run_git(checkout, "add", "-A")
    run_git(checkout, "commit", "-m", "revised runtime source")
    return run_git(checkout, "rev-parse", "HEAD").stdout.strip()


def set_read_only(path, read_only=True):
    """Set or clear the read-only attribute on a scratch file."""
    import stat

    path = Path(path)
    mode = path.stat().st_mode
    if read_only:
        path.chmod(mode & ~stat.S_IWRITE)
    else:
        path.chmod(mode | stat.S_IWRITE)


def snapshot_package(root):
    """Record the exact state of the three package members in a launcher root."""
    root = Path(root)
    state = {}
    for name in CLASS_A_MEMBER_NAMES:
        member = root / name
        if member.is_file():
            state[name] = ("file", sha256_of(member))
        elif member.is_dir():
            state[name] = ("directory", "")
        else:
            state[name] = ("absent", "")
    return state


def assert_no_forbidden_end_state(case, root, pre_transaction):
    """The four end states design section 6.5 declares unreachable.

    A new launcher with an old library, an old launcher with a new library, executable
    files that disagree with the manifest, or a manifest describing bytes that are not
    installed.
    """
    root = Path(root)
    observed = snapshot_package(root)
    failures = []

    launcher_changed = observed["launcher.ps1"] != pre_transaction["launcher.ps1"]
    lib_changed = observed["launcher_lib.ps1"] != pre_transaction["launcher_lib.ps1"]
    if launcher_changed != lib_changed:
        failures.append(
            "mixed installation: launcher.ps1 changed=%s, launcher_lib.ps1 changed=%s"
            % (launcher_changed, lib_changed)
        )

    manifest_path = root / MANIFEST_FILE_NAME
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = None
        if manifest is not None and isinstance(manifest.get("members"), list):
            for entry in manifest["members"]:
                member = root / entry.get("name", "")
                if not member.is_file():
                    failures.append(
                        "manifest describes bytes that are not installed: %r"
                        % entry.get("name")
                    )
                elif sha256_of(member) != entry.get("sha256"):
                    failures.append(
                        "installed bytes disagree with the manifest: %r"
                        % entry.get("name")
                    )
    if failures:
        raise AssertionError("%s reached a forbidden end state: %s" % (case, failures))


class InstallerOwnedPackageRollback(TierABase):
    """Task 11: design sections 6.5 and 7.2.1.

    The installer transaction is the ONLY rollback authority in this design. Neither
    publish primitive restores anything: they publish and report, and the installer
    decides. That single-owner rule is what keeps reverse-order restoration correct,
    because only the installer knows which members advanced and in what order.
    """

    def test_the_touched_set_is_reverse_publication_order_and_excludes_non_advanced(self):
        """Membership is decided by PublicationOccurred, not by which member failed."""
        with TemporaryScratch() as tmp:
            work = tmp / "work"
            work.mkdir()
            observed = probe_json(ANY_PS, "touchedorder", tmp, dir=work)
        self.assertEqual(["launcher_lib.ps1", "launcher.ps1"], observed["publicationOrder"])
        self.assertEqual(
            ["launcher.ps1", "launcher_lib.ps1"],
            observed["touchedNames"],
            "rollback walks the touched set in REVERSE publication order",
        )
        self.assertNotIn(
            MANIFEST_FILE_NAME,
            observed["touchedNames"],
            "a member that did not advance is not in the touched set",
        )

    def test_a_returned_replace_with_a_wrong_expected_hash_rolls_back_to_the_preimage(self):
        """EGRT-T12: a genuine Case B, restored by the installer and positively verified.

        -ExpectedSha256 is a mandatory PRODUCTION parameter, not a test hook. Passing a
        deliberately wrong value is a legitimate caller error, which is what lets this case
        exercise installer-owned rollback against a member that really did advance.
        """
        with TemporaryScratch() as tmp:
            work = tmp / "work"
            work.mkdir()
            observed = probe_json(ANY_PS, "rollback", tmp, dir=work, value="caseb")

        self.assertFalse(observed["publishSuccess"])
        self.assertEqual(
            "EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH", observed["publishSupportRef"]
        )
        self.assertTrue(
            observed["publicationOccurred"], "Case B means the destination advanced"
        )
        self.assertTrue(observed["backupCreated"])
        self.assertEqual(1, observed["touchedCount"])

        self.assertTrue(observed["rollbackVerified"], observed["rollbackSupportRef"])
        self.assertEqual("", observed["rollbackSupportRef"])
        self.assertEqual(
            observed["preimageSha"],
            observed["destinationSha"],
            "the destination is restored to the EXACT recorded preimage",
        )
        self.assertGreaterEqual(
            observed["rollbackResidueCount"],
            1,
            "restoring through the explicit-backup path retains the advanced bytes as "
            "recognised rollback residue",
        )

    def test_an_unrecoverable_advanced_member_is_not_reported_as_rolled_back(self):
        """PublicationOccurred true with no backup on disk is the one unrecoverable state."""
        with TemporaryScratch() as tmp:
            work = tmp / "work"
            work.mkdir()
            observed = probe_json(ANY_PS, "rollback", tmp, dir=work, value="unrecoverable")
        self.assertTrue(observed["publicationOccurred"])
        self.assertFalse(observed["backupStillThere"])
        self.assertFalse(
            observed["rollbackVerified"],
            "a restoration that cannot be positively verified is not successful",
        )
        self.assertEqual(
            "EG_LAUNCHER_REPLACE_PREIMAGE_UNRECOVERABLE",
            observed["rollbackSupportRef"],
        )


class InstallerRollbackAgainstTheRealInstaller(TierABase):
    """Task 11: rollback driven through the committed installer entry script."""

    def test_first_install_failure_returns_created_destinations_to_absent(self):
        """EGRT-T42: every newly created owned destination is removed and confirmed absent.

        A destination whose preimage was Absent is removed after a verified rollback and
        is NOT kept behind for diagnosis.
        """
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            # An entry occupying the second-published member's path makes its no-replace
            # move fail after the first member has already advanced.
            (root / "launcher.ps1").mkdir()
            pre = snapshot_package(root)

            completed = run_installer(ANY_PS, checkout, root, commit)
            status = installer_status(completed)

            self.assertEqual(EXIT_INSTALL_ROLLED_BACK, completed.returncode,
                             "%s\n%s" % (completed.stdout, completed.stderr))
            self.assertEqual("FAILED_ROLLED_BACK", status["status"])
            self.assertFalse(
                (root / "launcher_lib.ps1").exists(),
                "the member this transaction created must be removed and confirmed absent",
            )
            self.assertFalse((root / MANIFEST_FILE_NAME).exists())
            self.assertTrue(
                (root / "launcher.ps1").is_dir(),
                "the installer never removes an entry it did not create",
            )
            assert_no_forbidden_end_state("first-install failure", root, pre)

    def test_a_later_member_failure_restores_every_earlier_member_byte_for_byte(self):
        """EGRT-T43: the earlier member's post-rollback bytes equal its pre-transaction bytes."""
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            first = run_installer(ANY_PS, checkout, root, commit)
            self.assertEqual(0, first.returncode, first.stderr)

            revised = revise_scratch_source(checkout)
            pre = snapshot_package(root)
            original_lib = (root / "launcher_lib.ps1").read_bytes()

            # A read-only destination on the SECOND published member is a genuine Case A
            # mid-package failure: it throws before publication and does not advance.
            set_read_only(root / "launcher.ps1", True)
            try:
                completed = run_installer(ANY_PS, checkout, root, revised)
            finally:
                set_read_only(root / "launcher.ps1", False)

            status = installer_status(completed)
            self.assertEqual(EXIT_INSTALL_ROLLED_BACK, completed.returncode,
                             "%s\n%s" % (completed.stdout, completed.stderr))
            self.assertEqual("FAILED_ROLLED_BACK", status["status"])
            self.assertEqual(
                original_lib,
                (root / "launcher_lib.ps1").read_bytes(),
                "the earlier member must be restored byte for byte",
            )
            self.assertEqual(pre, snapshot_package(root))
            assert_no_forbidden_end_state("later-member failure", root, pre)

    def test_manifest_failure_rolls_back_executables_and_restores_the_prior_manifest(self):
        """EGRT-T44: with a pre-existing manifest it is restored to its preimage."""
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            first = run_installer(ANY_PS, checkout, root, commit)
            self.assertEqual(0, first.returncode, first.stderr)

            revised = revise_scratch_source(checkout)
            pre = snapshot_package(root)

            set_read_only(root / MANIFEST_FILE_NAME, True)
            try:
                completed = run_installer(ANY_PS, checkout, root, revised)
            finally:
                set_read_only(root / MANIFEST_FILE_NAME, False)

            status = installer_status(completed)
            self.assertEqual(EXIT_INSTALL_ROLLED_BACK, completed.returncode,
                             "%s\n%s" % (completed.stdout, completed.stderr))
            self.assertEqual("FAILED_ROLLED_BACK", status["status"])
            self.assertEqual(
                pre,
                snapshot_package(root),
                "the entire package must equal its pre-transaction state",
            )
            assert_no_forbidden_end_state("manifest failure", root, pre)

    def test_manifest_failure_on_a_clean_install_confirms_the_manifest_absent_again(self):
        """EGRT-T44: with no pre-existing manifest, the destination is confirmed absent."""
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            (root / MANIFEST_FILE_NAME).mkdir()
            pre = snapshot_package(root)

            completed = run_installer(ANY_PS, checkout, root, commit)
            status = installer_status(completed)

            self.assertEqual(EXIT_INSTALL_ROLLED_BACK, completed.returncode,
                             "%s\n%s" % (completed.stdout, completed.stderr))
            self.assertEqual("FAILED_ROLLED_BACK", status["status"])
            self.assertFalse((root / "launcher.ps1").exists())
            self.assertFalse((root / "launcher_lib.ps1").exists())
            self.assertTrue((root / MANIFEST_FILE_NAME).is_dir())
            self.assertEqual(pre, snapshot_package(root))
            assert_no_forbidden_end_state("clean-install manifest failure", root, pre)

    def test_existing_member_backups_survive_every_per_file_verification(self):
        """EGRT-T45: the preimage backup is still present when rollback needs it.

        Restoring every Existing member to its exact preimage is only possible because the
        retained backup was still on disk when rollback ran, which proves the backup
        survived every per-file verification and was not reaped before acceptance.
        Authority to reap belongs to the Phase 4 commit step and to nothing else.
        """
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            first = run_installer(ANY_PS, checkout, root, commit)
            self.assertEqual(0, first.returncode, first.stderr)

            revised = revise_scratch_source(checkout)
            pre = snapshot_package(root)

            set_read_only(root / MANIFEST_FILE_NAME, True)
            try:
                completed = run_installer(ANY_PS, checkout, root, revised)
            finally:
                set_read_only(root / MANIFEST_FILE_NAME, False)

            self.assertEqual(EXIT_INSTALL_ROLLED_BACK, completed.returncode,
                             "%s\n%s" % (completed.stdout, completed.stderr))
            self.assertEqual(
                pre,
                snapshot_package(root),
                "both executables were restored, which required their retained backups",
            )
            rollback_residue = class_b_entries(root, "rollback")
            for member in MANIFEST_MEMBER_NAMES:
                with self.subTest(member=member):
                    self.assertTrue(
                        any(("--%s--" % member) in name for name in rollback_residue),
                        "no rollback residue for %s, so no backup was consumed to restore "
                        "it; residue present: %r" % (member, rollback_residue),
                    )


class PostAcceptanceBackupCleanup(TierABase):
    """Task 12: design section 6.6, the single sanctioned narrow fail-closed exception.

    Reaping a backup is a transaction-commit action, never a per-file one. The section 7
    primitives prove one publication and have no view of the package, so they have no
    authority to reap. The installer holds every retained preimage until Phase 4
    acceptance, because until the manifest has been read back and the package re-verified,
    any of those preimages might still be needed by rollback.
    """

    def test_a_successful_update_leaves_no_backup_residue(self):
        """Backups are reaped, but only after whole-package acceptance."""
        with TemporaryScratch() as tmp:
            checkout, commit = build_scratch_checkout(tmp)
            root = tmp / "launcher_root"
            root.mkdir()
            first = run_installer(ANY_PS, checkout, root, commit)
            self.assertEqual(0, first.returncode, first.stderr)

            revised = revise_scratch_source(checkout)
            completed = run_installer(ANY_PS, checkout, root, revised)
            self.assertEqual(0, completed.returncode,
                             "%s\n%s" % (completed.stdout, completed.stderr))
            status = installer_status(completed)
            self.assertEqual("INSTALLED", status["status"])
            self.assertEqual("", status["support_ref"])
            self.assertEqual(0, status["backups_remaining"])
            self.assertEqual([], class_b_entries(root, "backup"))
            self.assertEqual([], class_b_entries(root, "staging"))
            self.assertEqual(sorted(CLASS_A_MEMBER_NAMES), launcher_root_entries(root))

            # The accepted installation is still internally consistent afterwards.
            manifest = json.loads(
                (root / MANIFEST_FILE_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(revised, manifest["admission_commit"])
            for entry in manifest["members"]:
                self.assertEqual(sha256_of(root / entry["name"]), entry["sha256"])

    def test_a_failed_cleanup_keeps_the_accepted_install_and_reports_it_visibly(self):
        """A redundant artefact after a verified success is never turned into an outage.

        Rolling back a fully accepted installation because a now-redundant backup could not
        be deleted would replace a good outcome with a worse one. The failure is not
        swallowed: absence is verified after every delete attempt and what remains is
        counted and reported under a bounded reference.
        """
        with TemporaryScratch() as tmp:
            work = tmp / "work"
            work.mkdir()
            observed = probe_json(ANY_PS, "cleanup", tmp, dir=work, value="locked")
        self.assertEqual(1, observed["backupsRemaining"])
        self.assertEqual(
            "EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE", observed["supportRef"]
        )
        self.assertTrue(
            observed["firstStillThere"], "the undeletable backup is retained as inert residue"
        )
        self.assertFalse(
            observed["secondStillThere"],
            "one failure must not abandon the rest of the reap",
        )

    def test_retained_cleanup_residue_still_satisfies_the_class_b_contract(self):
        """The installer never leaves an arbitrary filename in the launcher root.

        A retained backup must still parse as recognised residue, so the next launcher
        preflight passes rather than failing closed on a file the installer itself
        deliberately left and called inert.
        """
        with TemporaryScratch() as tmp:
            work = tmp / "work"
            work.mkdir()
            observed = probe_json(ANY_PS, "cleanup", tmp, dir=work, value="locked")
        self.assertTrue(observed["retainedIsClassB"])
        self.assertTrue(observed["retainedName"].startswith(RESIDUE_PREFIX))
        self.assertFalse(observed["retainedName"].endswith(".ps1"))

    def test_cleanup_never_deletes_a_file_this_transaction_did_not_create(self):
        """Only backups identified by THIS transaction's exact reserved name are reaped."""
        for variant in ("clean", "locked"):
            with self.subTest(variant=variant):
                with TemporaryScratch() as tmp:
                    work = tmp / "work"
                    work.mkdir()
                    observed = probe_json(
                        ANY_PS, "cleanup", tmp, dir=work, value=variant
                    )
                self.assertTrue(
                    observed["foreignStillThere"],
                    "a validly named backup from a different transaction must survive",
                )
                self.assertEqual("FOREIGN-RESIDUE", observed["foreignText"])

    def test_a_clean_cleanup_reaps_every_backup_and_reports_none_remaining(self):
        """The ordinary post-acceptance path reaps exactly this transaction's backups."""
        with TemporaryScratch() as tmp:
            work = tmp / "work"
            work.mkdir()
            observed = probe_json(ANY_PS, "cleanup", tmp, dir=work, value="clean")
        self.assertEqual(0, observed["backupsRemaining"])
        self.assertEqual("", observed["supportRef"])
        self.assertFalse(observed["firstStillThere"])
        self.assertFalse(observed["secondStillThere"])


class PostAcceptanceCleanupStructuralGuard(TierCBase):
    """Task 12, Tier C: cleanup authority belongs to the Phase 4 commit step alone."""

    def test_cleanup_is_called_once_and_only_after_the_failure_path_has_exited(self):
        """A cleanup failure cannot reach a rollback, because rollback has already exited.

        The installer's pre-acceptance failure block exits before the cleanup call site is
        reached, so the design's rule that an accepted installation is never rolled back
        over a redundant artefact is structural rather than a matter of ordering luck.
        """
        text = INSTALLER.read_text(encoding="utf-8")
        call_sites = [
            index for index in range(len(text))
            if text.startswith("Invoke-EgPostAcceptanceBackupCleanup", index)
        ]
        self.assertEqual(
            1, len(call_sites), "there must be exactly one cleanup call site"
        )
        rollback_sites = [
            index for index in range(len(text))
            if text.startswith("Invoke-EgPackageRollback", index)
        ]
        self.assertEqual(1, len(rollback_sites))
        self.assertLess(
            rollback_sites[0],
            call_sites[0],
            "cleanup must be reachable only after the failure path has already exited",
        )
        tail = text[call_sites[0]:]
        self.assertNotIn(
            "Invoke-EgPackageRollback",
            tail,
            "no rollback may follow the cleanup call site",
        )
        self.assertIn(
            "exit 0",
            tail,
            "a committed installation exits 0 even when cleanup left residue",
        )
        for forbidden in ("InstallRolledBack", "InstallRollbackIncomplete"):
            self.assertNotIn(
                forbidden,
                tail,
                "a cleanup failure must not reach a rollback exit band",
            )


CHILD_STUB_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OutPath,
    [int]$ExitCode = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ProcessVariable([string]$Name) {
    return [System.Environment]::GetEnvironmentVariable($Name, 'Process')
}

$username = Get-ProcessVariable 'ENERGYGRID_USERNAME'
$password = Get-ProcessVariable 'ENERGYGRID_PASSWORD'
$cache = Get-ProcessVariable 'PLAYWRIGHT_BROWSERS_PATH'

# PRESENCE and non-emptiness only for the credential variables. Their VALUES are never
# written to disk, emitted, measured, or hashed by this stub. The browser-cache value is a
# path rather than a secret, so it is recorded so the binding can be asserted.
$observed = [ordered]@{
    usernamePresent   = ($null -ne $username)
    passwordPresent   = ($null -ne $password)
    usernameNonEmpty  = (-not [string]::IsNullOrEmpty($username))
    passwordNonEmpty  = (-not [string]::IsNullOrEmpty($password))
    browserCacheValue = ([string]$cache)
    workingDirectory  = (Get-Location).Path
}

$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($OutPath, ($observed | ConvertTo-Json -Depth 8 -Compress), $utf8)
exit $ExitCode
"""


def write_child_stub(tmp):
    """Materialise the scratch child stub. The application itself is never invoked."""
    path = Path(tmp) / "eg_child_stub.ps1"
    path.write_text(CHILD_STUB_SCRIPT, encoding="utf-8")
    return path


def synthetic_credential_values():
    """Per-run throwaway values. Never real credentials, never reused."""
    import secrets

    return (
        "eg-synthetic-user-%s" % secrets.token_hex(8),
        "eg-synthetic-secret-%s" % secrets.token_hex(16),
    )


def make_synthetic_credential_artefact(exe, tmp, artefact_path=None):
    """Export a synthetic same-user DPAPI PSCredential CLIXML into a scratch path."""
    username, password = synthetic_credential_values()
    artefact = Path(artefact_path) if artefact_path else (Path(tmp) / "synthetic.credential.xml")
    spec = write_json(tmp, "cred_spec.json", {"username": username, "password": password})
    result = probe_json(exe, "credmake", tmp, path=artefact, json=spec)
    if not result["exported"]:
        raise AssertionError("the synthetic credential artefact was not written")
    return artefact, username, password


class DpapiCredentialContractMixin:
    """Design section 9.2, asserted on one interpreter.

    Git owns the import, injection, and cleanup BEHAVIOUR. The DPAPI artefact itself, its
    absolute path, the Windows user identity it is bound to, and every value it yields stay
    private and outside the repository.
    """

    exe = None

    def test_a_synthetic_same_user_dpapi_credential_imports_successfully(self):
        """EGRT-T21: the real DPAPI path, end to end, with throwaway values."""
        with TemporaryScratch() as tmp:
            artefact, username, password = make_synthetic_credential_artefact(self.exe, tmp)
            completed = probe(self.exe, "credimport", tmp, path=artefact)
        self.assertEqual(0, completed.returncode, completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertTrue(observed["importSuccess"], observed["importSupportRef"])
        self.assertEqual("", observed["importSupportRef"])
        self.assertFalse(observed["credentialIsNull"])
        self.assertTrue(observed["credentialImportOk"])
        self.assertTrue(observed["usernameNonEmpty"])
        self.assertTrue(observed["passwordNonEmpty"])
        self.assertEqual("", observed["viabilitySupportRef"])

        for stream_name, stream in (("stdout", completed.stdout), ("stderr", completed.stderr)):
            with self.subTest(stream=stream_name):
                self.assertNotIn(username, stream)
                self.assertNotIn(password, stream)

    def test_a_corrupted_or_unreadable_artefact_fails_closed(self):
        """EGRT-T22: absent, truncated, and non-PSCredential artefacts each fail closed."""
        with TemporaryScratch() as tmp:
            absent = tmp / "does_not_exist.credential.xml"
            observed = probe_json(self.exe, "credimport", tmp, path=absent)
            self.assertFalse(observed["importSuccess"])
            self.assertEqual(
                "EG_LAUNCHER_CREDENTIAL_ARTEFACT_MISSING", observed["importSupportRef"]
            )
            self.assertTrue(observed["credentialIsNull"])
            self.assertFalse(observed["credentialImportOk"])

        with TemporaryScratch() as tmp:
            artefact, _user, _password = make_synthetic_credential_artefact(self.exe, tmp)
            raw = artefact.read_bytes()
            artefact.write_bytes(raw[: len(raw) // 3])
            observed = probe_json(self.exe, "credimport", tmp, path=artefact)
            self.assertFalse(observed["importSuccess"])
            self.assertEqual(
                "EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED", observed["importSupportRef"]
            )
            self.assertTrue(observed["credentialIsNull"])

        with TemporaryScratch() as tmp:
            artefact = tmp / "not_a_credential.xml"
            # Valid CLIXML that deserialises to something other than a PSCredential.
            run_ps(
                self.exe,
                _write_scratch_script(
                    tmp,
                    "export_string.ps1",
                    "param([Parameter(Mandatory)][string]$Path)\r\n"
                    "'not-a-credential' | Export-Clixml -LiteralPath $Path\r\n",
                ),
                "-Path", str(artefact),
            )
            observed = probe_json(self.exe, "credimport", tmp, path=artefact)
            self.assertFalse(observed["importSuccess"])
            self.assertEqual(
                "EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED", observed["importSupportRef"]
            )
            self.assertTrue(observed["credentialIsNull"])


def _write_scratch_script(tmp, name, body):
    """Write a tiny scratch helper script and return its path."""
    path = Path(tmp) / name
    path.write_text(body, encoding="utf-8")
    return path


class DpapiCredentialTierA(DpapiCredentialContractMixin, TierABase):
    """Task 13, Tier A."""

    @classmethod
    def setUpClass(cls):
        TierABase.setUpClass()
        cls.exe = ANY_PS

    def _inject(self, tmp, artefact, preset_username=""):
        stub = write_child_stub(tmp)
        work = tmp / "work"
        if not work.exists():
            work.mkdir()
        kwargs = {
            "dir": work,
            "path": artefact,
            "path2": ANY_PS,
            "path3": stub,
        }
        if preset_username:
            kwargs["value2"] = preset_username
        return probe(ANY_PS, "credinject", tmp, **kwargs)

    def test_the_child_stub_observes_both_credential_variables_only_during_execution(self):
        """EGRT-T23: injection is scoped to the bounded child execution."""
        with TemporaryScratch() as tmp:
            artefact, username, password = make_synthetic_credential_artefact(ANY_PS, tmp)
            completed = self._inject(tmp, artefact)
        self.assertEqual(0, completed.returncode, completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertTrue(observed["childStubRan"])
        self.assertEqual(0, observed["childExit"])

        child = json.loads(observed["childObserved"])
        self.assertTrue(child["usernamePresent"], "the child must observe the username")
        self.assertTrue(child["passwordPresent"], "the child must observe the password")
        self.assertTrue(child["usernameNonEmpty"])
        self.assertTrue(child["passwordNonEmpty"])

        for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD"):
            with self.subTest(variable=name):
                self.assertFalse(
                    observed["before"][name]["present"],
                    "%s must not be present before injection" % name,
                )
                self.assertFalse(
                    observed["after"][name]["present"],
                    "%s must not be present after the child exits" % name,
                )

    def test_both_credential_variables_are_restored_exactly_afterwards(self):
        """EGRT-T24: previously absent stays absent; previously present is restored exactly."""
        with TemporaryScratch() as tmp:
            artefact, _user, _password = make_synthetic_credential_artefact(ANY_PS, tmp)
            completed = self._inject(tmp, artefact)
        observed = json.loads(completed.stdout)
        self.assertTrue(observed["restorePass"], observed["restoreSupportRef"])
        self.assertEqual("", observed["restoreSupportRef"])
        for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD"):
            with self.subTest(variable=name, case="previously absent"):
                self.assertFalse(observed["after"][name]["present"])
                self.assertEqual(
                    "",
                    observed["after"][name]["value"],
                    "an absent variable is REMOVED, never left set to an empty string",
                )

        preset = "eg-preexisting-username-value"
        with TemporaryScratch() as tmp:
            artefact, _user, _password = make_synthetic_credential_artefact(ANY_PS, tmp)
            completed = self._inject(tmp, artefact, preset_username=preset)
        observed = json.loads(completed.stdout)
        self.assertTrue(observed["restorePass"], observed["restoreSupportRef"])
        self.assertTrue(observed["before"]["ENERGYGRID_USERNAME"]["present"])
        self.assertEqual(preset, observed["before"]["ENERGYGRID_USERNAME"]["value"])
        self.assertTrue(observed["after"]["ENERGYGRID_USERNAME"]["present"])
        self.assertEqual(
            preset,
            observed["after"]["ENERGYGRID_USERNAME"]["value"],
            "a previously present variable is restored to its EXACT original value",
        )
        self.assertFalse(observed["after"]["ENERGYGRID_PASSWORD"]["present"])

    def test_no_user_or_machine_scope_credential_variable_is_ever_written(self):
        """EGRT-T25, dynamic half: the persistent scopes stay untouched on every path."""
        with TemporaryScratch() as tmp:
            artefact, _user, _password = make_synthetic_credential_artefact(ANY_PS, tmp)
            success = json.loads(self._inject(tmp, artefact).stdout)
        with TemporaryScratch() as tmp:
            absent = tmp / "does_not_exist.credential.xml"
            stub = write_child_stub(tmp)
            work = tmp / "work"
            work.mkdir()
            failure = json.loads(
                probe(
                    ANY_PS, "credinject", tmp,
                    dir=work, path=absent, path2=ANY_PS, path3=stub,
                ).stdout
            )
        self.assertFalse(failure["importSuccess"])
        self.assertFalse(
            failure["childStubRan"],
            "a failed import must not start the child at all",
        )
        for observed, label in ((success, "success"), (failure, "failure")):
            for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD"):
                with self.subTest(path=label, variable=name):
                    self.assertFalse(observed["userScope"][name])
                    self.assertFalse(observed["machineScope"][name])

    def test_no_credential_value_reaches_any_output_surface(self):
        """EGRT-T26, dynamic half: neither value appears anywhere, on success or failure."""
        with TemporaryScratch() as tmp:
            artefact, username, password = make_synthetic_credential_artefact(ANY_PS, tmp)
            completed = self._inject(tmp, artefact)
            surfaces = {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "child_observation_file": (tmp / "work" / "child_observed.json").read_text(
                    encoding="utf-8"
                ),
            }
        for surface, text in surfaces.items():
            for label, secret in (("username", username), ("password", password)):
                with self.subTest(surface=surface, value=label):
                    self.assertNotIn(secret, text)


class DpapiCredentialTierB(DpapiCredentialContractMixin, TierBBase):
    """Task 13, Tier B: the DPAPI path on the Windows PowerShell 5.1 boundary.

    SecureString export is not encrypted outside Windows, so the credential tests are
    pinned to the Windows boundary.
    """

    @classmethod
    def setUpClass(cls):
        TierBBase.setUpClass()
        cls.exe = DESKTOP_PS


class CredentialStaticGuards(TierCBase):
    """Task 13, Tier C: EGRT-T25, EGRT-T26, and EGRT-T49 over the committed runtime.

    On the testability boundary, stated honestly: cross-user and cross-machine rejection
    are NOT testable in hosted CI, because proving the artefact fails to import under a
    different Windows user needs a second interactive account. That gap is covered two
    ways, neither of which pretends to be the missing test. DPAPI CurrentUser protection
    supplies the property by construction rather than through code the launcher could get
    wrong, and the -Key/-SecureKey guard below regresses the part that is actually ours.
    """

    def _runtime_text(self):
        return {path.name: path.read_text(encoding="utf-8")
                for path in existing_runtime_ps1_files()}

    def test_no_committed_runtime_file_writes_a_persistent_environment_scope(self):
        """EGRT-T25, static half: only the Process scope is ever used."""
        for name, text in self._runtime_text().items():
            for number, line in non_comment_lines(text):
                if "SetEnvironmentVariable" not in line:
                    continue
                with self.subTest(runtime_file=name, line=number):
                    self.assertNotIn("'User'", line)
                    self.assertNotIn('"User"', line)
                    self.assertNotIn("'Machine'", line)
                    self.assertNotIn('"Machine"', line)
                    self.assertIn(
                        "'Process'",
                        line,
                        "every environment write must name the Process scope explicitly",
                    )

    def test_no_committed_cleanup_path_disposes_a_pscredential(self):
        """EGRT-T49: PSCredential does not implement IDisposable, so Dispose would throw."""
        for name, text in self._runtime_text().items():
            for number, line in non_comment_lines(text):
                if ".Dispose()" not in line:
                    continue
                with self.subTest(runtime_file=name, line=number):
                    lowered = line.lower()
                    for forbidden in ("credential", "pscredential"):
                        self.assertNotIn(
                            forbidden,
                            lowered,
                            "no cleanup path may dispose a PSCredential",
                        )

    def test_the_committed_import_path_uses_the_user_bound_mechanism_only(self):
        """EGRT-T49 support: a keyed export would silently void the DPAPI binding."""
        found_import = False
        for name, text in self._runtime_text().items():
            for number, line in non_comment_lines(text):
                if "Import-Clixml" not in line:
                    continue
                found_import = True
                with self.subTest(runtime_file=name, line=number):
                    self.assertNotIn("-Key", line)
                    self.assertNotIn("-SecureKey", line)
        self.assertTrue(
            found_import,
            "the credential import path must use Import-Clixml",
        )

    def test_no_committed_runtime_file_emits_a_credential_derived_quantity(self):
        """EGRT-T26, static half: no length, prefix, suffix, or hash of a credential value."""
        forbidden_patterns = (
            r"ENERGYGRID_PASSWORD['\"]?\s*\)?\s*\.Length",
            r"Get-FileHash[^\r\n]*Password",
            r"\$plainPassword\s*\|",
            r"Write-(Host|Output|Verbose|Warning|Error)[^\r\n]*\$?\w*[Pp]assword",
        )
        for name, text in self._runtime_text().items():
            for pattern in forbidden_patterns:
                with self.subTest(runtime_file=name, pattern=pattern):
                    self.assertIsNone(
                        re.search(pattern, text),
                        "%s appears to derive a reportable quantity from a credential"
                        % name,
                    )


if __name__ == "__main__":
    unittest.main()
