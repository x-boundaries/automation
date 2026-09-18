"""Static, in-memory, parser, and synthetic regression proof for Run156 G3."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest


HELPER = (
    Path(__file__).resolve().parents[1]
    / "runtime"
    / "tools"
    / "XB141-EnergyGrid-Run156.ps1"
)
REPO_ROOT = HELPER.parents[3]
OWNED_RELATIVE_PATHS = (
    "energygrid-bill-downloader/runtime/tools/XB141-EnergyGrid-Run156.ps1",
    "energygrid-bill-downloader/tests/test_run156_helper_static.py",
)
CURRENT_HEAD = "54b6a7e873c2b7117016bc9f65d6c7b7894dd16c"
CURRENT_TREE = "dcad3b64a0475d3df04fe3cd8f46d9e9cf477f17"
CURRENT_PARENT = "e8b90d6d907a2970e387a6379f4aca48a8fdfd01"
ACCEPTED_STALE_ADMISSION = "ee3d70fd3700b5e9c20874ec4472ce0f433e3134"


class Run156HelperStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HELPER.read_text(encoding="utf-8")
        native_start = cls.source.index("$script:R156NativeSource = @'")
        native_end = cls.source.index("'@", native_start + 1)
        cls.native = cls.source[native_start:native_end]
        bootstrap_marker = "$script:R156BootstrapScript = @'\n"
        bootstrap_start = cls.source.index(bootstrap_marker) + len(bootstrap_marker)
        bootstrap_end = cls.source.index("\n'@", bootstrap_start)
        cls.bootstrap = cls.source[bootstrap_start:bootstrap_end]

    def method_body(self, signature, next_signature):
        start = self.native.index(signature)
        end = self.native.index(next_signature, start + len(signature))
        return self.native[start:end]

    def source_function(self, signature, next_signature):
        start = self.source.index(signature)
        end = self.source.index(next_signature, start + len(signature))
        return self.source[start:end]

    def extracted_functions(self, names):
        """Extract only named production functions for an isolated PowerShell harness."""
        wanted = set(names)
        matches = list(
            re.finditer(
                r"(?m)^function\s+([A-Za-z0-9-]+)\s*\{",
                self.source,
            )
        )
        blocks = []
        for index, match in enumerate(matches):
            if match.group(1) not in wanted:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(self.source)
            blocks.append((match.start(), self.source[match.start():end].rstrip()))
        blocks.sort(key=lambda item: item[0])
        self.assertEqual(
            {self.source_function_name(block) for _, block in blocks},
            wanted,
            "every isolated harness function must come from the production source",
        )
        return "\n\n".join(block for _, block in blocks)

    @staticmethod
    def source_function_name(block):
        match = re.match(r"function\s+([A-Za-z0-9-]+)\s*\{", block)
        if match is None:
            raise AssertionError("isolated function extraction lost its declaration")
        return match.group(1)

    def run_isolated_powershell(self, script, environment=None, timeout=120):
        """Run a scratch harness, never the complete helper or a dot-sourced file."""
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell 5.1 is not available on this host")
        probe = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSEdition"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "Desktop":
            self.skipTest("Windows PowerShell 5.1 (PSEdition Desktop) is required")
        env = os.environ.copy()
        if environment:
            env.update({str(key): str(value) for key, value in environment.items()})
        with tempfile.TemporaryDirectory(prefix="r156_isolated_") as directory:
            harness = Path(directory) / "harness.ps1"
            harness.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(harness),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        if result.returncode != 0:
            self.fail(
                "isolated PowerShell harness failed: "
                f"stdout={result.stdout[-4096:]!r} stderr={result.stderr[-4096:]!r}"
            )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def run_bootstrap(self, child_source, timeout=30, environment=None):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell 5.1 is not available on this host")
        encoded = base64.b64encode(self.bootstrap.encode("utf-16-le")).decode("ascii")
        env = os.environ.copy()
        if environment:
            env.update({str(key): str(value) for key, value in environment.items()})
        return subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            input=child_source,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def run_synthetic_transport(self, mode, child_source, private_sentinel):
        functions = self.extracted_functions(
            (
                "Get-R156Properties",
                "Test-R156ExactNameMultiset",
                "Test-R156ExactPropertySet",
                "ConvertTo-R156EncodedCommand",
                "Get-R156ChildProtocol",
                "Test-R156DispatchMarkerObserved",
                "Invoke-R156Transport",
            )
        )
        script = (
            "$script:R156BootstrapScript = @'\n"
            + self.bootstrap
            + "\n'@\n"
            + r"""
$script:R156ChildScript = [Environment]::GetEnvironmentVariable('R156_SYNTHETIC_CHILD', 'Process')
$script:R156RealStarted = $false
$script:R156RealInstallerInvocations = 0
$script:R156AuthorityConsumed = 'NO'
$script:R156PackageMutation = 'NONE'
"""
            + functions
            + r"""
$private = [Environment]::GetEnvironmentVariable('R156_PRIVATE_SENTINEL', 'Process')
$result = Invoke-R156Transport `
    -Mode ([Environment]::GetEnvironmentVariable('R156_SYNTHETIC_MODE', 'Process')) `
    -CheckoutRoot ('C:\checkout-' + $private) `
    -InstallerPath ('C:\installer-' + $private + '.ps1') `
    -LauncherRoot ('C:\launcher-' + $private) `
    -AdmissionCommit '1111111111111111111111111111111111111111'
[pscustomobject]@{
    started = [bool]$result.Started
    supervisor_complete = [bool]$result.SupervisorComplete
    exit_code = [int]$result.ChildExitCode
    packet_present = $null -ne $result.Packet
    validation_status = if ($null -eq $result.Packet) { '' } else { [string]$result.Packet.validation_status }
    validation_current = if ($null -eq $result.Packet) { '' } else { [string]$result.Packet.validation_current }
    real_status = if ($null -eq $result.Packet) { '' } else { [string]$result.Packet.real_status }
    real_success_shape = if ($null -eq $result.Packet) { $false } else { [bool]$result.Packet.real_success_shape }
    real_started = [bool]$script:R156RealStarted
    real_invocations = [int]$script:R156RealInstallerInvocations
    authority_consumed = [string]$script:R156AuthorityConsumed
    package_mutation = [string]$script:R156PackageMutation
} | ConvertTo-Json -Compress
"""
        )
        lines = self.run_isolated_powershell(
            script,
            environment={
                "R156_SYNTHETIC_MODE": mode,
                "R156_SYNTHETIC_CHILD": child_source,
                "R156_PRIVATE_SENTINEL": private_sentinel,
            },
        )
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn(private_sentinel, "\n".join(lines))
        return json.loads(lines[0])

    @staticmethod
    def git_contract_constants():
        return """
$script:R156LibraryGitBlob = 'de75302dfb7ce3b1b4b37919d6b7e734d0503018'
$script:R156LibraryGitBlobLength = [int64]141393
$script:R156InstallerGitBlob = 'a5b670e3b043a026af1d7f2086df03fdb1e7fa13'
$script:R156InstallerGitBlobLength = [int64]25643
$script:R156LauncherGitBlob = 'd632068bbd5832cc46971278ca0f4fba3bf7e8f3'
$script:R156LauncherGitBlobLength = [int64]21081
$script:R156ExpectedHeadAtExecution = '0000000000000000000000000000000000000000'
$script:R156GitOutputLimitBytes = [int64]65536
$script:R156LocalGitReadOnlySubcommands = @('symbolic-ref', 'rev-parse', 'status', 'config', 'cat-file')
"""

    @staticmethod
    def parse_fields(lines):
        self_line = next((line for line in lines if line.startswith("started=")), None)
        if self_line is None:
            raise AssertionError(f"isolated process harness emitted no result: {lines!r}")
        fields = {}
        for field in self_line.split(";"):
            key, value = field.split("=", 1)
            fields[key] = value
        for key in ("started", "success", "timedout", "overflow", "stderrdiscarded"):
            if key in fields:
                fields[key] = fields[key].lower() == "true"
        for key in ("length", "exit"):
            if key in fields:
                fields[key] = int(fields[key])
        return fields

    @staticmethod
    def production_status_vector():
        return [
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.hooksPath=NUL",
            "-c",
            "submodule.recurse=false",
            "-c",
            "core.autocrlf=true",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ]

    @staticmethod
    def isolated_git_environment():
        environment = os.environ.copy()
        for name in tuple(environment):
            if name.startswith("GIT_") or name.startswith("GCM_"):
                environment.pop(name)
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": "NUL",
                "GIT_CONFIG_GLOBAL": "NUL",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_NO_LAZY_FETCH": "1",
            }
        )
        return environment

    def run_synthetic_git(self, repository, arguments, check=True):
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            env=self.isolated_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                "synthetic Git command failed: "
                f"arguments={arguments!r} exit={result.returncode} "
                f"stderr={result.stderr[-2048:]!r}"
            )
        return result

    def create_synthetic_eol_repository(self, directory):
        repository = Path(directory)
        self.run_synthetic_git(repository, ["-c", "init.defaultBranch=main", "init", "-q"])
        self.run_synthetic_git(repository, ["config", "user.name", "Run156 Offline Test"])
        self.run_synthetic_git(
            repository,
            ["config", "user.email", "run156-offline@example.invalid"],
        )
        (repository / "alpha.txt").write_bytes(b"alpha\nsecond\n")
        (repository / "beta.txt").write_bytes(b"beta\nsecond\n")
        self.run_synthetic_git(repository, ["-c", "core.autocrlf=true", "add", "--", "alpha.txt", "beta.txt"])
        self.run_synthetic_git(repository, ["-c", "core.autocrlf=true", "commit", "-q", "-m", "fixture"])
        (repository / "alpha.txt").unlink()
        (repository / "beta.txt").unlink()
        self.run_synthetic_git(
            repository,
            ["-c", "core.autocrlf=true", "checkout", "--", "alpha.txt", "beta.txt"],
        )
        time.sleep(0.02)
        (repository / "alpha.txt").write_bytes(b"alpha\r\nsecond\r\n")
        (repository / "beta.txt").write_bytes(b"beta\r\nsecond\r\n")
        return repository

    def synthetic_status(self, repository, vector=None):
        return self.run_synthetic_git(
            repository,
            self.production_status_vector() if vector is None else vector,
            check=False,
        )

    @staticmethod
    def read_witness(path):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return []
        if size > 4096:
            raise AssertionError("synthetic child witness exceeded its bounded size")
        try:
            text = path.read_bytes().decode("ascii")
        except UnicodeDecodeError as error:
            raise AssertionError("synthetic child witness was not ASCII") from error
        return [line for line in text.splitlines() if line]

    def process_functions(self):
        return self.extracted_functions(
            (
                "ConvertTo-R156NativeArgument",
                "New-R156GitProcessResult",
                "Get-R156CompletedReadResult",
                "Invoke-R156BoundedProcess",
                "Convert-R156BytesToHex",
                "Get-R156Sha256ForBytes",
            )
        )

    def git_output_functions(self):
        return self.extracted_functions(
            (
                "Convert-R156BytesToHex",
                "Get-R156Sha1ForGitObject",
                "Convert-R156Utf8Bytes",
                "Convert-R156ConfigBytes",
                "Test-R156SingleGitLineBytes",
                "Get-R156LocalGitCommandName",
                "Test-R156LocalGitArguments",
                "Get-R156LocalGitOutputContract",
                "Test-R156LocalGitOutput",
            )
        )

    def config_functions(self):
        return self.extracted_functions(
            (
                "Convert-R156Utf8Bytes",
                "Convert-R156ConfigBytes",
                "Test-R156BranchName",
                "Get-R156BranchConfigKey",
                "Test-R156ConfigAdmission",
                "Test-R156CanonicalOrigin",
            )
        )

    def worktree_config_functions(self):
        return self.extracted_functions(
            (
                "Test-R156WorktreeConfigSurfaceAbsent",
                "Test-R156WorktreeConfigFence",
            )
        )

    def run_repository_order_case(self, absence_results, status_case="clean"):
        functions = self.extracted_functions(
            (
                "Get-R156LocalGitCommandName",
                "Test-R156WorktreeConfigFence",
                "Get-R156RepositoryState",
            )
        )
        script = (
            r"""
$script:R156Events = New-Object System.Collections.ArrayList
$script:R156AbsenceResults = @($env:R156_ABSENCE_RESULTS -split ',' | ForEach-Object { [bool]::Parse($_) })
$script:R156AbsenceIndex = 0
$script:R156RepositoryConfigHandle = $null
$script:R156IndexHandle = $null
$script:R156ExpectedHeadAtExecution = '0000000000000000000000000000000000000000'
$script:R156ExpectedTreeAtExecution = '1111111111111111111111111111111111111111'
$script:R156ExpectedParentAtExecution = '2222222222222222222222222222222222222222'
$script:R156StatusCase = [string]$env:R156_STATUS_CASE
$script:R156StatusArguments = [string[]]@()

function Test-R156SamePath { return $true }
function Test-R156NormalDirectory { return $true }
function Test-R156NoReparseAncestors { return $true }
function Test-R156NormalFile { return $true }
function Open-R156ReadOnlyHandle { return (New-Object System.IO.MemoryStream) }
function Convert-R156ConfigBytes { return [object[]]@([pscustomobject]@{ Key = 'synthetic'; Value = 'synthetic' }) }
function Test-R156ConfigAdmission {
    return [pscustomobject]@{ Pass = $true; Origin = [string[]]@('origin'); WorktreeConfigEnabled = $true }
}
function Test-R156WorktreeConfigSurfaceAbsent {
    $value = $false
    if ($script:R156AbsenceIndex -lt $script:R156AbsenceResults.Count) {
        $value = [bool]$script:R156AbsenceResults[$script:R156AbsenceIndex]
    }
    $script:R156AbsenceIndex += 1
    [void]$script:R156Events.Add('absence:' + $value.ToString().ToLowerInvariant())
    return $value
}
function Invoke-R156LocalGitRead {
    param([string]$RepositoryRoot, [string[]]$Arguments)
    $command = Get-R156LocalGitCommandName -Arguments $Arguments
    [void]$script:R156Events.Add('git:' + $command)
    if ($command -ceq 'status') {
        $script:R156StatusArguments = [string[]]@($Arguments)
        if ($script:R156StatusCase -ceq 'dirty') {
            return [pscustomobject]@{ Success = $true; StdoutBytes = [System.Text.Encoding]::ASCII.GetBytes(" M synthetic.txt`n") }
        }
        if ($script:R156StatusCase -ceq 'nonzero') {
            return [pscustomobject]@{ Success = $false; Started = $true; ExitCode = 7; StdoutBytes = [byte[]]@() }
        }
        if ($script:R156StatusCase -ceq 'launch') {
            return [pscustomobject]@{ Success = $false; Started = $false; ExitCode = -1; StdoutBytes = [byte[]]@() }
        }
        if ($script:R156StatusCase -ceq 'timeout') {
            return [pscustomobject]@{ Success = $false; Started = $true; ExitCode = -1; TimedOut = $true; StdoutBytes = [byte[]]@() }
        }
        if ($script:R156StatusCase -ceq 'overflow') {
            return [pscustomobject]@{ Success = $false; Started = $true; ExitCode = -1; Overflow = $true; StdoutBytes = [byte[]]@() }
        }
        return [pscustomobject]@{ Success = $true; Started = $true; ExitCode = 0; StdoutBytes = [byte[]]@() }
    }
    $text = ''
    if ($command -ceq 'symbolic-ref') { $text = 'main' }
    elseif ($command -ceq 'rev-parse' -and $Arguments[-1] -ceq 'HEAD') { $text = $script:R156ExpectedHeadAtExecution }
    elseif ($command -ceq 'rev-parse' -and $Arguments[-1] -ceq '--show-toplevel') { $text = 'C:\XB\automation' }
    elseif ($command -ceq 'rev-parse' -and $Arguments[-1] -ceq '--is-inside-work-tree') { $text = 'true' }
    elseif ($command -ceq 'rev-parse' -and $Arguments[-1] -ceq '--git-dir') { $text = '.git' }
    elseif ($command -ceq 'rev-parse' -and $Arguments[-1] -ceq '--git-common-dir') { $text = '.git' }
    return [pscustomobject]@{ Success = $true; StdoutBytes = [byte[]]@(); Text = $text }
}
function Get-R156SingleGitLine { param($Result) return [string]$Result.Text }
function Resolve-R156RepositoryPath { return 'C:\XB\automation\.git' }
function Get-R156CommitTreeParentProof {
    return [pscustomobject]@{
        Tree = $script:R156ExpectedTreeAtExecution
        Parent = $script:R156ExpectedParentAtExecution
        ParentCount = 1
        CommitObjectHash = $script:R156ExpectedHeadAtExecution
    }
}
function Get-R156Metadata { return [pscustomobject]@{ Value = 'same' } }
function Read-R156HandleBytes { return [byte[]](1, 2, 3) }
function Get-R156Sha256ForBytes { return 'digest' }
function Test-R156MetadataEqual { return $true }
"""
            + functions
            + r"""
$state = Get-R156RepositoryState -RepositoryRoot 'C:\XB\automation'
[pscustomobject]@{
    read_ok = [bool]$state.ReadOk
    clean = [bool]$state.Clean
    admitted = [bool]($state.ReadOk -and $state.Clean)
    events = [string[]]@($script:R156Events)
    absence_checks = [int]$script:R156AbsenceIndex
    status_arguments = [string[]]@($script:R156StatusArguments)
} | ConvertTo-Json -Compress
"""
        )
        lines = self.run_isolated_powershell(
            script,
            environment={
                "R156_ABSENCE_RESULTS": ",".join(
                    "true" if value else "false" for value in absence_results
                ),
                "R156_STATUS_CASE": status_case,
            },
        )
        self.assertEqual(len(lines), 1, lines)
        return json.loads(lines[0])

    def run_trusted_source_order_case(self, absence_results):
        functions = self.extracted_functions(("Read-R156TrustedSource",))
        script = (
            r"""
$script:R156Events = New-Object System.Collections.ArrayList
$script:R156AbsenceResults = @($env:R156_ABSENCE_RESULTS -split ',' | ForEach-Object { [bool]::Parse($_) })
$script:R156AbsenceIndex = 0
$script:R156GitReads = 0
$script:R156ExpectedHeadAtExecution = '1111111111111111111111111111111111111111'
$script:R156SourceHandles = @()
$script:R156ExpectedSyntheticBlob = '0000000000000000000000000000000000000000'
$script:R156SyntheticBytes = [byte[]](120, 10)

function Test-R156LockedSourcePath { return $true }
function Test-R156SamePath { return $true }
function Test-R156NormalFile { return $true }
function Test-R156NoReparseAncestors { return $true }
function Test-R156WorktreeConfigSurfaceAbsent {
    $value = $false
    if ($script:R156AbsenceIndex -lt $script:R156AbsenceResults.Count) {
        $value = [bool]$script:R156AbsenceResults[$script:R156AbsenceIndex]
    }
    $script:R156AbsenceIndex += 1
    [void]$script:R156Events.Add('absence:' + $value.ToString().ToLowerInvariant())
    return $value
}
function Invoke-R156LocalGitRead {
    param([string]$RepositoryRoot, [string[]]$Arguments)
    $script:R156GitReads += 1
    [void]$script:R156Events.Add('git:' + [string]$Arguments[0])
    return [pscustomobject]@{
        Success = $true
        StdoutBytes = [byte[]]$script:R156SyntheticBytes
        Text = $script:R156ExpectedSyntheticBlob
    }
}
function Get-R156SingleGitLine { param($Result) return [string]$Result.Text }
function Test-R156CommittedByteContract { return $true }
function Get-R156Sha1ForGitObject { return [string]$script:R156ExpectedSyntheticBlob }
function Open-R156ReadOnlyHandle { return (New-Object System.IO.MemoryStream) }
function Get-R156Metadata { return [pscustomobject]@{ Value = 'same' } }
function Read-R156HandleBytes { return [byte[]]$script:R156SyntheticBytes }
function Test-R156MetadataEqual { return $true }
function Test-R156WorkingByteContract {
    return [pscustomobject]@{ NormalizedBytes = [byte[]]$script:R156SyntheticBytes; Ending = 'LF' }
}
function Test-R156ByteArraysEqual { return $true }
function Test-R156PowerShellParse { return $true }
function Get-R156Sha256ForBytes { return 'digest' }
"""
            + functions
            + r"""
$source = Read-R156TrustedSource `
    -RepositoryRoot 'C:\XB\automation' `
    -RelativePath 'energygrid-bill-downloader/runtime/launcher.ps1' `
    -ExpectedGitBlob $script:R156ExpectedSyntheticBlob `
    -ExpectedGitBlobLength 2
