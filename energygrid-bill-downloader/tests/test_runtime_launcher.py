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

function Test-ProbeVariablePresent([string]$Name) {
    # Presence is read through BOTH the framework getter and the environment provider. A
    # variable left present with an EMPTY value reads back as $null through the getter
    # while still occupying the process environment block a child process inherits, and an
    # empty GIT_DIR is not the same thing as an absent one: it breaks every subsequent Git
    # invocation. Reading only the getter would let that state pass unnoticed.
    if ($null -ne [System.Environment]::GetEnvironmentVariable($Name, 'Process')) {
        return $true
    }
    return (Test-Path -LiteralPath ('Env:\' + $Name))
}

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
            Set-EgProcessEnvironmentVariable -Name $seededName -Value $seeded[$seededName]
        }

        $before = [ordered]@{}
        foreach ($name in $names) {
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            $before[$name] = [ordered]@{
                present = (Test-ProbeVariablePresent -Name $name)
                value   = ([string]$observed)
            }
        }

        $bound = Invoke-GovernedGit -RepositoryRootPath $Dir -Arguments @('rev-parse', '--show-toplevel')

        $after = [ordered]@{}
        foreach ($name in $names) {
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            $after[$name] = [ordered]@{
                present = (Test-ProbeVariablePresent -Name $name)
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
        # Absence is established through the library's exact-removal helper. Assigning
        # $null directly through the framework leaves the name PRESENT with an empty value
        # on PowerShell 7, so the fixture would then be measuring its own artefact rather
        # than the restoration contract.
        Set-EgProcessEnvironmentVariable -Name 'EG_PROBE_ABSENT_NAME' -Value $null
        Set-EgProcessEnvironmentVariable -Name 'EG_PROBE_PRESENT_NAME' -Value 'original'

        $snapshot = Get-EgProcessEnvironmentSnapshot -Names $names

        # Disturb both names in opposite directions.
        Set-EgProcessEnvironmentVariable -Name 'EG_PROBE_ABSENT_NAME' -Value 'appeared'
        Set-EgProcessEnvironmentVariable -Name 'EG_PROBE_PRESENT_NAME' -Value 'overwritten'

        $restore = Restore-EgProcessEnvironmentSnapshot -Snapshot $snapshot

        $absentAfter = [System.Environment]::GetEnvironmentVariable('EG_PROBE_ABSENT_NAME', 'Process')
        $presentAfter = [System.Environment]::GetEnvironmentVariable('EG_PROBE_PRESENT_NAME', 'Process')

        [ordered]@{
            restorePass       = $restore.Pass
            restoreSupportRef = $restore.SupportRef
            absentPresent     = (Test-ProbeVariablePresent -Name 'EG_PROBE_ABSENT_NAME')
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
    'ordinalsort' {
        # Ordinal ordering, asserted directly. -Json is the PATH to a JSON file carrying
        # { names: [...] }. The expected order is fixed by character code, so it is the
        # same answer on every PowerShell edition and in every culture.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $supplied = [string[]]@($spec.names)
        [ordered]@{
            ordinal = @(Sort-EgOrdinalStringList -Value $supplied)
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
        # Constructed through .NET rather than ConvertTo-SecureString, so the fixture does
        # not depend on the Microsoft.PowerShell.Security module being autoloadable. A host
        # whose module path does not resolve it would otherwise fail here rather than
        # exercising the credential contract.
        $secure = New-Object System.Security.SecureString
        foreach ($character in ([string]$spec.password).ToCharArray()) {
            $secure.AppendChar($character)
        }
        $secure.MakeReadOnly()
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
            Set-EgProcessEnvironmentVariable -Name $names[0] -Value $Value2
        }
        else {
            Set-EgProcessEnvironmentVariable -Name $names[0] -Value $null
        }
        Set-EgProcessEnvironmentVariable -Name $names[1] -Value $null

        $before = [ordered]@{}
        foreach ($name in $names) {
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            $before[$name] = [ordered]@{ present = (Test-ProbeVariablePresent -Name $name); value = ([string]$observed) }
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
            $after[$name] = [ordered]@{ present = (Test-ProbeVariablePresent -Name $name); value = ([string]$observed) }
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
    'browsercache' {
        # The positive readiness check over a scratch cache. Read-only by contract:
        # nothing is created, downloaded, or repaired.
        $result = Test-EgBrowserCacheReady -BrowserCachePath $Path
        [ordered]@{
            pass          = $result.Pass
            supportRef    = $result.SupportRef
            checkNames    = @($result.Checks.Keys)
            checkOutcomes = @($result.Checks.Values)
            variableName  = $script:EgBrowserCacheVariableName
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'cachebind' {
        # Bind the supplied cache explicitly for a bounded child execution, then restore
        # the prior process-scope value exactly. -Value2 optionally pre-sets a CONFLICTING
        # ambient value, which must be overridden rather than honoured.
        $name = $script:EgBrowserCacheVariableName

        if ($Value2 -ne '') {
            Set-EgProcessEnvironmentVariable -Name $name -Value $Value2
        }
        else {
            Set-EgProcessEnvironmentVariable -Name $name -Value $null
        }

        $observedBefore = [System.Environment]::GetEnvironmentVariable($name, 'Process')
        $before = [ordered]@{
            present = (Test-ProbeVariablePresent -Name $name)
            value   = ([string]$observedBefore)
        }

        $ready = Test-EgBrowserCacheReady -BrowserCachePath $Path
        $childOutPath = Join-Path $Dir 'child_observed.json'
        $childExit = -1
        $restorePass = $false

        if ($ready.Pass) {
            $stubArguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-File', $Path3, '-OutPath', $childOutPath)
            $variables = [ordered]@{}
            $variables[$name] = $Path
            $injected = Invoke-EgWithInjectedProcessEnvironment -Variables $variables -Body {
                $started = Start-Process -FilePath $Path2 -ArgumentList $stubArguments `
                    -NoNewWindow -Wait -PassThru
                $started.ExitCode
            }
            $childExit = $injected.BodyResult
            $restorePass = $injected.Restore.Pass
        }

        $observedAfter = [System.Environment]::GetEnvironmentVariable($name, 'Process')
        $childObserved = ''
        if (Test-Path -LiteralPath $childOutPath -PathType Leaf) {
            $childObserved = [System.IO.File]::ReadAllText($childOutPath)
        }

        [ordered]@{
            readyPass     = $ready.Pass
            readySupport  = $ready.SupportRef
            childStubRan  = (Test-Path -LiteralPath $childOutPath -PathType Leaf)
            childExit     = $childExit
            childObserved = $childObserved
            restorePass   = $restorePass
            before        = $before
            afterPresent  = (Test-ProbeVariablePresent -Name $name)
            afterValue    = ([string]$observedAfter)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'sourceintegrity' {
        # Path-scoped governed source integrity over a scratch checkout. -Dir is the
        # checkout root, -Value the expected branch, and -Dir2 an optional decoy repository
        # to seed ambient Git variables at.
        if ($Dir2 -ne '') {
            Set-EgProcessEnvironmentVariable -Name 'GIT_DIR' -Value (Join-Path $Dir2 '.git')
            Set-EgProcessEnvironmentVariable -Name 'GIT_WORK_TREE' -Value $Dir2
            Set-EgProcessEnvironmentVariable -Name 'GIT_CONFIG_GLOBAL' -Value (Join-Path $Dir2 'decoy.gitconfig')
        }

        $result = Test-EgGovernedSourceIntegrity -CheckoutRootPath $Dir -ExpectedBranch $Value
        [ordered]@{
            pass           = $result.Pass
            supportRef     = $result.SupportRef
            checkNames     = @($result.Checks.Keys)
            checkOutcomes  = @($result.Checks.Values)
            governedPaths  = @(Get-EgGovernedSourcePaths)
            anyBranch      = $script:EgAnyBranchSentinel
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'bytecode' {
        # The ONE sanctioned overlay exception. -Json is the PATH to a JSON file carrying
        # { paths: [...] }.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $verdicts = @()
        foreach ($relativePath in @($spec.paths)) {
            $verdicts = $verdicts + ([ordered]@{
                path      = $relativePath
                sanctioned = (Test-EgIsSanctionedBytecodeArtefact -RelativePath $relativePath)
            })
        }
        [ordered]@{ verdicts = @($verdicts) } | ConvertTo-Json -Depth 8 -Compress
    }
    'supportrefs' {
        # The bounded, closed vocabulary and its exact-membership predicate.
        $live = @(Get-EgLauncherSupportRefs)
        $retired = @(Get-EgLauncherRetiredSupportRefs)
        [ordered]@{
            live            = @($live)
            liveCount       = @($live).Count
            retired         = @($retired)
            retiredCount    = @($retired).Count
            knownIsLive     = (Test-EgLauncherSupportRefLive -SupportRef 'EG_LAUNCHER_UNCLASSIFIED')
            unknownIsLive   = (Test-EgLauncherSupportRefLive -SupportRef 'EG_LAUNCHER_NOT_A_REAL_REFERENCE')
            lowercaseIsLive = (Test-EgLauncherSupportRefLive -SupportRef 'eg_launcher_unclassified')
            emptyIsLive     = (Test-EgLauncherSupportRefLive -SupportRef '')
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
            # Unwrap parentheses and array concatenation until the leftmost expression is
            # reached, so a parenthesised array plus array still reads as array-shaped.
            $candidate = $argumentsValue
            $unwrapping = $true
            while ($unwrapping) {
                $unwrapping = $false
                if ($candidate -is [System.Management.Automation.Language.ParenExpressionAst]) {
                    $inner = $candidate.Pipeline
                    if ($inner -is [System.Management.Automation.Language.PipelineAst]) {
                        $elements = @($inner.PipelineElements)
                        if ($elements.Count -eq 1) {
                            if ($elements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
                                $candidate = $elements[0].Expression
                                $unwrapping = $true
                                continue
                            }
                        }
                    }
                }
                if ($candidate -is [System.Management.Automation.Language.BinaryExpressionAst]) {
                    $candidate = $candidate.Left
                    $unwrapping = $true
                }
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


FUNCTION_BODY_TEXT_INSPECTOR = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Target)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$tokens, [ref]$errors)
$parseErrors = 0
if ($null -ne $errors) { $parseErrors = @($errors).Count }

$bodies = @()
foreach ($function in @($ast.FindAll(
    { param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))) {
    $bodies = $bodies + ([ordered]@{
        name = $function.Name
        body = $function.Body.Extent.Text
    })
}

[ordered]@{
    parseErrors = $parseErrors
    functions   = @($bodies)
} | ConvertTo-Json -Depth 8 -Compress
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
        observed["refusedType"] = ""
        return observed


class AtomicFileReplaceContractMixin:
    """The design section 7.2 replacement contract, asserted on one interpreter."""

    exe = None

    def test_valid_explicit_backup_replacement_succeeds(self):
        """EGRT-T06: a valid explicit-backup replacement succeeds and carries the source."""
        observed = replace_probe(self.exe, "ok")
        self.assertFalse(
            observed["refused"],
            "the fixture threw before producing a result: %s" % observed["refusedType"],
        )
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
                self.assertFalse(
                    observed["refused"],
                    "the fixture threw before producing a result: %s"
                    % observed["refusedType"],
                )
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
        """EGRT-T11: after a verified publication and after Case B, the backup is retained."""
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
        """An unparsable manifest is terminal and distinguishable from a missing one.

        A manifest document is defined as a single JSON OBJECT, so anything that does not
        parse to exactly one object is not a readable manifest and is reported as
        unparsable rather than as a content mismatch. That verdict is decided from the
        parse RESULT rather than from whether the parser threw, because the two PowerShell
        editions disagree about which malformed documents throw: 5.1 rejects a truncated
        array while 7 can accept it. Checking the result gives one answer on both.
        """
        cases = {
            "truncated_object": "{ not json at all",
            "empty_document": "",
            "truncated_array": "[1,2,3",
            "valid_array": "[1,2,3]",
            "empty_array": "[]",
            "bare_string": '"scalar"',
            "bare_number": "5",
            "bare_boolean": "true",
        }
        for label, raw in cases.items():
            with self.subTest(document=label):
                observed = manifest_probe(ANY_PS, manifest_raw=raw)
                self.assertFalse(observed["pass"])
                self.assertEqual(
                    "EG_LAUNCHER_MANIFEST_UNPARSABLE",
                    observed["supportRef"],
                    "%r is not a readable manifest document" % label,
                )

    def test_a_syntactically_valid_object_with_wrong_content_is_a_mismatch(self):
        """The distinction is preserved: a readable object with wrong fields is MISMATCH.

        This is the other half of the classification. If the unparsable verdict were
        widened carelessly it would swallow this case, and the two causes would stop being
        distinguishable on any edition.
        """
        payload = build_manifest_payload()
        payload["members"][0]["sha256"] = "f" * 64
        observed = manifest_probe(ANY_PS, manifest=payload)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISMATCH", observed["supportRef"])

        observed = manifest_probe(ANY_PS, manifest_raw='{"unexpected": "object"}')
        self.assertFalse(observed["pass"])
        self.assertEqual(
            "EG_LAUNCHER_MANIFEST_MISMATCH",
            observed["supportRef"],
            "a readable JSON object is a mismatch, never unparsable",
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

    def test_member_ordering_is_ordinal_and_not_culture_dependent(self):
        """Manifest order is part of the serialised bytes, so it must not vary by edition.

        Culture-aware comparison orders punctuation differently between Windows PowerShell
        5.1 and PowerShell 7, which would make identical input produce different bytes on
        different hosts and break hash idempotency. Ordinal ordering is fixed by character
        code, so the expected answer below is the same everywhere.
        """
        cases = {
            # '-' 45, '.' 46, 'X' 88, '_' 95
            "punctuation_spread": (
                ["a_b", "aXb", "a.b", "a-b"],
                ["a-b", "a.b", "aXb", "a_b"],
            ),
            # The real member pair, whose order actually diverged between editions.
            "manifest_members": (
                ["launcher_lib.ps1", "launcher.ps1"],
                ["launcher.ps1", "launcher_lib.ps1"],
            ),
            # Ordinal is case-sensitive: uppercase sorts before lowercase.
            "case_sensitivity": (["b", "A", "a", "B"], ["A", "B", "a", "b"]),
            "already_ordered": (["a", "b"], ["a", "b"]),
            "single": (["only"], ["only"]),
        }
        for label, (supplied, expected) in cases.items():
            with self.subTest(case=label):
                with TemporaryScratch() as tmp:
                    spec = write_json(tmp, "ordinal_spec.json", {"names": supplied})
                    observed = probe_json(ANY_PS, "ordinalsort", tmp, json=spec)
                self.assertEqual(expected, observed["ordinal"])

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


BROWSER_CACHE_VARIABLE = "PLAYWRIGHT_BROWSERS_PATH"


def snapshot_tree(root):
    """Map each relative path under root to its size, modification time, and hash."""
    root = Path(root)
    if not root.exists():
        return {}
    state = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            state[relative] = ("dir", 0, 0.0, "")
        else:
            stat = path.stat()
            state[relative] = ("file", stat.st_size, stat.st_mtime, sha256_of(path))
    return state


def build_scratch_browser_cache(root, provisioned=True, executable="chrome.exe",
                                child="chromium-1234"):
    """Create a scratch cache that LOOKS provisioned without downloading anything."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if not provisioned:
        return root
    target = root / child / "chrome-win"
    target.mkdir(parents=True, exist_ok=True)
    (target / executable).write_bytes(b"")
    return root


class BrowserCacheBinding(TierABase):
    """Task 14: design section 9.3.

    The launcher must positively bind the approved private browser cache and fail closed.
    It must never silently fall through to an ambient or default Playwright cache, because
    a run that quietly uses an unreviewed browser cache is a run whose behaviour nobody
    approved. The launcher never installs, updates, repairs, or downloads into the cache:
    provisioning stays an operator action.
    """

    def _readiness(self, cache_path):
        with TemporaryScratch() as tmp:
            return probe_json(ANY_PS, "browsercache", tmp, path=cache_path)

    def test_the_bound_variable_is_the_playwright_browsers_path(self):
        """The cache reaches the child through the variable the application expects."""
        with TemporaryScratch() as tmp:
            cache = build_scratch_browser_cache(tmp / "cache")
            observed = probe_json(ANY_PS, "browsercache", tmp, path=cache)
        self.assertEqual(BROWSER_CACHE_VARIABLE, observed["variableName"])
        self.assertEqual(["browser_cache_ready"], observed["checkNames"])

    def test_a_provisioned_cache_passes_the_positive_readiness_check(self):
        """Readiness confirms a provisioned Chromium is present, not merely a directory."""
        for executable in ("chrome.exe", "headless_shell.exe"):
            for child in ("chromium-1234", "chromium_1234"):
                with self.subTest(executable=executable, child=child):
                    with TemporaryScratch() as tmp:
                        cache = build_scratch_browser_cache(
                            tmp / "cache", executable=executable, child=child
                        )
                        observed = probe_json(ANY_PS, "browsercache", tmp, path=cache)
                    self.assertTrue(observed["pass"], observed["supportRef"])
                    self.assertEqual("", observed["supportRef"])
                    self.assertEqual(["PASS"], observed["checkOutcomes"])

    def test_a_missing_unreadable_or_unprovisioned_cache_fails_closed(self):
        """EGRT-T29: three sub-cases, each terminal, with no fallback to the default."""
        with TemporaryScratch() as tmp:
            observed = probe_json(ANY_PS, "browsercache", tmp, path=(tmp / "absent_cache"))
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED", observed["supportRef"])

        with TemporaryScratch() as tmp:
            cache = build_scratch_browser_cache(tmp / "cache", provisioned=False)
            observed = probe_json(ANY_PS, "browsercache", tmp, path=cache)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_BROWSER_CACHE_NOT_READY", observed["supportRef"])

        with TemporaryScratch() as tmp:
            cache = tmp / "cache"
            (cache / "chromium-1234" / "chrome-win").mkdir(parents=True)
            observed = probe_json(ANY_PS, "browsercache", tmp, path=cache)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_BROWSER_CACHE_NOT_READY", observed["supportRef"])

        with TemporaryScratch() as tmp:
            cache = tmp / "cache"
            (cache / "firefox-1234").mkdir(parents=True)
            (cache / "firefox-1234" / "firefox.exe").write_bytes(b"")
            observed = probe_json(ANY_PS, "browsercache", tmp, path=cache)
        self.assertFalse(
            observed["pass"], "a non-Chromium browser directory is not readiness"
        )
        self.assertEqual("EG_LAUNCHER_BROWSER_CACHE_NOT_READY", observed["supportRef"])

    def test_an_explicit_private_cache_path_reaches_the_child_stub(self):
        """EGRT-T27: the child observes the supplied path as the browser-cache variable."""
        with TemporaryScratch() as tmp:
            cache = build_scratch_browser_cache(tmp / "cache")
            work = tmp / "work"
            work.mkdir()
            stub = write_child_stub(tmp)
            observed = probe_json(
                ANY_PS, "cachebind", tmp,
                dir=work, path=cache, path2=ANY_PS, path3=stub,
            )
        self.assertTrue(observed["readyPass"])
        self.assertTrue(observed["childStubRan"])
        self.assertEqual(0, observed["childExit"])
        child = json.loads(observed["childObserved"])
        self.assertTrue(
            same_path(child["browserCacheValue"], cache),
            "the child observed %r rather than the supplied cache"
            % child["browserCacheValue"],
        )

    def test_a_conflicting_ambient_value_cannot_override_the_supplied_binding(self):
        """EGRT-T28: an ambient value pointing elsewhere is overridden, never honoured."""
        with TemporaryScratch() as tmp:
            cache = build_scratch_browser_cache(tmp / "cache")
            decoy = build_scratch_browser_cache(tmp / "decoy_cache")
            work = tmp / "work"
            work.mkdir()
            stub = write_child_stub(tmp)
            observed = probe_json(
                ANY_PS, "cachebind", tmp,
                dir=work, path=cache, path2=ANY_PS, path3=stub, value2=str(decoy),
            )
        child = json.loads(observed["childObserved"])
        self.assertTrue(
            same_path(child["browserCacheValue"], cache),
            "the supplied binding must win over the ambient value",
        )
        self.assertFalse(same_path(child["browserCacheValue"], decoy))
        self.assertTrue(observed["afterPresent"])
        self.assertTrue(
            same_path(observed["afterValue"], decoy),
            "the prior ambient value must be restored exactly",
        )

    def test_the_prior_browser_cache_value_is_restored_exactly(self):
        """EGRT-T30: previously absent stays absent; previously present is restored."""
        with TemporaryScratch() as tmp:
            cache = build_scratch_browser_cache(tmp / "cache")
            work = tmp / "work"
            work.mkdir()
            stub = write_child_stub(tmp)
            observed = probe_json(
                ANY_PS, "cachebind", tmp,
                dir=work, path=cache, path2=ANY_PS, path3=stub,
            )
        self.assertTrue(observed["restorePass"])
        self.assertFalse(observed["before"]["present"])
        self.assertFalse(
            observed["afterPresent"],
            "an absent variable is REMOVED, never left set to an empty string",
        )
        self.assertEqual("", observed["afterValue"])

    def test_a_failing_readiness_check_starts_no_child_and_does_not_fall_back(self):
        """There is no fallback. An unprovisioned cache fails the run."""
        with TemporaryScratch() as tmp:
            cache = build_scratch_browser_cache(tmp / "cache", provisioned=False)
            work = tmp / "work"
            work.mkdir()
            stub = write_child_stub(tmp)
            observed = probe_json(
                ANY_PS, "cachebind", tmp,
                dir=work, path=cache, path2=ANY_PS, path3=stub,
            )
        self.assertFalse(observed["readyPass"])
        self.assertEqual("EG_LAUNCHER_BROWSER_CACHE_NOT_READY", observed["readySupport"])
        self.assertFalse(observed["childStubRan"], "no child may start")
        self.assertFalse(observed["afterPresent"])

    def test_the_readiness_probe_never_writes_into_the_browser_cache(self):
        """The launcher binds and validates the cache; it never writes into it."""
        with TemporaryScratch() as tmp:
            cache = build_scratch_browser_cache(tmp / "cache")
            work = tmp / "work"
            work.mkdir()
            stub = write_child_stub(tmp)
            before = snapshot_tree(cache)
            probe_json(
                ANY_PS, "cachebind", tmp,
                dir=work, path=cache, path2=ANY_PS, path3=stub,
            )
            after = snapshot_tree(cache)
        self.assertEqual(
            before, after, "the browser cache must be byte-identical afterwards"
        )


class BrowserCacheStaticGuard(TierCBase):
    """Task 14, Tier C: the runtime never provisions, installs, or downloads a browser."""

    def test_no_committed_runtime_file_provisions_or_downloads_a_browser(self):
        """Provisioning stays an operator action, exactly as the runbook already requires."""
        # The cache VARIABLE name legitimately contains the word playwright, so the guard
        # targets provisioning invocations and network transports rather than the name.
        forbidden = (
            "playwright install",
            "-m playwright",
            "Invoke-WebRequest",
            "Invoke-RestMethod",
            "Start-BitsTransfer",
            "System.Net.WebClient",
            "System.Net.Http",
        )
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(runtime_file=path.name, token=token):
                    self.assertNotIn(
                        token.lower(),
                        text.lower(),
                        "%s must not reference %r" % (path.name, token),
                    )

    def test_no_committed_runtime_file_mutates_a_browser_cache_path(self):
        """No mutating command is applied to a path derived from the cache binding."""
        mutating = ("New-Item", "Copy-Item", "Remove-Item", "Move-Item", "Rename-Item")
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for number, line in non_comment_lines(text):
                if "BrowserCache" not in line:
                    continue
                for command in mutating:
                    with self.subTest(runtime_file=path.name, line=number, command=command):
                        self.assertNotIn(
                            command,
                            line,
                            "%s line %d applies %s to a browser-cache path"
                            % (path.name, number, command),
                        )


# --------------------------------------------------------------------------------------
# Launcher-root write authority fixtures (design section 17.2.1)
# --------------------------------------------------------------------------------------
# Every security identifier literal this module is permitted to contain. All are
# CONSTRUCTED or WELL-KNOWN values; none is read from the host, and no host identity is
# ever committed. The current user's identifier and the disable-candidate group are
# discovered at runtime inside the fixture and never travel through this module.
SYNTHETIC_SIDS = (
    "S-1-1-0",              # World, used as an unauthorised write-capable trustee
    "S-1-5-80-0",           # an unrelated service-class identifier, for the no-wildcard case
    "S-1-3-0",              # CREATOR OWNER, the refused placeholder
    "S-1-3-4",              # OWNER RIGHTS, which bounds an owner's implicit rights
)

# Well-known logon-session and authentication identities that are safe to mark deny-only in
# a restricted token: no system file grants access through them, so the child process still
# starts. The fixture picks the first one actually present in the running token.
DISABLE_CANDIDATE_SIDS = (
    "S-1-5-4",      # INTERACTIVE
    "S-1-2-1",      # CONSOLE LOGON
    "S-1-5-3",      # BATCH
    "S-1-5-2",      # NETWORK
    "S-1-5-64-36",  # Cloud Account Authentication
    "S-1-5-64-10",  # NTLM Authentication
    "S-1-5-113",    # Local account
    "S-1-5-15",     # This Organization
    "S-1-2-0",      # LOCAL
)

# The eight write-capable rights, as the .NET FileSystemRights names the fixture applies.
# The library declares the mask; these are the names that produce each bit.
WRITE_CAPABLE_RIGHT_NAMES = (
    "WriteData",                     # FILE_WRITE_DATA / FILE_ADD_FILE
    "AppendData",                    # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    "WriteExtendedAttributes",       # FILE_WRITE_EA
    "DeleteSubdirectoriesAndFiles",  # FILE_DELETE_CHILD
    "WriteAttributes",               # FILE_WRITE_ATTRIBUTES
    "Delete",                        # DELETE
    "ChangePermissions",             # WRITE_DAC
    "TakeOwnership",                 # WRITE_OWNER
)

EXPECTED_WRITE_CAPABLE_MASK = 0x000D0156

ACL_PROBE_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Lib,
    [Parameter(Mandatory)][string]$Op,
    [Parameter(Mandatory)][string]$Root,
    [string]$Shape = '',
    [string]$Right = '',
    [string]$Json = '',
    [string]$OutFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. $Lib

$Rights = [System.Security.AccessControl.FileSystemRights]
$selfSid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User
$worldSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-1-0')
$serviceSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-80-0')
$creatorOwnerSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-3-0')
$ownerRightsSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-3-4')
$readOnlyRights = $Rights::ReadAndExecute -bor $Rights::ReadPermissions

function Get-DisableCandidateSid {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $present = @()
    foreach ($group in $identity.Groups) { $present = $present + $group.Value }
    foreach ($candidate in @('S-1-5-4', 'S-1-2-1', 'S-1-5-3', 'S-1-5-2', 'S-1-5-64-36',
                             'S-1-5-64-10', 'S-1-5-113', 'S-1-5-15', 'S-1-2-0')) {
        if ($present -contains $candidate) {
            return (New-Object System.Security.Principal.SecurityIdentifier($candidate))
        }
    }
    return $null
}

function Get-ObservedOwnerSid([string]$Path) {
    # The owner Windows actually assigned. Creating a directory under an elevated token
    # makes the Administrators group the owner rather than the running user, so the trustee
    # fixture must read the owner instead of assuming it.
    $descriptor = Get-EgSecurityDescriptorForPath -Path $Path
    if ($null -eq $descriptor) { return $null }
    return $descriptor.GetOwner([System.Security.Principal.SecurityIdentifier])
}

function Resolve-SidTokenString([string]$Token) {
    # Placeholders resolve to a runtime-discovered identifier; anything else is passed
    # through VERBATIM, so a malformed value reaches the library's admission check rather
    # than being rejected by the fixture.
    if ($Token -ceq 'SELF') { return $selfSid.Value }
    if ($Token -ceq 'OWNER') {
        $owner = Get-ObservedOwnerSid -Path $Root
        if ($null -eq $owner) { return '' }
        return $owner.Value
    }
    if ($Token -ceq 'WORLD') { return $worldSid.Value }
    if ($Token -ceq 'SERVICE') { return $serviceSid.Value }
    if ($Token -ceq 'CREATOR_OWNER') { return $creatorOwnerSid.Value }
    if ($Token -ceq 'GROUP') {
        $candidate = Get-DisableCandidateSid
        if ($null -eq $candidate) { return '' }
        return $candidate.Value
    }
    return $Token
}

# FIXTURE MECHANISM ONLY. The accepted production security algorithm is not involved in,
# and is not changed by, anything below: the launcher reads descriptors through
# Get-EgSecurityDescriptorForPath and never writes one.
#
# [System.IO.Directory]::GetAccessControl and its SetAccessControl counterpart are .NET
# FRAMEWORK statics. They do not exist on the modern .NET that PowerShell 7 runs on, so the
# fixture could not even build its scratch descriptors there. The read path now uses the
# DirectorySecurity and FileSecurity constructors, which exist on both editions and are the
# same mechanism the production library already relies on. The write path has no single
# API present on both, so the fixture selects whichever this runtime actually provides,
# by capability rather than by exception handling, and fails loudly if neither exists.
#
# The ACL test semantics are unchanged: the same descriptors are constructed and the same
# assertions run against them.

function Get-ObjectSecurity([string]$Path) {
    $sections = [System.Security.AccessControl.AccessControlSections]::Owner -bor
        [System.Security.AccessControl.AccessControlSections]::Group -bor
        [System.Security.AccessControl.AccessControlSections]::Access
    if (Test-Path -LiteralPath $Path -PathType Container) {
        return (New-Object System.Security.AccessControl.DirectorySecurity($Path, $sections))
    }
    return (New-Object System.Security.AccessControl.FileSecurity($Path, $sections))
}

function Save-ObjectSecurity([string]$Path, $Acl) {
    if (Test-Path -LiteralPath $Path -PathType Container) {
        $info = New-Object System.IO.DirectoryInfo($Path)
    }
    else {
        $info = New-Object System.IO.FileInfo($Path)
    }

    # Windows PowerShell 5.1 on .NET Framework exposes the instance method.
    if ($null -ne $info.PSObject.Methods['SetAccessControl']) {
        $info.SetAccessControl($Acl)
        return
    }

    # Modern .NET moved the same operation onto FileSystemAclExtensions.
    if ($null -ne ([System.Management.Automation.PSTypeName]'System.IO.FileSystemAclExtensions').Type) {
        [System.IO.FileSystemAclExtensions]::SetAccessControl($info, $Acl)
        return
    }

    # Last resort, still the same operation: the provider cmdlet. Reached only when neither
    # framework surface exists, and guarded by a command lookup because this module has
    # been observed to be unavailable for autoload on some hosts.
    if ($null -ne (Get-Command -Name 'Set-Acl' -ErrorAction SilentlyContinue)) {
        Set-Acl -LiteralPath $Path -AclObject $Acl
        return
    }

    # Never silently skipped. If no mechanism exists the fixture fails loudly, so a missing
    # capability can never be mistaken for a passing security assertion.
    throw 'no access-control write mechanism is available on this PowerShell runtime'
}

function New-Rule([string]$Path, $Sid, $RightsValue, [bool]$Inheritable, [string]$Type) {
    if (Test-Path -LiteralPath $Path -PathType Container) {
        $inheritance = 'None'
        if ($Inheritable) { $inheritance = 'ContainerInherit, ObjectInherit' }
        return (New-Object System.Security.AccessControl.FileSystemAccessRule(
            $Sid, $RightsValue, $inheritance, 'None', $Type))
    }
    return (New-Object System.Security.AccessControl.FileSystemAccessRule($Sid, $RightsValue, $Type))
}

function Set-ProtectedDescriptor([string]$Path, [array]$Entries) {
    # Entries are ordered, and the order is preserved in the emitted list, which is what
    # makes the deny-before-allow and allow-before-deny cases distinguishable.
    $acl = Get-ObjectSecurity -Path $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($existing in @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))) {
        [void]$acl.RemoveAccessRuleSpecific($existing)
    }
    foreach ($entry in $Entries) {
        $acl.AddAccessRule((New-Rule -Path $Path -Sid $entry.Sid -RightsValue $entry.Rights `
            -Inheritable $entry.Inheritable -Type $entry.Type))
    }
    Save-ObjectSecurity -Path $Path -Acl $acl
}

function Set-NullDescriptor([string]$Path) {
    # Windows grants ALL access when an object has no discretionary access list, so this is
    # the fail-closed case both checks must reject.
    $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor(
        ("O:{0}G:{0}" -f $selfSid.Value))
    $bytes = New-Object byte[] $raw.BinaryLength
    $raw.GetBinaryForm($bytes, 0)
    $acl = Get-ObjectSecurity -Path $Path
    $acl.SetSecurityDescriptorBinaryForm($bytes,
        [System.Security.AccessControl.AccessControlSections]::Access)
    Save-ObjectSecurity -Path $Path -Acl $acl
}

function New-Entry($Sid, $RightsValue, [bool]$Inheritable = $false, [string]$Type = 'Allow') {
    return [pscustomobject]@{ Sid = $Sid; Rights = $RightsValue; Inheritable = $Inheritable; Type = $Type }
}

$emitted = switch ($Op) {
    'build' {
        # Build a scratch launcher root and stamp the requested descriptor shape.
        #
        # CLEANUP SAFETY, which constrains every shape below: the OWNER RIGHTS entry removes
        # the owner's implicit WRITE_DAC permanently for a principal without
        # SeTakeOwnershipPrivilege, so a directory carrying it could never be re-permissioned
        # or emptied again. It is therefore applied ONLY to FILES, and only inside a root
        # that keeps full control, so the parent's FILE_DELETE_CHILD always permits teardown.
        if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
            [void](New-Item -ItemType Directory -Path $Root)
        }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        $memberNames = @(Get-EgDeployedPackageMemberNames)
        foreach ($memberName in $memberNames) {
            $memberPath = Join-Path $Root $memberName
            if (-not (Test-Path -LiteralPath $memberPath -PathType Leaf)) {
                [System.IO.File]::WriteAllText($memberPath, ('# ' + $memberName), $utf8)
            }
        }

        $groupSid = Get-DisableCandidateSid
        $ownerSid = Get-ObservedOwnerSid -Path $Root
        if ($null -eq $ownerSid) { throw 'the scratch launcher root has no readable owner' }
        # Granted to the OBSERVED owner. Under an elevated token the owner is the
        # Administrators group rather than the running user, and the trustee check requires
        # every examined object's owner to be inside the supplied set.
        $fullSelf = @(New-Entry $ownerSid $Rights::FullControl)
        $restrictedMember = $null

        if ($Shape -ceq 'authorised_self') {
            Set-ProtectedDescriptor -Path $Root -Entries $fullSelf
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
        }
        elseif ($Shape -ceq 'world_write_root') {
            Set-ProtectedDescriptor -Path $Root -Entries (
                $fullSelf + @(New-Entry $worldSid $Rights::WriteData))
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
        }
        elseif ($Shape -ceq 'world_write_member') {
            Set-ProtectedDescriptor -Path $Root -Entries $fullSelf
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
            Set-ProtectedDescriptor -Path (Join-Path $Root 'launcher_lib.ps1') -Entries (
                $fullSelf + @(New-Entry $worldSid $Rights::WriteData))
        }
        elseif ($Shape -ceq 'world_read_root') {
            Set-ProtectedDescriptor -Path $Root -Entries (
                $fullSelf + @(New-Entry $worldSid $Rights::ReadAndExecute))
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
        }
        elseif ($Shape -ceq 'service_write_root') {
            Set-ProtectedDescriptor -Path $Root -Entries (
                $fullSelf + @(New-Entry $serviceSid $Rights::WriteData))
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
        }
        elseif ($Shape -ceq 'inherited_world_write') {
            # The write-capable entry is INHERITED by the members from the root, never
            # declared on them explicitly.
            Set-ProtectedDescriptor -Path $Root -Entries (
                $fullSelf + @(New-Entry $worldSid $Rights::WriteData $true))
            foreach ($memberName in $memberNames) {
                $memberAcl = Get-ObjectSecurity -Path (Join-Path $Root $memberName)
                $memberAcl.SetAccessRuleProtection($false, $true)
                Save-ObjectSecurity -Path (Join-Path $Root $memberName) -Acl $memberAcl
            }
        }
        elseif ($Shape -ceq 'deny_then_allow_world') {
            Set-ProtectedDescriptor -Path $Root -Entries (
                @(New-Entry $worldSid $Rights::WriteData $false 'Deny') +
                $fullSelf + @(New-Entry $worldSid $Rights::WriteData))
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
        }
        elseif ($Shape -ceq 'allow_then_deny_world') {
            Set-ProtectedDescriptor -Path $Root -Entries (
                $fullSelf + @(New-Entry $worldSid $Rights::WriteData) +
                @(New-Entry $worldSid $Rights::WriteData $false 'Deny'))
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
        }
        elseif ($Shape -ceq 'creator_owner_inheritable') {
            Set-ProtectedDescriptor -Path $Root -Entries (
                $fullSelf + @(New-Entry $creatorOwnerSid $Rights::WriteData $true))
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
        }
        elseif ($Shape -ceq 'null_dacl_member') {
            Set-ProtectedDescriptor -Path $Root -Entries $fullSelf
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
            Set-NullDescriptor -Path (Join-Path $Root 'launcher_lib.ps1')
            $restrictedMember = 'launcher_lib.ps1'
        }
        elseif ($Shape -ceq 'member_no_write') {
            # A package member the running token gets NO write-capable right on. The OWNER
            # RIGHTS entry is what makes that reachable at all, because an owner otherwise
            # always holds WRITE_DAC.
            Set-ProtectedDescriptor -Path $Root -Entries $fullSelf
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
            Set-ProtectedDescriptor -Path (Join-Path $Root 'launcher_lib.ps1') -Entries (
                @(New-Entry $selfSid $readOnlyRights) +
                @(New-Entry $ownerRightsSid $Rights::ReadPermissions))
            $restrictedMember = 'launcher_lib.ps1'
        }
        elseif ($Shape -ceq 'member_one_write_right') {
            # Exactly ONE write-capable right on top of read access. The descriptor never
            # grants all eight, which is what makes a union-of-all-write-rights
            # implementation report the object as safe and therefore fail the assertion.
            Set-ProtectedDescriptor -Path $Root -Entries $fullSelf
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
            $singleRight = [System.Security.AccessControl.FileSystemRights]$Right
            Set-ProtectedDescriptor -Path (Join-Path $Root 'launcher_lib.ps1') -Entries (
                @(New-Entry $selfSid ($readOnlyRights -bor $singleRight)) +
                @(New-Entry $ownerRightsSid $Rights::ReadPermissions))
            $restrictedMember = 'launcher_lib.ps1'
        }
        elseif ($Shape -ceq 'member_group_write') {
            # The member's ONLY write-capable grant is a group identity, and the owner's
            # implicit rights are bounded, so the verdict turns entirely on whether that
            # group identity is enabled in the evaluating token.
            Set-ProtectedDescriptor -Path $Root -Entries $fullSelf
            foreach ($memberName in $memberNames) {
                Set-ProtectedDescriptor -Path (Join-Path $Root $memberName) -Entries $fullSelf
            }
            if ($null -eq $groupSid) {
                throw 'no disable-candidate group identity is present in this token'
            }
            Set-ProtectedDescriptor -Path (Join-Path $Root 'launcher_lib.ps1') -Entries (
                @(New-Entry $selfSid $readOnlyRights) +
                @(New-Entry $ownerRightsSid $Rights::ReadPermissions) +
                @(New-Entry $groupSid $Rights::FullControl))
            $restrictedMember = 'launcher_lib.ps1'
        }
        else {
            throw ('unknown fixture shape: ' + $Shape)
        }

        $groupPresent = ($null -ne $groupSid)
        $emitted = [ordered]@{
            shape            = $Shape
            groupPresent     = $groupPresent
            restrictedMember = ([string]$restrictedMember)
        }
        $emitted | ConvertTo-Json -Depth 8 -Compress
    }
    'check' {
        # Evaluate both write checks over the launcher root and every package member.
        # -Json is the PATH to a JSON file carrying { authorised: [tokens...] }.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $tokens = @($spec.authorised)
        $resolved = @()
        foreach ($token in $tokens) {
            $resolved = $resolved + (Resolve-SidTokenString -Token $token)
        }

        # An empty or null set is refused by the mandatory parameter contract before the
        # function body runs, which is itself the refusal DD-12 requires. The fixture
        # reports that as the same bounded refusal rather than letting it escape.
        $admission = $null
        try {
            $admission = Test-EgAuthorisedWriteSidSet -AuthorisedSid ([string[]]@($resolved))
        }
        catch {
            $admission = [pscustomobject]@{
                Pass       = $false
                SupportRef = 'EG_LAUNCHER_ROOT_AUTHORISED_SID_SET_INVALID'
                Sids       = @()
            }
        }

        $objects = @([pscustomobject]@{ Name = '(root)'; Path = $Root })
        foreach ($memberName in @(Get-EgDeployedPackageMemberNames)) {
            $objects = $objects + ([pscustomobject]@{
                Name = $memberName
                Path = (Join-Path $Root $memberName)
            })
        }

        $tokenResults = @()
        $trusteeResults = @()
        foreach ($object in $objects) {
            $tokenCheck = Test-EgTokenWriteAccessToPath -Path $object.Path
            $tokenResults = $tokenResults + ([ordered]@{
                name            = $object.Name
                anyWriteGranted = $tokenCheck.AnyWriteGranted
                evaluated       = $tokenCheck.Evaluated
                supportRef      = $tokenCheck.SupportRef
            })
            if ($admission.Pass) {
                $trusteeCheck = Test-EgPathWriteTrusteesAuthorised -Path $object.Path `
                    -AuthorisedSid $admission.Sids
                $trusteeResults = $trusteeResults + ([ordered]@{
                    name       = $object.Name
                    authorised = $trusteeCheck.Authorised
                    evaluated  = $trusteeCheck.Evaluated
                    supportRef = $trusteeCheck.SupportRef
                })
            }
        }

        $privileges = Get-EgTokenPrivilegeNames
        [ordered]@{
            privilegeNames      = @($privileges.PrivilegeNames)
            admissionPass       = $admission.Pass
            admissionSupportRef = $admission.SupportRef
            admittedCount       = @($admission.Sids).Count
            mappedMask          = (Get-EgMappedWriteCapableMask)
            declaredMask        = $script:EgWriteCapableAccessMask
            tokenChecks         = @($tokenResults)
            trusteeChecks       = @($trusteeResults)
            privilegeReadOk     = $privileges.ReadOk
            privilegeCount      = @($privileges.PrivilegeNames).Count
            bypassPresent       = (Test-EgBypassPrivilegePresent -PrivilegeName $privileges.PrivilegeNames)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    'predicate' {
        # The bypass-privilege predicate over SUPPLIED observed name lists, plus the
        # constructed-name assertions. -Json is the PATH to { lists: [[names...], ...] }.
        $spec = Get-Content -LiteralPath $Json -Raw | ConvertFrom-Json
        $verdicts = @()
        foreach ($list in @($spec.lists)) {
            $names = [string[]]@($list)
            $verdicts = $verdicts + ([ordered]@{
                names   = @($names)
                present = (Test-EgBypassPrivilegePresent -PrivilegeName $names)
            })
        }
        $observed = Get-EgTokenPrivilegeNames
        [ordered]@{
            verdicts          = @($verdicts)
            bypassNames       = @($script:EgBypassPrivilegeNames)
            observedReadOk    = $observed.ReadOk
            observedCount     = @($observed.PrivilegeNames).Count
            observedHasBypass = (Test-EgBypassPrivilegePresent -PrivilegeName $observed.PrivilegeNames)
        } | ConvertTo-Json -Depth 8 -Compress
    }
    default {
        throw ('unknown acl probe operation: ' + $Op)
    }
}

# A probe launched under a derived token cannot have its standard output piped back by the
# launcher, so the emitted document is additionally written to -OutFile when one is given.
if ($OutFile -ne '') {
    Write-EgUtf8NoBomText -Path $OutFile -Text ([string]$emitted)
}
Write-Output $emitted
"""


RESTRICTED_TOKEN_RUNNER = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$CommandLine,
    [Parameter(Mandatory)][string]$ReportPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# TEST-SIDE interop only. Restricting one's OWN token needs no elevation, no second
# account, and no production launcher root, which is what makes the same-account
# separation case provable in hosted continuous integration. None of this appears in, or
# is reachable from, the committed runtime library: a static guard asserts that the
# runtime never calls CreateRestrictedToken, CreateProcessAsUser, or any impersonation
# entry point.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace EgTestToken {

    [StructLayout(LayoutKind.Sequential)]
    public struct SID_AND_ATTRIBUTES {
        public IntPtr Sid;
        public uint Attributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct STARTUPINFO {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public int dwX;
        public int dwY;
        public int dwXSize;
        public int dwYSize;
        public int dwXCountChars;
        public int dwYCountChars;
        public int dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION {
        public IntPtr hProcess;
        public IntPtr hThread;
        public int dwProcessId;
        public int dwThreadId;
    }

    public class LaunchOutcome {
        public bool Ok;
        public int ExitCode;
        public int LastError;
        public string Stage;
    }

    public static class Runner {
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern IntPtr GetCurrentProcess();
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool OpenProcessToken(IntPtr handle, uint access, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError = true)]
        static extern bool CreateRestrictedToken(IntPtr existing, uint flags,
            uint disableCount, SID_AND_ATTRIBUTES[] sidsToDisable, uint deleteCount,
            IntPtr privilegesToDelete, uint restrictCount, IntPtr sidsToRestrict,
            out IntPtr newToken);
        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        static extern bool CreateProcessAsUser(IntPtr token, string applicationName,
            string commandLine, IntPtr processAttributes, IntPtr threadAttributes,
            bool inheritHandles, uint creationFlags, IntPtr environment,
            string currentDirectory, ref STARTUPINFO startupInfo,
            out PROCESS_INFORMATION processInformation);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool GetExitCodeProcess(IntPtr handle, out int exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool CloseHandle(IntPtr handle);

        const uint TOKEN_ALL_ACCESS = 0xF01FF;

        public static LaunchOutcome Run(byte[] sidBytes, string commandLine) {
            LaunchOutcome outcome = new LaunchOutcome();
            outcome.Ok = false;
            outcome.ExitCode = -1;
            outcome.LastError = 0;
            outcome.Stage = "start";
            IntPtr token = IntPtr.Zero;
            IntPtr restricted = IntPtr.Zero;
            IntPtr sidBuffer = IntPtr.Zero;
            PROCESS_INFORMATION info = new PROCESS_INFORMATION();
            try {
                if (OpenProcessToken(GetCurrentProcess(), TOKEN_ALL_ACCESS, out token) == false) {
                    outcome.Stage = "OpenProcessToken";
                    outcome.LastError = Marshal.GetLastWin32Error();
                    return outcome;
                }
                sidBuffer = Marshal.AllocHGlobal(sidBytes.Length);
                Marshal.Copy(sidBytes, 0, sidBuffer, sidBytes.Length);
                SID_AND_ATTRIBUTES[] disable = new SID_AND_ATTRIBUTES[1];
                disable[0].Sid = sidBuffer;
                disable[0].Attributes = 0;
                if (CreateRestrictedToken(token, 0, 1, disable, 0, IntPtr.Zero, 0, IntPtr.Zero, out restricted) == false) {
                    outcome.Stage = "CreateRestrictedToken";
                    outcome.LastError = Marshal.GetLastWin32Error();
                    return outcome;
                }
                STARTUPINFO startup = new STARTUPINFO();
                startup.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                startup.lpDesktop = null;
                if (CreateProcessAsUser(restricted, null, commandLine, IntPtr.Zero, IntPtr.Zero, false, 0, IntPtr.Zero, null, ref startup, out info) == false) {
                    outcome.Stage = "CreateProcessAsUser";
                    outcome.LastError = Marshal.GetLastWin32Error();
                    return outcome;
                }
                WaitForSingleObject(info.hProcess, 0xFFFFFFFF);
                int code = -1;
                GetExitCodeProcess(info.hProcess, out code);
                outcome.Ok = true;
                outcome.ExitCode = code;
                outcome.Stage = "done";
                return outcome;
            }
            finally {
                if (info.hThread != IntPtr.Zero) { CloseHandle(info.hThread); }
                if (info.hProcess != IntPtr.Zero) { CloseHandle(info.hProcess); }
                if (sidBuffer != IntPtr.Zero) { Marshal.FreeHGlobal(sidBuffer); }
                if (restricted != IntPtr.Zero) { CloseHandle(restricted); }
                if (token != IntPtr.Zero) { CloseHandle(token); }
            }
        }
    }
}
'@

# Pick the first well-known logon-session or authentication identity actually present in
# this token. No system file grants access through any of them, so the child still starts.
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$present = @()
foreach ($group in $identity.Groups) { $present = $present + $group.Value }
$chosen = ''
foreach ($candidate in @('S-1-5-4', 'S-1-2-1', 'S-1-5-3', 'S-1-5-2', 'S-1-5-64-36',
                         'S-1-5-64-10', 'S-1-5-113', 'S-1-5-15', 'S-1-2-0')) {
    if ($present -contains $candidate) { $chosen = $candidate; break }
}

$report = [ordered]@{ chosen = $chosen; ok = $false; exitCode = -1; stage = 'no-candidate'; lastError = 0 }
if ($chosen -ne '') {
    $sid = New-Object System.Security.Principal.SecurityIdentifier($chosen)
    $bytes = New-Object byte[] $sid.BinaryLength
    $sid.GetBinaryForm($bytes, 0)
    $outcome = [EgTestToken.Runner]::Run($bytes, $CommandLine)
    $report['ok'] = $outcome.Ok
    $report['exitCode'] = $outcome.ExitCode
    $report['stage'] = $outcome.Stage
    $report['lastError'] = $outcome.LastError
}

$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($ReportPath, ($report | ConvertTo-Json -Depth 8 -Compress), $utf8)
Write-Output ($report | ConvertTo-Json -Depth 8 -Compress)
"""


def quote_for_command_line(value):
    """Quote one argument for a single Windows command-line string."""
    text = str(value)
    trailing = len(text) - len(text.rstrip("\\"))
    return '"%s%s"' % (text, "\\" * trailing)


def run_acl_probe_under_restricted_token(exe, tmp, root, authorised=("OWNER",)):
    """Evaluate the write checks under a token with one group identity marked deny-only."""
    runner = Path(tmp) / "eg_restricted_runner.ps1"
    runner.write_text(RESTRICTED_TOKEN_RUNNER, encoding="utf-8")
    acl_script = write_acl_probe(tmp)
    spec = write_json(tmp, "restricted_authorised.json", {"authorised": list(authorised)})
    out_file = Path(tmp) / "restricted_check.json"
    report_path = Path(tmp) / "restricted_report.json"

    parts = [
        quote_for_command_line(exe),
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", quote_for_command_line(acl_script),
        "-Lib", quote_for_command_line(LIB),
        "-Op", "check",
        "-Root", quote_for_command_line(root),
        "-Json", quote_for_command_line(spec),
        "-OutFile", quote_for_command_line(out_file),
    ]
    completed = run_ps(
        exe, runner,
        "-CommandLine", " ".join(parts),
        "-ReportPath", str(report_path),
    )
    if completed.returncode != 0:
        raise AssertionError(
            "restricted runner exited %d\nstdout:\n%s\nstderr:\n%s"
            % (completed.returncode, completed.stdout, completed.stderr)
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    observed = None
    if out_file.is_file():
        text = out_file.read_text(encoding="utf-8").strip()
        if text:
            observed = json.loads(text)
    return report, observed


def write_acl_probe(tmp):
    """Materialise the write-authority probe inside a scratch directory."""
    path = Path(tmp) / "eg_acl_probe.ps1"
    path.write_text(ACL_PROBE_SCRIPT, encoding="utf-8")
    return path


def acl_probe(exe, op, tmp, root, **kwargs):
    """Run the write-authority probe and return its parsed JSON."""
    script = write_acl_probe(tmp)
    args = ["-Lib", str(LIB), "-Op", op, "-Root", str(root)]
    args.extend(_named_args(kwargs))
    completed = run_ps(exe, script, *args)
    if completed.returncode != 0:
        raise AssertionError(
            "acl probe %r/%r exited %d\nstdout:\n%s\nstderr:\n%s"
            % (op, kwargs.get("shape", ""), completed.returncode,
               completed.stdout, completed.stderr)
        )
    return json.loads(completed.stdout)


def build_scratch_launcher_root(exe, tmp, shape, right=""):
    """Create a scratch launcher root carrying a deliberately shaped security descriptor."""
    root = Path(tmp) / "launcher_root"
    kwargs = {"shape": shape}
    if right:
        kwargs["right"] = right
    built = acl_probe(exe, "build", tmp, root, **kwargs)
    return root, built


def check_write_authority(exe, tmp, root, authorised=("OWNER",)):
    """Evaluate both launcher-root write checks over a prepared scratch root.

    The default authorised set is the OBSERVED owner of the scratch root, not the running
    user. Creating a directory under an elevated token makes the Administrators group the
    owner, and the trustee check requires every examined object's owner to be inside the
    supplied set, so assuming the running user would make the fixture pass only on
    non-elevated hosts.
    """
    spec = write_json(tmp, "authorised_spec.json", {"authorised": list(authorised)})
    return acl_probe(exe, "check", tmp, root, json=spec)


def outcome_for(results, name):
    """Pick one named object's outcome out of a probe result list."""
    for entry in results:
        if entry["name"] == name:
            return entry
    raise AssertionError("no outcome reported for %r" % name)


GOVERNED_SOURCE_PATHS = (
    "energygrid-bill-downloader/energygrid_bill_downloader",
    "energygrid-bill-downloader/requirements.txt",
)
GOVERNED_EXECUTABLE_SURFACE = "energygrid-bill-downloader/energygrid_bill_downloader"
ANY_BRANCH_SENTINEL = "ANY_BRANCH"

GOVERNED_CHECK_NAMES = (
    "source_repository_binding",
    "source_branch_binding",
    "source_paths_exist",
    "source_paths_tracked",
    "source_no_staged_modification",
    "source_no_unstaged_modification",
    "source_no_tracked_deletion",
    "source_no_untracked_overlay",
)


def build_governed_scratch_repo(tmp, branch="main"):
    """A scratch checkout shaped like the monorepo's EnergyGrid runtime-critical surface."""
    root = init_scratch_repo(tmp / "governed_checkout", branch=branch)
    package = root / GOVERNED_EXECUTABLE_SURFACE
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8", newline="")
    (package / "cli.py").write_text("# scratch cli\r\n", encoding="utf-8", newline="")
    (root / "energygrid-bill-downloader" / "requirements.txt").write_text(
        "playwright==1.61.0\r\n", encoding="utf-8", newline=""
    )
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "unrelated_tool.py").write_text(
        "# unrelated to EnergyGrid\r\n", encoding="utf-8", newline=""
    )
    (root / ".gitignore").write_text("*.ignored\r\n", encoding="utf-8", newline="")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "governed scratch surface")
    return root


class GovernedSourceIntegrity(TierABase):
    """Task 15: design section 10.3, reconciled with DL-XB-141-SCHEDULER-005.

    The repository is a monorepo. Requiring the deployed checkout to sit at one permanently
    fixed whole-repository HEAD would mean any accepted, unrelated merge stops the daily
    job, and any unrelated dirty file elsewhere does the same. That converts routine
    repository activity into an outage. The correct unit of protection is the EnergyGrid
    runtime-critical surface, not the repository.
    """

    def _check(self, root, branch=ANY_BRANCH_SENTINEL, decoy=""):
        with TemporaryScratch() as tmp:
            kwargs = {"dir": root, "value": branch}
            if decoy:
                kwargs["dir2"] = decoy
            return probe_json(ANY_PS, "sourceintegrity", tmp, **kwargs)

    def test_the_governed_surface_is_exactly_two_paths(self):
        """runtime/, tests/, docs/, task-scheduler/, and the example config are excluded.

        runtime/ is deliberately excluded because the launcher executes from the launcher
        root outside the checkout, so its integrity is covered by the installation manifest
        instead. Broadening to the whole monorepo because it is easier is exactly the
        failure this section exists to prevent.
        """
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            observed = self._check(root)
        self.assertEqual(list(GOVERNED_SOURCE_PATHS), observed["governedPaths"])
        self.assertEqual(ANY_BRANCH_SENTINEL, observed["anyBranch"])
        self.assertEqual(list(GOVERNED_CHECK_NAMES), observed["checkNames"])

    def test_a_clean_tracked_governed_surface_passes(self):
        """EGRT-T32."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            observed = self._check(root)
        self.assertTrue(observed["pass"], observed["supportRef"])
        self.assertEqual("", observed["supportRef"])
        self.assertEqual(["PASS"] * 8, observed["checkOutcomes"])

    def test_a_staged_modification_under_a_governed_path_fails(self):
        """EGRT-T33."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            target = root / GOVERNED_EXECUTABLE_SURFACE / "cli.py"
            target.write_text("# staged change\r\n", encoding="utf-8", newline="")
            run_git(root, "add", str(target))
            observed = self._check(root)
        self.assertFalse(observed["pass"])
        self.assertEqual(
            "EG_LAUNCHER_SOURCE_STAGED_MODIFICATION", observed["supportRef"]
        )

    def test_an_unstaged_modification_under_a_governed_path_fails(self):
        """EGRT-T34."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            target = root / "energygrid-bill-downloader" / "requirements.txt"
            target.write_text("playwright==0.0.0\r\n", encoding="utf-8", newline="")
            observed = self._check(root)
        self.assertFalse(observed["pass"])
        self.assertEqual(
            "EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION", observed["supportRef"]
        )

    def test_a_deletion_of_a_tracked_governed_file_fails(self):
        """EGRT-T35: reported as a deletion, distinguishably from a modification."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            (root / GOVERNED_EXECUTABLE_SURFACE / "cli.py").unlink()
            observed = self._check(root)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_SOURCE_DELETED", observed["supportRef"])

        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            run_git(root, "rm", "--cached", "%s/cli.py" % GOVERNED_EXECUTABLE_SURFACE)
            (root / GOVERNED_EXECUTABLE_SURFACE / "cli.py").unlink()
            observed = self._check(root)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_SOURCE_DELETED", observed["supportRef"])

    def test_untracked_overlay_rules_including_the_bytecode_exception(self):
        """EGRT-T36: an ignored overlay ALSO fails; Python bytecode does not."""
        failing = {
            "plain_untracked_python": "overlay.py",
            "gitignore_hidden_overlay": "overlay.ignored",
            "untracked_non_python": "overlay.txt",
        }
        for label, name in failing.items():
            with self.subTest(overlay=label):
                with TemporaryScratch() as tmp:
                    root = build_governed_scratch_repo(tmp)
                    (root / GOVERNED_EXECUTABLE_SURFACE / name).write_text(
                        "# overlay\r\n", encoding="utf-8", newline=""
                    )
                    observed = self._check(root)
                self.assertFalse(
                    observed["pass"],
                    "%s inside the governed executable surface must fail" % label,
                )
                self.assertEqual(
                    "EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY", observed["supportRef"]
                )

        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            cache = root / GOVERNED_EXECUTABLE_SURFACE / "__pycache__"
            cache.mkdir()
            (cache / "cli.cpython-314.pyc").write_bytes(b"\x00")
            (root / GOVERNED_EXECUTABLE_SURFACE / "stray.pyc").write_bytes(b"\x00")
            observed = self._check(root)
        self.assertTrue(
            observed["pass"],
            "Python bytecode is created by normal execution and must not fail the run: %s"
            % observed["supportRef"],
        )

    def test_a_dirty_file_outside_the_governed_paths_does_not_fail_the_run(self):
        """EGRT-T37: scope is the point. Routine repository activity is not an outage."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            (root / "scripts" / "unrelated_tool.py").write_text(
                "# modified elsewhere\r\n", encoding="utf-8", newline=""
            )
            (root / "untracked_at_the_root.txt").write_text(
                "untracked\r\n", encoding="utf-8", newline=""
            )
            (root / "energygrid-bill-downloader" / "docs_note.md").write_text(
                "not governed\r\n", encoding="utf-8", newline=""
            )
            observed = self._check(root)
        self.assertTrue(observed["pass"], observed["supportRef"])

    def test_a_moved_head_with_a_clean_governed_surface_does_not_fail_the_run(self):
        """EGRT-T38: no fixed whole-repository HEAD is required."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            (root / "scripts" / "unrelated_tool.py").write_text(
                "# a later accepted commit\r\n", encoding="utf-8", newline=""
            )
            run_git(root, "add", "-A")
            run_git(root, "commit", "-m", "unrelated accepted movement")
            observed = self._check(root)
        self.assertTrue(observed["pass"], observed["supportRef"])

    def test_ambient_git_variables_cannot_redirect_the_integrity_checks(self):
        """EGRT-T40: a decoy whose governed surface is dirty cannot capture the checks."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            decoy = init_scratch_repo(tmp / "decoy")
            decoy_package = decoy / GOVERNED_EXECUTABLE_SURFACE
            decoy_package.mkdir(parents=True)
            (decoy_package / "cli.py").write_text(
                "# decoy, deliberately untracked\r\n", encoding="utf-8", newline=""
            )
            (decoy / "decoy.gitconfig").write_text(
                "[core]\r\n\tquotepath = false\r\n", encoding="utf-8", newline=""
            )
            observed = self._check(root, decoy=decoy)
        self.assertTrue(
            observed["pass"],
            "the checks must evaluate the real repository: %s" % observed["supportRef"],
        )

    def test_branch_binding_and_the_any_branch_sentinel(self):
        """Binding is never disabled by omission: the sentinel is explicit."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            observed = self._check(root, branch="main")
            self.assertTrue(observed["pass"], observed["supportRef"])

            mismatched = self._check(root, branch="release")
            self.assertFalse(mismatched["pass"])
            self.assertEqual(
                "EG_LAUNCHER_SOURCE_BRANCH_MISMATCH", mismatched["supportRef"]
            )

    def test_the_any_branch_sentinel_disables_only_branch_binding(self):
        """Every other check still runs under the sentinel."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            run_git(root, "checkout", "-q", "-b", "some-other-branch")
            observed = self._check(root, branch=ANY_BRANCH_SENTINEL)
            self.assertTrue(observed["pass"], observed["supportRef"])

            (root / GOVERNED_EXECUTABLE_SURFACE / "cli.py").write_text(
                "# dirty\r\n", encoding="utf-8", newline=""
            )
            dirty = self._check(root, branch=ANY_BRANCH_SENTINEL)
            self.assertFalse(
                dirty["pass"], "the sentinel must not disable the other checks"
            )
            self.assertEqual(
                "EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION", dirty["supportRef"]
            )

    def test_a_repository_binding_mismatch_fails_closed(self):
        """EGRT-T40 support: the resolved top level must equal the supplied checkout root."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            nested = root / "energygrid-bill-downloader"
            observed = self._check(nested)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_SOURCE_BINDING_FAILED", observed["supportRef"])

    def test_a_missing_governed_path_fails_closed(self):
        """Each governed path must exist on disk."""
        with TemporaryScratch() as tmp:
            root = build_governed_scratch_repo(tmp)
            (root / "energygrid-bill-downloader" / "requirements.txt").unlink()
            run_git(root, "add", "-A")
            run_git(root, "commit", "-m", "remove requirements")
            observed = self._check(root)
        self.assertFalse(observed["pass"])
        self.assertEqual("EG_LAUNCHER_SOURCE_PATH_MISSING", observed["supportRef"])

    def test_an_untracked_governed_path_fails_closed(self):
        """A governed path present on disk but untracked fails closed."""
        with TemporaryScratch() as tmp:
            root = init_scratch_repo(tmp / "untracked_checkout")
            package = root / GOVERNED_EXECUTABLE_SURFACE
            package.mkdir(parents=True)
            (package / "cli.py").write_text("# untracked\r\n", encoding="utf-8", newline="")
            (root / "energygrid-bill-downloader" / "requirements.txt").write_text(
                "playwright==1.61.0\r\n", encoding="utf-8", newline=""
            )
            observed = self._check(root)
        self.assertFalse(observed["pass"])
        self.assertIn(
            observed["supportRef"],
            ("EG_LAUNCHER_SOURCE_PATH_UNTRACKED", "EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY"),
        )


class SanctionedBytecodeException(TierABase):
    """Task 15: the ONE sanctioned overlay exception, asserted directly."""

    def test_only_python_bytecode_is_sanctioned(self):
        """Every OTHER untracked entry inside the governed surface is a substitution."""
        cases = {
            "energygrid_bill_downloader/__pycache__/cli.cpython-314.pyc": True,
            "energygrid_bill_downloader/__pycache__": True,
            "energygrid_bill_downloader/stray.pyc": True,
            "energygrid_bill_downloader/nested/__pycache__/x.pyc": True,
            "energygrid_bill_downloader/overlay.py": False,
            "energygrid_bill_downloader/overlay.ignored": False,
            "energygrid_bill_downloader/pycache/x.py": False,
            "energygrid_bill_downloader/__pycache__extra/x.py": False,
            "energygrid_bill_downloader/notpyc.pycx": False,
            "": False,
        }
        with TemporaryScratch() as tmp:
            spec = write_json(tmp, "bytecode_spec.json", {"paths": list(cases)})
            observed = probe_json(ANY_PS, "bytecode", tmp, json=spec)
        actual = {entry["path"]: entry["sanctioned"] for entry in observed["verdicts"]}
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(expected, actual[path])


RUN_PRINCIPAL_REF = "EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE"
TRUSTEE_REF = "EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED"
SID_SET_REF = "EG_LAUNCHER_ROOT_AUTHORISED_SID_SET_INVALID"


class WriteAuthorityMixin:
    """Design section 17.2.1, DD-02 and DD-03, asserted on one interpreter."""

    exe = None

    def test_an_authorised_exact_trustee_passes_on_the_root_and_on_every_member(self):
        """EGRT-T58: asserted on the directory AND on each member individually.

        A member's own access list can differ from the directory's, and directory-level
        authority to add or delete children is by itself enough to replace a member, so
        neither object alone is sufficient.
        """
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(self.exe, tmp, "authorised_self")
            observed = check_write_authority(self.exe, tmp, root, authorised=("OWNER",))
        self.assertTrue(observed["admissionPass"], observed["admissionSupportRef"])
        self.assertEqual(1, observed["admittedCount"])
        self.assertEqual(
            4,
            len(observed["trusteeChecks"]),
            "the root and all three members must each be examined",
        )
        for entry in observed["trusteeChecks"]:
            with self.subTest(examined=entry["name"]):
                self.assertTrue(entry["evaluated"])
                self.assertTrue(entry["authorised"], entry["supportRef"])
                self.assertEqual("", entry["supportRef"])

    def test_exactly_one_granted_write_right_is_reported_writable(self):
        """EGRT-T59: the union false-negative guard.

        Windows grants an access check only when the descriptor allows ALL of the requested
        rights. Passing the union of every write-capable bit as DesiredAccess and reading a
        denied access status as "not writable" is therefore a false negative by
        construction: a token holding exactly one of those rights, enough to append to,
        delete, or re-permission the launcher, yields a denied status under that
        formulation and the run would proceed.

        Each case below grants a strict SUBSET of the write-capable rights, so a union
        implementation reports the object safe and fails this assertion.
        """
        for right in ("WriteData", "DeleteSubdirectoriesAndFiles"):
            with self.subTest(single_right=right):
                with TemporaryScratch() as tmp:
                    root, _built = build_scratch_launcher_root(
                        self.exe, tmp, "member_one_write_right", right=right
                    )
                    observed = check_write_authority(self.exe, tmp, root)
                member = outcome_for(observed["tokenChecks"], "launcher_lib.ps1")
                self.assertTrue(member["evaluated"])
                self.assertTrue(
                    member["anyWriteGranted"],
                    "a single granted %s must be observable as writable" % right,
                )
                self.assertEqual(RUN_PRINCIPAL_REF, member["supportRef"])

    def test_a_granted_mask_with_no_write_bit_is_reported_non_writable(self):
        """EGRT-T59: the control half. The check is an intersection, not a constant."""
        with TemporaryScratch() as tmp:
            root, built = build_scratch_launcher_root(self.exe, tmp, "member_no_write")
            self.assertEqual("launcher_lib.ps1", built["restrictedMember"])
            observed = check_write_authority(self.exe, tmp, root)
        member = outcome_for(observed["tokenChecks"], "launcher_lib.ps1")
        self.assertTrue(
            member["evaluated"], "an unreadable descriptor would be terminal, not a pass"
        )
        self.assertFalse(
            member["anyWriteGranted"],
            "a granted mask intersecting the write-capable mask in no bit is non-writable",
        )
        self.assertEqual("", member["supportRef"])

    def test_same_account_separation_is_proven_not_assumed(self):
        """EGRT-T62: the same token before and after restricting one group identity.

        Where installer and run principal are the same Windows account, binding write
        authority to that account's user identifier separates nothing, because normal
        split-token behaviour carries the same user identifier enabled in both contexts.
        Binding to a group identity separates them only while the host keeps producing a
        filtered token, which the access list cannot show. This asserts the difference is
        OBSERVABLE rather than assumed: one scratch member whose only write-capable grant
        is a group identity is writable under the unrestricted token and non-writable once
        that identity is deny-only.
        """
        with TemporaryScratch() as tmp:
            root, built = build_scratch_launcher_root(
                self.exe, tmp, "member_group_write"
            )
            self.assertTrue(
                built["groupPresent"],
                "no well-known logon-session identity is present in this token, so the "
                "same-account separation case cannot be constructed here",
            )
            unrestricted = check_write_authority(self.exe, tmp, root)
            report, restricted = run_acl_probe_under_restricted_token(
                self.exe, tmp, root
            )

        before = outcome_for(unrestricted["tokenChecks"], "launcher_lib.ps1")
        self.assertTrue(before["evaluated"])
        self.assertTrue(
            before["anyWriteGranted"],
            "the unrestricted token must be writable through the group grant",
        )

        self.assertTrue(
            report["ok"],
            "the restricted-token launch failed at %r with error %d"
            % (report["stage"], report["lastError"]),
        )
        self.assertEqual(
            0, report["exitCode"],
            "the child must run to completion under the restricted token",
        )
        self.assertIsNotNone(
            restricted, "the restricted child produced no observation document"
        )
        after = outcome_for(restricted["tokenChecks"], "launcher_lib.ps1")
        self.assertTrue(
            after["evaluated"],
            "the descriptor must still be readable under the restricted token",
        )
        self.assertFalse(
            after["anyWriteGranted"],
            "with the group identity deny-only the same object must be non-writable",
        )

    def test_the_write_capable_mask_is_generic_mapped_before_intersection(self):
        """EGRT-T71, dynamic half: the compared mask carries no generic right."""
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(self.exe, tmp, "authorised_self")
            observed = check_write_authority(self.exe, tmp, root)
        self.assertEqual(EXPECTED_WRITE_CAPABLE_MASK, observed["declaredMask"])
        self.assertEqual(EXPECTED_WRITE_CAPABLE_MASK, observed["mappedMask"])
        self.assertEqual(
            0,
            observed["mappedMask"] & 0xF0000000,
            "no generic right may survive into the compared mask",
        )


class WriteAuthorityTierA(WriteAuthorityMixin, TierABase):
    """Task 16, Tier A: the launcher-root write authority."""

    @classmethod
    def setUpClass(cls):
        TierABase.setUpClass()
        cls.exe = ANY_PS

    def test_a_write_capable_trustee_outside_the_supplied_set_fails_closed(self):
        """EGRT-T60: asserted on the directory and on a member independently."""
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "world_write_root")
            observed = check_write_authority(ANY_PS, tmp, root)
        entry = outcome_for(observed["trusteeChecks"], "(root)")
        self.assertTrue(entry["evaluated"])
        self.assertFalse(entry["authorised"])
        self.assertEqual(TRUSTEE_REF, entry["supportRef"])

        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "world_write_member")
            observed = check_write_authority(ANY_PS, tmp, root)
        self.assertTrue(
            outcome_for(observed["trusteeChecks"], "(root)")["authorised"],
            "the directory itself is clean in this case",
        )
        member = outcome_for(observed["trusteeChecks"], "launcher_lib.ps1")
        self.assertFalse(
            member["authorised"], "a member's own access list must be examined"
        )
        self.assertEqual(TRUSTEE_REF, member["supportRef"])

    def test_a_read_only_trustee_outside_the_set_is_not_write_capable(self):
        """Only WRITE-capable grants are constrained; a read grant is not a violation."""
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "world_read_root")
            observed = check_write_authority(ANY_PS, tmp, root)
        self.assertTrue(
            outcome_for(observed["trusteeChecks"], "(root)")["authorised"],
            "a read-only entry for an unlisted trustee is not write-capable",
        )

    def test_an_unrelated_service_class_trustee_fails_closed(self):
        """EGRT-T61: a service-class identifier carries no implicit authority."""
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "service_write_root")
            observed = check_write_authority(ANY_PS, tmp, root)
        entry = outcome_for(observed["trusteeChecks"], "(root)")
        self.assertFalse(
            entry["authorised"],
            "a rule of the form 'any identifier beginning with the service prefix' would "
            "authorise every service configured on the host rather than a named authority",
        )
        self.assertEqual(TRUSTEE_REF, entry["supportRef"])

    def test_an_inherited_write_capable_allow_entry_is_treated_as_explicit(self):
        """EGRT-T64: inheritance describes where an entry came from, not how much it grants."""
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "inherited_world_write")
            observed = check_write_authority(ANY_PS, tmp, root)
        member = outcome_for(observed["trusteeChecks"], "launcher_lib.ps1")
        self.assertFalse(
            member["authorised"],
            "an INHERITED write-capable entry for an unlisted trustee must fail closed",
        )
        self.assertEqual(TRUSTEE_REF, member["supportRef"])

    def test_a_deny_entry_never_authorises_a_trustee(self):
        """EGRT-T65: asserted in BOTH entry orders.

        Whether a given deny entry actually neutralises a given allow entry depends on
        their order in the list, which Windows walks in sequence. Cancelling an allow
        against a deny here would let a badly ordered list conceal a real grant.
        """
        for shape in ("deny_then_allow_world", "allow_then_deny_world"):
            with self.subTest(entry_order=shape):
                with TemporaryScratch() as tmp:
                    root, _built = build_scratch_launcher_root(ANY_PS, tmp, shape)
                    observed = check_write_authority(ANY_PS, tmp, root)
                entry = outcome_for(observed["trusteeChecks"], "(root)")
                self.assertFalse(entry["authorised"])
                self.assertEqual(TRUSTEE_REF, entry["supportRef"])

    def test_write_dac_write_owner_and_delete_are_each_write_capable(self):
        """EGRT-T66: each standard right alone is observable as writable."""
        for right in ("ChangePermissions", "TakeOwnership", "Delete"):
            with self.subTest(single_right=right):
                with TemporaryScratch() as tmp:
                    root, _built = build_scratch_launcher_root(
                        ANY_PS, tmp, "member_one_write_right", right=right
                    )
                    observed = check_write_authority(ANY_PS, tmp, root)
                member = outcome_for(observed["tokenChecks"], "launcher_lib.ps1")
                self.assertTrue(
                    member["anyWriteGranted"],
                    "%s alone must be treated as write-capable" % right,
                )

    def test_every_declared_write_capable_right_is_observable_as_writable(self):
        """The mask is the sole source, so each of its eight bits must be reachable."""
        for right in WRITE_CAPABLE_RIGHT_NAMES:
            with self.subTest(single_right=right):
                with TemporaryScratch() as tmp:
                    root, _built = build_scratch_launcher_root(
                        ANY_PS, tmp, "member_one_write_right", right=right
                    )
                    observed = check_write_authority(ANY_PS, tmp, root)
                member = outcome_for(observed["tokenChecks"], "launcher_lib.ps1")
                self.assertTrue(member["anyWriteGranted"], right)

    def test_an_examined_owner_outside_the_supplied_set_fails_closed(self):
        """EGRT-T66: an owner implicitly holds WRITE_DAC and can restore write at will.

        The scratch root is owned by the running account, so supplying only an unrelated
        identifier makes every examined object's owner fall outside the set, with no
        explicit write-capable entry required to reach the failure.
        """
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "authorised_self")
            observed = check_write_authority(ANY_PS, tmp, root, authorised=("WORLD",))
        entry = outcome_for(observed["trusteeChecks"], "(root)")
        self.assertFalse(
            entry["authorised"], "an owner outside the supplied set must fail closed"
        )
        self.assertEqual(TRUSTEE_REF, entry["supportRef"])

    def test_a_null_discretionary_access_control_list_fails_both_checks(self):
        """EGRT-T67: Windows grants ALL access when an object has none."""
        with TemporaryScratch() as tmp:
            root, built = build_scratch_launcher_root(ANY_PS, tmp, "null_dacl_member")
            self.assertEqual("launcher_lib.ps1", built["restrictedMember"])
            observed = check_write_authority(ANY_PS, tmp, root)
        token_entry = outcome_for(observed["tokenChecks"], "launcher_lib.ps1")
        self.assertTrue(token_entry["anyWriteGranted"], "a null list grants every right")
        self.assertEqual(RUN_PRINCIPAL_REF, token_entry["supportRef"])

        trustee_entry = outcome_for(observed["trusteeChecks"], "launcher_lib.ps1")
        self.assertFalse(
            trustee_entry["authorised"], "a null list must fail rather than pass"
        )
        self.assertEqual(TRUSTEE_REF, trustee_entry["supportRef"])

    def test_an_inheritable_creator_owner_entry_fails_closed(self):
        """EGRT-T68: the placeholder describes an unbounded future write set."""
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(
                ANY_PS, tmp, "creator_owner_inheritable"
            )
            observed = check_write_authority(ANY_PS, tmp, root)
        entry = outcome_for(observed["trusteeChecks"], "(root)")
        self.assertFalse(entry["authorised"])
        self.assertEqual(TRUSTEE_REF, entry["supportRef"])

    def test_the_creator_owner_placeholder_cannot_be_supplied_in_the_authorised_set(self):
        """EGRT-T68: the placeholder is refused at admission."""
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "authorised_self")
            observed = check_write_authority(
                ANY_PS, tmp, root, authorised=("SELF", "CREATOR_OWNER")
            )
        self.assertFalse(observed["admissionPass"])
        self.assertEqual(SID_SET_REF, observed["admissionSupportRef"])
        self.assertEqual(0, observed["admittedCount"])
        self.assertEqual(
            [],
            observed["trusteeChecks"],
            "a refused set must not be used for any comparison",
        )

    def test_an_empty_or_non_sid_authorised_set_is_refused(self):
        """DD-12: an account name is refused WITHOUT a name-resolution lookup."""
        for authorised in ((), ("BUILTIN\\Administrators",), ("not-a-sid",), ("S-1-",)):
            with self.subTest(authorised=authorised):
                with TemporaryScratch() as tmp:
                    root, _built = build_scratch_launcher_root(
                        ANY_PS, tmp, "authorised_self"
                    )
                    observed = check_write_authority(
                        ANY_PS, tmp, root, authorised=authorised
                    )
                self.assertFalse(observed["admissionPass"])
                self.assertEqual(SID_SET_REF, observed["admissionSupportRef"])

    def test_a_bypass_privilege_fails_the_run_principal_check(self):
        """EGRT-T69, cases (a) to (c): the predicate over OBSERVED names.

        Presence alone is sufficient, whether the privilege is enabled or disabled, and a
        list read successfully that holds neither name is not itself a failure.
        """
        lists = [
            ["SeTakeOwnershipPrivilege"],
            ["SeRestorePrivilege"],
            ["SeChangeNotifyPrivilege", "SeRestorePrivilege"],
            ["setakeownershipprivilege"],
            ["SeChangeNotifyPrivilege", "SeShutdownPrivilege"],
            [],
        ]
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "authorised_self")
            spec = write_json(tmp, "predicate_spec.json", {"lists": lists})
            observed = acl_probe(ANY_PS, "predicate", tmp, root, json=spec)

        self.assertEqual(
            ["SeTakeOwnershipPrivilege", "SeRestorePrivilege"], observed["bypassNames"]
        )
        verdicts = [entry["present"] for entry in observed["verdicts"]]
        self.assertEqual([True, True, True, True, False, False], verdicts)
        self.assertTrue(
            observed["observedReadOk"],
            "the running token's privilege information must be readable",
        )
        self.assertGreater(observed["observedCount"], 0)

    def test_the_privilege_reader_never_fabricates_a_name(self):
        """EGRT-T69, case (e): the reported verdict follows the OBSERVED list exactly.

        The reader must report only names it actually observed, and the bypass predicate
        must agree with that list rather than inventing or suppressing a membership.

        This asserts agreement rather than a fixed expectation, because whether the running
        token holds a bypass privilege is a property of the HOST, not of this code: an
        elevated administrator token legitimately holds both, which is exactly why such a
        principal fails the run-principal check by construction. Asserting "no bypass
        privilege here" would encode a non-elevation assumption and would fail on a
        correctly behaving elevated host. Agreement holds on every host and is the stronger
        property, because it catches a fabricated membership in either direction.
        """
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "authorised_self")
            observed = check_write_authority(ANY_PS, tmp, root)

        self.assertTrue(observed["privilegeReadOk"])
        self.assertGreater(observed["privilegeCount"], 0)

        names = observed["privilegeNames"]
        self.assertEqual(
            observed["privilegeCount"],
            len(names),
            "the reported count must describe the reported list",
        )
        for name in names:
            with self.subTest(privilege=name):
                self.assertTrue(
                    isinstance(name, str) and name.strip(),
                    "no reported privilege name may be empty or non-textual",
                )
                self.assertTrue(
                    name.startswith("Se"),
                    "%r is not a Windows privilege name" % name,
                )

        lowered = {name.lower() for name in names}
        expected_bypass = any(
            candidate.lower() in lowered
            for candidate in ("SeTakeOwnershipPrivilege", "SeRestorePrivilege")
        )
        self.assertEqual(
            expected_bypass,
            observed["bypassPresent"],
            "the bypass verdict must follow the observed list exactly, neither "
            "fabricating a membership nor suppressing one",
        )

    def test_no_security_identifier_material_reaches_any_returned_surface(self):
        """EGRT-T63, dynamic half: only a check name, an outcome, and a bounded reference."""
        with TemporaryScratch() as tmp:
            root, _built = build_scratch_launcher_root(ANY_PS, tmp, "world_write_root")
            spec = write_json(tmp, "authorised_spec.json", {"authorised": ["SELF"]})
            completed = run_ps(
                ANY_PS, write_acl_probe(tmp),
                "-Lib", str(LIB), "-Op", "check", "-Root", str(root),
                "-Json", str(spec),
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        observed = json.loads(completed.stdout)
        emitted = json.dumps(observed["tokenChecks"]) + json.dumps(observed["trusteeChecks"])
        self.assertIsNone(
            re.search(r"S-1-(?:\d+-)+\d+", emitted),
            "no security identifier may appear in either check's returned object",
        )
        for sid in SYNTHETIC_SIDS:
            with self.subTest(sid=sid):
                self.assertNotIn(sid, emitted)


REQUIRED_INTEROP_IMPORTS = (
    "GetCurrentProcess",
    "OpenProcessToken",
    "DuplicateTokenEx",
    "CloseHandle",
    "GetTokenInformation",
    "AccessCheck",
    "MapGenericMask",
    "LookupPrivilegeNameW",
)


class WriteAuthorityStaticGuards(TierCBase):
    """Task 16, Tier C: EGRT-T61, EGRT-T63, EGRT-T70, and EGRT-T71 static halves.

    These constrain how the access check is CONSTRUCTED rather than what it concludes,
    because a conforming conclusion reached by a non-conforming construction would still be
    a defect.
    """

    @classmethod
    def setUpClass(cls):
        cls.library = LIB.read_text(encoding="utf-8")

    def _function_bodies(self):
        if ANY_PS is None:
            self.skipTest("no PowerShell host found (pwsh or powershell)")
        with TemporaryScratch() as tmp:
            result = run_inspector(
                ANY_PS, tmp, FUNCTION_BODY_TEXT_INSPECTOR, target=LIB
            )
        self.assertEqual(0, result["parseErrors"])
        return {entry["name"]: entry["body"] for entry in result["functions"]}

    def test_the_interop_surface_is_complete_and_closed(self):
        """EGRT-T70: every required native function is declared and nothing else is."""
        declarations = re.findall(
            r"\[DllImport\([^\]]*\)\][^;]*?\b(\w+)\s*\(", self.library, re.S
        )
        security_source_start = self.library.index("$script:EgWin32SecurityInteropSource")
        security_source_end = self.library.index(
            "function Initialize-EgWin32SecurityInterop"
        )
        security_source = self.library[security_source_start:security_source_end]
        security_declarations = re.findall(
            r"\[DllImport\([^\]]*\)\][^;]*?\b(\w+)\s*\(", security_source, re.S
        )
        self.assertEqual(
            sorted(REQUIRED_INTEROP_IMPORTS),
            sorted(security_declarations),
            "the security interop surface must be exactly the eight required imports",
        )
        for required in REQUIRED_INTEROP_IMPORTS:
            with self.subTest(native=required):
                self.assertIn(required, declarations)

    def test_lookup_privilege_name_is_declared_with_an_explicit_entry_point(self):
        """EGRT-T70: charset name mangling must not decide which entry point is bound."""
        match = re.search(
            r"\[DllImport\(\"advapi32\.dll\"([^\]]*)\)\]\s*\n\s*private static extern bool LookupPrivilegeNameW",
            self.library,
        )
        self.assertIsNotNone(
            match, "LookupPrivilegeNameW must carry its own DllImport declaration"
        )
        attributes = match.group(1)
        self.assertIn("SetLastError = true", attributes)
        self.assertIn('EntryPoint = "LookupPrivilegeNameW"', attributes)
        self.assertIn("CharSet = CharSet.Unicode", attributes)

    def test_the_privilege_reader_uses_the_declared_lookup_and_never_a_substitute(self):
        """EGRT-T70: the privilege name is resolved only through the declared import."""
        self.assertIn("LookupPrivilegeNameW(null, ref luid, null, ref cchName)", self.library)
        self.assertIn(
            "LookupPrivilegeNameW(null, ref luid, builder, ref cchRetry)", self.library
        )
        self.assertNotIn("LookupPrivilegeValue", self.library)
        self.assertNotIn("LookupAccountName", self.library)
        self.assertNotIn("LookupAccountSid", self.library)

    def test_the_lookup_retry_buffer_is_sized_from_the_reported_length(self):
        """The retry is given the reported length plus one, and told the real capacity."""
        self.assertIn("int capacity = cchName + 1;", self.library)
        self.assertIn("StringBuilder builder = new StringBuilder(capacity);", self.library)
        self.assertIn("int cchRetry = capacity;", self.library)

    def test_the_access_check_uses_a_duplicated_impersonation_token(self):
        """EGRT-T70: the primary token is never the token argument to AccessCheck."""
        self.assertIn(
            "DuplicateTokenEx(processToken, TOKEN_QUERY, IntPtr.Zero, "
            "SECURITY_IDENTIFICATION, TOKEN_IMPERSONATION, out impersonationToken)",
            self.library,
        )
        self.assertIn("private const int SECURITY_IDENTIFICATION = 2;", self.library)
        self.assertIn("private const int TOKEN_IMPERSONATION = 2;", self.library)

        access_check_calls = re.findall(
            r"AccessCheck\(\s*securityDescriptor,\s*(\w+),", self.library
        )
        self.assertEqual(
            ["impersonationToken"],
            access_check_calls,
            "AccessCheck must be given the duplicated impersonation token only",
        )

    def test_the_launcher_never_impersonates_and_never_rebuilds_the_context(self):
        """EGRT-T70: the duplicate exists to be evaluated, never to be impersonated with."""
        forbidden = (
            "ImpersonateLoggedOnUser",
            "RevertToSelf",
            "SetThreadToken",
            "WindowsIdentity]::Impersonate",
            ".Impersonate(",
            "CreateProcessAsUser",
            "CreateRestrictedToken",
            "LogonUser",
        )
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(runtime_file=path.name, token=token):
                    self.assertNotIn(token, text)

    def test_every_duplicated_handle_is_closed_on_every_path(self):
        """EGRT-T70, dynamic support: handles are released in a finally block."""
        self.assertIn("if (impersonationToken != IntPtr.Zero) { CloseHandle(impersonationToken); }",
                      self.library)
        self.assertIn("if (processToken != IntPtr.Zero) { CloseHandle(processToken); }",
                      self.library)
        self.assertIn("if (privilegeSet != IntPtr.Zero) { Marshal.FreeHGlobal(privilegeSet); }",
                      self.library)
        self.assertIn("if (buffer != IntPtr.Zero) { Marshal.FreeHGlobal(buffer); }",
                      self.library)

    def test_the_prohibited_union_formulation_does_not_appear(self):
        """DD-02: DesiredAccess is MAXIMUM_ALLOWED and the verdict is an intersection."""
        self.assertIn("private const uint MAXIMUM_ALLOWED = 0x02000000;", self.library)
        self.assertIn(
            "AccessCheck(securityDescriptor, impersonationToken, MAXIMUM_ALLOWED,",
            self.library,
        )
        self.assertNotIn(
            "outcome.AccessStatus == false",
            self.library,
            "the access status must never drive the writability verdict",
        )

    def test_generic_mapping_is_applied_before_any_intersection(self):
        """EGRT-T71, static half: the compared mask is mapped first."""
        bodies = self._function_bodies()
        self.assertIn("Get-EgMappedWriteCapableMask", bodies)
        self.assertIn("MapGenericMask", bodies["Get-EgMappedWriteCapableMask"] +
                      self.library)

        check_body = bodies["Test-EgTokenWriteAccessToPath"]
        mapped_index = check_body.index("Get-EgMappedWriteCapableMask")
        band_index = check_body.index("-band")
        self.assertLess(
            mapped_index,
            band_index,
            "the mask must be generic-mapped before it is intersected",
        )
        self.assertIn("-band $mappedMask", check_body)

    def test_no_trustee_comparison_is_a_prefix_or_pattern_match(self):
        """EGRT-T61, static half: the supplied set is exhaustive and exact."""
        pattern_operators = ("-like", "-clike", "-match", "-cmatch", "StartsWith(",
                             "EndsWith(", "Contains(", "-in ", "-notlike")
        sid_markers = ("SecurityIdentifier", "AuthorisedSid", "$owner", "Owner",
                       "trustee", "Trustee")
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for number, line in non_comment_lines(text):
                if not any(marker in line for marker in sid_markers):
                    continue
                for operator in pattern_operators:
                    with self.subTest(runtime_file=path.name, line=number, operator=operator):
                        self.assertNotIn(
                            operator,
                            line,
                            "%s line %d compares a trustee identifier with %r: %s"
                            % (path.name, number, operator, line.strip()),
                        )

    def test_the_bypass_name_constant_is_not_reachable_from_the_token_reader(self):
        """EGRT-T69: a read failure can never be expressed as a synthesised privilege."""
        bodies = self._function_bodies()
        self.assertIn("Get-EgTokenPrivilegeNames", bodies)
        self.assertNotIn(
            "EgBypassPrivilegeNames",
            bodies["Get-EgTokenPrivilegeNames"],
            "the token reader must not consume the bypass-privilege names at all",
        )
        self.assertIn(
            "EgBypassPrivilegeNames",
            bodies["Test-EgBypassPrivilegePresent"],
            "the predicate is the only consumer of the bypass-privilege names",
        )
        for literal in ("SeTakeOwnershipPrivilege", "SeRestorePrivilege"):
            with self.subTest(literal=literal):
                self.assertNotIn(
                    literal,
                    bodies["Get-EgTokenPrivilegeNames"],
                    "no privilege name may be fabricated by the reader",
                )

    def test_no_security_identifier_literal_other_than_the_refused_placeholder(self):
        """EGRT-T63, static half: no host principal is committed."""
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"S-1-(?:\d+-)+\d+", text):
                with self.subTest(runtime_file=path.name, sid=match.group(0)):
                    self.assertEqual(
                        "S-1-3-0",
                        match.group(0),
                        "the only permitted identifier literal is the refused "
                        "CREATOR OWNER placeholder",
                    )

    def test_the_authorised_set_has_exactly_one_route_into_the_runtime(self):
        """DD-12: no default, no environment route, and no file the launcher reads it from."""
        self.assertNotIn(
            "AuthorisedLauncherRootWriteSid'",
            self.library,
            "the library takes the admitted set as a parameter, never by name lookup",
        )
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for number, line in non_comment_lines(text):
                if "AuthorisedLauncherRootWriteSid" not in line:
                    continue
                with self.subTest(runtime_file=path.name, line=number):
                    self.assertNotIn("GetEnvironmentVariable", line)
                    self.assertNotIn("= @(", line)
                    self.assertNotIn("Get-Content", line)


class WriteAuthorityTierB(WriteAuthorityMixin, TierBBase):
    """Task 16, Tier B: the write checks on the Windows PowerShell 5.1 boundary.

    The interop is compiled by that runtime here, so the here-string, its marshalling, and
    the access check are all proven against the production compatibility boundary.
    """

    @classmethod
    def setUpClass(cls):
        TierBBase.setUpClass()
        cls.exe = DESKTOP_PS


ORDERED_PREFLIGHT_CHECK_NAMES = (
    "launcher_root_entries_classified",
    "launcher_package_members_present",
    "launcher_package_parse_clean",
    "launcher_package_manifest_match",
    "checkout_root_exists_absolute",
    "config_path_exists_absolute",
    "python_exe_exists_absolute",
    "config_path_outside_checkout",
    "config_parses_json",
    "config_required_keys_present",
    "python_version_is_3_14",
    "governed_source_integrity",
    "browser_cache_ready",
    "launcher_root_outside_checkout",
    "launcher_root_not_writable_by_run_principal",
    "launcher_root_write_trustees_authorised",
    "launcher_files_not_reparse_points",
    "launcher_files_not_unexpectedly_readonly",
    "credential_import_ok",
    "username_nonempty",
    "password_nonempty",
)

NON_SECRET_CHECK_NAMES = ORDERED_PREFLIGHT_CHECK_NAMES[:18]
CREDENTIAL_CHECK_NAMES = ORDERED_PREFLIGHT_CHECK_NAMES[18:]

REQUIRED_CONFIG_KEYS = (
    "portal_url",
    "account_identity",
    "archive_root",
    "state_path",
    "temp_root",
    "log_root",
)

EXIT_PREFLIGHT_FAILED = 70

PYTHON_STUB_SCRIPT = r"""
@echo off
if "%~1"=="--version" (
  echo Python 3.14.7
  exit /b 0
)
exit /b 0
"""


class LauncherEnvironment:
    """A complete scratch deployment: a checkout, an installed launcher root, and host state.

    The launcher root is this script's own directory by contract, so the tests install the
    REAL committed runtime files into a scratch root through the REAL installer and then
    invoke the installed copy. Nothing here touches a production path.
    """

    def __init__(self, tmp, exe):
        self.tmp = Path(tmp)
        self.exe = exe
        self.checkout = None
        self.commit = None
        self.launcher_root = None
        self.config_path = None
        self.credential_path = None
        self.browser_cache = None
        self.log_root = None
        self.credential_username = None
        self.credential_password = None

    def build(self, config_overrides=None, omit_config_keys=(),
              provision_browser_cache=True, create_credential=True,
              config_inside_checkout=False):
        host = self.tmp / "private_host_state"
        host.mkdir()

        self.checkout = init_scratch_repo(self.tmp / "checkout")
        runtime = self.checkout / "energygrid-bill-downloader" / "runtime"
        runtime.mkdir(parents=True)
        for name in ("launcher.ps1", "launcher_lib.ps1"):
            shutil.copyfile(RUNTIME_DIR / name, runtime / name)
        package = self.checkout / GOVERNED_EXECUTABLE_SURFACE
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8", newline="")
        (package / "cli.py").write_text("# scratch cli\r\n", encoding="utf-8", newline="")
        (self.checkout / "energygrid-bill-downloader" / "requirements.txt").write_text(
            "playwright==1.61.0\r\n", encoding="utf-8", newline=""
        )
        run_git(self.checkout, "add", "-A")
        run_git(self.checkout, "commit", "-m", "scratch runtime deployment")
        self.commit = run_git(self.checkout, "rev-parse", "HEAD").stdout.strip()

        self.launcher_root = self.tmp / "launcher_root"
        self.launcher_root.mkdir()
        installed = run_installer(
            self.exe, self.checkout, self.launcher_root, self.commit
        )
        if installed.returncode != 0:
            raise AssertionError(
                "scratch installation failed\nstdout:\n%s\nstderr:\n%s"
                % (installed.stdout, installed.stderr)
            )

        config = {
            "portal_url": "https://portal.invalid.test/login",
            "account_identity": "REPLACE_WITH_ACCOUNT_IDENTITY",
            "archive_root": str(host / "archive"),
            "state_path": str(host / "state" / "operations.sqlite"),
            "temp_root": str(host / "temp"),
            "log_root": str(host / "logs"),
        }
        for key in omit_config_keys:
            config.pop(key, None)
        if config_overrides:
            config.update(config_overrides)
        if config_inside_checkout:
            self.config_path = self.checkout / "inside_checkout_config.json"
        else:
            self.config_path = host / "energygrid.private.json"
        self.config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        self.log_root = host / "logs"
        self.log_root.mkdir()

        self.browser_cache = host / "browser_cache"
        build_scratch_browser_cache(
            self.browser_cache, provisioned=provision_browser_cache
        )

        self.credential_path = host / "synthetic.credential.xml"
        if create_credential:
            _artefact, username, password = make_synthetic_credential_artefact(
                self.exe, self.tmp, artefact_path=self.credential_path
            )
            self.credential_username = username
            self.credential_password = password

        self.python_exe = host / "python_stub.cmd"
        self.python_exe.write_text(PYTHON_STUB_SCRIPT, encoding="utf-8", newline="")
        return self

    def installed_launcher(self):
        return self.launcher_root / "launcher.ps1"

    def run(self, *extra, validate_only=True, authorised=None, expected_branch="ANY_BRANCH",
            python_exe=None, config_path=None, credential_path=None,
            browser_cache=None, log_root=None):
        if authorised is None:
            authorised = ["S-1-1-0"]
        args = [
            "-ConfigPath", str(config_path or self.config_path),
            "-PythonExe", str(python_exe or self.python_exe),
            "-CheckoutRoot", str(self.checkout),
            "-CredentialPath", str(credential_path or self.credential_path),
            "-BrowserCachePath", str(browser_cache or self.browser_cache),
            "-ExpectedBranch", expected_branch,
            "-AuthorisedLauncherRootWriteSid",
        ]
        args.append(",".join(str(sid) for sid in authorised))
        if log_root is not None:
            args.extend(["-LogRoot", str(log_root)])
        if validate_only:
            args.append("-ValidateOnly")
        args.extend(extra)
        return run_ps(self.exe, self.installed_launcher(), *args)


def launcher_validation(completed):
    """Parse the launcher's single deterministic validation JSON object."""
    text = completed.stdout.strip()
    if not text:
        raise AssertionError(
            "the launcher emitted no validation object\nstdout:\n%s\nstderr:\n%s"
            % (completed.stdout, completed.stderr)
        )
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(
            "expected exactly one JSON object on standard output, got %d lines:\n%s"
            % (len(lines), text)
        )
    return json.loads(lines[0])


class LauncherPreflightContract(TierABase):
    """Task 16, steps 8 to 14: design sections 5.1, 5.2, 5.3, and 11.3."""

    def test_the_preflight_check_order_matches_the_committed_contract(self):
        """The emitted checks map carries the twenty-one stable names in exact order."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            completed = environment.run()
        observed = launcher_validation(completed)
        self.assertEqual(
            list(ORDERED_PREFLIGHT_CHECK_NAMES),
            list(observed["checks"].keys()),
            "the checks map key order is part of the contract",
        )
        self.assertEqual({"checks", "status", "support_ref"}, set(observed.keys()))

    def test_the_non_secret_preflight_reaches_the_launcher_root_security_group(self):
        """Positions 1 to 13 pass against a correctly prepared scratch deployment."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            completed = environment.run()
        observed = launcher_validation(completed)
        for name in ORDERED_PREFLIGHT_CHECK_NAMES[:13]:
            with self.subTest(check=name):
                self.assertEqual(
                    "PASS",
                    observed["checks"][name],
                    "%s failed; first failure was %r"
                    % (name, observed.get("support_ref")),
                )

    def test_a_failing_non_secret_preflight_causes_zero_credential_import_attempt(self):
        """EGRT-T48: the credential artefact is never opened when an earlier check fails.

        The artefact path is pointed at a file that does not exist, so any attempt to open
        it would be visible as a credential support reference. Each case below fails a
        DIFFERENT non-secret position.
        """
        sentinel = "does_not_exist.credential.xml"
        cases = {
            "browser_cache_ready": {"provision_browser_cache": False},
            "config_required_keys_present": {"omit_config_keys": ("portal_url",)},
            "config_path_outside_checkout": {"config_inside_checkout": True},
        }
        for expected_check, build_kwargs in cases.items():
            with self.subTest(failing_check=expected_check):
                with TemporaryScratch() as tmp:
                    environment = LauncherEnvironment(tmp, ANY_PS).build(**build_kwargs)
                    absent = Path(tmp) / sentinel
                    completed = environment.run(credential_path=absent)
                observed = launcher_validation(completed)
                self.assertEqual("FAIL", observed["checks"][expected_check])
                for name in CREDENTIAL_CHECK_NAMES:
                    self.assertEqual(
                        "FAIL",
                        observed["checks"][name],
                        "an unevaluated credential position must report FAIL, never PASS",
                    )
                self.assertNotIn(
                    "CREDENTIAL",
                    observed["support_ref"],
                    "no credential reference may be reported when a non-secret check "
                    "failed first: the artefact was never opened",
                )

    def test_a_failing_launcher_root_security_check_precedes_the_credential_import(self):
        """EGRT-T48: including each launcher-root security position, 14 to 18."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            absent = Path(tmp) / "does_not_exist.credential.xml"
            # The scratch launcher root is owned and writable by the running account, so
            # position 15 fails by construction here.
            completed = environment.run(credential_path=absent)
        observed = launcher_validation(completed)
        self.assertEqual(
            "FAIL", observed["checks"]["launcher_root_not_writable_by_run_principal"]
        )
        self.assertEqual(
            "EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE", observed["support_ref"]
        )
        for name in CREDENTIAL_CHECK_NAMES:
            self.assertEqual("FAIL", observed["checks"][name])

    def test_a_refused_authorised_identifier_set_fails_the_trustee_check(self):
        """DD-12: a refused set is reported under check 16 with its own reference."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            completed = environment.run(authorised=["not-a-sid"])
        observed = launcher_validation(completed)
        self.assertEqual(
            "FAIL", observed["checks"]["launcher_root_write_trustees_authorised"]
        )

    def test_a_missing_or_invalid_class_a_member_cannot_be_satisfied_by_residue(self):
        """EGRT-T56: recognised residue can never satisfy a missing package member."""
        for missing in ("launcher_lib.ps1", MANIFEST_FILE_NAME):
            with self.subTest(missing_member=missing):
                with TemporaryScratch() as tmp:
                    environment = LauncherEnvironment(tmp, ANY_PS).build()
                    target = environment.launcher_root / missing
                    residue = environment.launcher_root / residue_name("backup", missing)
                    shutil.move(str(target), str(residue))
                    completed = environment.run()
                if missing == "launcher_lib.ps1":
                    # The entry script dot-sources the library by a fixed join, so a
                    # missing library cannot be replaced by residue and the run cannot
                    # even reach its own preflight.
                    self.assertNotEqual(0, completed.returncode)
                    self.assertNotIn(
                        residue.name,
                        completed.stdout,
                        "residue must never be selected as a library",
                    )
                else:
                    observed = launcher_validation(completed)
                    self.assertEqual(
                        "FAIL", observed["checks"]["launcher_package_members_present"]
                    )
                    self.assertEqual(
                        "EG_LAUNCHER_PACKAGE_MEMBER_MISSING", observed["support_ref"]
                    )

    def test_an_unexpected_launcher_root_entry_fails_closed(self):
        """Class C is terminal, and it is the first position evaluated."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            (environment.launcher_root / "launcher-old.ps1").write_text(
                "# intruder\r\n", encoding="utf-8", newline=""
            )
            completed = environment.run()
        observed = launcher_validation(completed)
        self.assertEqual("FAIL", observed["checks"]["launcher_root_entries_classified"])
        self.assertEqual("EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY", observed["support_ref"])

    def test_recognised_residue_is_never_executed_imported_or_selected(self):
        """EGRT-T57, dynamic half: executable-content residue never runs."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            marker = Path(tmp) / "residue_executed.marker"
            residue = environment.launcher_root / residue_name("staging", "launcher_lib.ps1")
            residue.write_text(
                "New-Item -ItemType File -Path '%s'\r\n" % str(marker).replace("\\", "\\\\"),
                encoding="utf-8", newline="",
            )
            completed = environment.run()
            self.assertFalse(
                marker.exists(), "recognised residue must never be executed"
            )
        observed = launcher_validation(completed)
        self.assertEqual(
            "PASS",
            observed["checks"]["launcher_root_entries_classified"],
            "correctly named residue is tolerated, not executed",
        )

    def test_a_manifest_mismatch_fails_closed(self):
        """An installed member that no longer matches the manifest is terminal."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            member = environment.launcher_root / "launcher_lib.ps1"
            member.write_text(
                member.read_text(encoding="utf-8") + "\r\n# tampered\r\n",
                encoding="utf-8", newline="",
            )
            completed = environment.run()
        observed = launcher_validation(completed)
        self.assertEqual("FAIL", observed["checks"]["launcher_package_manifest_match"])
        self.assertEqual("EG_LAUNCHER_MANIFEST_MISMATCH", observed["support_ref"])

    def test_the_terminal_event_is_written_once_and_carries_no_private_data(self):
        """One launcher_failed line, exactly four keys, and never a changed exit code."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            with_log = environment.run(
                validate_only=False, log_root=environment.log_root, authorised=["S-1-1-0"]
            )
            self.assertEqual(EXIT_PREFLIGHT_FAILED, with_log.returncode)
            event_file = environment.log_root / "launcher_failed.jsonl"
            self.assertTrue(event_file.is_file(), "a failing run must append one event")
            lines = [line for line in event_file.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
            self.assertEqual(1, len(lines), "exactly one event per failing run")
            event = json.loads(lines[0])
            self.assertEqual(
                ["run_id", "phase", "status", "support_ref"], list(event.keys())
            )
            self.assertIsNone(
                re.search(r"[A-Za-z]:\\\\", json.dumps(event)),
                "no private path may reach the terminal event",
            )

            without_log = environment.run(validate_only=False)
            self.assertEqual(
                EXIT_PREFLIGHT_FAILED,
                without_log.returncode,
                "a run with no log root returns the same exit code",
            )

            unwritable = Path(tmp) / "no_such_log_root"
            with_bad_log = environment.run(validate_only=False, log_root=unwritable)
            self.assertEqual(
                EXIT_PREFLIGHT_FAILED,
                with_bad_log.returncode,
                "an unresolvable log root never changes the exit code",
            )

    def test_a_preflight_failure_exits_seventy_and_starts_no_child(self):
        """The launcher never continues past a failed check."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            completed = environment.run(validate_only=False)
        self.assertEqual(EXIT_PREFLIGHT_FAILED, completed.returncode)
        self.assertEqual(
            "", completed.stdout.strip(),
            "the real path emits no validation document",
        )

    def test_the_required_configuration_key_set_is_exactly_the_application_contract(self):
        """DD-05: each required key, omitted individually, fails position 10."""
        for key in REQUIRED_CONFIG_KEYS:
            with self.subTest(missing_key=key):
                with TemporaryScratch() as tmp:
                    environment = LauncherEnvironment(tmp, ANY_PS).build(
                        omit_config_keys=(key,)
                    )
                    completed = environment.run()
                observed = launcher_validation(completed)
                self.assertEqual(
                    "FAIL", observed["checks"]["config_required_keys_present"]
                )
                self.assertEqual(
                    "EG_LAUNCHER_CONFIG_KEY_MISSING", observed["support_ref"]
                )

    def test_an_unsupported_interpreter_version_fails_closed(self):
        """Position 11 requires a 3.14.x interpreter."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            stub = Path(tmp) / "wrong_python.cmd"
            stub.write_text(
                "@echo off\r\nif \"%~1\"==\"--version\" (\r\n  echo Python 3.12.4\r\n"
                "  exit /b 0\r\n)\r\nexit /b 0\r\n",
                encoding="utf-8", newline="",
            )
            completed = environment.run(python_exe=stub)
        observed = launcher_validation(completed)
        self.assertEqual("FAIL", observed["checks"]["python_version_is_3_14"])
        self.assertEqual(
            "EG_LAUNCHER_PYTHON_VERSION_UNSUPPORTED", observed["support_ref"]
        )

    def test_a_branch_mismatch_fails_the_governed_source_check(self):
        """Position 12 carries the branch binding when the sentinel is not supplied."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            completed = environment.run(expected_branch="release")
        observed = launcher_validation(completed)
        self.assertEqual("FAIL", observed["checks"]["governed_source_integrity"])
        self.assertEqual("EG_LAUNCHER_SOURCE_BRANCH_MISMATCH", observed["support_ref"])


class LauncherStaticGuards(TierCBase):
    """Task 16, Tier C: EGRT-T19, EGRT-T20, EGRT-T57 static half, and the ordering guard."""

    @classmethod
    def setUpClass(cls):
        cls.launcher = LAUNCHER.read_text(encoding="utf-8")
        cls.library = LIB.read_text(encoding="utf-8")

    def test_the_launcher_exit_band_is_disjoint_from_the_application_band(self):
        """EGRT-T19: read from the committed constants, not from convention."""
        runtime_band = set()
        for match in re.finditer(
            r"(?:PreflightFailed|InstallPreMutationFailed|InstallRolledBack|"
            r"InstallRollbackIncomplete)\s*=\s*(\d+)",
            self.library,
        ):
            runtime_band.add(int(match.group(1)))
        self.assertEqual({70, 71, 72, 73}, runtime_band)

        application_match = re.search(
            r"\$script:EgApplicationExitCodes\s*=\s*@\(([^)]*)\)", self.library
        )
        self.assertIsNotNone(application_match)
        application_band = {
            int(value.strip())
            for value in application_match.group(1).split(",")
            if value.strip()
        }
        self.assertEqual({0, 10, 20, 64}, application_band)
        self.assertEqual(
            set(), runtime_band & application_band, "the two bands must be disjoint"
        )

    def test_no_committed_runtime_file_contains_a_portal_url_literal(self):
        """EGRT-T20: no http or https string constant reaches a committed runtime file."""
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for number, line in non_comment_lines(text):
                with self.subTest(runtime_file=path.name, line=number):
                    self.assertIsNone(
                        re.search(r"https?://", line),
                        "%s line %d carries a URL literal" % (path.name, number),
                    )

    def test_the_only_dot_source_is_a_fixed_join_of_the_script_root(self):
        """EGRT-T57: the launcher never dot-sources a path derived from an enumeration."""
        dot_sources = re.findall(r"^\s*\.\s+(.+)$", self.launcher, re.M)
        self.assertEqual(
            1, len(dot_sources), "there must be exactly one dot-source in the launcher"
        )
        self.assertEqual(
            "(Join-Path $PSScriptRoot 'launcher_lib.ps1')", dot_sources[0].strip()
        )

    def test_no_runtime_file_invokes_or_imports_an_enumerated_path(self):
        """EGRT-T57: classification never yields a path that is executed or imported."""
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for number, line in non_comment_lines(text):
                if "ClassA" not in line and "ClassB" not in line and "ClassC" not in line:
                    continue
                for forbidden in (". $", "& $", "Import-Module", "Invoke-Expression"):
                    with self.subTest(runtime_file=path.name, line=number, token=forbidden):
                        self.assertNotIn(forbidden, line)

    def test_the_credential_import_lexically_follows_every_non_secret_check(self):
        """EGRT-T48, static half: ordering is structural, not a matter of reading order."""
        import_index = self.launcher.index("Import-EgLauncherCredential")
        self.assertEqual(
            1,
            self.launcher.count("Import-EgLauncherCredential"),
            "there must be exactly one credential import call site",
        )
        for predicate in (
            "Get-EgLauncherRootClassification",
            "Test-EgPowerShellFileParsesCleanly",
            "Compare-EgInstalledPackageToManifest",
            "Test-EgLauncherConfigContract",
            "Test-EgPythonVersionSupported",
            "Test-EgGovernedSourceIntegrity",
            "Test-EgBrowserCacheReady",
            "Test-EgLauncherRootSecurity",
        ):
            with self.subTest(predicate=predicate):
                self.assertLess(
                    self.launcher.index(predicate),
                    import_index,
                    "%s must be called before the credential import" % predicate,
                )

    def test_the_launcher_parameter_surface_is_exactly_the_design_contract(self):
        """Design section 5.1: eleven parameters and no more."""
        block = self.launcher[self.launcher.index("param("):self.launcher.index(")\n\nSet-StrictMode")]
        declared = re.findall(r"\$(\w+)\s*(?:=|,|\n|\))", block)
        names = []
        for name in declared:
            if name not in names:
                names.append(name)
        self.assertEqual(
            [
                "ConfigPath", "PythonExe", "CheckoutRoot", "CredentialPath",
                "BrowserCachePath", "ExpectedBranch", "AuthorisedLauncherRootWriteSid",
                "Command", "LogRoot", "ValidateOnly", "RunId",
            ],
            names,
        )
        for forbidden in ("-Headed", "PortalUrl", "ExpectedCommit", "Password"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, block)
        # DD-01: the launcher root is this script's own directory, so there is no
        # launcher-root parameter. Matched as a whole parameter name, because
        # -AuthorisedLauncherRootWriteSid legitimately contains the same substring.
        self.assertNotIn(
            "LauncherRoot",
            names,
            "the launcher root is $PSScriptRoot and is never a parameter",
        )

    # ---- DL-XB-141-RUNTIME-005-SOURCE-DURABILITY-A1 ---- #
    #
    # The amendment admits ONE more fixed operation name. It adds no parameter,
    # no headed switch, and no way for a caller to reach the child with anything
    # of their own. These guards are what stop that from drifting.

    def test_the_command_allowlist_is_exactly_the_three_admitted_operations(self):
        """A closed ValidateSet, read from the committed script rather than assumed."""
        match = re.search(
            r"\[ValidateSet\(([^)]*)\)\]\[string\]\$Command", self.launcher
        )
        self.assertIsNotNone(match, "-Command must carry a ValidateSet")
        admitted = re.findall(r"'([^']*)'", match.group(1))
        self.assertEqual(["run", "list", "login-diagnostic"], admitted)
        self.assertIn("$Command = 'run'", self.launcher, "the default stays `run`")

    def test_the_child_argument_vector_is_fixed_and_never_extended(self):
        """Five elements, one assignment, and no conditional append anywhere."""
        assignments = re.findall(r"^\s*\$childArguments\s*=.*$", self.launcher, re.M)
        self.assertEqual(
            [
                "$childArguments = @('-m', 'energygrid_bill_downloader', "
                "$Command, '--config', $ConfigPath)"
            ],
            [line.strip() for line in assignments],
            "the child argument vector is one fixed five-element assignment",
        )
        for forbidden in ("$childArguments +=", "$childArguments +", "$childArguments.Add"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.launcher)
        # Assigned once, read once. A third mention would be a mutation site.
        self.assertEqual(2, self.launcher.count("$childArguments"))

    def test_the_launcher_forwards_no_caller_supplied_argument_to_the_child(self):
        """No argument-forwarding surface, and no headed switch on any path."""
        for forbidden in (
            "-Headed",
            "$Headed",
            "--headed",
            "ArgumentList",
            "$args",
            "ValueFromRemainingArguments",
            "Invoke-Expression",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.launcher)

    def test_the_launcher_names_no_portal_credential_or_script_child_argument(self):
        """The five elements are the whole contract the child ever receives."""
        vector_start = self.launcher.index("$childArguments = @(")
        vector = self.launcher[vector_start:self.launcher.index(")", vector_start) + 1]
        for forbidden in ("http", "Password", "UserName", ".py", ".ps1", "PortalUrl"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, vector)

    def test_the_launcher_states_that_headed_is_not_a_generic_switch(self):
        """The committed comment must say what the parameter surface does not."""
        prose = normalised_prose(self.launcher[:self.launcher.index("[CmdletBinding()]")])
        for phrase in ("no headed switch", "login-diagnostic", "non-overridable"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), prose)

    def test_the_authorised_identifier_set_is_mandatory_with_no_default(self):
        """Design section 9.1: no default, no environment route, and no file route."""
        self.assertIn(
            "[Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]]"
            "$AuthorisedLauncherRootWriteSid",
            self.launcher,
        )
        self.assertIn(
            "[Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedBranch",
            self.launcher,
        )


class ValidateOnlyContract(TierABase):
    """Task 17: design section 8.

    -ValidateOnly exists so that every preflight, binding, security, and installation check
    can be exercised on the production host without any production mutation, and without
    owner approval for a mutating action.
    """

    def _snapshot_domain(self, environment):
        """Every root the zero-mutation contract covers."""
        return {
            "launcher_root": snapshot_tree(environment.launcher_root),
            "checkout": snapshot_tree(environment.checkout),
            "config_directory": snapshot_tree(environment.config_path.parent),
            "browser_cache": snapshot_tree(environment.browser_cache),
            "log_root": snapshot_tree(environment.log_root),
        }

    def test_validate_only_mutates_nothing(self):
        """EGRT-T14: identical snapshots across the whole domain, on BOTH entry scripts."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            before = self._snapshot_domain(environment)

            launcher = environment.run(log_root=environment.log_root)
            self.assertIn("checks", launcher.stdout)
            after_launcher = self._snapshot_domain(environment)

            installer = run_installer(
                ANY_PS, environment.checkout, environment.launcher_root,
                environment.commit, "-ValidateOnly",
            )
            self.assertEqual(0, installer.returncode, installer.stderr)
            after_installer = self._snapshot_domain(environment)

        for root in before:
            with self.subTest(root=root, entry_script="launcher.ps1"):
                self.assertEqual(before[root], after_launcher[root])
            with self.subTest(root=root, entry_script="install_or_update_launcher.ps1"):
                self.assertEqual(before[root], after_installer[root])

    def test_repeated_validation_is_byte_identical_and_idempotent(self):
        """EGRT-T15: two consecutive runs, byte-identical output and identical state."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()

            first = environment.run()
            second = environment.run()
            self.assertEqual(
                first.stdout,
                second.stdout,
                "launcher validation output carries no timestamp and no generated "
                "identifier, so it must be byte-identical",
            )
            snapshot_after_launcher = self._snapshot_domain(environment)

            first_install = run_installer(
                ANY_PS, environment.checkout, environment.launcher_root,
                environment.commit, "-ValidateOnly",
            )
            second_install = run_installer(
                ANY_PS, environment.checkout, environment.launcher_root,
                environment.commit, "-ValidateOnly",
            )
            self.assertEqual(0, first_install.returncode, first_install.stderr)
            self.assertEqual(first_install.stdout, second_install.stdout)
            self.assertEqual(snapshot_after_launcher, self._snapshot_domain(environment))

    def test_validate_only_launches_no_browser_and_performs_no_cache_write(self):
        """EGRT-T31: the cache is read, never written, and no browser is started."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            before = snapshot_tree(environment.browser_cache)
            completed = environment.run()
            after = snapshot_tree(environment.browser_cache)
        self.assertEqual(before, after)
        observed = launcher_validation(completed)
        self.assertEqual("PASS", observed["checks"]["browser_cache_ready"])

    def test_validate_only_emits_exactly_one_json_object_with_no_private_content(self):
        """One object, three keys, and nothing private on the surface."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            completed = environment.run()
            emitted = completed.stdout.strip()
            observed = launcher_validation(completed)

            self.assertEqual({"checks", "status", "support_ref"}, set(observed.keys()))
            self.assertIsNone(
                re.search(r"[A-Za-z]:\\\\", emitted),
                "no Windows absolute path may reach the validation surface",
            )
            self.assertIsNone(
                re.search(r"^\\\\\\\\", emitted), "no UNC path may reach the surface"
            )
            self.assertIsNone(
                re.search(r"S-1-(?:\d+-)+\d+", emitted),
                "no security identifier may reach the surface",
            )
            for sid in SYNTHETIC_SIDS:
                self.assertNotIn(sid, emitted)
            self.assertNotIn(str(environment.checkout), emitted)
            self.assertNotIn(str(environment.config_path), emitted)
            self.assertNotIn(str(environment.browser_cache), emitted)
            self.assertNotIn("account_identity", emitted)
            if environment.credential_username:
                self.assertNotIn(environment.credential_username, emitted)
                self.assertNotIn(environment.credential_password, emitted)

    def test_validate_only_output_stays_clean_when_a_write_check_fails(self):
        """EGRT-T63: on a run failing checks 15 and 16, still nothing identifying."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            completed = environment.run(authorised=["S-1-5-80-0"])
            emitted = completed.stdout.strip()
        observed = launcher_validation(completed)
        self.assertEqual(
            "FAIL", observed["checks"]["launcher_root_not_writable_by_run_principal"]
        )
        self.assertIsNone(re.search(r"S-1-(?:\d+-)+\d+", emitted))
        self.assertNotIn("S-1-5-80-0", emitted)

    def test_validate_only_failure_exits_seventy_and_names_the_first_failing_check(self):
        """Exit 0 means every check passed; a failure exits 70 with a bounded reference."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build(
                provision_browser_cache=False
            )
            completed = environment.run()
        self.assertEqual(EXIT_PREFLIGHT_FAILED, completed.returncode)
        observed = launcher_validation(completed)
        self.assertEqual("FAIL", observed["status"])
        self.assertEqual("EG_LAUNCHER_BROWSER_CACHE_NOT_READY", observed["support_ref"])
        self.assertEqual(
            "FAIL",
            observed["checks"]["browser_cache_ready"],
            "the reported reference belongs to the FIRST failing position",
        )
        for earlier in ORDERED_PREFLIGHT_CHECK_NAMES[:12]:
            with self.subTest(earlier_check=earlier):
                self.assertEqual("PASS", observed["checks"][earlier])

    def test_installer_validate_only_generates_no_transaction_identifier(self):
        """DD-09: validation creates no residue, so it needs no transaction identifier."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            before = launcher_root_entries(environment.launcher_root)
            completed = run_installer(
                ANY_PS, environment.checkout, environment.launcher_root,
                environment.commit, "-ValidateOnly",
            )
            after = launcher_root_entries(environment.launcher_root)
            residue = class_b_entries(environment.launcher_root)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(before, after, "the launcher root gains no entry")
        self.assertEqual([], residue)
        self.assertIsNone(
            re.search(
                r"\.eglauncher-", completed.stdout
            ),
            "no reserved residue name may appear in validation output",
        )
        observed = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual({"checks", "status"}, set(observed.keys()))
        self.assertNotIn(
            "ALREADY_CURRENT",
            json.dumps(observed["checks"]),
            "ALREADY_CURRENT is a real-path status and never a validation check outcome",
        )

    def test_installer_validate_only_reports_a_refusal_without_mutating(self):
        """A failing admission still emits the validation shape and mutates nothing."""
        with TemporaryScratch() as tmp:
            environment = LauncherEnvironment(tmp, ANY_PS).build()
            before = snapshot_tree(environment.launcher_root)
            completed = run_installer(
                ANY_PS, environment.checkout, environment.launcher_root,
                "0" * 40, "-ValidateOnly",
            )
            after = snapshot_tree(environment.launcher_root)
        self.assertEqual(EXIT_INSTALL_PRE_MUTATION_FAILED, completed.returncode)
        self.assertEqual(before, after)
        observed = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("FAIL", observed["status"])
        self.assertEqual(
            "EG_LAUNCHER_INSTALL_ADMISSION_INVALID", observed["support_ref"]
        )


class ValidateOnlyStaticGuards(TierCBase):
    """Task 17, Tier C: EGRT-T31's static half and the determinism constraints."""

    def test_no_validation_path_can_reach_a_mutating_command(self):
        """The validation branch on each entry script returns before any mutation."""
        launcher = LAUNCHER.read_text(encoding="utf-8")
        installer = INSTALLER.read_text(encoding="utf-8")

        # On the installer, every mutating call site must appear AFTER the validation
        # branch exits, so validation is structurally incapable of reaching one.
        validate_exit = installer.index(
            "Write-Output (ConvertTo-EgValidationJson -Checks $script:EgInstallerChecks `\n"
            "        -Status 'PASS' -SupportRef '')"
        )
        for mutating in ("WriteAllBytes", "Invoke-AtomicFileReplace",
                         "Invoke-PublishToAbsentDestination", "New-EgOperationId",
                         "Invoke-EgPostAcceptanceBackupCleanup"):
            with self.subTest(mutating=mutating):
                first = installer.index(mutating)
                self.assertGreater(
                    first,
                    validate_exit,
                    "%s must not be reachable from the validation branch" % mutating,
                )

        # On the launcher, the child process is started only after the validation exit.
        launcher_validate_exit = launcher.index("Exit-EgLauncher -ExitCode 0")
        for mutating in ("$child.Start()", "Invoke-EgWithInjectedProcessEnvironment"):
            with self.subTest(mutating=mutating):
                self.assertGreater(
                    launcher.index(mutating),
                    launcher_validate_exit,
                    "%s must not be reachable from the validation branch" % mutating,
                )

    def test_the_validation_document_is_built_deterministically(self):
        """No timestamp, no generated identifier, and an ordered dictionary throughout."""
        library = LIB.read_text(encoding="utf-8")
        serialiser_start = library.index("function ConvertTo-EgValidationJson")
        serialiser_end = library.index("function Write-EgUtf8NoBomText")
        serialiser = library[serialiser_start:serialiser_end]
        self.assertIn("[ordered]@{}", serialiser)
        self.assertIn("ConvertTo-Json -Depth 8 -Compress", serialiser)
        for forbidden in ("Get-Date", "NewGuid", "DateTime", "Now", "Stopwatch"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialiser)

    def test_no_runtime_file_emits_a_timestamp_or_generated_identifier_in_validation(self):
        """Determinism is what makes the idempotency assertion a strict byte comparison."""
        for path in existing_runtime_ps1_files():
            text = path.read_text(encoding="utf-8")
            for number, line in non_comment_lines(text):
                if "ConvertTo-EgValidationJson" not in line:
                    continue
                with self.subTest(runtime_file=path.name, line=number):
                    for forbidden in ("Get-Date", "NewGuid", "[datetime]"):
                        self.assertNotIn(forbidden, line)


LIVE_SUPPORT_REFS = (
    "EG_LAUNCHER_REPLACE_ARGUMENT_INVALID",
    "EG_LAUNCHER_REPLACE_SHARING_VIOLATION",
    "EG_LAUNCHER_REPLACE_ACCESS_DENIED",
    "EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH",
    "EG_LAUNCHER_REPLACE_PREIMAGE_UNRECOVERABLE",
    "EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT",
    "EG_LAUNCHER_PUBLISH_RACE_LOST",
    "EG_LAUNCHER_PUBLISH_POSTIMAGE_MISMATCH",
    "EG_LAUNCHER_CREDENTIAL_ARTEFACT_MISSING",
    "EG_LAUNCHER_CREDENTIAL_IMPORT_FAILED",
    "EG_LAUNCHER_CREDENTIAL_INCOMPLETE",
    "EG_LAUNCHER_CREDENTIAL_RESTORE_FAILED",
    "EG_LAUNCHER_BROWSER_CACHE_UNRESOLVED",
    "EG_LAUNCHER_BROWSER_CACHE_NOT_READY",
    "EG_LAUNCHER_BROWSER_CACHE_BIND_FAILED",
    "EG_LAUNCHER_SOURCE_BINDING_FAILED",
    "EG_LAUNCHER_SOURCE_BRANCH_MISMATCH",
    "EG_LAUNCHER_SOURCE_PATH_MISSING",
    "EG_LAUNCHER_SOURCE_PATH_UNTRACKED",
    "EG_LAUNCHER_SOURCE_STAGED_MODIFICATION",
    "EG_LAUNCHER_SOURCE_UNSTAGED_MODIFICATION",
    "EG_LAUNCHER_SOURCE_DELETED",
    "EG_LAUNCHER_SOURCE_UNTRACKED_OVERLAY",
    "EG_LAUNCHER_GIT_INVOCATION_FAILED",
    "EG_LAUNCHER_GIT_SUBCOMMAND_FORBIDDEN",
    "EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY",
    "EG_LAUNCHER_PACKAGE_MEMBER_MISSING",
    "EG_LAUNCHER_PACKAGE_PARSE_FAILED",
    "EG_LAUNCHER_MANIFEST_MISSING",
    "EG_LAUNCHER_MANIFEST_UNPARSABLE",
    "EG_LAUNCHER_MANIFEST_MISMATCH",
    "EG_LAUNCHER_PATH_NOT_ABSOLUTE",
    "EG_LAUNCHER_PATH_MISSING",
    "EG_LAUNCHER_CONFIG_INSIDE_CHECKOUT",
    "EG_LAUNCHER_CONFIG_UNPARSABLE",
    "EG_LAUNCHER_CONFIG_KEY_MISSING",
    "EG_LAUNCHER_PYTHON_VERSION_UNSUPPORTED",
    "EG_LAUNCHER_ROOT_INSIDE_CHECKOUT",
    "EG_LAUNCHER_ROOT_ACL_RUN_PRINCIPAL_WRITABLE",
    "EG_LAUNCHER_ROOT_ACL_WRITE_TRUSTEE_UNAUTHORISED",
    "EG_LAUNCHER_ROOT_AUTHORISED_SID_SET_INVALID",
    "EG_LAUNCHER_ROOT_REPARSE_POINT",
    "EG_LAUNCHER_FILE_UNEXPECTEDLY_READONLY",
    "EG_LAUNCHER_INSTALL_ADMISSION_INVALID",
    "EG_LAUNCHER_INSTALL_STAGING_FAILED",
    "EG_LAUNCHER_INSTALL_MANIFEST_VERIFY_FAILED",
    "EG_LAUNCHER_INSTALL_ROLLBACK_INCOMPLETE",
    "EG_LAUNCHER_INSTALL_BACKUP_CLEANUP_INCOMPLETE",
    "EG_LAUNCHER_UNCLASSIFIED",
)

LIVE_SUPPORT_REF_COUNT = 49

# Design section 17.2 records the first as retired vocabulary that must not be emitted. The
# second existed only in an earlier revision of the plan, was never implemented and never
# emitted by any build, so there is no earlier evidence to keep readable: it is guarded as
# an absent string rather than entered in the retired set (DD-08).
SUPERSEDED_NAMES = (
    "launcher_root_write_restricted_to_install_principal",
    "EG_LAUNCHER_ROOT_ACL_WRITE_NOT_RESTRICTED",
)


class SupportReferenceVocabulary(TierABase):
    """Task 18: design section 11.4. The vocabulary is bounded and closed."""

    def test_the_declared_live_set_matches_the_design_exactly(self):
        """The count is verified by the suite rather than asserted in prose."""
        with TemporaryScratch() as tmp:
            observed = probe_json(ANY_PS, "supportrefs", tmp)
        self.assertEqual(list(LIVE_SUPPORT_REFS), observed["live"])
        self.assertEqual(LIVE_SUPPORT_REF_COUNT, observed["liveCount"])
        self.assertEqual([], observed["retired"])
        self.assertEqual(0, observed["retiredCount"])

    def test_membership_is_exact_and_case_sensitive(self):
        """An unrecognised reference is never treated as live."""
        with TemporaryScratch() as tmp:
            observed = probe_json(ANY_PS, "supportrefs", tmp)
        self.assertTrue(observed["knownIsLive"])
        self.assertFalse(observed["unknownIsLive"])
        self.assertFalse(observed["lowercaseIsLive"])
        self.assertFalse(observed["emptyIsLive"])


class SupportReferenceStaticGuards(TierCBase):
    """Task 18, Tier C: EGRT-T18 reachability over the committed runtime files."""

    @classmethod
    def setUpClass(cls):
        cls.sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in existing_runtime_ps1_files()
        }
        cls.combined = "\n".join(cls.sources.values())

    def _extracted(self):
        found = set()
        for text in self.sources.values():
            found.update(re.findall(r"EG_LAUNCHER_[A-Z0-9_]+", text))
        return found

    def test_no_support_reference_outside_the_bounded_vocabulary_is_emitted(self):
        """EGRT-T18: the extracted set equals the live set exactly."""
        extracted = self._extracted()
        self.assertEqual(
            set(LIVE_SUPPORT_REFS),
            extracted,
            "extra: %r; missing: %r"
            % (sorted(extracted - set(LIVE_SUPPORT_REFS)),
               sorted(set(LIVE_SUPPORT_REFS) - extracted)),
        )
        self.assertEqual(LIVE_SUPPORT_REF_COUNT, len(extracted))

    def test_every_live_support_reference_is_reachable_at_a_raising_site(self):
        """EGRT-T18: each reference appears somewhere other than the declaration itself."""
        library = self.sources["launcher_lib.ps1"]
        declaration_start = library.index("$script:EgLauncherSupportRefs = @(")
        declaration_end = library.index(
            "$script:EgLauncherRetiredSupportRefs", declaration_start
        )
        declaration = library[declaration_start:declaration_end]
        outside_declaration = (
            library[:declaration_start] + library[declaration_end:]
            + "\n".join(
                text for name, text in self.sources.items()
                if name != "launcher_lib.ps1"
            )
        )
        for reference in LIVE_SUPPORT_REFS:
            with self.subTest(reference=reference):
                self.assertIn(
                    reference, declaration, "the reference must be declared"
                )
                self.assertIn(
                    reference,
                    outside_declaration,
                    "%s is declared but never raised anywhere" % reference,
                )

    def test_every_retired_support_reference_is_unreachable(self):
        """EGRT-T18: vacuously satisfied while the retired set is empty (DD-08)."""
        library = self.sources["launcher_lib.ps1"]
        retired_declaration = re.search(
            r"\$script:EgLauncherRetiredSupportRefs\s*=\s*@\(([^)]*)\)", library
        )
        self.assertIsNotNone(retired_declaration)
        retired = re.findall(r"EG_LAUNCHER_[A-Z0-9_]+", retired_declaration.group(1))
        self.assertEqual([], retired, "the retired set is empty at first implementation")
        for reference in retired:
            with self.subTest(retired=reference):
                self.assertNotIn(reference, LIVE_SUPPORT_REFS)

    def test_the_superseded_launcher_root_write_names_appear_nowhere(self):
        """Retired vocabulary must not be emitted, and a never-implemented name must not exist."""
        scanned = dict(self.sources)
        for extra in (EXAMPLE_SETTINGS, RUNTIME_README, Path(__file__)):
            if extra.is_file():
                scanned[extra.name] = extra.read_text(encoding="utf-8")
        for name in SUPERSEDED_NAMES:
            for source_name, text in scanned.items():
                with self.subTest(superseded=name, source=source_name):
                    if source_name == Path(__file__).name:
                        # This module names them in order to guard them; the guard itself
                        # is the only permitted occurrence.
                        occurrences = text.count(name)
                        self.assertLessEqual(
                            occurrences,
                            2,
                            "the superseded name may appear only in its own guard",
                        )
                        continue
                    self.assertNotIn(name, text)

    def test_the_two_vocabularies_cannot_collide(self):
        """The application's vocabulary and the runtime's stay disjoint."""
        cli = (PROJECT_ROOT / "energygrid_bill_downloader" / "cli.py").read_text(
            encoding="utf-8"
        )
        self.assertIsNone(
            re.search(r"EG_LAUNCHER_[A-Z0-9_]+", cli),
            "no runtime reference may appear in the application",
        )
        for text in self.sources.values():
            self.assertIsNone(re.search(r"\bEG_LOGIN_[A-Z0-9_]+", text))
            self.assertIsNone(re.search(r"\bAPP_ERROR_[A-Z0-9_]+", text))

    def test_the_unclassified_reference_is_the_only_fallback(self):
        """An unrecognised failure records EG_LAUNCHER_UNCLASSIFIED rather than leaking."""
        self.assertIn("EG_LAUNCHER_UNCLASSIFIED", self.combined)
        mapper_start = self.sources["launcher_lib.ps1"].index(
            "function Get-EgReplaceSupportRef"
        )
        mapper_end = self.sources["launcher_lib.ps1"].index(
            "function New-EgPublicationResult"
        )
        mapper = self.sources["launcher_lib.ps1"][mapper_start:mapper_end]
        self.assertIn("EG_LAUNCHER_UNCLASSIFIED", mapper)
        for forbidden in (".Message", "Exception.Message", "-match '"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(
                    forbidden,
                    mapper,
                    "classification must read TYPE and HRESULT only, never message text",
                )


# --------------------------------------------------------------------------------------
# Privacy guard over committed files (design section 17.1)
# --------------------------------------------------------------------------------------
# Never committed: username; password; credential blob or DPAPI material; connection
# string; private account identity; private absolute server path; UNC path; host or
# principal identity; any host-specific secret value. This guard enforces the mechanically
# checkable subset over the committed runtime files, the example settings file, the runtime
# README, and this test module itself.

FORBIDDEN_PATTERNS = {
    "windows_absolute_path": r"[A-Za-z]:\\\\",
    "unc_path": r"\\\\\\\\[A-Za-z0-9]",
    "credential_assignment":
        r"(?i)(password|passwd|pwd|secret|token|apikey|api_key)\s*=\s*['\"][^'\"]+['\"]",
    "http_url": r"https?://",
    "sid_literal": r"S-1-(?:\d+-)+\d+",
}
_FORBIDDEN_PATTERN_DECLARATION_MARKER = "FORBIDDEN_PATTERNS = {"

ALLOWED_PLACEHOLDER_PREFIX = "REPLACE_WITH_"

# The single identifier literal a committed runtime file may contain: the CREATOR OWNER
# placeholder the admission check refuses. It names no host principal.
RUNTIME_ALLOWED_SID_LITERALS = ("S-1-3-0",)

# In this test module the permitted identifiers are the declared well-known and constructed
# sets. None is read from the host.
TEST_MODULE_ALLOWED_SID_LITERALS = tuple(SYNTHETIC_SIDS) + tuple(DISABLE_CANDIDATE_SIDS)

# A machine or domain account identifier always carries the machine-authority prefix. That
# is what a host-derived identifier would look like, and it must appear nowhere at all.
#
# Composed from fragments deliberately: written as one literal it would itself be a
# host-shaped identifier in this file, and the guard below would correctly flag it. That is
# the guard working, not a false positive, so the constant avoids being one.
HOST_ACCOUNT_SID_PREFIX = "S-1-" + "5-" + "21-"

# RFC 2606 reserves .invalid, so a host under it cannot resolve. Test fixtures may name
# such a host; a committed runtime file may name no host at all.
RESERVED_TEST_HOST_SUFFIX = ".invalid"


def privacy_scanned_runtime_files():
    """Committed runtime surfaces: every runtime .ps1, the settings example, the README."""
    scanned = list(existing_runtime_ps1_files())
    for extra in (EXAMPLE_SETTINGS, RUNTIME_README):
        if extra.is_file():
            scanned.append(extra)
    return tuple(scanned)


def scannable_lines(text, skip_declaration_marker=None):
    """Yield (number, line), optionally skipping this guard's own pattern declaration."""
    in_declaration = False
    for number, line in enumerate(text.splitlines(), start=1):
        if skip_declaration_marker and skip_declaration_marker in line:
            in_declaration = True
            continue
        if in_declaration:
            if line.startswith("}"):
                in_declaration = False
            continue
        yield number, line


class CommittedFilePrivacyGuard(TierCBase):
    """Task 19: EGRT-T13 over every committed surface this change touches."""

    def test_no_committed_runtime_file_carries_private_or_secret_material(self):
        """EGRT-T13: no private absolute path, UNC path, credential, URL, or identifier."""
        scanned = privacy_scanned_runtime_files()
        self.assertTrue(scanned, "at least runtime/launcher_lib.ps1 must exist")
        for path in scanned:
            text = path.read_text(encoding="utf-8")
            for name, pattern in FORBIDDEN_PATTERNS.items():
                compiled = re.compile(pattern)
                for number, line in scannable_lines(text):
                    match = compiled.search(line)
                    if match is None:
                        continue
                    if name == "sid_literal" and match.group(0) in RUNTIME_ALLOWED_SID_LITERALS:
                        continue
                    if (path == EXAMPLE_SETTINGS
                            and ALLOWED_PLACEHOLDER_PREFIX in line):
                        # A placeholder-only value in the example settings file is the
                        # required committed content, not private material.
                        continue
                    with self.subTest(committed_file=path.name, pattern=name, line=number):
                        self.fail(
                            "%s line %d carries %s material: %s"
                            % (path.name, number, name, line.strip())
                        )

    def test_no_account_identity_or_host_identity_literal_is_committed(self):
        """account_identity may appear only as a key name, never with a value."""
        for path in privacy_scanned_runtime_files():
            text = path.read_text(encoding="utf-8")
            for number, line in scannable_lines(text):
                if "account_identity" not in line:
                    continue
                with self.subTest(committed_file=path.name, line=number):
                    stripped = line.strip()
                    permitted = (
                        stripped.startswith("#")
                        or "'account_identity'" in stripped
                        or "`account_identity`" in stripped
                        or '"account_identity"' in stripped
                        or ALLOWED_PLACEHOLDER_PREFIX in stripped
                    )
                    self.assertTrue(
                        permitted,
                        "%s line %d appears to carry an account identity value: %s"
                        % (path.name, number, stripped),
                    )

    def test_no_principal_identity_reaches_a_committed_file(self):
        """EGRT-T63 support: no host-derived identifier anywhere, in any scanned file."""
        scanned = list(privacy_scanned_runtime_files()) + [Path(__file__)]
        for path in scanned:
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(FORBIDDEN_PATTERNS["sid_literal"], text):
                with self.subTest(committed_file=path.name, sid=match.group(0)):
                    self.assertFalse(
                        match.group(0).startswith(HOST_ACCOUNT_SID_PREFIX),
                        "a machine or domain account identifier must never be committed",
                    )

    def test_this_test_module_carries_only_declared_well_known_identifiers(self):
        """Every identifier literal in the test module is a declared well-known value."""
        text = Path(__file__).read_text(encoding="utf-8")
        permitted = set(TEST_MODULE_ALLOWED_SID_LITERALS)
        # Truncated and malformed values the admission check is asserted to refuse.
        permitted.update({"S-1-", "S-1-5-80-0"})
        for match in re.finditer(FORBIDDEN_PATTERNS["sid_literal"], text):
            with self.subTest(sid=match.group(0)):
                self.assertIn(
                    match.group(0),
                    permitted,
                    "an undeclared identifier literal appears in the test module",
                )

    def test_this_test_module_names_no_resolvable_host(self):
        """Any host a fixture names is under the reserved .invalid top-level domain."""
        text = Path(__file__).read_text(encoding="utf-8")
        for number, line in scannable_lines(text, _FORBIDDEN_PATTERN_DECLARATION_MARKER):
            for match in re.finditer(r"https?://([A-Za-z0-9.\-]+)", line):
                host = match.group(1)
                with self.subTest(line=number, host=host):
                    self.assertTrue(
                        host.endswith(RESERVED_TEST_HOST_SUFFIX)
                        or RESERVED_TEST_HOST_SUFFIX + "." in host,
                        "a fixture host must be under the reserved .invalid domain",
                    )

    def test_no_committed_file_carries_a_windows_absolute_path_literal(self):
        """A private absolute server path is never committed, in any scanned file."""
        scanned = list(privacy_scanned_runtime_files()) + [Path(__file__)]
        for path in scanned:
            text = path.read_text(encoding="utf-8")
            marker = None
            if path == Path(__file__):
                marker = _FORBIDDEN_PATTERN_DECLARATION_MARKER
            for number, line in scannable_lines(text, marker):
                with self.subTest(committed_file=path.name, line=number):
                    self.assertIsNone(
                        re.search(r"[A-Za-z]:\\\\", line),
                        "%s line %d carries an absolute path literal: %s"
                        % (path.name, number, line.strip()),
                    )

    def test_the_guard_detects_each_defect_it_exists_to_catch(self):
        """The guard is proven to FAIL on the defect, not merely to pass on clean source.

        Each defect is injected into a scratch copy inside a scratch directory, never into
        a committed file, and the copy is discarded with the directory.
        """
        baseline = LIB.read_text(encoding="utf-8")
        injections = {
            "windows_absolute_path": "\n# a private path such as C:\\\\Private\\\\EnergyGrid\n",
            "unc_path": "\n# a private share such as \\\\\\\\fileserver\\\\energygrid\n",
            "sid_literal": (
                "\n$script:EgScratchDefect = '"
                + HOST_ACCOUNT_SID_PREFIX
                + "1111111111-2222222222-3333333333-1001'\n"
            ),
            "http_url": "\n$script:EgScratchPortal = 'https://portal.example.com/login'\n",
            "credential_assignment": "\n$script:EgScratchSecret = ''\npassword = 'not-a-real-secret'\n",
        }
        with TemporaryScratch() as tmp:
            for name, injection in injections.items():
                with self.subTest(defect=name):
                    scratch = tmp / ("scratch_%s.ps1" % name)
                    scratch.write_text(baseline + injection, encoding="utf-8")
                    compiled = re.compile(FORBIDDEN_PATTERNS[name])
                    hits = []
                    for number, line in scannable_lines(
                        scratch.read_text(encoding="utf-8")
                    ):
                        match = compiled.search(line)
                        if match is None:
                            continue
                        if (name == "sid_literal"
                                and match.group(0) in RUNTIME_ALLOWED_SID_LITERALS):
                            continue
                        hits.append((number, match.group(0)))
                    self.assertTrue(
                        hits, "the guard failed to report the %s defect" % name
                    )


HOST_SUPPLIED_LAUNCHER_PARAMETERS = (
    "ConfigPath",
    "PythonExe",
    "CheckoutRoot",
    "CredentialPath",
    "BrowserCachePath",
    "ExpectedBranch",
    "AuthorisedLauncherRootWriteSid",
    "LogRoot",
)


def to_snake_case(name):
    """Convert a PowerShell Verb-Noun style parameter name to a settings key."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class ExampleSettingsShape(TierCBase):
    """Task 20: design section 9.1. The file is a SHAPE, never a configuration.

    It is superseded on the host by real private settings, it is not deployed to the
    launcher root, and no committed script reads it at runtime. Carrying a key here
    therefore creates no second route into the launcher: the launcher's only route for the
    authorised write-trustee set remains its mandatory parameter.
    """

    def test_the_example_settings_file_parses_and_is_placeholder_only(self):
        """Every value is a REPLACE_WITH_ placeholder, or an array of them."""
        self.assertTrue(
            EXAMPLE_SETTINGS.is_file(), "runtime/launcher.settings.example.json must exist"
        )
        parsed = json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8"))
        self.assertIsInstance(parsed, dict)
        for key, value in parsed.items():
            with self.subTest(key=key):
                if isinstance(value, list):
                    self.assertTrue(value, "an array value must not be empty")
                    for element in value:
                        self.assertIsInstance(element, str)
                        self.assertTrue(
                            element.startswith(ALLOWED_PLACEHOLDER_PREFIX),
                            "%r is not a placeholder" % element,
                        )
                else:
                    self.assertIsInstance(value, str)
                    self.assertTrue(
                        value.startswith(ALLOWED_PLACEHOLDER_PREFIX),
                        "%r is not a placeholder" % value,
                    )

    def test_no_example_value_is_a_path_or_an_identifier(self):
        """A placeholder shape carries no private absolute path, UNC path, or identifier."""
        text = EXAMPLE_SETTINGS.read_text(encoding="utf-8")
        for name in ("windows_absolute_path", "unc_path", "sid_literal", "http_url"):
            with self.subTest(pattern=name):
                self.assertIsNone(
                    re.search(FORBIDDEN_PATTERNS[name], text),
                    "the example settings file carries %s material" % name,
                )

    def test_the_example_settings_keys_match_the_launcher_host_supplied_parameters(self):
        """A parameter added later without a documented slot fails this test."""
        parsed = json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8"))
        expected = {to_snake_case(name) for name in HOST_SUPPLIED_LAUNCHER_PARAMETERS}
        self.assertEqual(
            expected,
            set(parsed.keys()),
            "the key set must equal the host-supplied launcher parameters",
        )
        self.assertEqual(8, len(parsed))

    def test_the_authorised_identifier_key_is_an_array(self):
        """The launcher parameter is a string array, so the documented slot is too."""
        parsed = json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8"))
        self.assertIsInstance(
            parsed["authorised_launcher_root_write_sid"],
            list,
            "the authorised set is one or more identifiers",
        )

    def test_the_example_settings_file_is_never_deployed(self):
        """EGRT-T47: sharing the runtime directory does not make a file deployable."""
        library = LIB.read_text(encoding="utf-8")
        self.assertNotIn("launcher.settings.example.json", library)
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("launcher.settings.example.json", installer)

    def test_no_committed_script_reads_the_example_settings_file_at_runtime(self):
        """Design section 9.1: no file is a route by which the authorised set arrives."""
        for path in existing_runtime_ps1_files():
            with self.subTest(runtime_file=path.name):
                self.assertNotIn(
                    "settings.example", path.read_text(encoding="utf-8")
                )


def normalised_prose(text):
    """Collapse whitespace so a phrase search is not defeated by a line break."""
    return re.sub(r"\s+", " ", text).lower()


SCHEDULER_EXAMPLE = PROJECT_ROOT / "task-scheduler" / "register_task.example.ps1"
PROJECT_RUNBOOK = PROJECT_ROOT / "docs" / "runbook.md"
PROJECT_README = PROJECT_ROOT / "README.md"
DESIGN_DOCUMENT = PROJECT_ROOT / "docs" / "runtime_source_durability_design.md"

RUNTIME_README_REQUIRED_SECTIONS = (
    "What is canonical here and what is not",
    "The three-member deployed package",
    "Launcher parameter surface",
    "Exit bands",
    "Launcher-root entry classes",
    "ValidateOnly",
    "Installation",
    "What the runtime never does",
    "Launcher-root write authority",
)

RUNBOOK_SECTION_HEADING = "## Runtime launcher installation and validation"

# The four concerns EGRT-I30 requires the runbook to keep clearly separate and named.
MIGRATION_CONCERNS = (
    "installer transaction backup cleanup",
    "migration recovery holding",
    "launcher functional validation",
    "recovery-copy retirement or restoration",
)

SCHEDULER_MUTATING_COMMANDS = (
    "Register-ScheduledTask",
    "New-ScheduledTask",
    "Start-ScheduledTask",
    "Set-ScheduledTask",
    "Unregister-ScheduledTask",
    "New-ScheduledTaskAction",
    "New-ScheduledTaskTrigger",
    "schtasks",
)


class RuntimeDocumentation(TierCBase):
    """Task 21: design sections 4, 14, 15, and 16. Repository documentation only.

    Describing a separately gated host action is not authority to perform it. The scheduler
    example stays inert, and the host half of the launcher-root write-authority criterion
    is documented as an owner action rather than claimed by this suite.
    """

    def test_the_scheduler_example_names_the_accepted_python_314_contract(self):
        """The stale interpreter placeholder is corrected; the file stays a shape."""
        text = SCHEDULER_EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("<PYTHON_3_12_EXE>", text)
        self.assertEqual(1, text.count("<PYTHON_3_14_EXE>"))

    def test_the_scheduler_example_names_the_launcher_as_the_scheduled_shape(self):
        """The launcher is the only thing the Scheduled Task will ever invoke."""
        text = SCHEDULER_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("runtime/launcher.ps1", text.replace("\\", "/"))

    def test_the_scheduler_example_remains_inert(self):
        """It registers, starts, alters, and removes nothing, and still parses cleanly."""
        text = SCHEDULER_EXAMPLE.read_text(encoding="utf-8")
        for command in SCHEDULER_MUTATING_COMMANDS:
            with self.subTest(command=command):
                self.assertNotIn(command, text)
        if ANY_PS is None:
            self.skipTest("no PowerShell host found (pwsh or powershell)")
        with TemporaryScratch() as tmp:
            result = run_inspector(
                ANY_PS, tmp, PARSE_INSPECTOR, target=SCHEDULER_EXAMPLE
            )
        self.assertEqual(0, result["parseErrors"])

    def test_the_runtime_readme_documents_every_required_contract_section(self):
        """The directory-level runtime contract states contracts, not narrative."""
        self.assertTrue(RUNTIME_README.is_file(), "runtime/README.md must exist")
        text = RUNTIME_README.read_text(encoding="utf-8")
        for heading in RUNTIME_README_REQUIRED_SECTIONS:
            with self.subTest(section=heading):
                self.assertIn(heading, text)

    def test_the_runtime_readme_states_the_exact_class_b_syntax(self):
        """The documented contract cannot drift from the residue-name parser."""
        text = RUNTIME_README.read_text(encoding="utf-8")
        self.assertIn(RESIDUE_PREFIX, text)
        for kind in RESIDUE_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, text)
        self.assertIn(
            "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", text
        )
        for member in CLASS_A_MEMBER_NAMES:
            with self.subTest(member=member):
                self.assertIn(member, text)

    def test_the_runtime_readme_states_the_exit_bands(self):
        """Both bands, and the fact that they are disjoint."""
        text = RUNTIME_README.read_text(encoding="utf-8")
        for code in ("70", "71", "72", "73", "0", "10", "20", "64"):
            with self.subTest(code=code):
                self.assertIn(code, text)

    def test_the_runtime_readme_states_the_write_authority_contract(self):
        """Two independent expectations, both terminal, neither implying the other."""
        text = RUNTIME_README.read_text(encoding="utf-8")
        self.assertIn("launcher_root_not_writable_by_run_principal", text)
        self.assertIn("launcher_root_write_trustees_authorised", text)
        self.assertIn("SeTakeOwnershipPrivilege", text)
        self.assertIn("SeRestorePrivilege", text)
        self.assertIn("AuthorisedLauncherRootWriteSid", text)

    def test_the_runbook_keeps_the_four_migration_concerns_separate(self):
        """EGRT-I30: four distinct, explicitly named concerns."""
        text = PROJECT_RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(RUNBOOK_SECTION_HEADING, text)
        section = text[text.index(RUNBOOK_SECTION_HEADING):]
        end = section.find("\n## ", len(RUNBOOK_SECTION_HEADING))
        if end != -1:
            section = section[:end]
        prose = normalised_prose(section)
        for concern in MIGRATION_CONCERNS:
            with self.subTest(concern=concern):
                self.assertIn(concern.lower(), prose)

    def test_the_runbook_section_is_placed_between_the_documented_neighbours(self):
        """Placed after Controlled first validation and before Recovery guidance."""
        text = PROJECT_RUNBOOK.read_text(encoding="utf-8")
        self.assertLess(
            text.index("## Controlled first validation"),
            text.index(RUNBOOK_SECTION_HEADING),
        )
        self.assertLess(
            text.index(RUNBOOK_SECTION_HEADING), text.index("## Recovery guidance")
        )

    def test_the_runbook_encodes_the_equivalent_context_validation_requirement(self):
        """EGRT-I31 host half: same account, same elevation state, and why it matters."""
        text = PROJECT_RUNBOOK.read_text(encoding="utf-8")
        section = text[text.index(RUNBOOK_SECTION_HEADING):]
        end = section.find("\n## ", len(RUNBOOK_SECTION_HEADING))
        if end != -1:
            section = section[:end]
        prose = normalised_prose(section)
        for phrase in (
            "same account",
            "elevation",
            "token of the process",
            "proves nothing",
            "AuthorisedLauncherRootWriteSid",
            "outside Git",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), prose)

    def test_the_runbook_states_the_verified_before_removal_ordering(self):
        """EGRT-D08: the recovery copy is verified BEFORE the in-root artefact is removed."""
        text = PROJECT_RUNBOOK.read_text(encoding="utf-8")
        section = text[text.index(RUNBOOK_SECTION_HEADING):]
        end = section.find("\n## ", len(RUNBOOK_SECTION_HEADING))
        if end != -1:
            section = section[:end]
        prose = normalised_prose(section)
        for phrase in ("SHA-256", "before", "Class C", "fails closed", "no copy at all"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), prose)

    def test_the_scheduler_artefacts_name_no_diagnostic_command(self):
        """Scheduler semantics are unchanged: the scheduled shape still runs `run`."""
        text = SCHEDULER_EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("login-diagnostic", text)
        self.assertNotIn("-Headed", text)
        for command in SCHEDULER_MUTATING_COMMANDS:
            with self.subTest(command=command):
                self.assertNotIn(command, text)

    def test_the_runtime_readme_documents_the_bounded_diagnostic_command(self):
        """The directory-level contract states the third command and its bounds."""
        text = RUNTIME_README.read_text(encoding="utf-8")
        self.assertIn("`run` (default), `list`, or `login-diagnostic`", text)
        prose = normalised_prose(text)
        for phrase in (
            "no generic headed switch",
            "implicit and non-overridable",
            "exactly five",
            "nothing is appended conditionally",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), prose)

    def test_the_design_document_records_the_accepted_amendment(self):
        """The narrow amendment is recorded; every unaffected clause stays controlling."""
        text = DESIGN_DOCUMENT.read_text(encoding="utf-8")
        self.assertIn("DL-XB-141-RUNTIME-005-SOURCE-DURABILITY-A1", text)
        self.assertIn("### 5.4 Bounded login diagnostic operation", text)
        self.assertIn("energygrid.login_diagnostic.v1", text)
        prose = normalised_prose(text)
        for phrase in (
            "every other clause of `dl-xb-141-runtime-005-source-durability` remains "
            "controlling",
            "does not add a headed switch",
            "scheduler semantics are unchanged",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), prose)

    def test_the_runbook_documents_the_diagnostic_argument_and_exit_contract(self):
        """The operator-facing contract matches the committed command surface."""
        text = PROJECT_RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(
            "python -m energygrid_bill_downloader login-diagnostic "
            "--config <EXTERNAL_CONFIG_JSON>",
            text,
        )
        prose = normalised_prose(text)
        for phrase in (
            "accepts `--config` and nothing else",
            "rejects `--headed`",
            "energygrid.login_diagnostic.v1",
            "never exits `10`",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), prose)

    def test_no_documentation_file_presents_localsystem_as_the_run_principal(self):
        """A token holding a bypass privilege fails by construction."""
        documents = {
            "runtime/README.md": RUNTIME_README,
            "docs/runbook.md": PROJECT_RUNBOOK,
            "task-scheduler/register_task.example.ps1": SCHEDULER_EXAMPLE,
        }
        for label, path in documents.items():
            text = path.read_text(encoding="utf-8")
            for token in ("LocalSystem", "NT AUTHORITY\\SYSTEM", "NT AUTHORITY\\\\SYSTEM"):
                if token not in text:
                    continue
                lines = text.splitlines()
                for index, line in enumerate(lines):
                    if token not in line:
                        continue
                    window = normalised_prose(
                        " ".join(lines[max(0, index - 2):index + 3])
                    )
                    with self.subTest(document=label, line=index + 1, token=token):
                        self.assertTrue(
                            " not " in window or "never" in window
                            or "cannot" in window or "fails" in window,
                            "%s line %d names %s without excluding it: %s"
                            % (label, index + 1, token, line.strip()),
                        )
        readme = RUNTIME_README.read_text(encoding="utf-8")
        self.assertIn(
            "LocalSystem",
            readme,
            "the runtime README must state why LocalSystem cannot be the run principal",
        )

    def test_no_documentation_file_carries_a_sid_literal(self):
        """No principal identity reaches the documentation."""
        for path in (RUNTIME_README,):
            with self.subTest(document=path.name):
                self.assertIsNone(
                    re.search(
                        FORBIDDEN_PATTERNS["sid_literal"],
                        path.read_text(encoding="utf-8"),
                    )
                )
        text = PROJECT_RUNBOOK.read_text(encoding="utf-8")
        section = text[text.index(RUNBOOK_SECTION_HEADING):]
        end = section.find("\n## ", len(RUNBOOK_SECTION_HEADING))
        if end != -1:
            section = section[:end]
        self.assertIsNone(re.search(FORBIDDEN_PATTERNS["sid_literal"], section))

    def test_no_documentation_file_carries_a_private_path(self):
        """The runtime README carries no absolute or UNC path at all."""
        text = RUNTIME_README.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z]:\\", text))
        self.assertIsNone(re.search(r"\\\\[A-Za-z0-9]", text))

        runbook = PROJECT_RUNBOOK.read_text(encoding="utf-8")
        section = runbook[runbook.index(RUNBOOK_SECTION_HEADING):]
        end = section.find("\n## ", len(RUNBOOK_SECTION_HEADING))
        if end != -1:
            section = section[:end]
        self.assertIsNone(
            re.search(r"[A-Za-z]:\\", section),
            "the new runbook section adds no absolute path literal",
        )

    def test_the_project_readme_points_at_the_runtime_layer(self):
        """The approved host mechanism the README refers to is now source-controlled."""
        text = PROJECT_README.read_text(encoding="utf-8")
        self.assertIn("## Runtime layer", text)
        self.assertIn("runtime/README.md", text)
        self.assertIn("runtime/launcher_lib.ps1", text)


if __name__ == "__main__":
    unittest.main()
