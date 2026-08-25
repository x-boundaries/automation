# Energy@Grid runtime launcher library.
#
# Design lock: DL-XB-141-RUNTIME-005-SOURCE-DURABILITY.
# Controlling specification: energygrid-bill-downloader/docs/runtime_source_durability_design.md
# Controlling plan: energygrid-bill-downloader/docs/runtime_source_durability_implementation_plan.md
#
# This file is a PURE, DOT-SOURCEABLE LIBRARY (EGRT-I01). It performs no filesystem
# mutation at import time, no network access, no Git invocation at load, and no live
# action. Every function is deterministic given its inputs and its explicitly supplied
# paths, which is what makes the offline test suite possible. It mirrors the separation
# already established by scripts/member_create_uat_runner_lib.ps1.
#
# The dependency direction is one-way: entry scripts dot-source this library, and this
# library never dot-sources an entry script and never reads a caller's $PSScriptRoot.
#
# Compatibility boundary: every construct here must execute correctly under Windows
# PowerShell 5.1 on .NET Framework 4.x (PSEdition Desktop), which is the production
# runtime. The prohibited PowerShell 7 only constructs are guarded statically by the
# project test suite.
#
# Nothing private is committed here. No credential value, account identity, private
# absolute path, UNC path, host identity, or principal identity appears in this file.

Set-StrictMode -Version Latest

# --------------------------------------------------------------------------------------
# Native no-replace publication interop (design section 7.4)
# --------------------------------------------------------------------------------------
# Windows MoveFileExW WITHOUT MOVEFILE_REPLACE_EXISTING is the primitive that fails rather
# than overwrites if the destination has appeared in the meantime. It is the same
# no-replace move discipline the application already uses when publishing a validated PDF
# (energygrid_bill_downloader/publication.py) and the AutoCount capability probe
# (scripts/member_expiry_capability_probe_lib.ps1).
#
# This is a TYPE DEFINITION emitted once, guarded by a type-presence test so a second
# dot-source in the same session does not throw a duplicate-type error. It performs no
# filesystem action, so the pure-library rule in EGRT-I01 is preserved.
$script:EgNativePublicationInteropSource = @'
using System;
using System.Runtime.InteropServices;

namespace EgRuntime {
    public class NativeMoveResult {
        public bool Ok;
        public int LastError;
    }

    public static class NativePublication {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "MoveFileExW")]
        private static extern bool MoveFileExW(string lpExistingFileName, string lpNewFileName, uint dwFlags);

        // Write-through ONLY. Replacement, cross-volume copying, and reboot-delayed
        // scheduling are never requested, so the rename is no-replace, same-volume, and
        // synchronous through the documented write-through completion boundary. A losing
        // race is reported to the caller, never resolved by replacing.
        private const uint MOVEFILE_WRITE_THROUGH = 0x00000008;

        public static NativeMoveResult MoveNoReplaceWriteThrough(string source, string destination) {
            NativeMoveResult result = new NativeMoveResult();
            result.Ok = MoveFileExW(source, destination, MOVEFILE_WRITE_THROUGH);
            result.LastError = 0;
            if (result.Ok == false) {
                result.LastError = Marshal.GetLastWin32Error();
            }
            return result;
        }
    }
}
'@

if (-not ([System.Management.Automation.PSTypeName]'EgRuntime.NativePublication').Type) {
    Add-Type -TypeDefinition $script:EgNativePublicationInteropSource
}

function Get-EgLauncherLibraryContract {
    # The library's own bounded self-description. Pure; no side effect.
    [CmdletBinding()]
    param()

    [pscustomobject]@{
        SchemaVersion       = 'eg_launcher_lib/v1'
        DeployedMemberNames = @('launcher.ps1', 'launcher_lib.ps1', 'installation_manifest.json')
    }
}

# --------------------------------------------------------------------------------------
# Hashing and bounded exception classification
# --------------------------------------------------------------------------------------

