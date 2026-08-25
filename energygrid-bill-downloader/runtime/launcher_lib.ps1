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