[pscustomobject]@{
    accepted = $null -ne $source
    events = [string[]]@($script:R156Events)
    git_reads = [int]$script:R156GitReads
    absence_checks = [int]$script:R156AbsenceIndex
} | ConvertTo-Json -Compress
"""
        )
        lines = self.run_isolated_powershell(
            script,
            environment={
                "R156_ABSENCE_RESULTS": ",".join(
                    "true" if value else "false" for value in absence_results
                )
            },
        )
        self.assertEqual(len(lines), 1, lines)
        return json.loads(lines[0])

    def run_final_predispatch_case(self, final_absence):
        start = self.source.index("    $canonicalLibraryBeforeReal =")
        dispatch = self.source.index(
            "    $realResult = Invoke-R156Transport -Mode 'REAL'",
            start,
        )
        end = self.source.index("\n", dispatch) + 1
        production_sequence = self.source[start:end]
        script = (
            r"""
$script:R156Events = New-Object System.Collections.ArrayList
$script:R156RealInstallerInvocations = 0
$script:R156LibraryRelative = 'library'
$script:R156LibraryGitBlob = '1111111111111111111111111111111111111111'
$script:R156LibraryGitBlobLength = 1
$script:R156LauncherRelative = 'launcher'
$script:R156LauncherGitBlob = '2222222222222222222222222222222222222222'
$script:R156LauncherGitBlobLength = 1
$script:R156InstallerRelative = 'installer'
$script:R156InstallerGitBlob = '3333333333333333333333333333333333333333'
$script:R156InstallerGitBlobLength = 1
$script:R156ExpectedInstalledAdmissionAtExecution = '4444444444444444444444444444444444444444'
$script:R156ExpectedHeadAtExecution = '5555555555555555555555555555555555555555'
$script:R156GithubAuth = 'FAIL'
$checkout = 'C:\XB\automation'
$topologyBeforeReal = [pscustomobject]@{ Candidate = 'C:\Program Files\Synthetic' }
$canonicalSources = [ordered]@{ 'launcher.ps1' = $null; 'launcher_lib.ps1' = $null }

function Read-R156TrustedSource {
    [void]$script:R156Events.Add('trusted-source')
    return [pscustomobject]@{ Path = 'C:\synthetic\installer.ps1' }
}
function Read-R156ManifestState {
    [void]$script:R156Events.Add('manifest')
    return [pscustomobject]@{ Pass = $true }
}
function Test-R156RemoteHead {
    [void]$script:R156Events.Add('remote-head')
    return $true
}
function Test-R156WorktreeConfigSurfaceAbsent {
    [void]$script:R156Events.Add('final-absence')
    return [bool]::Parse([string]$env:R156_FINAL_ABSENCE)
}
function Stop-R156Gate { throw 'synthetic-stop' }
function Invoke-R156Transport {
    [void]$script:R156Events.Add('real')
    $script:R156RealInstallerInvocations += 1
    return [pscustomobject]@{ Started = $true }
}

function Invoke-R156ProductionPredispatch {
"""
            + production_sequence
            + r"""
}

try { Invoke-R156ProductionPredispatch } catch { }
[pscustomobject]@{
    events = [string[]]@($script:R156Events)
    real_installer_invocations = [int]$script:R156RealInstallerInvocations
} | ConvertTo-Json -Compress
"""
        )
        lines = self.run_isolated_powershell(
            script,
            environment={"R156_FINAL_ABSENCE": "true" if final_absence else "false"},
        )
        self.assertEqual(len(lines), 1, lines)
        return json.loads(lines[0])

    def json_functions(self):
        return self.extracted_functions(
            (
                "Get-R156FullPath",
                "Get-R156ParentDirectory",
                "Test-R156NormalFile",
                "Test-R156NoReparseAncestors",
                "Open-R156ReadOnlyHandle",
                "Read-R156HandleBytes",
                "Convert-R156Utf8Bytes",
                "Skip-R156JsonWhitespace",
                "Get-R156JsonHexValue",
                "Get-R156JsonString",
                "Test-R156JsonNumber",
                "Test-R156JsonLiteral",
                "Test-R156JsonValue",
                "Test-R156JsonObject",
                "Test-R156JsonArray",
                "Test-R156StrictJsonBytes",
                "Read-R156StrictJsonObject",
            )
        )

    @staticmethod
    def stream_child_script():
        return r"""
$ProgressPreference = 'SilentlyContinue'
$mode = [string]$env:R156_CHILD_MODE
$stdout = [Console]::OpenStandardOutput()
$stderr = [Console]::OpenStandardError()
$exitCode = 0