function Get-EgFileSha256 {
    # Lowercase hexadecimal SHA-256 of a file, or the empty string when the path does not
    # exist as a file. Every hash in this library is normalised the same way, so a hash
    # comparison never depends on the casing a provider happened to return.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ''
    }
    try {
        $computed = Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop
        return $computed.Hash.ToLowerInvariant()
    }
    catch {
        # The file exists but could not be opened for reading, which is what a destination
        # held with an exclusive share looks like. This function must not throw, because
        # both publish primitives are contractually required to RETURN a structured result
        # rather than raise (design section 7.1), and a primitive that threw here would
        # never surface the real File.Replace exception type and HRESULT.
        #
        # The empty string is a fail-closed signal, not a swallowed failure: it can never
        # equal an expected 64-character hash, so every verification that consumes it
        # fails. It is also never mistaken for a matching preimage, because an unreadable
        # preimage compares equal only to an equally unreadable postimage, which is
        # exactly the case where the destination provably did not advance.
        return ''
    }
}

function Get-EgUnderlyingException {
    # Unwrap the exception PowerShell wraps around a failed .NET method invocation, so
    # classification sees the real type and HRESULT rather than the wrapper's.
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)

    $candidate = $Exception
    while ($candidate -is [System.Management.Automation.MethodInvocationException]) {
        if ($null -eq $candidate.InnerException) {
            break
        }
        $candidate = $candidate.InnerException
    }
    return $candidate
}

function Get-EgExceptionHResultString {
    # The HRESULT in exact 0x%08X form. Never any message text (design section 11.2).
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)

    return [string]::Format('0x{0:X8}', $Exception.HResult)
}

function Get-EgReplaceSupportRef {
    # Map a replacement failure to its bounded support reference by exception TYPE and
    # HRESULT ONLY. Exception message text is never read, and no control-flow decision
    # anywhere in this library is made by reading it (design section 7.2 rule 5).
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Exception]$Exception)

    if ($Exception -is [System.UnauthorizedAccessException]) {
        return 'EG_LAUNCHER_REPLACE_ACCESS_DENIED'
    }
    if ($Exception -is [System.ArgumentException]) {
        return 'EG_LAUNCHER_REPLACE_ARGUMENT_INVALID'
    }
    if ($Exception -is [System.IO.IOException]) {
        if ((Get-EgExceptionHResultString -Exception $Exception) -eq '0x80070020') {
            return 'EG_LAUNCHER_REPLACE_SHARING_VIOLATION'
        }
    }
    return 'EG_LAUNCHER_UNCLASSIFIED'
}

# --------------------------------------------------------------------------------------
# Publication result shape (design section 7.1)
# --------------------------------------------------------------------------------------

function New-EgPublicationResult {
    # Internal factory. Guarantees all ten PublicationResult fields are ALWAYS present,
    # in a fixed order, on every path including failure. ExceptionTypeName and HResult are
    # the empty string when no exception occurred; there is no RolledBack field, because
    # neither publish primitive ever rolls back.
    [CmdletBinding()]
    param(
        [bool]$Success,
        [AllowEmptyString()][string]$SupportRef = '',
        [AllowEmptyString()][string]$ExceptionTypeName = '',
        [AllowEmptyString()][string]$HResult = '',
        [Parameter(Mandatory)][ValidateSet('Existing', 'Absent')][string]$PreimageState,
        [AllowEmptyString()][string]$PreimageSha256 = '',
        [AllowEmptyString()][string]$PostimageSha256 = '',
        [bool]$PublicationOccurred,
        [bool]$BackupCreated,
        [bool]$BackupRetained
    )

    [pscustomobject]@{
        Success             = $Success
        SupportRef          = $SupportRef
        ExceptionTypeName   = $ExceptionTypeName
        HResult             = $HResult
        PreimageState       = $PreimageState
        PreimageSha256      = $PreimageSha256
        PostimageSha256     = $PostimageSha256
        PublicationOccurred = $PublicationOccurred
        BackupCreated       = $BackupCreated
        BackupRetained      = $BackupRetained
    }
}

function Test-EgSameDirectory {
    # Whether two paths resolve to the same containing directory. The same-directory rule
    # is stronger than the same-volume requirement ReplaceFileW imposes, and is enforced
    # because it is mechanically checkable (design section 7.2 rule 1).
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$FirstPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SecondPath
    )

    $firstDirectory = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($FirstPath))
    $secondDirectory = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($SecondPath))
    return ([string]::Equals($firstDirectory, $secondDirectory, [System.StringComparison]::OrdinalIgnoreCase))
}

