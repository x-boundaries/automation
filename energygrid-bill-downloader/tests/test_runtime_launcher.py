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


if __name__ == "__main__":
    unittest.main()