function Write-R156ChildWitness {
    param([Parameter(Mandatory)][string]$Phase)
    $witnessPath = [string]$env:R156_WITNESS_PATH
    if ([string]::IsNullOrWhiteSpace($witnessPath)) {
        throw 'missing synthetic witness path'
    }
    [System.IO.File]::AppendAllText(
        $witnessPath,
        $Phase + [Environment]::NewLine,
        [System.Text.Encoding]::ASCII
    )
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class R156ChildNative
{
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GetStdHandle(int nStdHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CloseHandle(IntPtr hObject);
}
'@

function Close-R156ChildStandardHandle {
    param(
        [Parameter(Mandatory)]
        [ValidateSet('stdout', 'stderr')]
        [string]$Name
    )
    $identifier = if ($Name -ceq 'stdout') { -11 } else { -12 }
    $handle = [R156ChildNative]::GetStdHandle($identifier)
    if ($handle -eq [IntPtr]::Zero -or $handle.ToInt64() -eq -1) {
        $lastError = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        Write-R156ChildWitness -Phase ("native-close-failed:{0}:get:{1}" -f $Name, $lastError)
        throw ("GetStdHandle failed for {0}; win32_error={1}" -f $Name, $lastError)
    }
    $closed = [R156ChildNative]::CloseHandle($handle)
    if (-not $closed) {
        $lastError = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        Write-R156ChildWitness -Phase ("native-close-failed:{0}:close:{1}" -f $Name, $lastError)
        throw ("CloseHandle failed for {0}; win32_error={1}" -f $Name, $lastError)
    }
    Write-R156ChildWitness -Phase ($Name + '-os-closed')
}

function Write-R156ChildText {
    param([Parameter(Mandatory)][System.IO.Stream]$Stream, [Parameter(Mandatory)][string]$Text)
    $bytes = [System.Text.Encoding]::ASCII.GetBytes($Text)
    if ($bytes.Length -gt 0) {
        $Stream.Write($bytes, 0, $bytes.Length)
        $Stream.Flush()
    }
}

function Write-R156ChildFile {
    param([Parameter(Mandatory)][System.IO.Stream]$Stream, [Parameter(Mandatory)][string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -gt 0) {
        $Stream.Write($bytes, 0, $bytes.Length)
        $Stream.Flush()
    }
}

function Write-R156ChildFill {
    param([Parameter(Mandatory)][System.IO.Stream]$Stream, [Parameter(Mandatory)][int]$Count)
    $bytes = New-Object byte[] $Count
    $Stream.Write($bytes, 0, $bytes.Length)
    $Stream.Flush()
}

switch ($mode) {
        'payload' {
            Write-R156ChildFile -Stream $stdout -Path $env:R156_PAYLOAD_PATH
            if (([System.IO.FileInfo]$env:R156_ERROR_PAYLOAD_PATH).Length -gt 0) {
                Write-R156ChildFile -Stream $stderr -Path $env:R156_ERROR_PAYLOAD_PATH
            }
        }
        'empty' {
        }
        'stdout-first' {
            Write-R156ChildText -Stream $stdout -Text 'OUT'
            Write-R156ChildWitness -Phase 'stdout-payload-written'
            Close-R156ChildStandardHandle -Name 'stdout'
            Start-Sleep -Milliseconds 150
            Write-R156ChildWitness -Phase 'post-stdout-closure-alive'
            Write-R156ChildText -Stream $stderr -Text 'ERR'
            Write-R156ChildWitness -Phase 'stderr-write-after-stdout-closure'
            Close-R156ChildStandardHandle -Name 'stderr'
            Write-R156ChildWitness -Phase 'both-os-handles-closed'
        }
        'stderr-first' {
            Write-R156ChildText -Stream $stderr -Text 'ERR'
            Write-R156ChildWitness -Phase 'stderr-payload-written'
            Close-R156ChildStandardHandle -Name 'stderr'
            Start-Sleep -Milliseconds 150
            Write-R156ChildWitness -Phase 'post-stderr-closure-alive'
            Write-R156ChildText -Stream $stdout -Text 'OUT'
            Write-R156ChildWitness -Phase 'stdout-write-after-stderr-closure'
            Close-R156ChildStandardHandle -Name 'stdout'
            Write-R156ChildWitness -Phase 'both-os-handles-closed'
        }
        'simultaneous' {
            Write-R156ChildText -Stream $stdout -Text 'OUT'
            Write-R156ChildWitness -Phase 'stdout-payload-written'
            Write-R156ChildText -Stream $stderr -Text 'ERR'
            Write-R156ChildWitness -Phase 'stderr-payload-written'
            Close-R156ChildStandardHandle -Name 'stdout'
            Close-R156ChildStandardHandle -Name 'stderr'
            Write-R156ChildWitness -Phase 'both-os-handles-closed'
        }
        'stdout-after-stderr' {
            Write-R156ChildText -Stream $stderr -Text 'ERR'
            Write-R156ChildWitness -Phase 'stderr-payload-written'
            Close-R156ChildStandardHandle -Name 'stderr'
            Start-Sleep -Milliseconds 60
            Write-R156ChildWitness -Phase 'post-stderr-closure-alive'
            Write-R156ChildFill -Stream $stdout -Count 32768
            Write-R156ChildWitness -Phase 'stdout-write1-after-stderr-closure'
            Start-Sleep -Milliseconds 60
            Write-R156ChildFill -Stream $stdout -Count 32768
            Write-R156ChildWitness -Phase 'stdout-write2-after-stderr-closure'
            Close-R156ChildStandardHandle -Name 'stdout'
            Write-R156ChildWitness -Phase 'both-os-handles-closed'
        }
        'stderr-after-stdout' {
            Write-R156ChildText -Stream $stdout -Text 'OUT'
            Write-R156ChildWitness -Phase 'stdout-payload-written'
            Close-R156ChildStandardHandle -Name 'stdout'
            Start-Sleep -Milliseconds 60
            Write-R156ChildWitness -Phase 'post-stdout-closure-alive'
            Write-R156ChildFill -Stream $stderr -Count 32768
            Write-R156ChildWitness -Phase 'stderr-write1-after-stdout-closure'
            Start-Sleep -Milliseconds 60
            Write-R156ChildFill -Stream $stderr -Count 32768
            Write-R156ChildWitness -Phase 'stderr-write2-after-stdout-closure'
            Close-R156ChildStandardHandle -Name 'stderr'
            Write-R156ChildWitness -Phase 'both-os-handles-closed'
        }
        'both-closed-alive' {
            Write-R156ChildText -Stream $stdout -Text 'OUT'
            Write-R156ChildWitness -Phase 'stdout-payload-written'
            Write-R156ChildText -Stream $stderr -Text 'ERR'
            Write-R156ChildWitness -Phase 'stderr-payload-written'
            Close-R156ChildStandardHandle -Name 'stdout'
            Close-R156ChildStandardHandle -Name 'stderr'
            Write-R156ChildWitness -Phase 'both-os-handles-closed'
            Write-R156ChildWitness -Phase 'post-both-closure-alive'
            Start-Sleep -Milliseconds 5000
        }
        'timeout' {
            Start-Sleep -Milliseconds 5000
        }
        'stdout-overflow' {
            Write-R156ChildFill -Stream $stdout -Count 70000
        }
        'stderr-overflow' {
            Write-R156ChildFill -Stream $stderr -Count 70000
        }
        'nonzero' {
            Write-R156ChildText -Stream $stdout -Text 'OUT'
            Write-R156ChildWitness -Phase 'stdout-payload-written'
            Close-R156ChildStandardHandle -Name 'stdout'
            Close-R156ChildStandardHandle -Name 'stderr'
            Write-R156ChildWitness -Phase 'both-os-handles-closed'
            $exitCode = 7
        }
        default {
            throw 'unknown synthetic child mode'
        }
}

if ($exitCode -ne 0) {
    exit $exitCode
}
"""

    def run_bounded_process(
        self,
        mode,
        stdout_limit=65536,
        stderr_limit=65536,
        timeout_milliseconds=2000,
        payload=b"",
        error_payload=b"",
    ):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell 5.1 is not available on this host")
        with tempfile.TemporaryDirectory(prefix="r156_process_") as directory:
            root = Path(directory)
            payload_path = root / "payload.bin"
            error_path = root / "error.bin"
            child_path = root / "child.ps1"
            witness_path = root / "witness.state"
            payload_path.write_bytes(payload)
            error_path.write_bytes(error_payload)
            child_path.write_text(self.stream_child_script(), encoding="utf-8")
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                + self.process_functions()
                + r"""
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
if ([string]$env:R156_CHILD_MODE -ceq 'not-started') {
    $startInfo.FileName = 'C:\R156-missing\not-a-process.exe'
}
else {
    $startInfo.FileName = [string]$env:R156_CHILD_POWERSHELL
}
$startInfo.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + (ConvertTo-R156NativeArgument -Value $env:R156_CHILD_SCRIPT)
$startInfo.UseShellExecute = $false
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.CreateNoWindow = $true
$startInfo.EnvironmentVariables['R156_CHILD_MODE'] = [string]$env:R156_CHILD_MODE
$startInfo.EnvironmentVariables['R156_PAYLOAD_PATH'] = [string]$env:R156_PAYLOAD_PATH
$startInfo.EnvironmentVariables['R156_ERROR_PAYLOAD_PATH'] = [string]$env:R156_ERROR_PAYLOAD_PATH
$startInfo.EnvironmentVariables['R156_WITNESS_PATH'] = [string]$env:R156_WITNESS_PATH
$result = Invoke-R156BoundedProcess -StartInfo $startInfo -TimeoutMilliseconds ([int]$env:R156_TIMEOUT_MS) -MaximumOutputBytes ([int64]$env:R156_STDOUT_LIMIT) -MaximumErrorBytes ([int64]$env:R156_STDERR_LIMIT)
$algorithm = [System.Security.Cryptography.SHA256]::Create()
try {
    $sha = [BitConverter]::ToString($algorithm.ComputeHash($result.StdoutBytes)).Replace('-', '').ToLowerInvariant()
}
finally {
    $algorithm.Dispose()
}
Write-Output ('started=' + [string]$result.Started + ';success=' + [string]$result.Success + ';length=' + [string]$result.StdoutBytes.Length + ';exit=' + [string]$result.ExitCode + ';timedout=' + [string]$result.TimedOut + ';overflow=' + [string]$result.Overflow + ';stderrdiscarded=' + [string]$result.StderrDiscarded + ';sha256=' + $sha)
"""
            )
            lines = self.run_isolated_powershell(
                script,
                environment={
                    "R156_CHILD_MODE": mode,
                    "R156_CHILD_POWERSHELL": powershell,
                    "R156_CHILD_SCRIPT": str(child_path),
                    "R156_PAYLOAD_PATH": str(payload_path),
                    "R156_ERROR_PAYLOAD_PATH": str(error_path),
                    "R156_WITNESS_PATH": str(witness_path),
                    "R156_STDOUT_LIMIT": str(stdout_limit),
                    "R156_STDERR_LIMIT": str(stderr_limit),
                    "R156_TIMEOUT_MS": str(timeout_milliseconds),
                },
                timeout=30,
            )
            witness = self.read_witness(witness_path)
            native_failures = [line for line in witness if line.startswith("native-close-failed:")]
            if native_failures:
                self.fail("synthetic child native handle close failed: " + " | ".join(native_failures))
        fields = self.parse_fields(lines)
        sha_line = next(line for line in lines if line.startswith("started="))
        fields["sha256"] = sha_line.split("sha256=", 1)[1]
        fields["witness"] = witness
        return fields

    def run_local_output_check(self, object_argument, payload):
        with tempfile.TemporaryDirectory(prefix="r156_output_contract_") as directory:
            payload_path = Path(directory) / "payload.bin"
            payload_path.write_bytes(payload)
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                + self.git_contract_constants()
                + self.git_output_functions()
                + r"""
$arguments = @('cat-file', 'blob', [string]$env:R156_OBJECT_ARGUMENT)
$bytes = [System.IO.File]::ReadAllBytes($env:R156_PAYLOAD_PATH)
$contract = Get-R156LocalGitOutputContract -Arguments $arguments
$valid = Test-R156LocalGitOutput -Arguments $arguments -Bytes $bytes
if ($null -eq $contract) {
    Write-Output ('valid=' + [string]$valid + ';contract=none')
}
else {
    Write-Output ('valid=' + [string]$valid + ';contract_limit=' + [string]$contract.StdoutLimitBytes + ';contract_length=' + [string]$contract.ExpectedBlobLength + ';contract_object=' + [string]$contract.ExpectedBlobObject)
}
"""
            )
            lines = self.run_isolated_powershell(
                script,
                environment={
                    "R156_OBJECT_ARGUMENT": object_argument,
                    "R156_PAYLOAD_PATH": str(payload_path),
                },
            )
        fields = {}
        for field in lines[-1].split(";"):
            key, value = field.split("=", 1)
            fields[key] = value
        fields["valid"] = fields["valid"].lower() == "true"
        for key in ("contract_limit", "contract_length"):
            if key in fields:
                fields[key] = int(fields[key])
        return fields

    def run_empty_byte_matrix(self):
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            + self.git_contract_constants()
            + self.git_output_functions()
            + self.extracted_functions(("New-R156GitProcessResult",))
            + r"""
$statusArguments = @('-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=NUL', '-c', 'submodule.recurse=false', '-c', 'core.autocrlf=true', 'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
$cleanStatus = Test-R156LocalGitOutput -Arguments $statusArguments -Bytes ([byte[]]@())
$dirtyStatus = Test-R156LocalGitOutput -Arguments $statusArguments -Bytes ([System.Text.Encoding]::ASCII.GetBytes(" M file`n"))
$required = @(
    [pscustomobject]@{ Name = 'symbolic'; Arguments = @('symbolic-ref', '--short', '-q', 'HEAD') },
    [pscustomobject]@{ Name = 'head'; Arguments = @('rev-parse', '--verify', 'HEAD') },
    [pscustomobject]@{ Name = 'config'; Arguments = @('config', '--file=C:\XB\automation\.git\config', '--no-includes', '--null', '--list') },
    [pscustomobject]@{ Name = 'commit'; Arguments = @('cat-file', 'commit', $script:R156ExpectedHeadAtExecution) },
    [pscustomobject]@{ Name = 'blob'; Arguments = @('cat-file', 'blob', $script:R156LibraryGitBlob) }
)
$results = @()
foreach ($case in $required) {
    $results = $results + (Test-R156LocalGitOutput -Arguments $case.Arguments -Bytes ([byte[]]@()))
}
$notStarted = New-R156GitProcessResult -Started $false -Success $false -ExitCode -1 -StdoutBytes ([byte[]]@())
$timeout = New-R156GitProcessResult -Started $true -Success $false -ExitCode -1 -StdoutBytes ([byte[]]@()) -TimedOut $true
$overflow = New-R156GitProcessResult -Started $true -Success $false -ExitCode -1 -StdoutBytes ([byte[]]@()) -Overflow $true
$nonzero = New-R156GitProcessResult -Started $true -Success $false -ExitCode 7 -StdoutBytes ([byte[]]@())
$failedEmpty = @($notStarted, $timeout, $overflow, $nonzero) | Where-Object { $_.StdoutBytes.Length -eq 0 }
Write-Output ('clean_status=' + [string]$cleanStatus + ';dirty_status=' + [string]$dirtyStatus + ';failed_empty=' + [string]($failedEmpty.Count -eq 4))
foreach ($case in $required) {
    Write-Output ('data_' + $case.Name + '=' + [string](Test-R156LocalGitOutput -Arguments $case.Arguments -Bytes ([byte[]]@())))
}
"""
        )
        lines = self.run_isolated_powershell(script)
        values = {}
        for line in lines:
            for field in line.split(";"):
                key, value = field.split("=", 1)
                values[key] = value.lower() == "true"
        return values

    def run_fault_cancel_matrix(self):
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            + self.extracted_functions(("Get-R156CompletedReadResult",))
            + r"""
$faultSource = New-Object 'System.Threading.Tasks.TaskCompletionSource[int]'
[void]$faultSource.SetException((New-Object -TypeName System.InvalidOperationException -ArgumentList 'synthetic'))
$fault = Get-R156CompletedReadResult -Task $faultSource.Task -Active $true
$cancelSource = New-Object 'System.Threading.Tasks.TaskCompletionSource[int]'
[void]$cancelSource.SetCanceled()
$cancelled = Get-R156CompletedReadResult -Task $cancelSource.Task -Active $true
$inactiveSource = New-Object 'System.Threading.Tasks.TaskCompletionSource[int]'
[void]$inactiveSource.SetResult(0)
$inactive = Get-R156CompletedReadResult -Task $inactiveSource.Task -Active $false
Write-Output ('fault_success=' + [string]$fault.Success + ';faulted=' + [string]$fault.Faulted + ';cancel_success=' + [string]$cancelled.Success + ';cancelled=' + [string]$cancelled.Canceled + ';inactive_invariant=' + [string]$inactive.InvariantViolation)
"""
        )
        lines = self.run_isolated_powershell(script)
        values = {}
        for field in lines[-1].split(";"):
            key, value = field.split("=", 1)
            values[key] = value.lower() == "true"
        return values

    def run_acl_matrix(self):
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            + self.extracted_functions(("Test-R156MutationCapableFileSystemRights",))
            + r"""
function New-R156RawFileSystemRights {
    param([Parameter(Mandatory)][string]$Hex)
    $bits = [Convert]::ToUInt32($Hex, 16)
    $signed = [BitConverter]::ToInt32([BitConverter]::GetBytes($bits), 0)
    return [System.Enum]::ToObject(
        [System.Security.AccessControl.FileSystemRights],
        $signed
    )
}

$cases = [ordered]@{
    Read = [System.Security.AccessControl.FileSystemRights]::Read
    ReadAndExecute = [System.Security.AccessControl.FileSystemRights]::ReadAndExecute
    Synchronize = [System.Security.AccessControl.FileSystemRights]::Synchronize
    WriteData = [System.Security.AccessControl.FileSystemRights]::WriteData
    AppendData = [System.Security.AccessControl.FileSystemRights]::AppendData
    WriteExtendedAttributes = [System.Security.AccessControl.FileSystemRights]::WriteExtendedAttributes
    DeleteSubdirectoriesAndFiles = [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
    WriteAttributes = [System.Security.AccessControl.FileSystemRights]::WriteAttributes
    Delete = [System.Security.AccessControl.FileSystemRights]::Delete
    ChangePermissions = [System.Security.AccessControl.FileSystemRights]::ChangePermissions
    TakeOwnership = [System.Security.AccessControl.FileSystemRights]::TakeOwnership
    Write = [System.Security.AccessControl.FileSystemRights]::Write
    Modify = [System.Security.AccessControl.FileSystemRights]::Modify
    FullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
    GenericRead = New-R156RawFileSystemRights -Hex '80000000'
    GenericExecute = New-R156RawFileSystemRights -Hex '20000000'
    GenericReadExecute = New-R156RawFileSystemRights -Hex 'A0000000'
    GenericWrite = New-R156RawFileSystemRights -Hex '40000000'
    GenericReadWrite = New-R156RawFileSystemRights -Hex 'C0000000'
    GenericAll = New-R156RawFileSystemRights -Hex '10000000'
    HighPrimitive = New-R156RawFileSystemRights -Hex 'A0000002'
}
foreach ($entry in $cases.GetEnumerator()) {
    Write-Output ($entry.Key + '=' + [string](Test-R156MutationCapableFileSystemRights -Rights $entry.Value))
}
"""
        )
        lines = self.run_isolated_powershell(script)
        values = {}
        for line in lines:
            key, value = line.split("=", 1)
            values[key] = value.lower() == "true"
        return values

    def run_protected_root_acl_matrix(self):
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            + self.extracted_functions(
                (
                    "Test-R156ProtectedInstallationRoot",
                    "Test-R156MutationCapableFileSystemRights",
                )
            )
            + r"""
function Test-R156NormalDirectory { return $true }
function Test-R156SamePath { return $false }
function Test-R156PathWithin { return $true }
function Test-R156NoReparseAncestors { return $true }
function Get-Acl { return [pscustomobject]@{ Access = @($script:R156AclRule) } }

function New-R156RawFileSystemRights {
    param([Parameter(Mandatory)][string]$Hex)
    $bits = [Convert]::ToUInt32($Hex, 16)
    $signed = [BitConverter]::ToInt32([BitConverter]::GetBytes($bits), 0)
    return [System.Enum]::ToObject(
        [System.Security.AccessControl.FileSystemRights],
        $signed
    )
}

$cases = @(
    [pscustomobject]@{ Name = 'CreatorOwnerGenericAllInheritOnly'; Identity = 'CREATOR OWNER'; Rights = (New-R156RawFileSystemRights -Hex '10000000'); Propagation = [System.Security.AccessControl.PropagationFlags]::InheritOnly },
    [pscustomobject]@{ Name = 'CreatorOwnerGenericAllNone'; Identity = 'CREATOR OWNER'; Rights = (New-R156RawFileSystemRights -Hex '10000000'); Propagation = [System.Security.AccessControl.PropagationFlags]::None },
    [pscustomobject]@{ Name = 'BuiltinUsersGenericAllInheritOnly'; Identity = 'BUILTIN\Users'; Rights = (New-R156RawFileSystemRights -Hex '10000000'); Propagation = [System.Security.AccessControl.PropagationFlags]::InheritOnly },
    [pscustomobject]@{ Name = 'EveryoneWriteDataNone'; Identity = 'Everyone'; Rights = [System.Security.AccessControl.FileSystemRights]::WriteData; Propagation = [System.Security.AccessControl.PropagationFlags]::None },
    [pscustomobject]@{ Name = 'EveryoneWriteDataInheritOnly'; Identity = 'Everyone'; Rights = [System.Security.AccessControl.FileSystemRights]::WriteData; Propagation = [System.Security.AccessControl.PropagationFlags]::InheritOnly },
    [pscustomobject]@{ Name = 'AuthenticatedUsersGenericWriteInheritOnly'; Identity = 'Authenticated Users'; Rights = (New-R156RawFileSystemRights -Hex '40000000'); Propagation = [System.Security.AccessControl.PropagationFlags]::InheritOnly },
    [pscustomobject]@{ Name = 'CreatorOwnerReadAndExecuteNone'; Identity = 'CREATOR OWNER'; Rights = [System.Security.AccessControl.FileSystemRights]::ReadAndExecute; Propagation = [System.Security.AccessControl.PropagationFlags]::None }
)

foreach ($case in $cases) {
    $script:R156AclRule = [pscustomobject]@{
        AccessControlType = [System.Security.AccessControl.AccessControlType]::Allow
        IdentityReference = $case.Identity
        FileSystemRights = $case.Rights
        PropagationFlags = $case.Propagation
    }
    $accepted = Test-R156ProtectedInstallationRoot -InstallationRoot 'C:\Program Files\EnergyGrid' -ProgramFilesRoot 'C:\Program Files'
    Write-Output ($case.Name + '=' + [string]$accepted)
}
"""
        )
        lines = self.run_isolated_powershell(script)
        values = {}
        for line in lines:
            key, value = line.split("=", 1)
            values[key] = value.lower() == "true"
        return values

    def run_json_matrix(self, cases):
        with tempfile.TemporaryDirectory(prefix="r156_json_cases_") as directory:
            root = Path(directory)
            for name, data in cases.items():
                (root / (name + ".json")).write_bytes(data)
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                "$script:R156MetadataJsonLimitBytes = [int64]65536\n"
                "$script:R156MetadataJsonMaxDepth = [int]32\n"
                "$script:R156MetadataJsonMaxBytesPerRead = [int]8192\n"
                + self.json_functions()
                + r"""
$files = @(Get-ChildItem -LiteralPath $env:R156_JSON_DIR -File | Sort-Object Name)
foreach ($file in $files) {
    $object = Read-R156StrictJsonObject -Path $file.FullName
    if ($null -eq $object) {
        Write-Output ($file.BaseName + ';kind=null')
    }
    else {
        Write-Output ($file.BaseName + ';kind=object;count=' + [string](@($object.PSObject.Properties).Count))
    }
}
"""
            )
            lines = self.run_isolated_powershell(
                script,
                environment={"R156_JSON_DIR": str(root)},
            )
        results = {}
        for line in lines:
            fields = line.split(";")
            name = fields[0]
            result = {key: value for key, value in (field.split("=", 1) for field in fields[1:])}
            if "count" in result:
                result["count"] = int(result["count"])
            results[name] = result
        return results

    @staticmethod
    def config_accepts(entries):
        allowed = {
            "core.repositoryformatversion": "0",
            "core.filemode": None,
            "core.bare": "false",
            "core.logallrefupdates": "true",
            "core.symlinks": None,
            "core.ignorecase": None,
            "remote.origin.url": None,
            "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
            "branch.main.remote": "origin",
            "branch.main.merge": "refs/heads/main",
        }
        canonical = {
            "https://github.com/x-boundaries/automation",
            "https://github.com/x-boundaries/automation.git",
            "git@github.com:x-boundaries/automation",
            "git@github.com:x-boundaries/automation.git",
            "ssh://git@github.com:x-boundaries/automation",
            "ssh://git@github.com:x-boundaries/automation.git",
        }
        if len(entries) != len(allowed):
            return False
        seen = set()
        for key, value in entries:
            if key not in allowed or key in seen:
                return False
            seen.add(key)
            if key == "remote.origin.url":
                if value not in canonical:
                    return False
            elif key in {"core.filemode", "core.symlinks", "core.ignorecase"}:
                if value not in {"true", "false"}:
                    return False
            elif value != allowed[key]:
                return False
        return seen == set(allowed)

    @staticmethod
    def canonical_config_entries():
        return [
            ("core.repositoryformatversion", "0"),
            ("core.filemode", "false"),
            ("core.bare", "false"),
            ("core.logallrefupdates", "true"),
            ("core.symlinks", "false"),
            ("core.ignorecase", "true"),
            ("remote.origin.url", "https://github.com/x-boundaries/automation.git"),
            ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
            ("branch.main.remote", "origin"),
            ("branch.main.merge", "refs/heads/main"),
        ]

    @staticmethod
    def config_bytes(entries):
        return b"".join(
            key.encode("ascii") + b"\n" + value.encode("utf-8") + b"\0"
            for key, value in entries
        )

    def run_production_config_cases(self, cases):
        with tempfile.TemporaryDirectory(prefix="r156_config_") as directory:
            root = Path(directory)
            names = {}
            for index, (name, payload) in enumerate(cases.items()):
                fixture_name = f"case_{index:03d}.bin"
                (root / fixture_name).write_bytes(payload)
                names[fixture_name] = name
            script = (
                "$script:R156CanonicalOrigins = @("
                "'https://github.com/x-boundaries/automation',"
                "'https://github.com/x-boundaries/automation.git',"
                "'git@github.com:x-boundaries/automation',"
                "'git@github.com:x-boundaries/automation.git',"
                "'ssh://git@github.com/x-boundaries/automation',"
                "'ssh://git@github.com:x-boundaries/automation.git')\n"
                + self.config_functions()
                + r"""
$files = @(Get-ChildItem -LiteralPath $env:R156_CONFIG_DIR -File | Sort-Object Name)
foreach ($file in $files) {
    $entries = Convert-R156ConfigBytes -Bytes ([System.IO.File]::ReadAllBytes($file.FullName))
    $parsed = $null -ne $entries
    $admission = $null
    if ($parsed) {
        $admission = Test-R156ConfigAdmission -Entries ([object[]]$entries)
    }
    $admitted = $null -ne $admission -and [bool]$admission.Pass
    $worktree = $admitted -and [bool]$admission.WorktreeConfigEnabled
    Write-Output ($file.Name + ';parsed=' + $parsed + ';admitted=' + $admitted + ';worktree=' + $worktree)
}
"""
            )
            lines = self.run_isolated_powershell(
                script,
                environment={"R156_CONFIG_DIR": str(root)},
            )
        results = {}
        for line in lines:
            fixture_name, *fields = line.split(";")
            results[names[fixture_name]] = {
                key: value.lower() == "true"
                for key, value in (field.split("=", 1) for field in fields)
            }
        return results, "\n".join(lines)

    def run_worktree_config_matrix(self):
        with tempfile.TemporaryDirectory(prefix="r156_worktree_config_") as directory:
            script = (
                self.worktree_config_functions()
                + r"""
$root = [string]$env:R156_WORKTREE_ROOT
$absentParent = Join-Path $root 'absent'
$fileParent = Join-Path $root 'file'
$directoryParent = Join-Path $root 'directory'
$reparseParent = Join-Path $root 'reparse'
$target = Join-Path $root 'target'
foreach ($path in @($absentParent, $fileParent, $directoryParent, $reparseParent, $target)) {
    [void][System.IO.Directory]::CreateDirectory($path)
}
$absentPath = Join-Path $absentParent 'config.worktree'
$filePath = Join-Path $fileParent 'config.worktree'
$directoryPath = Join-Path $directoryParent 'config.worktree'
$reparsePath = Join-Path $reparseParent 'config.worktree'
$indeterminatePath = Join-Path (Join-Path $root 'missing-parent') 'config.worktree'
[System.IO.File]::WriteAllText($filePath, 'x')
[void][System.IO.Directory]::CreateDirectory($directoryPath)
$junction = New-Item -ItemType Junction -Path $reparsePath -Target $target -ErrorAction Stop
$values = [ordered]@{
    absent = Test-R156WorktreeConfigSurfaceAbsent -Path $absentPath
    file = Test-R156WorktreeConfigSurfaceAbsent -Path $filePath
    directory = Test-R156WorktreeConfigSurfaceAbsent -Path $directoryPath
    reparse = Test-R156WorktreeConfigSurfaceAbsent -Path $reparsePath
    indeterminate = Test-R156WorktreeConfigSurfaceAbsent -Path $indeterminatePath
    disabled_preserves = Test-R156WorktreeConfigFence -Enabled $false -AbsentBefore $false -AbsentAfter $false
    enabled_absent = Test-R156WorktreeConfigFence -Enabled $true -AbsentBefore $true -AbsentAfter $true
    enabled_file = Test-R156WorktreeConfigFence -Enabled $true -AbsentBefore $false -AbsentAfter $false
    enabled_directory = Test-R156WorktreeConfigFence -Enabled $true -AbsentBefore $false -AbsentAfter $false
    enabled_reparse = Test-R156WorktreeConfigFence -Enabled $true -AbsentBefore $false -AbsentAfter $false
    enabled_indeterminate = Test-R156WorktreeConfigFence -Enabled $true -AbsentBefore $false -AbsentAfter $false
    enabled_appeared_after = Test-R156WorktreeConfigFence -Enabled $true -AbsentBefore $true -AbsentAfter $false
}
foreach ($item in $values.GetEnumerator()) {
    Write-Output ($item.Key + '=' + [string]$item.Value)
}
"""
            )
            lines = self.run_isolated_powershell(
                script,
                environment={"R156_WORKTREE_ROOT": directory},
            )
        return {
            key: value.lower() == "true"
            for key, value in (line.split("=", 1) for line in lines)
        }

    @staticmethod
    def canonical_byte_contract(committed):
        if (
            not committed
            or b"\xef\xbb\xbf" in committed
            or b"\r" in committed
            or not committed.endswith(b"\n")
            or committed.endswith(b"\n\n")
        ):
            return False
        try:
            committed.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        if any(
            (byte < 0x20 and byte not in (0x09, 0x0A)) or byte == 0x7F
            for byte in committed
        ):
            return False
        return True

    @staticmethod
    def working_byte_contract(working):
        if not working or b"\xef\xbb\xbf" in working:
            return None
        try:
            working.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None

        has_crlf = False
        has_bare_lf = False
        for index, byte in enumerate(working):
            if byte == 0x0D:
                if index + 1 >= len(working) or working[index + 1] != 0x0A:
                    return None
                has_crlf = True
            elif byte == 0x0A:
                if index == 0 or working[index - 1] != 0x0D:
                    has_bare_lf = True
            elif (byte < 0x20 and byte != 0x09) or byte == 0x7F:
                return None
        if has_crlf and has_bare_lf:
            return None
        if not working.endswith(b"\n"):
            return None
        if has_crlf and not working.endswith(b"\r\n"):
            return None

        normalized = working.replace(b"\r\n", b"\n")
        if normalized.endswith(b"\n\n"):
            return None
        return normalized

    @classmethod
    def normalize_contract(cls, working, committed):
        if not cls.canonical_byte_contract(committed):
            return False
        normalized = cls.working_byte_contract(working)
        return normalized is not None and normalized == committed

    @staticmethod
    def git_object_sha1(object_type, raw):
        header = f"{object_type} {len(raw)}\0".encode("ascii")
        return hashlib.sha1(header + raw).hexdigest()

    @staticmethod
    def commit_headers(tree, parent):
        return (
            f"tree {tree}\n"
            f"parent {parent}\n"
            "author WJ <10020253+weijunswj@users.noreply.github.com> 0 +0000\n"
            "committer WJ <10020253+weijunswj@users.noreply.github.com> 0 +0000\n"
            "\n"
            "synthetic\n"
        ).encode("utf-8")

    def test_signed_gpgsig_one_space_continuation_is_accepted(self):
        tree = "a" * 40
        parent = "b" * 40
        raw = (
            f"tree {tree}\n"
            f"parent {parent}\n"
            "author WJ <10020253+weijunswj@users.noreply.github.com> 0 +0000\n"
            "committer WJ <10020253+weijunswj@users.noreply.github.com> 0 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n"
            " \n"
            " -----END PGP SIGNATURE-----\n"
            "\n"
            "synthetic\n"
        ).encode("utf-8")
        head = self.git_object_sha1("commit", raw)
        functions = self.extracted_functions(
            (
                "Convert-R156BytesToHex",
                "Get-R156Sha1ForGitObject",
                "Convert-R156Utf8Bytes",
                "Get-R156CommitTreeParentProof",
            )
        )
        script = (
            "$script:R156Checkout = 'C:\\XB\\automation'\n"
            "function Invoke-R156LocalGitRead {\n"
            "    return [pscustomobject]@{\n"
            "        Success = $true\n"
            "        StdoutBytes = [Convert]::FromBase64String($env:R156_RAW_COMMIT)\n"
            "    }\n"
            "}\n"
            + functions
            + "\n$result = Get-R156CommitTreeParentProof "
            + f"-ExpectedHeadValue '{head}' "
            + f"-ExpectedTreeValue '{tree}' "
            + f"-ExpectedParentValue '{parent}'\n"
            + "[pscustomobject]@{ accepted = ($null -ne $result) } "
            + "| ConvertTo-Json -Compress\n"
        )
        lines = self.run_isolated_powershell(
            script,
            environment={
                "R156_RAW_COMMIT": __import__("base64").b64encode(raw).decode("ascii")
            },
        )
        self.assertEqual(json.loads(lines[-1]), {"accepted": True})

    @staticmethod
    def remote_record(head):
        return f"{head}\trefs/heads/main\n".encode("ascii")

    def preimage_production_source(self, manifest_source=None):
        """Extract the production preimage functions without loading the helper."""
        function_ranges = (
            ("function Test-R156CommitText", "function Convert-R156BytesToHex"),
            ("function Convert-R156BytesToHex", "function Get-R156Sha256ForBytes"),
            ("function Get-R156Sha256ForBytes", "function Get-R156Properties"),
            ("function Convert-R156Utf8Bytes", "function Test-R156LockedSourcePath"),
            ("function Get-R156FullPath", "function Test-R156SamePath"),
            ("function Test-R156SamePath", "function Test-R156PathWithin"),
            ("function Test-R156PathWithin", "function Test-R156NormalDirectory"),
            ("function Test-R156CanonicalOrigin", "function Test-R156ClassAClassification"),
            ("function Test-R156RepositoryIdentity", "function Test-R156RepositoryFence"),
            ("function Test-R156RepositoryFence", "function Get-R156SingleGitLine"),
            ("function Test-R156ClassAClassification", "function Get-R156ClassAPaths"),
            ("function Read-R156ManifestState", "function Get-R156DirectChildren"),
            ("function Assert-R156PostProof", "$tokenContext = $null"),
        )
        blocks = []
        for signature, next_signature in function_ranges:
            if signature == "function Read-R156ManifestState" and manifest_source is not None:
                block = manifest_source
            else:
                block = self.source_function(signature, next_signature)
            blocks.append(block.rstrip())
        return "\n\n".join(blocks)

    def run_preimage_boundary_case(
        self,
        *,
        expected_head=CURRENT_HEAD,
        expected_tree=CURRENT_TREE,
        expected_parent=CURRENT_PARENT,
        expected_installed=ACCEPTED_STALE_ADMISSION,
        actual_head=CURRENT_HEAD,
        actual_tree=CURRENT_TREE,
        actual_parent=CURRENT_PARENT,
        parent_count=1,
        manifest_sequence=None,
        post_admission=CURRENT_HEAD,
        _manifest_source=None,
    ):
        """Exercise extracted production gates in a synthetic, side-effect-free fixture."""
        manifests = list(manifest_sequence or [expected_installed] * 3)
        self.assertEqual(len(manifests), 3)
        with tempfile.TemporaryDirectory(prefix="r156_preimage_fixture_") as directory:
            fixture_root = Path(directory)
            (fixture_root / "launcher.ps1").write_bytes(b"# synthetic launcher\n")
            (fixture_root / "launcher_lib.ps1").write_bytes(b"# synthetic library\n")
            script = (
                self.preimage_production_source(_manifest_source)
                + r'''
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:R156ExpectedBranch = 'main'
$script:R156CanonicalOrigins = @('https://github.com/x-boundaries/automation')
$script:R156ManifestFileName = 'installation_manifest.json'
$script:R156PackageNames = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
$script:R156ManifestNames = @('launcher.ps1', 'launcher_lib.ps1')
$script:R156ManifestSchema = 'eg_launcher_installation_manifest/v1'
$script:R156ProgramData = 'C:\Synthetic\ProgramData'
$script:R156ExpectedHeadAtExecution = ''
$script:R156ExpectedTreeAtExecution = ''
$script:R156ExpectedParentAtExecution = ''
$script:R156ExpectedInstalledAdmissionAtExecution = ''
$script:R156PostManifest = 'NOT_RUN'
$script:R156PostPackageManifest = 'NOT_RUN'
$script:R156CanonicalEquivalence = 'NOT_RUN'
$script:R156LauncherClassification = 'NOT_RUN'
$script:R156RepositoryContinuity = 'NOT_RUN'
$script:R156SyntheticRoot = [string]$env:R156_SYNTH_ROOT
$script:R156SyntheticCandidate = $script:R156SyntheticRoot
$script:R156SyntheticManifestAdmissions = [string[]]@(
    [string]$env:R156_SYNTH_MANIFEST_1,
    [string]$env:R156_SYNTH_MANIFEST_2,
    [string]$env:R156_SYNTH_MANIFEST_3
)
$script:R156SyntheticPostAdmission = [string]$env:R156_SYNTH_POST_ADMISSION
$script:R156SyntheticPhase = 'PRE'
$script:R156SyntheticPreReadIndex = 0
$script:R156SyntheticPreReadCount = 0
$script:R156SyntheticPostReadCount = 0
$script:R156SyntheticLastAdmission = ''
$script:R156SyntheticRealInstallerInvocations = 0
$script:R156SyntheticSupportRef = ''
$script:R156SyntheticNormalFiles = [string[]]@(
    'C:\XB\automation\.git\config',
    'C:\XB\automation\.git\index',
    (Join-Path $script:R156SyntheticCandidate 'launcher.ps1'),
    (Join-Path $script:R156SyntheticCandidate 'launcher_lib.ps1'),
    (Join-Path $script:R156SyntheticCandidate 'installation_manifest.json')
)

# These stubs are environmental seams only. The admission comparison, repository
# identity/fence, and post-proof bodies above are extracted from production.
function Test-R156NormalDirectory {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)
    return ([string]$Path -ceq 'C:\XB\automation\.git')
}

function Test-R156NormalFile {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)
    return (@($script:R156SyntheticNormalFiles) -contains [string]$Path)
}

function Test-R156NoReparseAncestors {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)
    return $true
}

function Get-EgLauncherRootClassification {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath)
    return [pscustomobject]@{
        Pass = $true
        ClassA = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
        ClassB = @()
        ClassC = @()
        MissingMembers = @()
    }
}

function Get-R156ClassAPaths {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot)
    return [string[]]@(
        (Join-Path $LauncherRoot 'launcher.ps1'),
        (Join-Path $LauncherRoot 'launcher_lib.ps1'),
        (Join-Path $LauncherRoot 'installation_manifest.json')
    )
}

function Read-R156StrictJsonObject {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)
    $expectedPath = Join-Path $script:R156SyntheticCandidate $script:R156ManifestFileName
    if (-not [string]::Equals($Path, $expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $null
    }
    if ($script:R156SyntheticPhase -ceq 'PRE') {
        $index = [int]$script:R156SyntheticPreReadIndex
        if ($index -ge $script:R156SyntheticManifestAdmissions.Count) {
            return $null
        }
        $admission = [string]$script:R156SyntheticManifestAdmissions[$index]
        $script:R156SyntheticPreReadIndex = $index + 1
        $script:R156SyntheticPreReadCount++
    }
    else {
        $admission = [string]$script:R156SyntheticPostAdmission
        $script:R156SyntheticPostReadCount++
    }
    $script:R156SyntheticLastAdmission = $admission
    return [pscustomobject]@{
        schema_version = $script:R156ManifestSchema
        admission_commit = $admission
    }
}

function Test-EgInstallationManifestShape {
    param([Parameter(Mandatory)]$ManifestObject)
    return [pscustomobject]@{ Pass = $true }
}

function Compare-EgInstalledPackageToManifest {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath)
    return [pscustomobject]@{ Pass = $true }
}

function Test-R156PowerShellParse {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path,
        [Parameter(Mandatory)][byte[]]$Bytes
    )
    return $true
}

function Stop-R156Gate {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SupportRef)
    $script:R156SyntheticSupportRef = $SupportRef
    throw 'synthetic bounded gate stop'
}

function Read-R156Locator {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LocatorPath)
    return [pscustomobject]@{
        RuntimeRoot = 'C:\Synthetic\Runtime'
        Metadata = 'stable'
    }
}

function Test-R156LocatorContinuity {
    param([Parameter(Mandatory)]$Before, [Parameter(Mandatory)]$After)
    return $true
}

function Resolve-R156Topology {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RuntimeRoot,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot
    )
    return [pscustomobject]@{ Candidate = $script:R156SyntheticCandidate }
}

function Test-R156TopologyContinuity {
    param([Parameter(Mandatory)]$Before, [Parameter(Mandatory)]$After)
    return $true
}

function Test-R156PrivateBindingsOutsideCheckout {
    param(
        [Parameter(Mandatory)]$Topology,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot
    )
    return $true
}

function Test-R156LocatorSecurity {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ProgramData,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LocatorPath,
        [Parameter(Mandatory)]$TokenContext
    )
    return $true
}

function Test-R156LauncherSecurity {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRoot,
        [Parameter(Mandatory)]$ClassAPath,
        [Parameter(Mandatory)]$TokenContext
    )
    return $true
}

$script:R156SyntheticRepositoryState = [pscustomobject]@{
    Branch = 'main'
    Head = ([string]$env:R156_SYNTH_ACTUAL_HEAD).ToLowerInvariant()
    Tree = ([string]$env:R156_SYNTH_ACTUAL_TREE).ToLowerInvariant()
    Parent = ([string]$env:R156_SYNTH_ACTUAL_PARENT).ToLowerInvariant()
    ParentCount = [int]$env:R156_SYNTH_PARENT_COUNT
    TopLevel = 'C:\XB\automation'
    InsideWorkTree = 'true'
    GitDirectory = 'C:\XB\automation\.git'
    CommonDirectory = 'C:\XB\automation\.git'
    ConfigAdmission = [pscustomobject]@{
        Pass = $true
        InstalledAdmission = [string]$env:R156_SYNTH_INSTALLED_PROVENANCE
    }
    Origin = [string[]]@('https://github.com/x-boundaries/automation')
    Clean = $true
    ReadOk = $true
}

function Get-R156RepositoryState {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRoot)
    return $script:R156SyntheticRepositoryState
}

function Invoke-SyntheticR156Boundary {
    param(
        [Parameter(Mandatory)][string]$ExpectedHead,
        [Parameter(Mandatory)][string]$ExpectedTree,
        [Parameter(Mandatory)][string]$ExpectedParent,
        [Parameter(Mandatory)][string]$ExpectedInstalledAdmission,
        [Parameter(Mandatory)][string]$PostAdmission
    )

    $result = [ordered]@{
        admission_valid = $false
        repository_fence = $false
        repository_fence_exercised = $false
        expected_parent_state = ''
        expected_installed_state = ''
        stale_reads = 0
        production_manifest_reads = 0
        manifest_guard_rejection_observed = $false
        pre_dispatch_passed = $false
        real_installer_invocations = 0
        synthetic_dispatch_boundary = $false
        transport_admission = ''
        post_proof_exercised = $false
        post_proof_current = $false
        post_proof_stale = $false
        post_proof_terminal = 'NOT_RUN'
        terminal = 'BLOCKED'
    }
    if (-not (Test-R156CommitText -Value $ExpectedHead) -or
        -not (Test-R156CommitText -Value $ExpectedTree) -or
        -not (Test-R156CommitText -Value $ExpectedParent) -or
        -not (Test-R156CommitText -Value $ExpectedInstalledAdmission)) {
        return [pscustomobject]$result
    }

    $script:R156ExpectedHeadAtExecution = $ExpectedHead.ToLowerInvariant()
    $script:R156ExpectedTreeAtExecution = $ExpectedTree.ToLowerInvariant()
    $script:R156ExpectedParentAtExecution = $ExpectedParent.ToLowerInvariant()
    $script:R156ExpectedInstalledAdmissionAtExecution = $ExpectedInstalledAdmission.ToLowerInvariant()
    $result.admission_valid = $true
    $result.expected_parent_state = $script:R156ExpectedParentAtExecution
    $result.expected_installed_state = $script:R156ExpectedInstalledAdmissionAtExecution

    $result.repository_fence_exercised = $true
    $repositoryFence = Test-R156RepositoryFence `
        -State $script:R156SyntheticRepositoryState `
        -RepositoryRoot 'C:\XB\automation' `
        -ExpectedHeadValue $script:R156ExpectedHeadAtExecution `
        -ExpectedTreeValue $script:R156ExpectedTreeAtExecution `
        -ExpectedParentValue $script:R156ExpectedParentAtExecution
    $result.repository_fence = [bool]$repositoryFence
    if (-not $result.repository_fence) {
        return [pscustomobject]$result
    }

    $canonicalSources = [ordered]@{}
    foreach ($name in @('launcher.ps1', 'launcher_lib.ps1')) {
        $path = Join-Path $script:R156SyntheticCandidate $name
        $bytes = [System.IO.File]::ReadAllBytes($path)
        $canonicalSources[$name] = [pscustomobject]@{
            CommittedBytes = [byte[]]$bytes
            Sha256 = (Get-R156Sha256ForBytes -Bytes $bytes)
            ByteLength = [int64]$bytes.Length
        }
    }

    $script:R156SyntheticPhase = 'PRE'
    for ($index = 0; $index -lt 3; $index++) {
        $manifest = Read-R156ManifestState `
            -LauncherRoot $script:R156SyntheticCandidate `
            -ExpectedAdmission $script:R156ExpectedInstalledAdmissionAtExecution `
            -CanonicalSources $canonicalSources
        if ($null -eq $manifest) {
            $result.stale_reads = [int]$script:R156SyntheticPreReadCount
            $result.production_manifest_reads = [int]$script:R156SyntheticPreReadCount
            $result.manifest_guard_rejection_observed = (
                [string]$script:R156SyntheticLastAdmission -cne
                $script:R156ExpectedInstalledAdmissionAtExecution
            )
            return [pscustomobject]$result
        }
    }
    $result.stale_reads = [int]$script:R156SyntheticPreReadCount
    $result.production_manifest_reads = [int]$script:R156SyntheticPreReadCount
    $result.pre_dispatch_passed = $true
    $script:R156SyntheticRealInstallerInvocations = 1
    $result.real_installer_invocations = [int]$script:R156SyntheticRealInstallerInvocations
    $result.synthetic_dispatch_boundary = $true
    $result.transport_admission = $script:R156ExpectedHeadAtExecution

    $script:R156SyntheticPhase = 'POST'
    $result.post_proof_exercised = $true
    $postOutcome = 'BLOCKED'
    try {
        Assert-R156PostProof `
            -LocatorPath (Join-Path $script:R156SyntheticRoot 'runtime_locator.json') `
            -LocatorBefore ([pscustomobject]@{ RuntimeRoot = 'C:\Synthetic\Runtime'; Metadata = 'stable' }) `
            -TopologyBefore ([pscustomobject]@{ RuntimeRoot = 'C:\Synthetic\Runtime' }) `
            -CheckoutRoot 'C:\XB\automation' `
            -ExpectedHeadValue $script:R156ExpectedHeadAtExecution `
            -CanonicalSources $canonicalSources `
            -TokenContext ([pscustomobject]@{})
        $postOutcome = 'PASS'
    }
    catch {
        $postOutcome = 'BLOCKED'
    }
    $result.post_proof_terminal = $postOutcome
    $result.post_proof_current = ($postOutcome -ceq 'PASS')
    $result.post_proof_stale = ([string]$PostAdmission -ceq $script:R156ExpectedInstalledAdmissionAtExecution)
    if ($result.post_proof_current) {
        $result.terminal = 'PASS'
    }
    else {
        $result.terminal = 'POST_PROOF_FAILED'
    }
    return [pscustomobject]$result
}

$result = Invoke-SyntheticR156Boundary `
    -ExpectedHead ([string]$env:R156_SYNTH_EXPECTED_HEAD) `
    -ExpectedTree ([string]$env:R156_SYNTH_EXPECTED_TREE) `
    -ExpectedParent ([string]$env:R156_SYNTH_EXPECTED_PARENT) `
    -ExpectedInstalledAdmission ([string]$env:R156_SYNTH_EXPECTED_INSTALLED) `
    -PostAdmission ([string]$env:R156_SYNTH_POST_ADMISSION)
$result | ConvertTo-Json -Compress
'''
            )
            environment = {
                "R156_SYNTH_ROOT": str(fixture_root),
                "R156_SYNTH_EXPECTED_HEAD": expected_head,
                "R156_SYNTH_EXPECTED_TREE": expected_tree,
                "R156_SYNTH_EXPECTED_PARENT": expected_parent,
                "R156_SYNTH_EXPECTED_INSTALLED": expected_installed,
                "R156_SYNTH_INSTALLED_PROVENANCE": expected_installed,
                "R156_SYNTH_ACTUAL_HEAD": actual_head,
                "R156_SYNTH_ACTUAL_TREE": actual_tree,
                "R156_SYNTH_ACTUAL_PARENT": actual_parent,
                "R156_SYNTH_PARENT_COUNT": str(parent_count),
                "R156_SYNTH_MANIFEST_1": manifests[0],
                "R156_SYNTH_MANIFEST_2": manifests[1],
                "R156_SYNTH_MANIFEST_3": manifests[2],
                "R156_SYNTH_POST_ADMISSION": post_admission,
            }
            lines = self.run_isolated_powershell(script, environment=environment)
        self.assertEqual(len(lines), 1, lines)
        return json.loads(lines[0])

    def run_manifest_equivalence_case(
        self,
        *,
        launcher_installed=b"# launcher\r\nWrite-Output 'launcher'\r\n",
        library_installed=b"# library\r\nfunction Invoke-Library { }\r\n",
        launcher_canonical=b"# launcher\nWrite-Output 'launcher'\n",
        library_canonical=b"# library\nfunction Invoke-Library { }\n",
        manifest_schema="eg_launcher_installation_manifest/v1",
        manifest_admission=ACCEPTED_STALE_ADMISSION,
        expected_admission=ACCEPTED_STALE_ADMISSION,
        manifest_shape=True,
        package_pass=True,
        non_normal_name="",
        parse_fail_name="",
    ):
        """Exercise the production manifest/canonical boundary without live surfaces."""
        functions = self.extracted_functions(
            (
                "Convert-R156Utf8Bytes",
                "Test-R156ByteArraysEqual",
                "Convert-R156WorkingBytesToLf",
                "Test-R156WorkingByteContract",
                "Test-R156ClassAClassification",
                "Get-R156ClassAPaths",
                "Read-R156ManifestState",
            )
        )
        with tempfile.TemporaryDirectory(prefix="r156_manifest_fixture_") as directory:
            root = Path(directory)
            (root / "launcher.ps1").write_bytes(launcher_installed)
            (root / "launcher_lib.ps1").write_bytes(library_installed)
            (root / "installation_manifest.json").write_bytes(b"{}\n")
            script = (
                functions
                + r'''
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:R156ManifestFileName = 'installation_manifest.json'
$script:R156PackageNames = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
$script:R156ManifestNames = @('launcher.ps1', 'launcher_lib.ps1')
$script:R156ManifestSchema = 'eg_launcher_installation_manifest/v1'
$script:R156PackageCalls = 0
$script:R156RawDigestMatch = $false

function Test-R156NormalFile {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)
    if ([string]::IsNullOrEmpty([string]$env:R156_NON_NORMAL_NAME)) { return $true }
    return ([System.IO.Path]::GetFileName($Path) -cne [string]$env:R156_NON_NORMAL_NAME)
}

function Get-EgLauncherRootClassification {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath)
    return [pscustomobject]@{
        Pass = $true
        ClassA = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
        ClassB = @()
        ClassC = @()
        MissingMembers = @()
    }
}

function Read-R156StrictJsonObject {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)
    return [pscustomobject]@{
        schema_version = [string]$env:R156_MANIFEST_SCHEMA
        admission_commit = [string]$env:R156_MANIFEST_ADMISSION
    }
}

function Test-EgInstallationManifestShape {
    param([Parameter(Mandatory)]$ManifestObject)
    return [pscustomobject]@{ Pass = [bool]::Parse([string]$env:R156_MANIFEST_SHAPE) }
}

function Get-SyntheticSha256 {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Compare-EgInstalledPackageToManifest {
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$LauncherRootPath)
    $script:R156PackageCalls++
    $launcherRaw = [System.IO.File]::ReadAllBytes((Join-Path $LauncherRootPath 'launcher.ps1'))
    $libraryRaw = [System.IO.File]::ReadAllBytes((Join-Path $LauncherRootPath 'launcher_lib.ps1'))
    $script:R156RawDigestMatch = (
        (Get-SyntheticSha256 -Bytes $launcherRaw) -ceq [string]$env:R156_LAUNCHER_RAW_SHA256 -and
        (Get-SyntheticSha256 -Bytes $libraryRaw) -ceq [string]$env:R156_LIBRARY_RAW_SHA256
    )
    return [pscustomobject]@{
        Pass = ([bool]::Parse([string]$env:R156_PACKAGE_PASS) -and $script:R156RawDigestMatch)
    }
}

function Test-R156PowerShellParse {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path,
        [Parameter(Mandatory)][byte[]]$Bytes
    )
    if ([string]::IsNullOrEmpty([string]$env:R156_PARSE_FAIL_NAME)) { return $true }
    return ([System.IO.Path]::GetFileName($Path) -cne [string]$env:R156_PARSE_FAIL_NAME)
}

$launcherCanonical = [Convert]::FromBase64String([string]$env:R156_LAUNCHER_CANONICAL)
$libraryCanonical = [Convert]::FromBase64String([string]$env:R156_LIBRARY_CANONICAL)
$canonicalSources = [ordered]@{
    'launcher.ps1' = [pscustomobject]@{ CommittedBytes = [byte[]]$launcherCanonical }
    'launcher_lib.ps1' = [pscustomobject]@{ CommittedBytes = [byte[]]$libraryCanonical }
}
$state = Read-R156ManifestState `
    -LauncherRoot ([string]$env:R156_FIXTURE_ROOT) `
    -ExpectedAdmission ([string]$env:R156_EXPECTED_ADMISSION) `
    -CanonicalSources $canonicalSources
[pscustomobject]@{
    accepted = ($null -ne $state)
    package_calls = [int]$script:R156PackageCalls
    raw_digest_match = [bool]$script:R156RawDigestMatch
} | ConvertTo-Json -Compress
'''
            )
            encode = lambda value: __import__("base64").b64encode(value).decode("ascii")
            environment = {
                "R156_FIXTURE_ROOT": str(root),
                "R156_LAUNCHER_CANONICAL": encode(launcher_canonical),
                "R156_LIBRARY_CANONICAL": encode(library_canonical),
                "R156_LAUNCHER_RAW_SHA256": hashlib.sha256(launcher_installed).hexdigest(),
                "R156_LIBRARY_RAW_SHA256": hashlib.sha256(library_installed).hexdigest(),
                "R156_MANIFEST_SCHEMA": manifest_schema,
                "R156_MANIFEST_ADMISSION": manifest_admission,
                "R156_EXPECTED_ADMISSION": expected_admission,
                "R156_MANIFEST_SHAPE": str(manifest_shape),
                "R156_PACKAGE_PASS": str(package_pass),
                "R156_NON_NORMAL_NAME": non_normal_name,
                "R156_PARSE_FAIL_NAME": parse_fail_name,
            }
            lines = self.run_isolated_powershell(script, environment=environment)
        self.assertEqual(len(lines), 1, lines)
        return json.loads(lines[0])

    def read_head_blob(self, relative_path):
        environment = os.environ.copy()
        environment["GIT_NO_REPLACE_OBJECTS"] = "1"
        environment["GIT_NO_LAZY_FETCH"] = "1"
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", f"HEAD:{relative_path}"],
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            resolved.returncode,
            0,
            f"HEAD blob resolution failed for {relative_path}",
        )
        resolved_text = resolved.stdout.decode("ascii", errors="strict")
        match = re.fullmatch(r"([0-9a-f]{40})\r?\n", resolved_text)
        self.assertIsNotNone(match, f"invalid HEAD blob identity for {relative_path}")
        object_id = match.group(1)

        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            blob.returncode,
            0,
            f"HEAD blob read failed for {relative_path}",
        )
        return object_id, blob.stdout

    def test_helper_parses_under_windows_powershell_51_when_available(self):
        powershell = shutil.which("powershell.exe")
        if os.name != "nt" or powershell is None:
            self.skipTest("Windows PowerShell 5.1 parser is not available on this host")

        command = (
            "$tokens=$null; $errors=$null; "
            "if ([string]$PSVersionTable.PSEdition -cne 'Desktop' -or "
            "[int]$PSVersionTable.PSVersion.Major -ne 5 -or "
            "[int]$PSVersionTable.PSVersion.Minor -ne 1) { exit 2 }; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$env:R156_HELPER_PARSE_PATH,[ref]$tokens,[ref]$errors) | Out-Null; "
            "if (@($errors).Count -ne 0) { "
            "foreach ($parseError in @($errors) | Select-Object -First 8) { "
            "Write-Output (('{0}:{1}:{2}' -f "
            "[int]$parseError.Extent.StartLineNumber, "
            "[int]$parseError.Extent.StartColumnNumber, "
            "[string]$parseError.ErrorId)) }; exit 1 }; exit 0"
        )
        environment = os.environ.copy()
        environment["R156_HELPER_PARSE_PATH"] = str(HELPER)
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raw_diagnostics = result.stdout[:2048].decode("ascii", errors="ignore")
            diagnostics = [
                line
                for line in raw_diagnostics.splitlines()
                if re.fullmatch(r"[0-9]+:[0-9]+:[A-Za-z0-9_]+", line)
            ][:8]
            bounded = ", ".join(diagnostics) if diagnostics else "none"
            self.fail(
                "Windows PowerShell 5.1 parse failed; "
                f"bounded diagnostics: {bounded}"
            )

    def test_stdin_bootstrap_is_frozen_and_safely_below_command_line_limit(self):
        expected = """Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$source = [Console]::In.ReadToEnd()
if ([string]::IsNullOrEmpty($source)) {
    throw 'missing child script'
}
$child = [ScriptBlock]::Create($source)
& $child
exit 0"""
        self.assertEqual(self.bootstrap, expected)
        encoded = base64.b64encode(self.bootstrap.encode("utf-16-le")).decode("ascii")
        command_line = subprocess.list2cmdline(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ]
        )
        self.assertLess(len(command_line), 4096)
        self.assertLess(len(command_line), 32767)

    def test_bootstrap_waits_for_eof_before_child_execution(self):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell 5.1 is not available on this host")
        encoded = base64.b64encode(self.bootstrap.encode("utf-16-le")).decode("ascii")
        with tempfile.TemporaryDirectory(prefix="r156_eof_") as directory:
            marker = Path(directory) / "started.txt"
            env = os.environ.copy()
            env["R156_EOF_MARKER"] = str(marker)
            child = (
                "[IO.File]::WriteAllText($env:R156_EOF_MARKER, 'started')\r\n"
                "[Console]::Out.WriteLine('after-eof')\r\n"
            )
            process = subprocess.Popen(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                process.stdin.write(child)
                process.stdin.flush()
                time.sleep(0.5)
                self.assertFalse(marker.exists())
                process.stdin.close()
                process.stdin = None
                stdout, stderr = process.communicate(timeout=30)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "after-eof")
            self.assertTrue(marker.is_file())

    def test_bootstrap_normalizes_only_normal_return_and_preserves_failures(self):
        nested_exit = """
$inner = [PowerShell]::Create()
try {
    [void]$inner.AddScript('exit 23')
    [void]$inner.Invoke()
}
finally {
    $inner.Dispose()
}
[Console]::Out.WriteLine('nested-returned')
""".replace("\n", "\r\n")
        normal = self.run_bootstrap(nested_exit)
        self.assertEqual(normal.returncode, 0, normal.stderr)
        self.assertEqual(normal.stdout.strip(), "nested-returned")

        for name, source in (
            ("empty", ""),
            ("parse", "if ("),
            ("throw", "throw 'synthetic uncaught child failure'"),
        ):
            with self.subTest(name=name):
                failed = self.run_bootstrap(source)
                self.assertNotEqual(failed.returncode, 0)

    def test_child_protocol_rejects_missing_malformed_duplicate_and_extra_packets(self):
        functions = self.extracted_functions(
            (
                "Get-R156Properties",
                "Test-R156ExactNameMultiset",
                "Test-R156ExactPropertySet",
                "Get-R156ChildProtocol",
            )
        )
        packet = (
            'R156|PACKET|{"protocol":"xb-r156-child/v1",'
            '"mode":"VALIDATE_ONLY","canonical_valid":true,'
            '"validation_status":"PASS","validation_current":"FAIL",'
            '"real_status":"","real_backups_remaining":-1,'
            '"real_success_shape":false}'
        )
        script = (
            functions
            + "\n$packet = '"
            + packet
            + "'\n"
            + r"""
$cases = [ordered]@{
    valid = "R156|BEGIN|VALIDATE_ONLY`r`n$packet`r`nR156|END|VALIDATE_ONLY`r`n"
    missing = "R156|BEGIN|VALIDATE_ONLY`r`nR156|END|VALIDATE_ONLY`r`n"
    malformed = "R156|BEGIN|VALIDATE_ONLY`r`nR156|PACKET|{`r`nR156|END|VALIDATE_ONLY`r`n"
    duplicate = "R156|BEGIN|VALIDATE_ONLY`r`n$packet`r`n$packet`r`nR156|END|VALIDATE_ONLY`r`n"
    extra = "R156|BEGIN|VALIDATE_ONLY`r`n$packet`r`nR156|END|VALIDATE_ONLY`r`nEXTRA`r`n"
}
$result = [ordered]@{}
foreach ($entry in $cases.GetEnumerator()) {
    $result[$entry.Key] = $null -ne (Get-R156ChildProtocol -Mode 'VALIDATE_ONLY' -Stdout $entry.Value)
}
$result | ConvertTo-Json -Compress
"""
        )
        lines = self.run_isolated_powershell(script)
        self.assertEqual(len(lines), 1, lines)
        result = json.loads(lines[0])
        self.assertTrue(result["valid"])
        for name in ("missing", "malformed", "duplicate", "extra"):
            self.assertFalse(result[name], name)

    def test_full_crlf_validate_only_normalizes_exit_and_stderr_rejects_privately(self):
        packet = (
            '{"protocol":"xb-r156-child/v1","mode":"VALIDATE_ONLY",'
            '"canonical_valid":true,"validation_status":"PASS",'
            '"validation_current":"FAIL","real_status":"",'
            '"real_backups_remaining":-1,"real_success_shape":false}'
        )
        child_lines = [
            "$inner = [PowerShell]::Create()",
            "try { [void]$inner.AddScript('exit 31'); [void]$inner.Invoke() } finally { $inner.Dispose() }",
            "[Console]::Out.WriteLine('R156|BEGIN|VALIDATE_ONLY')",
            f"[Console]::Out.WriteLine('R156|PACKET|{packet}')",
            "[Console]::Out.WriteLine('R156|END|VALIDATE_ONLY')",
        ]
        child = "\r\n".join(child_lines) + "\r\n"
        private = "R156_PRIVATE_TRANSPORT_SENTINEL"
        accepted = self.run_synthetic_transport("VALIDATE_ONLY", child, private)
        self.assertTrue(accepted["started"])
        self.assertTrue(accepted["supervisor_complete"])
        self.assertEqual(accepted["exit_code"], 0)
        self.assertTrue(accepted["packet_present"])
        self.assertEqual(accepted["validation_status"], "PASS")
        self.assertEqual(accepted["validation_current"], "FAIL")

        noisy_child = (
            f"[Console]::Error.Write('{private}')\r\n"
            + "\r\n".join(child_lines[2:])
            + "\r\n"
        )
        rejected = self.run_synthetic_transport("VALIDATE_ONLY", noisy_child, private)
        self.assertEqual(rejected["exit_code"], 0)
        self.assertFalse(rejected["packet_present"])

    def test_real_semantic_failure_is_protocol_classified_and_accounted_once(self):
        packet = (
            '{"protocol":"xb-r156-child/v1","mode":"REAL",'
            '"canonical_valid":true,"validation_status":"",'
            '"validation_current":"","real_status":"FAILED_PREFLIGHT",'
            '"real_backups_remaining":1,"real_success_shape":false}'
        )
        child = "\r\n".join(
            (
                "[Console]::Out.WriteLine('R156|BEGIN|REAL')",
                "[Console]::Out.WriteLine('R156|DISPATCH|REAL')",
                f"[Console]::Out.WriteLine('R156|PACKET|{packet}')",
                "[Console]::Out.WriteLine('R156|END|REAL')",
                "",
            )
        )
        result = self.run_synthetic_transport(
            "REAL", child, "R156_PRIVATE_REAL_SENTINEL"
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["packet_present"])
        self.assertEqual(result["real_status"], "FAILED_PREFLIGHT")
        self.assertFalse(result["real_success_shape"])
        self.assertTrue(result["real_started"])
        self.assertEqual(result["real_invocations"], 1)
        self.assertEqual(result["authority_consumed"], "YES")
        self.assertEqual(result["package_mutation"], "CANONICAL_TRANSACTION_ATTEMPTED")

        forged_child = child.replace(
            '"real_success_shape":false', '"real_success_shape":true'
        )
        forged = self.run_synthetic_transport(
            "REAL", forged_child, "R156_PRIVATE_FORGED_SENTINEL"
        )
        self.assertEqual(forged["exit_code"], 0)
        self.assertFalse(forged["packet_present"])
        self.assertEqual(forged["real_status"], "")

        real_policy = self.source[
            self.source.index("$script:R156InstallerStatus = [string]$realResult.Packet.real_status") :
            self.source.index("Assert-R156PostProof", self.source.index("$script:R156InstallerStatus = [string]$realResult.Packet.real_status"))
        ]
        self.assertIn("$script:R156InstallerStatus -cne 'INSTALLED'", real_policy)
        self.assertIn("-not [bool]$realResult.Packet.real_success_shape", real_policy)

    def test_access_check_token_constants_are_exact_and_distinct(self):
        identification = re.findall(
            r"private const int SECURITY_IDENTIFICATION = ([0-9]+);",
            self.native,
        )
        impersonation = re.findall(
            r"private const int TOKEN_IMPERSONATION = ([0-9]+);",
            self.native,
        )

        self.assertEqual(identification, ["1"])
        self.assertEqual(impersonation, ["2"])
        self.assertNotEqual(identification, impersonation)
        self.assertNotIn(
            "private const int SECURITY_IDENTIFICATION = 2;",
            self.native,
        )

    def test_access_check_uses_the_exact_duplicated_token_shape(self):
        check = self.native[
            self.native.index("public static AccessCheckOutcome CheckMaximumAllowed") :
        ]

        self.assertEqual(self.source.count("DuplicateTokenEx("), 2)
        self.assertEqual(
            self.native.count("public static AccessCheckOutcome CheckMaximumAllowed("),
            1,
        )
        self.assertRegex(
            check,
            r"DuplicateTokenEx\(\s*primaryToken,\s*TOKEN_QUERY,\s*"
            r"IntPtr\.Zero,\s*SECURITY_IDENTIFICATION,\s*"
            r"TOKEN_IMPERSONATION,\s*out impersonation\)",
        )
        self.assertEqual(check.count("DuplicateTokenEx("), 1)
        self.assertRegex(
            check,
            r"AccessCheck\(\s*descriptor,\s*impersonation,\s*"
            r"MAXIMUM_ALLOWED,\s*ref mapping,\s*privilegeSet,\s*"
            r"ref privilegeSetLength,\s*out granted,\s*out status\)",
        )
        self.assertEqual(check.count("AccessCheck("), 1)
        self.assertNotRegex(
            check,
            r"AccessCheck\(\s*descriptor,\s*primaryToken\b",
        )

    def test_access_check_failures_and_duplicated_handle_cleanup_remain_fail_closed(self):
        check = self.native[
            self.native.index("public static AccessCheckOutcome CheckMaximumAllowed") :
        ]

        self.assertIn("public int LastError;", self.native)
        self.assertRegex(
            check,
            r"if \(!DuplicateTokenEx\([\s\S]*?\)\) \{\s*"
            r"outcome\.LastError = Marshal\.GetLastWin32Error\(\);\s*"
            r"return outcome;\s*\}",
        )
        self.assertRegex(
            check,
            r"if \(!AccessCheck\([\s\S]*?\)\) \{\s*"
            r"outcome\.LastError = Marshal\.GetLastWin32Error\(\);\s*"
            r"return outcome;\s*\}",
        )
        self.assertEqual(
            check.count("outcome.LastError = Marshal.GetLastWin32Error();"),
            2,
        )
        self.assertLess(
            check.index("outcome.Evaluated = false;"),
            check.index("DuplicateTokenEx("),
        )
        self.assertGreater(
            check.index("outcome.Evaluated = true;"),
            check.index("AccessCheck("),
        )
        self.assertRegex(
            check,
            r"finally \{[\s\S]*?"
            r"if \(impersonation != IntPtr\.Zero\) \{\s*"
            r"CloseHandle\(impersonation\);\s*\}",
        )
        self.assertEqual(check.count("CloseHandle(impersonation);"), 1)

    def test_pointer_readers_extract_sids_before_freeing_native_buffers(self):
        user = self.method_body(
            "public static byte[] ReadTokenUserSid",
            "public static byte[] ReadTokenOwnerSid",
        )
        owner = self.method_body(
            "public static byte[] ReadTokenOwnerSid",
            "public static TokenGroupRecord[] ReadTokenGroups",
        )
        groups = self.method_body(
            "public static TokenGroupRecord[] ReadTokenGroups",
            "// Scalar and inline-value token classes",
        )

        for body in (user, owner, groups):
            self.assertNotIn("ReadTokenInformationBytes", body)
            self.assertLess(
                body.index("TryGetTokenInformationBuffer"),
                body.index("TryCopySidFromBuffer"),
            )
            self.assertLess(
                body.index("TryCopySidFromBuffer"),
                body.index("Marshal.FreeHGlobal(buffer)"),
            )
        self.assertIn("return sidBytes", user)
        self.assertIn("return sidBytes", owner)
        self.assertIn("result[index].Sid = sidBytes", groups)

        self.assertIn("returnedLength < structureLength", user)
        self.assertIn("returnedLength < structureLength", owner)
        self.assertIn("countOffset", groups)
        self.assertIn("entriesLength", groups)
        self.assertIn("TryCopySidFromBuffer", groups)

    def test_generic_token_query_helper_remains_122_only_and_class_18_uses_it(self):
        helper = self.method_body(
            "private static bool TryGetTokenInformationBuffer",
            "private static bool TryGetBufferOffset",
        )
        elevation = self.method_body(
            "public static int GetTokenElevationType",
            "public static IntPtr GetLinkedToken",
        )

        self.assertIn("IntPtr.Zero,\n                    0,", helper)
        self.assertIn("firstError != ERROR_INSUFFICIENT_BUFFER", helper)
        self.assertNotIn("ERROR_BAD_LENGTH", helper)
        self.assertNotIn("TOKEN_LINKED_TOKEN_CLASS", helper)
        self.assertIn(
            "ReadTokenInformationBytes(token, TOKEN_ELEVATION_TYPE_CLASS)",
            elevation,
        )

    def test_linked_token_query_is_direct_exact_and_pointer_width_safe(self):
        self.assertRegex(
            self.native,
            r"\[StructLayout\(LayoutKind\.Sequential\)\]\s+"
            r"public struct TOKEN_LINKED_TOKEN\s*\{\s*"
            r"public IntPtr LinkedToken;\s*\}",
        )
        linked = self.method_body(
            "public static IntPtr GetLinkedToken",
            "public static string[] ReadTokenPrivilegeNames",
        )

        self.assertNotIn("TryGetTokenInformationBuffer", linked)
        self.assertNotIn("ReadTokenInformationBytes", linked)
        self.assertNotIn("IntPtr.Zero,\n                        0,", linked)
        for marker in (
            "Marshal.SizeOf(typeof(TOKEN_LINKED_TOKEN))",
            "nativeSize != IntPtr.Size",
            "Marshal.AllocHGlobal(nativeSize)",
            "Marshal.WriteIntPtr(buffer, IntPtr.Zero)",
            "TOKEN_LINKED_TOKEN_CLASS",
            "buffer,\n                        nativeSize,",
            "returnedLength != nativeSize",
            "linkedToken = Marshal.ReadIntPtr(buffer)",
        ):
            self.assertIn(marker, linked)
        self.assertLess(
            linked.index("Marshal.ReadIntPtr(buffer)"),
            linked.index("Marshal.FreeHGlobal(buffer)"),
        )
        self.assertNotIn("return buffer", linked)

    def test_linked_token_handle_rejection_cleanup_and_transfer_are_fail_closed(self):
        linked = self.method_body(
            "public static IntPtr GetLinkedToken",
            "public static string[] ReadTokenPrivilegeNames",
        )

        self.assertRegex(
            linked,
            r"if\s*\(\s*returnedLength\s*!=\s*nativeSize\s*\|\|\s*"
            r"linkedToken\s*==\s*IntPtr\.Zero\s*\|\|\s*"
            r"linkedToken\s*==\s*token\s*\)\s*\{\s*"
            r"return\s+IntPtr\.Zero;\s*\}\s*transferred\s*=\s*true;",
        )
        self.assertIn("linkedToken == IntPtr.Zero", linked)
        self.assertIn("linkedToken == token", linked)
        self.assertIn("bool transferred = false", linked)
        self.assertIn("!transferred", linked)
        self.assertIn("linkedToken != IntPtr.Zero", linked)
        self.assertIn("linkedToken != token", linked)
        self.assertIn("CloseHandle(linkedToken)", linked)
        self.assertLess(linked.index("transferred = true"), linked.index("return linkedToken"))
        self.assertLess(linked.index("return linkedToken"), linked.index("finally"))
        self.assertEqual(linked.count("CloseHandle(linkedToken)"), 1)
        self.assertEqual(linked.count("Marshal.FreeHGlobal(buffer)"), 1)

    def test_full_and_linked_token_security_fences_remain_intact(self):
        context = self.source_function(
            "function Get-R156TokenContext",
            "function Get-R156RawSecurityDescriptor",
        )

        for marker in (
            "EG_R156_TOKEN_READ_FAILED",
            "EG_R156_SYSTEM_CONTEXT",
            "EG_R156_NON_INTERACTIVE_CONTEXT",
            "$elevationType -ne 2",
            "$fullAdminMatches -ne 1",
            "($group.Attributes -band 0x00000004) -eq 0",
            "($group.Attributes -band 0x00000010) -ne 0",
            "$linked -eq [IntPtr]::Zero -or $linked -eq $full",
            "EG_R156_FILTERED_TOKEN_ACCOUNT_MISMATCH",
            "GetTokenElevationType($linked) -ne 3",
            "$linkedAdminMatches -ne 1",
            "($group.Attributes -band 0x00000010) -eq 0",
            "($group.Attributes -band 0x00000004) -ne 0",
            "EG_R156_FILTERED_TOKEN_PRIVILEGE_READ_FAILED",
            "SeTakeOwnershipPrivilege",
            "SeRestorePrivilege",
            "EG_R156_FILTERED_TOKEN_BYPASS_PRIVILEGE",
        ):
            self.assertIn(marker, context)
        self.assertIn("$success = $true", context)
        self.assertIn("if (-not $success)", context)
        self.assertIn("[EgR156.Native]::CloseToken($linked)", context)

    def test_signed_commit_header_fail_closed_guards_remain_intact(self):
        proof = self.source_function(
            "function Get-R156CommitTreeParentProof",
            "function Close-R156TrackedHandles",
        )

        for marker in (
            "$lastHeader -notin @('gpgsig', 'mergetag')",
            "'^(?<name>[a-z][a-z0-9-]*) (?<value>[^\\r\\n]+)$'",
            "$allowedHeaders -notcontains $name",
            "$counts.ContainsKey($name)",
            "foreach ($required in @('tree', 'parent', 'author', 'committer'))",
        ):
            self.assertIn(marker, proof)

    def test_sid_pointer_and_structure_bounds_fail_closed(self):
        self.assertIn("targetAddress < bufferAddress", self.native)
        self.assertIn("targetAddress > bufferEnd - (long)minimumLength", self.native)
        self.assertIn("returned <= 0", self.native)
        self.assertIn("returned > required", self.native)
        self.assertIn("subAuthorityCount > SID_MAX_SUB_AUTHORITIES", self.native)
        self.assertIn("!HasBufferRange(bufferLength, sidOffset, sidLength)", self.native)
        self.assertIn("count > 65536", self.native)
        self.assertIn("!HasBufferRange(returnedLength, firstOffset, entriesLength)", self.native)
        self.assertIn("catch {\n                return null;", self.native)

    def test_local_and_remote_git_wrappers_are_distinct(self):
        self.assertEqual(self.source.count("function Invoke-R156LocalGitRead {"), 1)
        self.assertEqual(self.source.count("function Invoke-R156RemoteHeadProof {"), 1)
        self.assertNotIn("function Invoke-R156Git {", self.source)
        self.assertIn("Test-R156LocalGitArguments", self.source)
        self.assertIn("Test-R156RemoteGitArguments", self.source)
        self.assertIn("R156LocalGitReadOnlySubcommands", self.source)
        self.assertIn("R156RemoteGitReadOnlySubcommands", self.source)

    def test_absolute_git_and_gcm_trust_anchor_is_frozen(self):
        self.assertEqual(
            self.source.count(
                "$startInfo.FileName = [string]$script:R156FrozenGitPath"
            ),
            2,
        )
        self.assertNotIn("$startInfo.FileName = 'git.exe'", self.source)
        self.assertNotIn("FileName='git.exe'", self.source)
        self.assertNotIn("credential.helper=manager", self.source)
        self.assertIn("git-credential-manager.exe", self.source)
        self.assertIn("Get-R156CredentialHelperConfig", self.source)
        self.assertIn("'credential.helper='", self.source)
        self.assertIn("credential.helper=\"", self.source)
        self.assertIn("ProgramFiles", self.source)
        self.assertIn("ProgramFilesX86", self.source)
        self.assertIn("Test-R156ProtectedInstallationRoot", self.source)
        self.assertIn("FileSystemRights", self.source)
        self.assertIn("Test-R156NoReparseAncestors", self.source)
        self.assertNotIn("Get-Command", self.source)
        self.assertNotIn("$env:PATH", self.source)

    def test_git_process_environment_and_output_are_bounded(self):
        local = self.source_function(
            "function Invoke-R156LocalGitRead",
            "function Get-R156RemoteWorkingDirectory",
        )
        remote = self.source_function(
            "function Invoke-R156RemoteHeadProof",
            "function Get-R156RepositoryState",
        )
        for wrapper in (local, remote):
            self.assertIn("Where-Object { ([string]$_) -like 'GIT_*' }", wrapper)
            self.assertIn("EnvironmentVariables.Remove", wrapper)
            self.assertIn("RedirectStandardOutput", wrapper)
            self.assertIn("RedirectStandardError", wrapper)
            self.assertIn("GIT_TERMINAL_PROMPT", wrapper)
        self.assertIn("Where-Object { ([string]$_) -like 'GCM_*' }", remote)
        self.assertIn("GCM_INTERACTIVE", remote)
        self.assertIn("StderrDiscarded", self.source)
        self.assertIn("ReadAsync", self.source)
        self.assertIn("MaximumOutputBytes", self.source)
        self.assertIn("TimedOut", self.source)
        self.assertIn("Overflow", self.source)
        self.assertIn("process.Kill", self.source)
        self.assertIn("process.ExitCode -eq 0", self.source)

    def test_remote_discovery_isolation_is_fixed_and_repository_free(self):
        remote = self.source_function(
            "function Get-R156RemoteWorkingDirectory",
            "function Invoke-R156RemoteHeadProof",
        )
        self.assertIn("[Environment+SpecialFolder]::Windows", remote)
        self.assertIn("StartingDirectory", remote)
        self.assertIn("CeilingDirectory", remote)
        self.assertIn("[System.IO.Path]::GetPathRoot", remote)
        self.assertIn("EnvironmentVariables['GIT_CEILING_DIRECTORIES']", self.source)
        self.assertIn("File]::Exists($marker)", remote)
        self.assertIn("Directory]::Exists($marker)", remote)
        self.assertNotIn("GIT_DIR=NUL", self.source)
        self.assertNotIn("['GIT_DIR']", self.source)
        self.assertIn(
            "'https://github.com/x-boundaries/automation.git'",
            self.source,
        )
        self.assertNotIn("RemoteOrigin", self.source)

    def test_local_git_command_matrix_is_exact_and_read_only(self):
        contract = self.source_function(
            "function Test-R156LocalGitArguments",
            "function Test-R156RemoteGitArguments",
        )
        for marker in (
            "symbolic-ref",
            "'--short'",
            "'-q'",
            "'HEAD'",
            "rev-parse",
            "'--verify'",
            "'--show-toplevel'",
            "'--is-inside-work-tree'",
            "'--git-dir'",
            "'--git-common-dir'",
            "status",
            "'--porcelain=v1'",
            "'--untracked-files=all'",
            "'--ignore-submodules=none'",
            "config",
            "'--no-includes'",
            "'--null'",
            "'--list'",
            "cat-file",
            "'commit'",
            "'blob'",
        ):
            self.assertIn(marker, contract)
        self.assertIn(
            "'--git-dir=C:\\XB\\automation\\.git'",
            self.source,
        )
        self.assertIn(
            "'--work-tree=C:\\XB\\automation'",
            self.source,
        )
        for marker in (
            "'--no-optional-locks'",
            "'--no-replace-objects'",
            "'--no-lazy-fetch'",
            "'--literal-pathspecs'",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("rev-list", self.source)
        self.assertNotIn("hash-object", self.source)
        self.assertNotIn("HEAD^{tree}", self.source)

    def test_production_status_vector_command_name_and_allowlist_are_exact(self):
        state = self.run_repository_order_case([True, True])
        exact = self.production_status_vector()
        self.assertEqual(state["status_arguments"], exact)
        self.assertTrue(state["clean"])
        self.assertTrue(state["admitted"])

        script = (
            "$ErrorActionPreference = 'Stop'\n"
            + self.git_contract_constants()
            + self.extracted_functions(
                ("Get-R156LocalGitCommandName", "Test-R156LocalGitArguments")
            )
            + r'''
$exact = @('-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=NUL', '-c', 'submodule.recurse=false', '-c', 'core.autocrlf=true', 'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
$missing = @($exact[0..7] + $exact[10..13])
$falsePolicy = [string[]]$exact.Clone(); $falsePolicy[9] = 'core.autocrlf=false'
$inputPolicy = [string[]]$exact.Clone(); $inputPolicy[9] = 'core.autocrlf=input'
$wrongCase = [string[]]$exact.Clone(); $wrongCase[9] = 'core.autoCrlf=true'
$malformedPrefix = [string[]]$exact.Clone(); $malformedPrefix[1] = 'core.fsmonitor=True'
$reordered = [string[]]$exact.Clone()
$reordered[6] = '-c'; $reordered[7] = 'core.autocrlf=true'
$reordered[8] = '-c'; $reordered[9] = 'submodule.recurse=false'
$duplicate = @($exact[0..9] + @('-c', 'core.autocrlf=true') + $exact[10..13])
$extra = @($exact + '--ignored-option')
$alternateFlag = [string[]]$exact.Clone(); $alternateFlag[11] = '--porcelain=v2'
$cases = [ordered]@{
    exact = $exact
    missing = $missing
    false_policy = $falsePolicy
    input_policy = $inputPolicy
    wrong_case = $wrongCase
    malformed_prefix = $malformedPrefix
    reordered = $reordered
    duplicate = $duplicate
    extra = $extra
    alternate_flag = $alternateFlag
}
$results = [ordered]@{}
foreach ($name in $cases.Keys) {
    $arguments = [string[]]@($cases[$name])
    $results[$name] = [ordered]@{
        command = Get-R156LocalGitCommandName -Arguments $arguments
        allowed = Test-R156LocalGitArguments -Arguments $arguments
    }
}
$results | ConvertTo-Json -Compress -Depth 4
'''
        )
        lines = self.run_isolated_powershell(script)
        results = json.loads(lines[-1])
        self.assertEqual(results["exact"]["command"], "status")
        self.assertTrue(results["exact"]["allowed"])
        for name in (
            "missing",
            "false_policy",
            "input_policy",
            "wrong_case",
            "malformed_prefix",
            "reordered",
            "duplicate",
            "extra",
            "alternate_flag",
        ):
            with self.subTest(case=name):
                self.assertFalse(results[name]["allowed"])
        for name in (
            "missing",
            "false_policy",
            "input_policy",
            "wrong_case",
            "malformed_prefix",
            "reordered",
            "duplicate",
        ):
            with self.subTest(parser_case=name):
                self.assertNotEqual(results[name]["command"], "status")

    def test_offline_eol_policy_reproduces_false_dirty_and_preserves_clean(self):
        with tempfile.TemporaryDirectory(prefix="r156_eol_policy_") as directory:
            repository = self.create_synthetic_eol_repository(directory)
            production = self.synthetic_status(repository)
            self.assertEqual(production.returncode, 0, production.stderr)
            self.assertEqual(production.stdout, b"")

            false_vector = self.production_status_vector()
            false_vector[9] = "core.autocrlf=false"
            false_policy = self.synthetic_status(repository, false_vector)
            self.assertEqual(false_policy.returncode, 0, false_policy.stderr)
            self.assertEqual(
                false_policy.stdout.splitlines(),
                [b" M alpha.txt", b" M beta.txt"],
            )

    def test_production_eol_policy_keeps_every_genuine_dirty_class_dirty(self):
        cases = {
            "substantive_content": lambda repository: (
                repository / "alpha.txt"
            ).write_bytes(b"changed\r\nsecond\r\n"),
            "non_eol_whitespace": lambda repository: (
                repository / "alpha.txt"
            ).write_bytes(b"alpha \r\nsecond\r\n"),
            "untracked": lambda repository: (
                repository / "untracked.txt"
            ).write_bytes(b"untracked\r\n"),
            "tracked_deletion": lambda repository: (
                repository / "alpha.txt"
            ).unlink(),
            "tracked_rename": lambda repository: self.run_synthetic_git(
                repository, ["mv", "alpha.txt", "renamed.txt"]
            ),
            "staged_modification": lambda repository: (
                (repository / "alpha.txt").write_bytes(b"staged\r\nsecond\r\n"),
                self.run_synthetic_git(
                    repository,
                    ["-c", "core.autocrlf=true", "add", "--", "alpha.txt"],
                ),
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory(prefix="r156_genuine_dirty_") as directory:
                    repository = self.create_synthetic_eol_repository(directory)
                    mutate(repository)
                    status = self.synthetic_status(repository)
                    self.assertEqual(status.returncode, 0, status.stderr)
                    self.assertNotEqual(status.stdout, b"")

    def test_repository_state_status_failures_and_nonempty_output_fail_closed(self):
        for status_case in ("nonzero", "launch", "timeout", "overflow", "dirty"):
            with self.subTest(status_case=status_case):
                state = self.run_repository_order_case(
                    [True, True], status_case=status_case
                )
                self.assertFalse(state["clean"])
                self.assertFalse(state["admitted"])

    def test_eol_policy_is_explicit_only_on_status_and_isolation_stays_fixed(self):
        state = self.source_function(
            "function Get-R156RepositoryState",
            "function Resolve-R156RepositoryPath",
        )
        self.assertEqual(state.count("core.autocrlf=true"), 1)
        self.assertNotIn("core.autocrlf=false", self.source)
        self.assertNotIn("core.autocrlf=input", self.source)
        local = self.source_function(
            "function Invoke-R156LocalGitRead",
            "function Get-R156RemoteWorkingDirectory",
        )
        for binding in (
            "EnvironmentVariables['GIT_CONFIG_NOSYSTEM'] = '1'",
            "EnvironmentVariables['GIT_CONFIG_SYSTEM'] = 'NUL'",
            "EnvironmentVariables['GIT_CONFIG_GLOBAL'] = 'NUL'",
        ):
            self.assertIn(binding, local)

    def test_remote_command_and_helper_scope_are_fixed(self):
        remote = self.source_function(
            "function Invoke-R156RemoteHeadProof",
            "function Get-R156RepositoryState",
        )
        for marker in (
            "'ls-remote'",
            "'--quiet'",
            "'--refs'",
            "'--exit-code'",
            "'refs/heads/main'",
            "credential.interactive=false",
            "protocol.allow=never",
            "protocol.https.allow=always",
            "credential.helper=",
            "GIT_ALLOW_PROTOCOL",
            "'GCM_INTERACTIVE'] = '0'",
        ):
            self.assertIn(marker, remote)
        self.assertIn(
            "GIT_CONFIG_NOSYSTEM",
            remote,
        )
        self.assertIn("GIT_CONFIG_SYSTEM", remote)
        self.assertIn("GIT_CONFIG_GLOBAL", remote)

    def test_config_admission_rejects_includes_url_rewrites_and_unknown_keys(self):
        base = self.canonical_config_entries()
        self.assertTrue(self.config_accepts(base))
        for replacement in (
            ("include.path", "C:\\user\\config"),
            ("includeIf.gitdir", "C:\\user\\config"),
            ("url.https://evil/.insteadOf", "https://github.com/x-boundaries/automation.git"),
            ("credential.helper", "manager"),
            ("core.fsmonitor", "true"),
            ("remote.origin.promisor", "true"),
        ):
            mutated = list(base)
            mutated[1] = replacement
            self.assertFalse(self.config_accepts(mutated))
        self.assertIn("Test-R156ConfigAdmission", self.source)
        self.assertIn("acceptedKeys", self.source)
        self.assertIn("'--file=C:\\XB\\automation\\.git\\config'", self.source)
        self.assertIn("'--no-includes'", self.source)

    def test_production_config_parser_and_admission_behavioural_matrix(self):
        base = self.canonical_config_entries()
        historical_names = (
            "codex/energygrid-run156",
            "feature/archive-reader",
            "fix/portal-entry",
            "release/energygrid-v1",
            "ops/package-alignment",
            "audit/repository-fence",
            "chore/runtime-update",
            "docs/launcher-runbook",
            "test/synthetic-fixtures",
            "security/git-trust-anchor",
            "maintenance/legacy.branch",
            "WJ/Owner_branch-1",
        )
        observed_37 = list(base)
        for name in historical_names:
            observed_37.extend(
                (
                    (f"branch.{name}.remote", "origin"),
                    (f"branch.{name}.merge", f"refs/heads/{name}"),
                )
            )
        identity_name = "R156_PRIVATE_NAME_SENTINEL"
        identity_email = "r156-private-email-sentinel@example.invalid"
        observed_37.extend(
            (
                ("user.name", identity_name),
                ("user.email", identity_email),
                ("extensions.worktreeconfig", "true"),
            )
        )
        self.assertEqual(len(observed_37), 37)

        ordinal_pairs = list(base) + [
            ("branch.Feature/One.remote", "origin"),
            ("branch.Feature/One.merge", "refs/heads/Feature/One"),
            ("branch.feature/One.remote", "origin"),
            ("branch.feature/One.merge", "refs/heads/feature/One"),
        ]
        maximum_length_name = "a" * 255
        cases = {
            "original_ten": self.config_bytes(base),
            "observed_37": self.config_bytes(observed_37),
            "slash_pair": self.config_bytes(
                base
                + [
                    ("branch.feature/nested-name.remote", "origin"),
                    (
                        "branch.feature/nested-name.merge",
                        "refs/heads/feature/nested-name",
                    ),
                ]
            ),
            "ordinal_pairs": self.config_bytes(ordinal_pairs),
            "maximum_length_branch": self.config_bytes(
                base
                + [
                    (f"branch.{maximum_length_name}.remote", "origin"),
                    (
                        f"branch.{maximum_length_name}.merge",
                        f"refs/heads/{maximum_length_name}",
                    ),
                ]
            ),
            "user_identity": self.config_bytes(
                base
                + [("user.name", identity_name), ("user.email", identity_email)]
            ),
            "worktree_extension": self.config_bytes(
                base + [("extensions.worktreeconfig", "true")]
            ),
            "branch_remote_wrong": self.config_bytes(
                base
                + [
                    ("branch.feature/one.remote", "upstream"),
                    ("branch.feature/one.merge", "refs/heads/feature/one"),
                ]
            ),
            "branch_merge_wrong": self.config_bytes(
                base
                + [
                    ("branch.feature/one.remote", "origin"),
                    ("branch.feature/one.merge", "refs/heads/feature/two"),
                ]
            ),
            "branch_orphan": self.config_bytes(
                base + [("branch.feature/one.remote", "origin")]
            ),
            "duplicate_user": self.config_bytes(
                base + [("user.name", "one"), ("user.name", "two")]
            ),
            "duplicate_historical_branch_key": self.config_bytes(
                base
                + [
                    ("branch.feature/one.remote", "origin"),
                    ("branch.feature/one.remote", "origin"),
                    ("branch.feature/one.merge", "refs/heads/feature/one"),
                ]
            ),
            "duplicate_extension": self.config_bytes(
                base
                + [
                    ("extensions.worktreeconfig", "true"),
                    ("extensions.worktreeconfig", "true"),
                ]
            ),
            "unknown_branch_field": self.config_bytes(
                base + [("branch.feature/one.rebase", "true")]
            ),
            "worktree_extension_false": self.config_bytes(
                base + [("extensions.worktreeconfig", "false")]
            ),
            "invalid_utf8": b"user.name\n\xff\0",
            "missing_terminal_nul": self.config_bytes(base)[:-1],
            "carriage_return": b"user.name\r\nvalue\0",
            "missing_separator": b"user.name\0",
            "extra_separator": b"user.name\nvalue\nmore\0",
            "empty_key": b"\nvalue\0",
        }

        malformed_names = (
            "-leading",
            "/leading",
            "trailing/",
            "repeated//slash",
            "dot/./component",
            "dot/../component",
            "contains..dots",
            ".leading-dot/component",
            "trailing-dot./component",
            "component.lock",
            "white space",
            "back\\slash",
            "meta~name",
            "meta^name",
            "meta:name",
            "meta?name",
            "meta*name",
            "meta[name",
            "meta@{name",
        )
        for index, name in enumerate(malformed_names):
            cases[f"malformed_{index:02d}"] = self.config_bytes(
                base
                + [
                    (f"branch.{name}.remote", "origin"),
                    (f"branch.{name}.merge", f"refs/heads/{name}"),
                ]
            )
        overlong = "a" * 256
        cases["overlong_branch"] = self.config_bytes(
            base
            + [
                (f"branch.{overlong}.remote", "origin"),
                (f"branch.{overlong}.merge", f"refs/heads/{overlong}"),
            ]
        )

        security_extras = (
            ("unknown.setting", "value"),
            ("include.path", "C:\\private\\config"),
            ("includeIf.gitdir", "C:\\private\\config"),
            ("credential.helper", "manager"),
            ("url.https://evil/.insteadOf", "https://github.com/x-boundaries/automation.git"),
            ("alias.deploy", "!danger"),
            ("core.fsmonitor", "true"),
            ("core.hooksPath", "C:\\hooks"),
            ("safe.directory", "*"),
            ("submodule.recurse", "true"),
            ("remote.origin.promisor", "true"),
            ("remote.upstream.url", "https://github.com/x-boundaries/automation.git"),
            ("extensions.objectformat", "sha256"),
            ("extensions.arbitrary", "true"),
            ("user.signingkey", "opaque"),
        )
        for index, extra in enumerate(security_extras):
            cases[f"security_extra_{index:02d}"] = self.config_bytes(base + [extra])

        for index, (key, value) in enumerate(base):
            removed = base[:index] + base[index + 1 :]
            cases[f"required_removed_{index:02d}"] = self.config_bytes(removed)
            changed = list(base)
            replacement = "unexpected"
            if key == "remote.origin.url":
                replacement = "https://github.com/example/other.git"
            changed[index] = (key, replacement if replacement != value else "changed")
            cases[f"required_changed_{index:02d}"] = self.config_bytes(changed)
            duplicate = list(base) + [(key, value)]
            cases[f"required_duplicate_{index:02d}"] = self.config_bytes(duplicate)

        results, output = self.run_production_config_cases(cases)
        for name in (
            "original_ten",
            "observed_37",
            "slash_pair",
            "ordinal_pairs",
            "maximum_length_branch",
            "user_identity",
            "worktree_extension",
        ):
            with self.subTest(accepted=name):
                self.assertTrue(results[name]["parsed"], results[name])
                self.assertTrue(results[name]["admitted"], results[name])
        self.assertFalse(results["original_ten"]["worktree"])
        self.assertTrue(results["worktree_extension"]["worktree"])
        self.assertTrue(results["observed_37"]["worktree"])

        admission_failures = {
            "branch_remote_wrong",
            "branch_merge_wrong",
            "branch_orphan",
            "duplicate_user",
            "duplicate_historical_branch_key",
            "duplicate_extension",
            "worktree_extension_false",
            "unknown_branch_field",
        }
        admission_failures.update(
            name
            for name in cases
            if name.startswith(("security_extra_", "required_"))
        )
        for name in admission_failures:
            with self.subTest(admission_failure=name):
                self.assertFalse(results[name]["admitted"], results[name])

        parser_failures = {
            "invalid_utf8",
            "missing_terminal_nul",
            "carriage_return",
            "missing_separator",
            "extra_separator",
            "empty_key",
            "overlong_branch",
        }
        parser_failures.update(name for name in cases if name.startswith("malformed_"))
        for name in parser_failures:
            with self.subTest(parser_failure=name):
                self.assertFalse(results[name]["parsed"], results[name])
                self.assertFalse(results[name]["admitted"], results[name])

        self.assertNotIn(identity_name, output)
        self.assertNotIn(identity_email, output)

    def test_worktree_config_absence_fence_behavioural_matrix(self):
        values = self.run_worktree_config_matrix()
        self.assertTrue(values["absent"])
        for name in ("file", "directory", "reparse", "indeterminate"):
            with self.subTest(surface=name):
                self.assertFalse(values[name])
        self.assertTrue(values["disabled_preserves"])
        self.assertTrue(values["enabled_absent"])
        for name in (
            "enabled_file",
            "enabled_directory",
            "enabled_reparse",
            "enabled_indeterminate",
            "enabled_appeared_after",
        ):
            with self.subTest(fence=name):
                self.assertFalse(values[name])

    def test_worktree_config_fence_wraps_all_config_consuming_repository_reads(self):
        state = self.source_function(
            "function Get-R156RepositoryState",
            "function Resolve-R156RepositoryPath",
        )
        before = state.index("$worktreeConfigAbsentBefore")
        config_read = state.index("$configResult = Invoke-R156LocalGitRead")
        first_repository_read = state.index("$branchResult = Invoke-R156LocalGitRead")
        last_repository_read = state.index("$statusResult = Invoke-R156LocalGitRead")
        after = state.index("$worktreeConfigAbsentAfter")
        read_ok = state.index("$state.ReadOk")
        self.assertLess(before, config_read)
        self.assertLess(config_read, first_repository_read)
        self.assertLess(before, first_repository_read)
        self.assertLess(last_repository_read, after)
        self.assertLess(after, read_ok)
        self.assertIn("C:\\XB\\automation\\.git\\config.worktree", state)
        self.assertIn("$worktreeConfigFence", state)
        self.assertIn("-Enabled $true", state)

    def test_initial_repository_read_order_and_appearance_race_use_production_function(self):
        accepted = self.run_repository_order_case((True, True))
        self.assertTrue(accepted["read_ok"], accepted)
        self.assertEqual(accepted["events"][0], "absence:true")
        self.assertEqual(accepted["events"][1], "git:config")
        self.assertEqual(accepted["events"][-1], "absence:true")
        self.assertEqual(accepted["absence_checks"], 2)

        appeared = self.run_repository_order_case((True, False))
        self.assertFalse(appeared["read_ok"], appeared)
        self.assertEqual(appeared["events"][0], "absence:true")
        self.assertEqual(appeared["events"][1], "git:config")
        self.assertEqual(appeared["events"][-1], "absence:false")
        self.assertEqual(appeared["absence_checks"], 2)

    def test_trusted_source_git_window_is_fenced_by_production_function(self):
        source = self.source_function(
            "function Read-R156TrustedSource",
            "function Read-R156Locator",
        )
        before = source.index("$worktreeConfigAbsentBefore")
        resolve = source.index("$resolved = Invoke-R156LocalGitRead")
        cat_file = source.index("$committedResult = Invoke-R156LocalGitRead")
        after = source.index("$worktreeConfigAbsentAfter")
        accept = source.index("$resolvedValue = Get-R156SingleGitLine")
        self.assertLess(before, resolve)
        self.assertLess(resolve, cat_file)
        self.assertLess(cat_file, after)
        self.assertLess(after, accept)

        accepted = self.run_trusted_source_order_case((True, True))
        self.assertTrue(accepted["accepted"], accepted)
        self.assertEqual(
            accepted["events"],
            ["absence:true", "git:rev-parse", "git:cat-file", "absence:true"],
        )
        self.assertEqual(accepted["git_reads"], 2)
        self.assertEqual(accepted["absence_checks"], 2)

        appeared_after_repository_admission = self.run_trusted_source_order_case((False,))
        self.assertFalse(
            appeared_after_repository_admission["accepted"],
            appeared_after_repository_admission,
        )
        self.assertEqual(
            appeared_after_repository_admission["events"],
            ["absence:false"],
        )
        self.assertEqual(appeared_after_repository_admission["git_reads"], 0)

        appeared_during_read = self.run_trusted_source_order_case((True, False))
        self.assertFalse(appeared_during_read["accepted"], appeared_during_read)
        self.assertEqual(
            appeared_during_read["events"],
            ["absence:true", "git:rev-parse", "git:cat-file", "absence:false"],
        )
        self.assertEqual(appeared_during_read["git_reads"], 2)

    def test_final_predispatch_sequence_rechecks_absence_after_trusted_reads(self):
        start = self.source.index("    $canonicalLibraryBeforeReal =")
        real = self.source.index("    $realResult = Invoke-R156Transport -Mode 'REAL'", start)
        sequence = self.source[start:real]
        last_trusted = sequence.rindex("Read-R156TrustedSource")
        manifest = sequence.rindex("Read-R156ManifestState")
        remote = sequence.rindex("Test-R156RemoteHead")
        final_absence = sequence.rindex("Test-R156WorktreeConfigSurfaceAbsent")
        self.assertLess(last_trusted, manifest)
        self.assertLess(manifest, remote)
        self.assertLess(remote, final_absence)
        self.assertNotIn("Invoke-R156LocalGitRead", sequence[final_absence:])

        appeared_before_dispatch = self.run_final_predispatch_case(False)
        self.assertEqual(
            appeared_before_dispatch["events"],
            [
                "trusted-source",
                "trusted-source",
                "trusted-source",
                "manifest",
                "remote-head",
                "final-absence",
            ],
        )
        self.assertEqual(appeared_before_dispatch["real_installer_invocations"], 0)

        absent_before_dispatch = self.run_final_predispatch_case(True)
        self.assertEqual(absent_before_dispatch["events"][-2:], ["final-absence", "real"])
        self.assertEqual(absent_before_dispatch["real_installer_invocations"], 1)

    def test_status_is_side_effect_free_and_index_is_held(self):
        status = self.source_function(
            "function Get-R156RepositoryState",
            "function Resolve-R156RepositoryPath",
        )
        for marker in (
            "core.fsmonitor=false",
            "core.untrackedCache=false",
            "core.hooksPath=NUL",
            "submodule.recurse=false",
            "core.autocrlf=true",
            "'--ignore-submodules=none'",
            "R156IndexHandle",
            "indexMetadataBefore",
            "indexMetadataAfter",
            "indexDigestBefore",
            "indexDigestAfter",
            "indexSideEffectFree",
            "FileShare]::Read",
            "GIT_OPTIONAL_LOCKS",
        ):
            self.assertIn(marker, status + self.source)
        self.assertIn("R156RepositoryConfigHandle", status)
        self.assertIn("ConfigAdmission", status)

    def test_cat_file_rejects_filters_textconv_mailmap_and_arbitrary_objects(self):
        contract = self.source_function(
            "function Test-R156LocalGitArguments",
            "function Test-R156RemoteGitArguments",
        )
        self.assertIn("R156LibraryGitBlob", contract)
        self.assertIn("R156InstallerGitBlob", contract)
        self.assertIn("R156LauncherGitBlob", contract)
        for marker in (
            "--filters",
            "--textconv",
            "--follow-symlinks",
            "--mailmap",
            "arbitrary",
        ):
            self.assertNotIn(marker, contract)
        self.assertIn("ExpectedHeadAtExecution", contract)
        self.assertIn("'cat-file', 'commit'", self.source)
        self.assertIn("'cat-file', 'blob'", self.source)

    def test_raw_commit_tree_parent_and_blob_hash_proofs_are_present(self):
        proof = self.source_function(
            "function Get-R156CommitTreeParentProof",
            "function Close-R156TrackedHandles",
        )
        for marker in (
            "RawCommitBytes",
            "Get-R156Sha1ForGitObject",
            "CommitObjectHash",
            "headerText",
            "allowedHeaders",
            "counts",
            "'tree'",
            "'parent'",
            "ExpectedTreeValue",
            "ExpectedParentValue",
            "ParentCount",
        ):
            self.assertIn(marker, proof)
        tree = "a" * 40
        parent = "b" * 40
        raw = self.commit_headers(tree, parent)
        self.assertEqual(self.git_object_sha1("commit", raw), self.git_object_sha1("commit", raw))
        headers = raw.split(b"\n\n", 1)[0].decode("utf-8").splitlines()
        self.assertEqual([line.split(" ", 1)[1] for line in headers if line.startswith("tree ")], [tree])
        self.assertEqual([line.split(" ", 1)[1] for line in headers if line.startswith("parent ")], [parent])
        duplicate_parent = raw.replace(
            f"parent {parent}\n".encode(),
            f"parent {parent}\nparent {parent}\n".encode(),
        )
        self.assertEqual(
            len(re.findall(r"^parent [0-9a-f]{40}$", duplicate_parent.decode(), re.MULTILINE)),
            2,
        )

        source = self.source_function(
            "function Read-R156TrustedSource",
            "function Read-R156Locator",
        )
        for marker in (
            "rev-parse",
            "ExpectedGitBlob",
            "cat-file",
            "committedBytes",
            "Get-R156Sha1ForGitObject -Type 'blob'",
            "workingBytes",
            "NormalizedBytes",
            "Test-R156ByteArraysEqual",
            "Test-R156PowerShellParse -Path $workingPath -Bytes $workingBytes",
            "R156SourceHandles",
        ):
            self.assertIn(marker, source)

    def test_byte_contract_synthetic_matrix(self):
        committed = b"alpha\nbeta\n"
        canonical_cases = (
            ("valid uniform LF", committed, True),
            ("CRLF is not canonical", b"alpha\r\nbeta\r\n", False),
            ("BOM", b"\xef\xbb\xbf" + committed, False),
            ("invalid UTF-8", b"alpha\n\xff\n", False),
            ("NUL", b"alpha\x00\n", False),
            ("EOT", b"alpha\x04\n", False),
            ("other forbidden C0 control", b"alpha\x01\n", False),
            ("DEL", b"alpha\x7f\n", False),
            ("missing terminal LF", b"alpha\nbeta", False),
            ("surplus terminal LF", b"alpha\nbeta\n\n", False),
        )
        for label, candidate, expected in canonical_cases:
            with self.subTest(contract="canonical", case=label):
                self.assertEqual(self.canonical_byte_contract(candidate), expected)

        working_cases = (
            ("valid uniform LF", committed, True),
            ("valid uniform CRLF", b"alpha\r\nbeta\r\n", True),
            ("mixed line endings", b"alpha\r\nbeta\n", False),
            ("lone CR", b"alpha\rbeta\n", False),
            ("BOM", b"\xef\xbb\xbf" + committed, False),
            ("invalid UTF-8", b"alpha\n\xff\n", False),
            ("NUL", b"alpha\x00\n", False),
            ("EOT", b"alpha\x04\n", False),
            ("other forbidden C0 control", b"alpha\x01\n", False),
            ("DEL", b"alpha\x7f\n", False),
            ("missing terminal LF", b"alpha\nbeta", False),
            ("surplus terminal LF", b"alpha\nbeta\n\n", False),
        )
        for label, candidate, expected in working_cases:
            with self.subTest(contract="working", case=label):
                self.assertEqual(self.normalize_contract(candidate, committed), expected)

        self.assertFalse(self.normalize_contract(b"alpha\n", b"omega\n"))
        for marker in (
            "Test-R156CommittedByteContract",
            "Test-R156WorkingByteContract",
            "Convert-R156WorkingBytesToLf",
            "NormalizedBytes",
            "Ending = if ($hasCrlf)",
            "UTF8Encoding($false, $true)",
            "0xEF",
            "0x0D",
            "0x0A",
        ):
            self.assertIn(marker, self.source)

    def test_source_and_repository_handles_deny_replacement(self):
        for marker in (
            "Open-R156ReadOnlyHandle",
            "FileMode]::Open",
            "FileAccess]::Read",
            "FileShare]::Read",
            "R156RepositoryConfigHandle",
            "R156SourceHandles",
            "R156IndexHandle",
            "Test-R156NoReparseAncestors",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("FileShare]::ReadWrite", self.source)
        self.assertNotIn("FileShare]::Delete", self.source)

    def test_malformed_extra_output_overflow_timeout_and_nonzero_fail_closed(self):
        head = "0" * 40
        self.assertEqual(self.remote_record(head), f"{head}\trefs/heads/main\n".encode())
        self.assertNotEqual(
            self.remote_record(head) + b"extra\n",
            self.remote_record(head),
        )
        for marker in (
            "Test-R156SingleGitLineBytes",
            "Convert-R156ConfigBytes",
            "StdoutBytes",
            "StderrDiscarded",
            "MaximumOutputBytes",
            "TimedOut",
            "Overflow",
            "ExitCode",
            "Success",
            "process.Kill",
            "RedirectStandardOutput",
            "RedirectStandardError",
        ):
            self.assertIn(marker, self.source)

    def test_frozen_boundaries_and_dispatch_protocol_remain_unchanged(self):
        transport = self.source_function(
            "function Invoke-R156Transport",
            "function Test-R156PrivateBindingsOutsideCheckout",
        )
        self.assertIn("Where-Object { ([string]$_) -like 'GIT_*' }", transport)
        self.assertIn("EnvironmentVariables.Remove", transport)
        collect = transport.index("$inheritedGitNames = @(")
        remove = transport.index("foreach ($name in $inheritedGitNames)")
        child_binding = transport.index("$startInfo.EnvironmentVariables['EG_R156_MODE']")
        start = transport.index("$started = [bool]$process.Start()")
        self.assertLess(collect, remove)
        self.assertLess(remove, child_binding)
        self.assertLess(child_binding, start)
        self.assertIn("$startInfo.RedirectStandardInput = $true", transport)
        stdout_read = transport.index(
            "$stdoutTask = $process.StandardOutput.ReadToEndAsync()"
        )
        stderr_read = transport.index(
            "$stderrTask = $process.StandardError.ReadToEndAsync()"
        )
        child_write = transport.index(
            "$process.StandardInput.Write($script:R156ChildScript)"
        )
        child_flush = transport.index("$process.StandardInput.Flush()")
        real_accounting = transport.index("$script:R156RealStarted = $true")
        stdin_close = transport.index("$process.StandardInput.Close()")
        self.assertLess(start, stdout_read)
        self.assertLess(start, stderr_read)
        self.assertLess(stdout_read, child_write)
        self.assertLess(stderr_read, child_write)
        self.assertLess(child_write, child_flush)
        self.assertLess(child_flush, real_accounting)
        self.assertLess(real_accounting, stdin_close)
        arguments = transport[
            transport.index("$startInfo.Arguments = (") :
            transport.index("$startInfo.UseShellExecute", transport.index("$startInfo.Arguments = ("))
        ]
        self.assertIn("$encoded", arguments)
        self.assertNotIn("R156ChildScript", arguments)
        self.assertIn(
            "ConvertTo-R156EncodedCommand -ScriptText $script:R156BootstrapScript",
            transport,
        )
        self.assertEqual(self.source.count("Invoke-R156Transport -Mode 'REAL'"), 1)
        self.assertEqual(self.source.count("$script:R156RealStarted = $true"), 1)
        self.assertEqual(self.source.count("$script:R156RealInstallerInvocations = 1"), 1)
        self.assertEqual(self.source.count("$script:R156AuthorityConsumed = 'YES'"), 1)
        self.assertIn("$script:R156RealInstallerInvocations = 1", self.source)
        self.assertIn("$script:R156AuthorityConsumed = 'YES'", self.source)
        self.assertIn("'CONTROLLER_REQUIRED_POST_DISPATCH'", self.source)
        self.assertIn("$fields['retry_allowed'] = 'NO'", self.source)

        self.assertNotIn("R156ExpectedParentForPreimage", self.source)
        parameter_start = self.source.index("param(")
        parameter_end = self.source.index(")\n\nSet-StrictMode", parameter_start) + 1
        parameter_block = self.source[parameter_start:parameter_end]
        self.assertRegex(
            parameter_block,
            r"\[string\]\$ExpectedInstalledAdmission\s*\)",
        )
        self.assertNotRegex(parameter_block, r"\$ExpectedInstalledAdmission\s*=")

        admission_start = self.source.index("$tokenContext = $null")
        admission_end = self.source.index(
            "if ([string]$PSVersionTable",
            admission_start,
        )
        admission_block = self.source[admission_start:admission_end]
        for name in (
            "ExpectedHead",
            "ExpectedTree",
            "ExpectedParent",
            "ExpectedInstalledAdmission",
        ):
            self.assertEqual(
                admission_block.count(f"Test-R156CommitText -Value ${name}"),
                1,
                name,
            )
        self.assertIn(
            "$script:R156ExpectedInstalledAdmissionAtExecution = $ExpectedInstalledAdmission.ToLowerInvariant()",
            admission_block,
        )
        self.assertNotIn(
            "$script:R156ExpectedInstalledAdmissionAtExecution = $ExpectedParent",
            admission_block,
        )

        stale_calls = re.findall(
            r"Read-R156ManifestState\s+-LauncherRoot\s+[^\r\n]+?-ExpectedAdmission\s+\$script:R156ExpectedInstalledAdmissionAtExecution",
            self.source,
        )
        self.assertEqual(len(stale_calls), 3)
        stale_parent_calls = re.findall(
            r"Read-R156ManifestState\s+-LauncherRoot\s+[^\r\n]+?-ExpectedAdmission\s+\$script:R156ExpectedParentAtExecution",
            self.source,
        )
        self.assertEqual(stale_parent_calls, [])
        self.assertIn(
            "Get-R156CommitTreeParentProof -ExpectedHeadValue $script:R156ExpectedHeadAtExecution -ExpectedTreeValue $script:R156ExpectedTreeAtExecution -ExpectedParentValue $script:R156ExpectedParentAtExecution",
            self.source,
        )
        self.assertGreaterEqual(
            self.source.count("-ExpectedParentValue $script:R156ExpectedParentAtExecution"),
            5,
        )
        self.assertEqual(
            self.source.count("Invoke-R156Transport -Mode 'VALIDATE_ONLY'"),
            1,
        )
        self.assertEqual(
            self.source.count("Invoke-R156Transport -Mode 'REAL'"),
            1,
        )
        for transport in (
            "Invoke-R156Transport -Mode 'VALIDATE_ONLY'",
            "Invoke-R156Transport -Mode 'REAL'",
        ):
            transport_start = self.source.index(transport)
            transport_line = self.source[transport_start:self.source.index("\n", transport_start)]
            self.assertIn(
                "-AdmissionCommit $script:R156ExpectedHeadAtExecution",
                transport_line,
            )
        post_proof = self.source_function(
            "function Assert-R156PostProof",
            "$tokenContext = $null",
        )
        self.assertIn(
            "Read-R156ManifestState -LauncherRoot $topologyAfter.Candidate -ExpectedAdmission $ExpectedHeadValue",
            post_proof,
        )
        self.assertNotIn(
            "-AdmissionCommit $script:R156ExpectedInstalledAdmissionAtExecution",
            self.source,
        )
        self.assertNotIn(ACCEPTED_STALE_ADMISSION, self.source)
        fence_calls = re.findall(
            r"Test-R156RepositoryFence\s+-State\s+[^\r\n]+",
            self.source,
        )
        self.assertEqual(len(fence_calls), 4)

    def test_preimage_decoupling_accepts_valid_distinct_identities(self):
        result = self.run_preimage_boundary_case()
        self.assertTrue(result["admission_valid"])
        self.assertTrue(result["repository_fence"])
        self.assertTrue(result["repository_fence_exercised"])
        self.assertEqual(result["production_manifest_reads"], 3)
        self.assertTrue(result["pre_dispatch_passed"])
        self.assertEqual(result["stale_reads"], 3)
        self.assertEqual(result["real_installer_invocations"], 1)
        self.assertTrue(result["synthetic_dispatch_boundary"])
        self.assertEqual(result["transport_admission"], CURRENT_HEAD)
        self.assertTrue(result["post_proof_exercised"])

    def test_preimage_decoupling_manifest_admission_is_mutation_sensitive(self):
        manifest_source = self.source_function(
            "function Read-R156ManifestState",
            "function Get-R156DirectChildren",
        )
        admission_guard = (
            "    if ([string]$manifest.admission_commit -cne $ExpectedAdmission) {\n"
            "        return $null\n"
            "    }"
        )
        self.assertEqual(manifest_source.count(admission_guard), 1)
        defective_source = manifest_source.replace(admission_guard, "", 1)
        self.assertEqual(defective_source.count(admission_guard), 0)
        self.assertEqual(
            manifest_source.count(admission_guard) - defective_source.count(admission_guard),
            1,
        )

        case = {
            "expected_installed": "c" * 40,
            "manifest_sequence": [ACCEPTED_STALE_ADMISSION] * 3,
            "post_admission": CURRENT_HEAD,
        }
        canonical = self.run_preimage_boundary_case(**case)
        defective = self.run_preimage_boundary_case(
            **case,
            _manifest_source=defective_source,
        )

        self.assertTrue(canonical["repository_fence_exercised"])
        self.assertEqual(canonical["production_manifest_reads"], 1)
        self.assertTrue(canonical["manifest_guard_rejection_observed"])
        self.assertFalse(canonical["pre_dispatch_passed"])
        self.assertEqual(canonical["real_installer_invocations"], 0)

        self.assertTrue(defective["repository_fence_exercised"])
        self.assertEqual(defective["production_manifest_reads"], 3)
        self.assertTrue(defective["pre_dispatch_passed"])
        self.assertTrue(defective["synthetic_dispatch_boundary"])
        self.assertEqual(defective["real_installer_invocations"], 1)
        self.assertEqual(defective["terminal"], "PASS")

    def test_preimage_decoupling_wrong_installed_admission_blocks_real_dispatch(self):
        result = self.run_preimage_boundary_case(
            expected_installed="c" * 40,
            manifest_sequence=[ACCEPTED_STALE_ADMISSION] * 3,
        )
        self.assertTrue(result["admission_valid"])
        self.assertTrue(result["repository_fence"])
        self.assertTrue(result["repository_fence_exercised"])
        self.assertEqual(result["production_manifest_reads"], 1)
        self.assertTrue(result["manifest_guard_rejection_observed"])
        self.assertFalse(result["pre_dispatch_passed"])
        self.assertEqual(result["real_installer_invocations"], 0)

    def test_preimage_decoupling_wrong_repository_parent_blocks_real_dispatch(self):
        result = self.run_preimage_boundary_case(
            expected_parent="b" * 40,
            manifest_sequence=[ACCEPTED_STALE_ADMISSION] * 3,
        )
        self.assertTrue(result["admission_valid"])
        self.assertTrue(result["repository_fence_exercised"])
        self.assertFalse(result["repository_fence"])
        self.assertEqual(result["production_manifest_reads"], 0)
        self.assertEqual(result["stale_reads"], 0)
        self.assertEqual(result["real_installer_invocations"], 0)

    def test_preimage_decoupling_swapped_identities_block_real_dispatch(self):
        result = self.run_preimage_boundary_case(
            expected_parent=ACCEPTED_STALE_ADMISSION,
            expected_installed=CURRENT_PARENT,
            manifest_sequence=[ACCEPTED_STALE_ADMISSION] * 3,
        )
        self.assertTrue(result["admission_valid"])
        self.assertTrue(result["repository_fence_exercised"])
        self.assertFalse(result["repository_fence"])
        self.assertEqual(result["real_installer_invocations"], 0)

    def test_preimage_decoupling_already_current_manifest_is_a_contradiction(self):
        result = self.run_preimage_boundary_case(
            manifest_sequence=[CURRENT_HEAD] * 3,
        )
        self.assertTrue(result["repository_fence"])
        self.assertTrue(result["repository_fence_exercised"])
        self.assertEqual(result["production_manifest_reads"], 1)
        self.assertTrue(result["manifest_guard_rejection_observed"])
        self.assertFalse(result["pre_dispatch_passed"])
        self.assertEqual(result["stale_reads"], 1)
        self.assertEqual(result["real_installer_invocations"], 0)

    def test_preimage_decoupling_moved_preimage_blocks_late_real_dispatch(self):
        result = self.run_preimage_boundary_case(
            manifest_sequence=[
                ACCEPTED_STALE_ADMISSION,
                ACCEPTED_STALE_ADMISSION,
                "d" * 40,
            ],
        )
        self.assertTrue(result["repository_fence"])
        self.assertTrue(result["repository_fence_exercised"])
        self.assertEqual(result["production_manifest_reads"], 3)
        self.assertTrue(result["manifest_guard_rejection_observed"])
        self.assertFalse(result["pre_dispatch_passed"])
        self.assertEqual(result["stale_reads"], 3)
        self.assertEqual(result["real_installer_invocations"], 0)

    def test_preimage_decoupling_post_proof_accepts_only_current_head(self):
        current = self.run_preimage_boundary_case(post_admission=CURRENT_HEAD)
        stale = self.run_preimage_boundary_case(
            post_admission=ACCEPTED_STALE_ADMISSION,
        )
        self.assertEqual(current["terminal"], "PASS")
        self.assertTrue(current["post_proof_exercised"])
        self.assertEqual(current["post_proof_terminal"], "PASS")
        self.assertTrue(current["post_proof_current"])
        self.assertFalse(current["post_proof_stale"])
        self.assertEqual(stale["terminal"], "POST_PROOF_FAILED")
        self.assertTrue(stale["post_proof_exercised"])
        self.assertEqual(stale["post_proof_terminal"], "BLOCKED")
        self.assertFalse(stale["post_proof_current"])
        self.assertTrue(stale["post_proof_stale"])

    def test_preimage_decoupling_parent_cardinality_requires_one(self):
        for count in (0, 2):
            with self.subTest(parent_count=count):
                result = self.run_preimage_boundary_case(parent_count=count)
                self.assertTrue(result["admission_valid"])
                self.assertTrue(result["repository_fence_exercised"])
                self.assertFalse(result["repository_fence"])
                self.assertEqual(result["real_installer_invocations"], 0)

    def test_preimage_decoupling_installed_admission_provenance_is_explicit(self):
        result = self.run_preimage_boundary_case()
        self.assertTrue(result["repository_fence_exercised"])
        self.assertEqual(result["production_manifest_reads"], 3)
        self.assertEqual(result["expected_parent_state"], CURRENT_PARENT)
        self.assertEqual(result["expected_installed_state"], ACCEPTED_STALE_ADMISSION)
        self.assertNotEqual(
            result["expected_parent_state"],
            result["expected_installed_state"],
        )

    def test_live248_crlf_installed_members_are_canonically_equivalent(self):
        result = self.run_manifest_equivalence_case()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["package_calls"], 1, result)
        self.assertTrue(result["raw_digest_match"], result)

    def test_installed_content_drift_surviving_normalization_is_rejected(self):
        cases = (
            {"launcher_installed": b"# launcher\r\nWrite-Output 'drift'\r\n"},
            {"library_installed": b"# library\r\nfunction Invoke-Drift { }\r\n"},
        )
        for case in cases:
            with self.subTest(member=next(iter(case))):
                result = self.run_manifest_equivalence_case(**case)
                self.assertFalse(result["accepted"], result)
                self.assertEqual(result["package_calls"], 1, result)
                self.assertTrue(result["raw_digest_match"], result)

    def test_unsupported_installed_line_endings_fail_closed(self):
        cases = (
            {"launcher_installed": b"# launcher\r\nWrite-Output 'launcher'\n"},
            {"library_installed": b"# library\rfunction Invoke-Library { }\n"},
        )
        for case in cases:
            with self.subTest(member=next(iter(case))):
                result = self.run_manifest_equivalence_case(**case)
                self.assertFalse(result["accepted"], result)
                self.assertEqual(result["package_calls"], 1, result)
                self.assertTrue(result["raw_digest_match"], result)

    def test_manifest_equivalence_preserves_all_precomparison_gates(self):
        cases = (
            {"manifest_schema": "eg_launcher_installation_manifest/v2"},
            {"manifest_admission": "f" * 40},
            {"manifest_shape": False},
            {"package_pass": False},
            {"non_normal_name": "launcher.ps1"},
            {"parse_fail_name": "launcher.ps1"},
            {"parse_fail_name": "launcher_lib.ps1"},
        )
        for case in cases:
            with self.subTest(case=case):
                result = self.run_manifest_equivalence_case(**case)
                self.assertFalse(result["accepted"], result)

        manifest_reader = self.source_function(
            "function Read-R156ManifestState",
            "function Get-R156DirectChildren",
        )
        strict_reader = self.source_function(
            "function Read-R156StrictJsonObject",
            "function Read-R156TrustedSource",
        )
        read_handle = self.source_function(
            "function Open-R156ReadOnlyHandle",
            "function Read-R156HandleBytes",
        )
        self.assertLess(
            manifest_reader.index("Compare-EgInstalledPackageToManifest"),
            manifest_reader.index("Test-R156WorkingByteContract -Bytes $installedBytes"),
        )
        self.assertIn("Test-R156NormalFile -Path $installedPath", manifest_reader)
        self.assertIn("Test-R156PowerShellParse -Path $installedPath -Bytes $installedBytes", manifest_reader)
        self.assertIn("Open-R156ReadOnlyHandle -Path $Path", strict_reader)
        self.assertIn("Test-R156StrictJsonBytes -Bytes $bytes", strict_reader)
        self.assertIn("Test-R156NormalFile -Path $Path", read_handle)
        self.assertIn("Test-R156NoReparseAncestors -Path $Path", read_handle)

    def test_head_blobs_are_canonical_for_both_owned_files(self):
        for relative_path in OWNED_RELATIVE_PATHS:
            with self.subTest(path=relative_path):
                object_id, committed = self.read_head_blob(relative_path)
                self.assertTrue(self.canonical_byte_contract(committed))
                self.assertEqual(self.git_object_sha1("blob", committed), object_id)

    def test_working_tree_bytes_match_verified_head_blobs_after_normalization(self):
        for relative_path in OWNED_RELATIVE_PATHS:
            with self.subTest(path=relative_path):
                _, committed = self.read_head_blob(relative_path)
                working = (REPO_ROOT / Path(relative_path)).read_bytes()
                normalized = self.working_byte_contract(working)
                self.assertIsNotNone(normalized)
                self.assertEqual(normalized, committed)

    def test_git_output_contract_behavioural_matrix(self):
        frozen = (
            ("library", "de75302dfb7ce3b1b4b37919d6b7e734d0503018", 141393),
            ("installer", "a5b670e3b043a026af1d7f2086df03fdb1e7fa13", 25643),
            ("launcher", "d632068bbd5832cc46971278ca0f4fba3bf7e8f3", 21081),
        )
        blobs = {}
        for name, object_id, expected_length in frozen:
            with self.subTest(blob=name):
                result = subprocess.run(
                    ["git", "cat-file", "blob", object_id],
                    cwd=REPO_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(len(result.stdout), expected_length)
                self.assertEqual(self.git_object_sha1("blob", result.stdout), object_id)
                blobs[name] = result.stdout

                process = self.run_bounded_process(
                    "payload",
                    stdout_limit=expected_length,
                    payload=result.stdout,
                    timeout_milliseconds=10000,
                )
                self.assertTrue(process["success"], process)
                self.assertEqual(process["length"], expected_length)
                self.assertEqual(process["sha256"], hashlib.sha256(result.stdout).hexdigest())
                self.assertTrue(process["stderrdiscarded"])

                contract = self.run_local_output_check(object_id, result.stdout)
                self.assertTrue(contract["valid"], contract)
                self.assertEqual(contract["contract_limit"], expected_length)
                self.assertEqual(contract["contract_length"], expected_length)
                self.assertEqual(contract["contract_object"], object_id)

        library_id = frozen[0][1]
        library = blobs["library"]
        short = self.run_local_output_check(library_id, library[:-1])
        self.assertFalse(short["valid"], short)
        extra = self.run_local_output_check(library_id, library + b"x")
        self.assertFalse(extra["valid"], extra)
        wrong_bytes = bytearray(library)
        wrong_bytes[len(wrong_bytes) // 2] ^= 0x01
        wrong = self.run_local_output_check(library_id, bytes(wrong_bytes))
        self.assertFalse(wrong["valid"], wrong)
        wrong_object = self.run_local_output_check("0" * 40, library)
        self.assertFalse(wrong_object["valid"], wrong_object)
        wrong_admitted_id = self.run_local_output_check(frozen[1][1], library)
        self.assertFalse(wrong_admitted_id["valid"], wrong_admitted_id)

        size_plus_one = self.run_bounded_process(
            "payload",
            stdout_limit=len(library),
            payload=library + b"x",
        )
        self.assertFalse(size_plus_one["success"], size_plus_one)
        self.assertTrue(size_plus_one["overflow"], size_plus_one)
        self.assertEqual(size_plus_one["length"], 0)

        ordinary = b"o" * 65536
        ordinary_result = self.run_bounded_process(
            "payload",
            stdout_limit=65536,
            payload=ordinary,
        )
        self.assertTrue(ordinary_result["success"], ordinary_result)
        self.assertEqual(ordinary_result["length"], 65536)
        ordinary_overflow = self.run_bounded_process(
            "payload",
            stdout_limit=65536,
            payload=ordinary + b"o",
        )
        self.assertFalse(ordinary_overflow["success"], ordinary_overflow)
        self.assertTrue(ordinary_overflow["overflow"], ordinary_overflow)
        self.assertEqual(ordinary_overflow["length"], 0)

        stderr_at_limit = self.run_bounded_process(
            "payload",
            stdout_limit=len(library),
            stderr_limit=65536,
            payload=library,
            error_payload=b"e" * 65536,
            timeout_milliseconds=10000,
        )
        self.assertTrue(stderr_at_limit["success"], stderr_at_limit)
        self.assertEqual(stderr_at_limit["length"], len(library))
        stderr_overflow = self.run_bounded_process(
            "payload",
            stdout_limit=len(library),
            stderr_limit=65536,
            payload=library,
            error_payload=b"e" * 65537,
        )
        self.assertFalse(stderr_overflow["success"], stderr_overflow)
        self.assertTrue(stderr_overflow["overflow"], stderr_overflow)
        self.assertEqual(stderr_overflow["length"], 0)

    def test_empty_byte_protocol_behavioural_matrix(self):
        values = self.run_empty_byte_matrix()
        self.assertTrue(values["clean_status"])
        self.assertFalse(values["dirty_status"])
        self.assertTrue(values["failed_empty"])
        for name in ("symbolic", "head", "config", "commit", "blob"):
            with self.subTest(command=name):
                self.assertFalse(values["data_" + name])

    def test_redirected_stream_lifecycle_behavioural_matrix(self):
        child_script = self.stream_child_script()
        self.assertIn('GetStdHandle', child_script)
        self.assertIn('CloseHandle', child_script)
        self.assertIn('-11', child_script)
        self.assertIn('-12', child_script)
        self.assertNotIn('$stdout.Dispose()', child_script)
        self.assertNotIn('$stderr.Dispose()', child_script)

        stdout_first_witness = (
            'stdout-payload-written',
            'stdout-os-closed',
            'post-stdout-closure-alive',
            'stderr-write-after-stdout-closure',
            'stderr-os-closed',
            'both-os-handles-closed',
        )
        stderr_first_witness = (
            'stderr-payload-written',
            'stderr-os-closed',
            'post-stderr-closure-alive',
            'stdout-write-after-stderr-closure',
            'stdout-os-closed',
            'both-os-handles-closed',
        )
        successful = {
            "empty": (b"", ()),
            "stdout-first": (b"OUT", stdout_first_witness),
            "stderr-first": (b"OUT", stderr_first_witness),
            "simultaneous": (
                b"OUT",
                (
                    'stdout-payload-written',
                    'stderr-payload-written',
                    'stdout-os-closed',
                    'stderr-os-closed',
                    'both-os-handles-closed',
                ),
            ),
            "stdout-after-stderr": (
                b"\x00" * 65536,
                (
                    'stderr-payload-written',
                    'stderr-os-closed',
                    'post-stderr-closure-alive',
                    'stdout-write1-after-stderr-closure',
                    'stdout-write2-after-stderr-closure',
                    'stdout-os-closed',
                    'both-os-handles-closed',
                ),
            ),
            "stderr-after-stdout": (
                b"OUT",
                (
                    'stdout-payload-written',
                    'stdout-os-closed',
                    'post-stdout-closure-alive',
                    'stderr-write1-after-stdout-closure',
                    'stderr-write2-after-stdout-closure',
                    'stderr-os-closed',
                    'both-os-handles-closed',
                ),
            ),
        }
        for mode, (expected_output, expected_witness) in successful.items():
            with self.subTest(mode=mode):
                result = self.run_bounded_process(mode, timeout_milliseconds=3000)
                self.assertTrue(result["success"], result)
                self.assertFalse(result["timedout"], result)
                self.assertFalse(result["overflow"], result)
                self.assertEqual(result["length"], len(expected_output))
                self.assertEqual(result["sha256"], hashlib.sha256(expected_output).hexdigest())
                self.assertEqual(result["witness"], list(expected_witness))

        for mode in ("timeout", "both-closed-alive"):
            with self.subTest(timeout_mode=mode):
                started = time.monotonic()
                timeout_milliseconds = 1500 if mode == "both-closed-alive" else 500
                result = self.run_bounded_process(mode, timeout_milliseconds=timeout_milliseconds)
                elapsed = time.monotonic() - started
                self.assertFalse(result["success"], result)
                self.assertTrue(result["timedout"], result)
                self.assertFalse(result["overflow"], result)
                self.assertEqual(result["length"], 0)
                self.assertLess(
                    elapsed,
                    3.0,
                    "the bounded pump must not wait for the intentionally live child after stream EOF",
                )
                if mode == "both-closed-alive":
                    self.assertEqual(
                        result["witness"],
                        [
                            'stdout-payload-written',
                            'stderr-payload-written',
                            'stdout-os-closed',
                            'stderr-os-closed',
                            'both-os-handles-closed',
                            'post-both-closure-alive',
                        ],
                    )
                else:
                    self.assertEqual(result["witness"], [])

        for mode in ("stdout-overflow", "stderr-overflow"):
            with self.subTest(overflow_mode=mode):
                result = self.run_bounded_process(mode, timeout_milliseconds=2000)
                self.assertFalse(result["success"], result)
                self.assertTrue(result["overflow"], result)
                self.assertEqual(result["length"], 0)

        nonzero = self.run_bounded_process("nonzero", timeout_milliseconds=3000)
        self.assertFalse(nonzero["success"], nonzero)
        self.assertEqual(nonzero["exit"], 7)
        self.assertEqual(nonzero["length"], 0)

        not_started = self.run_bounded_process("not-started", timeout_milliseconds=500)
        self.assertFalse(not_started["started"], not_started)
        self.assertFalse(not_started["success"], not_started)
        self.assertEqual(not_started["length"], 0)

    def test_completed_read_fault_cancel_and_inactive_states_are_fail_closed(self):
        values = self.run_fault_cancel_matrix()
        self.assertFalse(values["fault_success"])
        self.assertTrue(values["faulted"])
        self.assertFalse(values["cancel_success"])
        self.assertTrue(values["cancelled"])
        self.assertTrue(values["inactive_invariant"])

    def test_acl_primitive_mutation_behavioural_matrix(self):
        values = self.run_acl_matrix()
        for name in ("Read", "ReadAndExecute", "Synchronize"):
            with self.subTest(accepted=name):
                self.assertFalse(values[name])
        for name in (
            "WriteData",
            "AppendData",
            "WriteExtendedAttributes",
            "DeleteSubdirectoriesAndFiles",
            "WriteAttributes",
            "Delete",
            "ChangePermissions",
            "TakeOwnership",
            "Write",
            "Modify",
            "FullControl",
        ):
            with self.subTest(rejected=name):
                self.assertTrue(values[name])
        expected_raw = {
            "GenericRead": False,
            "GenericExecute": False,
            "GenericReadExecute": False,
            "GenericWrite": True,
            "GenericReadWrite": True,
            "GenericAll": True,
            "HighPrimitive": True,
        }
        for name, expected in expected_raw.items():
            with self.subTest(raw_mask=name):
                self.assertEqual(values[name], expected)

    def test_protected_root_creator_owner_inheritonly_behavioural_matrix(self):
        values = self.run_protected_root_acl_matrix()
        expected = {
            "CreatorOwnerGenericAllInheritOnly": True,
            "CreatorOwnerGenericAllNone": False,
            "BuiltinUsersGenericAllInheritOnly": False,
            "EveryoneWriteDataNone": False,
            "EveryoneWriteDataInheritOnly": False,
            "AuthenticatedUsersGenericWriteInheritOnly": False,
            "CreatorOwnerReadAndExecuteNone": True,
        }
        self.assertEqual(values, expected)

    def test_strict_json_reader_behavioural_matrix(self):
        depth = 33
        cases = {
            "valid": b'{"name":"grid","nested":{"enabled":true},"items":[1,null]}',
            "empty_object": b"{}",
            "malformed": b'{"a":}',
            "empty": b"",
            "whitespace_only": b" \r\n\t ",
            "invalid_utf8": b'{"a":"\xff"}',
            "bom": b"\xef\xbb\xbf{" + b'"a":1}',
            "top_array": b"[1,2]",
            "scalar": b"1",
            "null_root": b"null",
            "exact_duplicate": b'{"a":1,"a":2}',
            "case_duplicate": b'{"A":1,"a":2}',
            "escaped_duplicate": b'{"a":1,"\\u0061":2}',
            "nested_scope_valid": b'{"outer":{"a":1},"other":{"a":2},"a":3}',
            "nested_scope_duplicate": b'{"outer":{"a":1,"a":2}}',
            "trailing_garbage": b'{"a":1}x',
            "concatenated": b'{"a":1}{"b":2}',
            "oversize": b'{"x":"' + (b"a" * 65530) + b'"}',
            "depth_over_32": (b'{"a":' * depth) + b"0" + (b"}" * depth),
        }
        results = self.run_json_matrix(cases)
        self.assertEqual(set(results), set(cases))
        self.assertEqual(results["valid"]["kind"], "object")
        self.assertEqual(results["valid"]["count"], 3)
        self.assertEqual(results["empty_object"]["kind"], "object")
        self.assertEqual(results["empty_object"]["count"], 0)
        self.assertEqual(results["nested_scope_valid"]["kind"], "object")
        self.assertEqual(results["nested_scope_valid"]["count"], 3)
        for name in cases:
            if name not in {"valid", "empty_object", "nested_scope_valid"}:
                with self.subTest(json_case=name):
                    self.assertEqual(results[name]["kind"], "null")



if __name__ == "__main__":
    unittest.main()