function Invoke-AtomicFileReplace {
    # Publish a staging file over an EXISTING destination, through
    # [System.IO.File]::Replace with a mandatory explicit same-directory backup path.
    #
    # This primitive NEVER rolls back and NEVER deletes a backup. It publishes and
    # reports; restoring a preimage is the installer transaction's exclusive
    # responsibility (design sections 6.5 and 7.2 rules 3 and 4), because only the
    # installer knows which other package members have already advanced and in what order
    # they must be undone.
    #
    # The mandatory, non-empty -BackupPath is the structural closure of the Run119
    # null-backup defect (design section 18.2). The prohibition does not rely on any
    # runtime rejecting a null argument, because the parameter contract refuses it first.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$BackupPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedSha256
    )

    # Step 1. A backup path outside the destination directory is refused before any call
    # reaches the filesystem, so the destination is byte-identical afterwards.
    if (-not (Test-EgSameDirectory -FirstPath $BackupPath -SecondPath $DestinationPath)) {
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_REPLACE_ARGUMENT_INVALID' `
            -PreimageState 'Existing' `
            -PreimageSha256 (Get-EgFileSha256 -Path $DestinationPath) `
            -PostimageSha256 (Get-EgFileSha256 -Path $DestinationPath) `
            -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
    }

    # Step 2. The preimage hash is recorded before anything is attempted. It is what makes
    # the two failure states of design section 7.2.1 decidable from observed state.
    $preimageSha = Get-EgFileSha256 -Path $DestinationPath

    $publicationOccurred = $false
    $exceptionTypeName = ''
    $hresult = ''
    $supportRef = ''
    $threw = $false

    try {
        [System.IO.File]::Replace($SourcePath, $DestinationPath, $BackupPath)
        # Set immediately after the call returns and before any verification work.
        $publicationOccurred = $true
    }
    catch {
        $threw = $true
        $underlying = Get-EgUnderlyingException -Exception $_.Exception
        $exceptionTypeName = $underlying.GetType().FullName
        $hresult = Get-EgExceptionHResultString -Exception $underlying
        $supportRef = Get-EgReplaceSupportRef -Exception $underlying
        # Design section 7.2.1 determination: the call threw, so the destination advanced
        # only if its observed hash no longer equals the recorded preimage.
        if ((Get-EgFileSha256 -Path $DestinationPath) -ne $preimageSha) {
            $publicationOccurred = $true
        }
    }

    # Observed on disk, never inferred from the requested path: supplying a backup path is
    # not evidence that a backup file exists.
    $backupCreated = (Test-Path -LiteralPath $BackupPath -PathType Leaf)
    $postimageSha = Get-EgFileSha256 -Path $DestinationPath

    if (-not $threw) {
        # Step 6. Success is positively verified, never assumed. A returned call is not a
        # successful replacement until the destination hash matches.
        if ($postimageSha -ceq $ExpectedSha256) {
            return New-EgPublicationResult -Success $true `
                -PreimageState 'Existing' -PreimageSha256 $preimageSha `
                -PostimageSha256 $postimageSha -PublicationOccurred $true `
                -BackupCreated $backupCreated -BackupRetained $backupCreated
        }
        # Case B: the destination advanced and verification failed. The backup is retained
        # and the primitive performs no self-rollback.
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_REPLACE_POSTIMAGE_MISMATCH' `
            -PreimageState 'Existing' -PreimageSha256 $preimageSha `
            -PostimageSha256 $postimageSha -PublicationOccurred $true `
            -BackupCreated $backupCreated -BackupRetained $backupCreated
    }

    return New-EgPublicationResult -Success $false -SupportRef $supportRef `
        -ExceptionTypeName $exceptionTypeName -HResult $hresult `
        -PreimageState 'Existing' -PreimageSha256 $preimageSha `
        -PostimageSha256 $postimageSha -PublicationOccurred $publicationOccurred `
        -BackupCreated $backupCreated -BackupRetained $backupCreated
}

function Test-EgPowerShellFileParsesCleanly {
    # Whether a .ps1 file parses with zero errors. Read-only; the file is never executed,
    # dot-sourced, or imported by this check.
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $parseErrors = $null
    $parseTokens = $null
    try {
        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $Path, [ref]$parseTokens, [ref]$parseErrors)
    }
    catch {
        return $false
    }
    if ($null -eq $parseErrors) {
        return $true
    }
    return (@($parseErrors).Count -eq 0)
}

function Invoke-EgNoReplaceMove {
    # Same-directory move that will NOT clobber. Returns the observed native outcome; the
    # caller decides what a failure means.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath
    )

    $native = [EgRuntime.NativePublication]::MoveNoReplaceWriteThrough($SourcePath, $DestinationPath)
    [pscustomobject]@{
        Ok        = $native.Ok
        LastError = [int]$native.LastError
    }
}

function Invoke-PublishToAbsentDestination {
    # Publish a staging file to a destination whose ABSENCE has been positively
    # established. This is the clean-first-install path and it never calls
    # [System.IO.File]::Replace, which requires the destination to already exist.
    #
    # There is no backup, because there was nothing to back up. This primitive never rolls
    # back: undoing a publication is the installer transaction's responsibility, and means
    # returning the destination to absence (design section 7.4 step 6).
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$SourcePath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$DestinationPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedSha256
    )

    # Step 1. Absence is established first, not assumed. An unexpectedly present
    # destination file means the caller's preimage classification was wrong, and that is
    # terminal rather than something to recover from by overwriting.
    if (Test-Path -LiteralPath $DestinationPath -PathType Leaf) {
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_PUBLISH_DESTINATION_UNEXPECTEDLY_PRESENT' `
            -PreimageState 'Absent' `
            -PostimageSha256 (Get-EgFileSha256 -Path $DestinationPath) `
            -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
    }

    # Step 2. The staging file is complete before publication: it exists, parses cleanly
    # when it is a PowerShell file, and hashes to the expected value. Nothing partially
    # written is ever published. A staging file that fails any of these is a staging
    # failure, reported as such rather than attempted.
    $stagingOk = (Test-Path -LiteralPath $SourcePath -PathType Leaf)
    if ($stagingOk) {
        if ([System.IO.Path]::GetExtension($SourcePath) -ieq '.ps1') {
            $stagingOk = (Test-EgPowerShellFileParsesCleanly -Path $SourcePath)
        }
    }
    if ($stagingOk) {
        $stagingOk = ((Get-EgFileSha256 -Path $SourcePath) -ceq $ExpectedSha256)
    }
    if (-not $stagingOk) {
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_INSTALL_STAGING_FAILED' `
            -PreimageState 'Absent' `
            -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
    }

    # Step 3. A same-directory move that will not clobber. A losing race is reported,
    # never resolved by replacing.
    $moved = Invoke-EgNoReplaceMove -SourcePath $SourcePath -DestinationPath $DestinationPath
    if (-not $moved.Ok) {
        if (Test-Path -LiteralPath $DestinationPath) {
            return New-EgPublicationResult -Success $false `
                -SupportRef 'EG_LAUNCHER_PUBLISH_RACE_LOST' `
                -PreimageState 'Absent' `
                -PostimageSha256 (Get-EgFileSha256 -Path $DestinationPath) `
                -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
        }
        return New-EgPublicationResult -Success $false `
            -SupportRef 'EG_LAUNCHER_PUBLISH_POSTIMAGE_MISMATCH' `
            -PreimageState 'Absent' `
            -PublicationOccurred $false -BackupCreated $false -BackupRetained $false
    }

    # Step 4. The postimage is verified. Publication is not treated as successful until
    # the destination hash matches.
    $postimageSha = Get-EgFileSha256 -Path $DestinationPath
    if ($postimageSha -ceq $ExpectedSha256) {
        return New-EgPublicationResult -Success $true `
            -PreimageState 'Absent' -PostimageSha256 $postimageSha `
            -PublicationOccurred $true -BackupCreated $false -BackupRetained $false
    }
    return New-EgPublicationResult -Success $false `
        -SupportRef 'EG_LAUNCHER_PUBLISH_POSTIMAGE_MISMATCH' `
        -PreimageState 'Absent' -PostimageSha256 $postimageSha `
        -PublicationOccurred $true -BackupCreated $false -BackupRetained $false
}

# --------------------------------------------------------------------------------------
# Process-scope environment snapshot and exact restoration
# --------------------------------------------------------------------------------------

# The two credential variable names the application reads from its environment. Declared
# once here because Restore-EgProcessEnvironmentSnapshot needs them to select its bounded
# support reference; the credential import path consumes the same constant.
$script:EgCredentialVariableNames = @('ENERGYGRID_USERNAME', 'ENERGYGRID_PASSWORD')

function Get-EgProcessEnvironmentSnapshot {
    # Record the exact process-scope state of the named variables.
    #
    # Present is $false for a variable that does not exist, which is a DIFFERENT state
    # from a variable that exists and holds an empty string. Restoration depends on the
    # distinction (design sections 9.2 and 10.2).
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Names)

    $snapshot = [ordered]@{}
    foreach ($name in $Names) {
        $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
        $snapshot[$name] = [pscustomobject]@{
            Present = ($null -ne $observed)
            Value   = ([string]$observed)
        }
    }
    return $snapshot
}

function Restore-EgProcessEnvironmentSnapshot {
    # Restore a snapshot EXACTLY. A name recorded Present $false is REMOVED, never set to
    # an empty string. A failed restoration is terminal rather than silent.
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Snapshot)

    $checks = [ordered]@{}
    $pass = $true
    $supportRef = ''

    $names = @($Snapshot.Keys)
    foreach ($name in $names) {
        $entry = $Snapshot[$name]
        $desired = $null
        if ($entry.Present) {
            $desired = $entry.Value
        }
        try {
            [System.Environment]::SetEnvironmentVariable($name, $desired, 'Process')
            $observed = [System.Environment]::GetEnvironmentVariable($name, 'Process')
            if ($entry.Present) {
                if ($null -eq $observed -or $observed -ne $entry.Value) {
                    $pass = $false
                }
            }
            else {
                if ($null -ne $observed) {
                    $pass = $false
                }
            }
        }
        catch {
            $pass = $false
        }
    }

    if (-not $pass) {
        $supportRef = 'EG_LAUNCHER_UNCLASSIFIED'
        foreach ($credentialName in $script:EgCredentialVariableNames) {
            if ($names -contains $credentialName) {
                $supportRef = 'EG_LAUNCHER_CREDENTIAL_RESTORE_FAILED'
            }
        }
    }

    $outcome = 'FAIL'
    if ($pass) { $outcome = 'PASS' }
    $checks['process_environment_restored'] = $outcome

    [pscustomobject]@{
        Pass       = $pass
        SupportRef = $supportRef
        Checks     = $checks
    }
}

# --------------------------------------------------------------------------------------
# Governed Git invocation (design sections 10.1, 10.2, 10.3)
# --------------------------------------------------------------------------------------

function Get-EgGovernedGitEnvironmentNames {
    # The exact nineteen ambient Git variables design section 10.2 names, in a fixed
    # order. Each is neutralised before a governed read and exactly restored afterwards.
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

# The exhaustive READ-ONLY Git subcommand allowlist (design section 10.3). Every Git
# operation that fetches, updates, moves, or rewrites state is prohibited outright: the
# runtime layer performs zero Git network operations and zero Git state mutations.
# Bringing the deployed checkout to a newer source revision is a separately controlled
# operator or repository action, never something the unattended job does to itself.
$script:EgGitAllowedSubcommands = @('rev-parse', 'symbolic-ref', 'ls-files', 'diff', 'status')

function Get-EgGitAllowedSubcommands {
    # The allowlist, enumerated explicitly.
    [CmdletBinding()]
    param()

    @($script:EgGitAllowedSubcommands)
}

function Test-EgGitSubcommandAllowed {
    # Exact, case-sensitive membership of the allowlist. A prefix, pattern, or
    # case-insensitive match is deliberately NOT accepted.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Subcommand)

    foreach ($allowed in $script:EgGitAllowedSubcommands) {
        if ($allowed -ceq $Subcommand) {
            return $true
        }
    }
    return $false
}

function ConvertTo-EgNativeArgumentString {
    # Build a Windows command line from an argument vector.
    #
    # ProcessStartInfo.ArgumentList does not exist on .NET Framework 4.x, so the argument
    # string is composed here rather than delegated to the runtime. Every argument is
    # quoted and any trailing backslash run is doubled, which is the documented Windows
    # command-line rule and is what keeps a path ending in a separator from escaping the
    # closing quote. A double quote inside an argument is not produced by any caller in
    # this library and is not accommodated by a fallback.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Argument)

    $parts = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $Argument) {
        $trailingBackslashes = 0
        for ($index = $item.Length - 1; $index -ge 0; $index--) {
            if ($item[$index] -eq '\') {
                $trailingBackslashes++
            }
            else {
                break
            }
        }
        $escaped = $item
        if ($trailingBackslashes -gt 0) {
            $escaped = $item + ('\' * $trailingBackslashes)
        }
        $parts.Add('"' + $escaped + '"')
    }
    return ($parts -join ' ')
}

function Split-EgProcessOutputLines {
    # Split captured standard output into lines, dropping trailing empty entries.
    #
    # PowerShell unrolls a collection returned from a function, so this function emits its
    # lines and EVERY call site forces the collection shape with the array subexpression.
    # Pipeline scalar collapse is the direct cause of the Run119 output-shape defect
    # (design section 18.1): a one-line result must not collapse to a bare string and a
    # zero-line result must not collapse to $null.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][AllowNull()][string]$Text)

    if ([string]::IsNullOrEmpty($Text)) {
        return
    }

    $split = $Text -split "`r`n|`n"
    $last = $split.Length - 1
    while ($last -ge 0 -and $split[$last] -eq '') {
        $last--
    }
    if ($last -lt 0) {
        return
    }
    return $split[0..$last]
}

function Invoke-GovernedGit {
    # The single governed Git read. Returns a GovernedGitResult and never throws for a
    # non-zero Git exit.
    #
    # Standard output and standard error are captured through System.Diagnostics.Process
    # with redirected streams. Native stderr is never merged inline, because on Windows
    # PowerShell 5.1 that wraps each line in an ErrorRecord and falsifies the success
    # variable even for a process that exited zero (design section 10.1).
    #
    # Raw Git output text never reaches a log, a console summary, or any result field
    # other than Lines, and Lines is consumed by this library rather than emitted.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$RepositoryRootPath,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]]$Arguments
    )

    # The allowlist gate runs first and starts no process. A prohibited subcommand can
    # therefore never reach the filesystem or the network, whatever else the caller passed.
    if (-not (Test-EgGitSubcommandAllowed -Subcommand $Arguments[0])) {
        return [pscustomobject]@{
            Success    = $false
            ExitCode   = [int](-1)
            Lines      = [string[]]@()
            SupportRef = 'EG_LAUNCHER_GIT_SUBCOMMAND_FORBIDDEN'
        }
    }

    $fullArguments = @('-C', $RepositoryRootPath, '--no-optional-locks') + $Arguments

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'git'
    $startInfo.Arguments = ConvertTo-EgNativeArgumentString -Argument $fullArguments
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    # Ambient Git variables are neutralised for the duration of the read and restored
    # exactly in the finally block. The repository is selected explicitly with -C, and
    # --no-optional-locks keeps a governed read from mutating the repository.
    $governedNames = @(Get-EgGovernedGitEnvironmentNames)
    $environmentSnapshot = Get-EgProcessEnvironmentSnapshot -Names $governedNames

    $exitCode = -1
    $standardOutput = ''
    $process = $null
    try {
        foreach ($governedName in $governedNames) {
            [System.Environment]::SetEnvironmentVariable($governedName, $null, 'Process')
        }
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        [void]$process.Start()
        # Both streams are drained asynchronously before the wait, so neither can fill its
        # buffer and deadlock the child.
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $standardOutput = $outputTask.Result
        # The captured error stream is drained and discarded. It is deliberately never
        # placed in the result object, a log, or a console surface (design section 11.2).
        [void]$errorTask.Result
        $exitCode = $process.ExitCode
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
        [void](Restore-EgProcessEnvironmentSnapshot -Snapshot $environmentSnapshot)
    }

    $lines = @(Split-EgProcessOutputLines -Text $standardOutput)
    $supportRef = ''
    if ($exitCode -ne 0) {
        $supportRef = 'EG_LAUNCHER_GIT_INVOCATION_FAILED'
    }

    [pscustomobject]@{
        Success    = ($exitCode -eq 0)
        ExitCode   = [int]$exitCode
        Lines      = [string[]]$lines
        SupportRef = $supportRef
    }
}
