# Energy@Grid runtime launcher entry script.
#
# Design lock: DL-XB-141-RUNTIME-005-SOURCE-DURABILITY.
# Controlling specification: energygrid-bill-downloader/docs/runtime_source_durability_design.md
#
# This script starts the reviewed Python application on the production host. It is the only
# thing the Scheduled Task will ever invoke, once scheduling is separately approved.
#
# THE LAUNCHER ROOT IS THIS SCRIPT'S OWN DIRECTORY. Package-member paths are fixed joins of
# that directory with the three Class A names. The root is enumerated only to CLASSIFY;
# recognised residue is never dot-sourced, invoked, imported, or selected as a fallback, and
# can never satisfy a missing package member.
#
# There is no parameter that accepts a credential value, no portal parameter, no
# browser-install parameter, no commit-pin parameter, and STILL no headed switch. Headed
# execution is an implicit and non-overridable property of the fixed 'login-diagnostic'
# operation only; it is not a mode any caller can select, and 'run' and 'list' are
# unaffected. The fixed 'download-preflight-diagnostic' operation is always headless and
# never dispatches a Download (DL-XB-199 G2-083).
# -Command is a closed allowlist of four fixed operation names, and the child argument
# vector stays a fixed five elements with nothing appended conditionally. Two
# parameters are mandatory specifically so that omitting an argument can never silently
# disable a security expectation: -ExpectedBranch takes the literal ANY_BRANCH sentinel
# rather than being optional, and -AuthorisedLauncherRootWriteSid is the only route by
# which the authorised write-trustee set reaches the launcher.
#
# The Python child's standard streams are redirected, captured in full, and relayed
# verbatim to this launcher's own standard output and standard error after the child
# completes, so a redirected caller receives the application's own output. The launcher
# stays application-output agnostic: it never parses, filters, reshapes, or interprets a
# single byte of that output, and it never varies the relay by -Command.
#
# Nothing private is committed here. No credential value, account identity, private absolute
# path, UNC path, host identity, or principal identity appears in this file.

[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ConfigPath,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$PythonExe,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CheckoutRoot,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$CredentialPath,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$BrowserCachePath,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$ExpectedBranch,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]]$AuthorisedLauncherRootWriteSid,
    [ValidateSet('run', 'list', 'login-diagnostic', 'download-preflight-diagnostic')][string]$Command = 'run',
    [string]$LogRoot,
    [switch]$ValidateOnly,
    [string]$RunId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# The ONLY dot-source in this script, and a fixed join of this script's own directory with
# the fixed library name. The launcher never enumerates the root to locate a library.
. (Join-Path $PSScriptRoot 'launcher_lib.ps1')

$launcherRoot = $PSScriptRoot

# --------------------------------------------------------------------------------------
# Ordered preflight state
# --------------------------------------------------------------------------------------
# The twenty-one stable check names, in the exact order design section 5.2 evaluates them.
# Positions 1 to 18 are the NON-SECRET preflight. Every one of them completes before
# position 19 is attempted, so credential import is literally the last check and a run that
# will fail for any other reason never opens the credential artefact at all. That ordering
# is a security property, not a performance preference.
$script:EgOrderedCheckNames = @(
    'launcher_root_entries_classified',
    'launcher_package_members_present',
    'launcher_package_parse_clean',
    'launcher_package_manifest_match',
    'checkout_root_exists_absolute',
    'config_path_exists_absolute',
    'python_exe_exists_absolute',
    'config_path_outside_checkout',
    'config_parses_json',
    'config_required_keys_present',
    'python_version_is_3_14',
    'governed_source_integrity',
    'browser_cache_ready',
    'launcher_root_outside_checkout',
    'launcher_root_not_writable_by_run_principal',
    'launcher_root_write_trustees_authorised',
    'launcher_files_not_reparse_points',
    'launcher_files_not_unexpectedly_readonly',
    'credential_import_ok',
    'username_nonempty',
    'password_nonempty'
)

# Every position starts FAIL and is promoted only by an evaluation that passed. A position
# that was never reached therefore reports FAIL rather than PASS, which is the fail-closed
# direction: an unevaluated security expectation is never reported as satisfied.
$script:EgChecks = [ordered]@{}
foreach ($name in $script:EgOrderedCheckNames) {
    $script:EgChecks[$name] = 'FAIL'
}

$script:EgFirstFailure = ''
$script:EgFirstFailureRef = ''

function Set-EgCheckOutcome {
    param(
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Name,
        [bool]$Pass,
        [AllowEmptyString()][string]$SupportRef = ''
    )

    if ($Pass) {
        $script:EgChecks[$Name] = 'PASS'
        return
    }
    $script:EgChecks[$Name] = 'FAIL'
    if ([string]::IsNullOrEmpty($script:EgFirstFailure)) {
        $script:EgFirstFailure = $Name
        $script:EgFirstFailureRef = $SupportRef
        if ([string]::IsNullOrEmpty($script:EgFirstFailureRef)) {
            $script:EgFirstFailureRef = 'EG_LAUNCHER_UNCLASSIFIED'
        }
    }
}

function Merge-EgCheckResult {
    # Fold a predicate's CheckResult into the ordered preflight map, promoting only the
    # positions it actually evaluated as PASS.
    param(
        [Parameter(Mandatory)]$Result,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Positions
    )

    foreach ($position in $Positions) {
        $outcome = 'FAIL'
        if ($Result.Checks.Contains($position)) {
            $outcome = [string]$Result.Checks[$position]
        }
        Set-EgCheckOutcome -Name $position -Pass ($outcome -ceq 'PASS') -SupportRef $Result.SupportRef
    }
}

function Test-EgPreflightShouldContinue {
    # The REAL path is fail-fast: design section 5.2 states the launcher never continues
    # past a failed check. A validation run continues through the whole non-secret block so
    # the operator sees every outcome at once, which is what makes the emitted checks map a
    # complete report rather than a truncated one. It never reaches the credential import
    # unless every non-secret position passed.
    if ([string]::IsNullOrEmpty($script:EgFirstFailure)) {
        return $true
    }
    return [bool]$ValidateOnly
}

function Exit-EgLauncher {
    param([int]$ExitCode)

    if ($ValidateOnly) {
        $status = 'PASS'
        $supportRef = ''
        if (-not [string]::IsNullOrEmpty($script:EgFirstFailure)) {
            $status = 'FAIL'
            $supportRef = $script:EgFirstFailureRef
        }
        Write-Output (ConvertTo-EgValidationJson -Checks $script:EgChecks -Status $status `
            -SupportRef $supportRef)
    }
    exit $ExitCode
}

function Exit-EgPreflightFailure {
    # The terminal event is written on the REAL path only. A validation run creates,
    # modifies, deletes, and renames nothing, and design section 8 names a log file
    # explicitly among the things it must not create, so validation emits its bounded
    # JSON document and nothing else.
    if (-not $ValidateOnly) {
        Write-EgLauncherTerminalEvent -LogRoot ([string]$LogRoot) -RunId ([string]$RunId) `
            -Phase 'preflight' -Status 'FAILED' -SupportRef $script:EgFirstFailureRef
    }
    Exit-EgLauncher -ExitCode $script:EgLauncherExitCodes['PreflightFailed']
}

# --------------------------------------------------------------------------------------
# Positions 1 to 4 - launcher-root integrity
# --------------------------------------------------------------------------------------
# If the package itself is invalid the run fails regardless of how residue classified.

$classification = Get-EgLauncherRootClassification -LauncherRootPath $launcherRoot
$classAOk = ((@($classification.ClassC).Count -eq 0))
Set-EgCheckOutcome -Name 'launcher_root_entries_classified' -Pass $classAOk `
    -SupportRef 'EG_LAUNCHER_ROOT_UNEXPECTED_ENTRY'
Set-EgCheckOutcome -Name 'launcher_package_members_present' `
    -Pass ((@($classification.MissingMembers).Count -eq 0)) `
    -SupportRef 'EG_LAUNCHER_PACKAGE_MEMBER_MISSING'

if (Test-EgPreflightShouldContinue) {
    $parseClean = $true
    foreach ($executableName in @('launcher.ps1', 'launcher_lib.ps1')) {
        if (-not (Test-EgPowerShellFileParsesCleanly -Path (Join-Path $launcherRoot $executableName))) {
            $parseClean = $false
        }
    }
    Set-EgCheckOutcome -Name 'launcher_package_parse_clean' -Pass $parseClean `
        -SupportRef 'EG_LAUNCHER_PACKAGE_PARSE_FAILED'
}

if (Test-EgPreflightShouldContinue) {
    $manifestMatch = Compare-EgInstalledPackageToManifest -LauncherRootPath $launcherRoot
    Set-EgCheckOutcome -Name 'launcher_package_manifest_match' -Pass $manifestMatch.Pass `
        -SupportRef $manifestMatch.SupportRef
}

# --------------------------------------------------------------------------------------
# Positions 5 to 7 - supplied paths exist and are absolute
# --------------------------------------------------------------------------------------

if (Test-EgPreflightShouldContinue) {
    foreach ($pair in @(
        [pscustomobject]@{ Name = 'checkout_root_exists_absolute'; Path = $CheckoutRoot; Container = $true },
        [pscustomobject]@{ Name = 'config_path_exists_absolute'; Path = $ConfigPath; Container = $false },
        [pscustomobject]@{ Name = 'python_exe_exists_absolute'; Path = $PythonExe; Container = $false })) {

        $absolute = [System.IO.Path]::IsPathRooted($pair.Path)
        if (-not $absolute) {
            Set-EgCheckOutcome -Name $pair.Name -Pass $false -SupportRef 'EG_LAUNCHER_PATH_NOT_ABSOLUTE'
            continue
        }
        $exists = $false
        if ($pair.Container) {
            $exists = (Test-Path -LiteralPath $pair.Path -PathType Container)
        }
        else {
            $exists = (Test-Path -LiteralPath $pair.Path -PathType Leaf)
        }
        Set-EgCheckOutcome -Name $pair.Name -Pass $exists -SupportRef 'EG_LAUNCHER_PATH_MISSING'
    }
}

# --------------------------------------------------------------------------------------
# Positions 8 to 10 - private configuration contract
# --------------------------------------------------------------------------------------

if (Test-EgPreflightShouldContinue) {
    $configResult = Test-EgLauncherConfigContract -ConfigPath $ConfigPath -CheckoutRootPath $CheckoutRoot
    Merge-EgCheckResult -Result $configResult -Positions @(
        'config_path_outside_checkout', 'config_parses_json', 'config_required_keys_present')
}

# --------------------------------------------------------------------------------------
# Position 11 - interpreter version
# --------------------------------------------------------------------------------------

if (Test-EgPreflightShouldContinue) {
    $pythonResult = Test-EgPythonVersionSupported -PythonExe $PythonExe
    Merge-EgCheckResult -Result $pythonResult -Positions @('python_version_is_3_14')
}

# --------------------------------------------------------------------------------------
# Position 12 - path-scoped governed source integrity
# --------------------------------------------------------------------------------------

if (Test-EgPreflightShouldContinue) {
    $sourceResult = Test-EgGovernedSourceIntegrity -CheckoutRootPath $CheckoutRoot `
        -ExpectedBranch $ExpectedBranch
    Set-EgCheckOutcome -Name 'governed_source_integrity' -Pass $sourceResult.Pass `
        -SupportRef $sourceResult.SupportRef
}

# --------------------------------------------------------------------------------------
# Position 13 - private browser-cache readiness
# --------------------------------------------------------------------------------------

if (Test-EgPreflightShouldContinue) {
    $cacheResult = Test-EgBrowserCacheReady -BrowserCachePath $BrowserCachePath
    Merge-EgCheckResult -Result $cacheResult -Positions @('browser_cache_ready')
}

# --------------------------------------------------------------------------------------
# Positions 14 to 18 - launcher-root security expectations
# --------------------------------------------------------------------------------------

if (Test-EgPreflightShouldContinue) {
    $securityResult = Test-EgLauncherRootSecurity -LauncherRootPath $launcherRoot `
        -CheckoutRootPath $CheckoutRoot `
        -AuthorisedLauncherRootWriteSid $AuthorisedLauncherRootWriteSid
    Merge-EgCheckResult -Result $securityResult -Positions @(
        'launcher_root_outside_checkout',
        'launcher_root_not_writable_by_run_principal',
        'launcher_root_write_trustees_authorised',
        'launcher_files_not_reparse_points',
        'launcher_files_not_unexpectedly_readonly')
}

# --------------------------------------------------------------------------------------
# Positions 19 to 21 - the credential artefact, and only now
# --------------------------------------------------------------------------------------
# Reached ONLY when every non-secret position above passed. A run that will fail for any
# other reason never opens the credential artefact at all.

$credential = $null
$credentialUsable = $false

if ([string]::IsNullOrEmpty($script:EgFirstFailure)) {
    $imported = Import-EgLauncherCredential -CredentialPath $CredentialPath
    Set-EgCheckOutcome -Name 'credential_import_ok' -Pass $imported.Success `
        -SupportRef $imported.SupportRef
    if ($imported.Success) {
        $viability = Test-EgCredentialViability -Credential $imported.Credential
        Set-EgCheckOutcome -Name 'username_nonempty' -Pass $viability.UsernameNonEmpty `
            -SupportRef $viability.SupportRef
        Set-EgCheckOutcome -Name 'password_nonempty' -Pass $viability.PasswordNonEmpty `
            -SupportRef $viability.SupportRef
        if ($viability.UsernameNonEmpty -and $viability.PasswordNonEmpty) {
            $credential = $imported.Credential
            $credentialUsable = $true
        }
    }
}

if (-not [string]::IsNullOrEmpty($script:EgFirstFailure)) {
    Exit-EgPreflightFailure
}

if ($ValidateOnly) {
    # A validation run records only the three credential booleans. The imported object is
    # discarded immediately and no value survives it. Nothing is created, modified,
    # deleted, or renamed, no child is started beyond the read-only interpreter probe and
    # the governed Git reads, no browser is launched, and no portal is contacted.
    $credential = $null
    Exit-EgLauncher -ExitCode 0
}

if (-not $credentialUsable) {
    Exit-EgPreflightFailure
}

# --------------------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------------------
# Exactly three process-scope variables are set immediately before the child starts and
# restored on the finally-equivalent path. No other environment change is made.

$plainUsername = $credential.UserName
$plainPassword = ''
$unmanaged = [System.IntPtr]::Zero
try {
    $unmanaged = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($credential.Password)
    $plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($unmanaged)
}
finally {
    if ($unmanaged -ne [System.IntPtr]::Zero) {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($unmanaged)
    }
}

$injected = [ordered]@{}
$injected[$script:EgCredentialVariableNames[0]] = $plainUsername
$injected[$script:EgCredentialVariableNames[1]] = $plainPassword
$injected[$script:EgBrowserCacheVariableName] = $BrowserCachePath

$workingDirectory = Join-Path $CheckoutRoot 'energygrid-bill-downloader'
$childArguments = @('-m', 'energygrid_bill_downloader', $Command, '--config', $ConfigPath)

$childExitCode = $script:EgLauncherExitCodes['PreflightFailed']
$restoreResult = $null
$bindOk = $false
$childStdOut = ''
$childStdErr = ''
try {
    $outcome = Invoke-EgWithInjectedProcessEnvironment -Variables $injected -Body {
        # The browser-cache binding is verified POSITIVELY before the child starts. An
        # ambient value pointing elsewhere is overridden rather than honoured, and a
        # binding that cannot be established is terminal rather than a silent fall-through
        # to the default cache location.
        $boundCache = [System.Environment]::GetEnvironmentVariable(
            $script:EgBrowserCacheVariableName, 'Process')
        if ($boundCache -cne $BrowserCachePath) {
            [pscustomobject]@{ BindOk = $false; ExitCode = -1; StdOut = ''; StdErr = '' }
        }
        else {
            # EG-TRANSPORT-CAPTURE-BEGIN
            $startInfo = New-Object System.Diagnostics.ProcessStartInfo
            $startInfo.FileName = $PythonExe
            $startInfo.Arguments = ConvertTo-EgNativeArgumentString -Argument $childArguments
            $startInfo.WorkingDirectory = $workingDirectory
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            # Redirection is what makes the child's output reachable at all. An unredirected
            # native grandchild writes to the console handle this launcher inherited, which a
            # redirected caller of the launcher never observes.
            $startInfo.RedirectStandardOutput = $true
            $startInfo.RedirectStandardError = $true

            $child = $null
            try {
                $child = New-Object System.Diagnostics.Process
                $child.StartInfo = $startInfo
                [void]$child.Start()
                # BOTH readers are started before either is awaited. Draining one stream to
                # completion first lets the other stream's pipe buffer fill and block the
                # child forever, which is the classic redirection deadlock under Windows
                # PowerShell 5.1 and production .NET. The awaits therefore observe two
                # already-running reads rather than serialising them, and WaitForExit runs
                # only once both streams have reached end of file.
                $stdOutReader = $child.StandardOutput.ReadToEndAsync()
                $stdErrReader = $child.StandardError.ReadToEndAsync()
                $capturedStdOut = $stdOutReader.GetAwaiter().GetResult()
                $capturedStdErr = $stdErrReader.GetAwaiter().GetResult()
                $child.WaitForExit()
                [pscustomobject]@{
                    BindOk   = $true
                    ExitCode = $child.ExitCode
                    StdOut   = [string]$capturedStdOut
                    StdErr   = [string]$capturedStdErr
                }
            }
            finally {
                if ($null -ne $child) { $child.Dispose() }
            }
            # EG-TRANSPORT-CAPTURE-END
        }
    }
    $bindOk = [bool]$outcome.BodyResult.BindOk
    $childExitCode = [int]$outcome.BodyResult.ExitCode
    $childStdOut = [string]$outcome.BodyResult.StdOut
    $childStdErr = [string]$outcome.BodyResult.StdErr
    $restoreResult = $outcome.Restore
}
finally {
    # Bounded lifetime and reference removal. Dispose is never called on the credential,
    # because PSCredential does not implement IDisposable and the call would throw.
    $plainPassword = ''
    $plainUsername = ''
    $credential = $null
    $injected = $null
}

# The application owns its output contract; this launcher only carries it. Relay happens
# after the child completes and after the injected environment has been restored, for a
# zero and a non-zero child exit alike, and before any launcher-owned terminal path can
# run, so application output is never discarded by a later launcher failure. The console
# writers are used rather than the object pipeline because the pipeline formatter can wrap
# and reflow a long line; these writers emit the captured text byte-for-byte with nothing
# added, removed, parsed, or filtered.
if (-not [string]::IsNullOrEmpty($childStdOut)) {
    [Console]::Out.Write($childStdOut)
    [Console]::Out.Flush()
}
if (-not [string]::IsNullOrEmpty($childStdErr)) {
    [Console]::Error.Write($childStdErr)
    [Console]::Error.Flush()
}

if ($null -ne $restoreResult) {
    if (-not $restoreResult.Pass) {
        # A failed restoration is terminal rather than silent.
        $script:EgFirstFailure = 'credential_import_ok'
        $script:EgFirstFailureRef = $restoreResult.SupportRef
        Exit-EgPreflightFailure
    }
}

if (-not $bindOk) {
    $script:EgFirstFailure = 'browser_cache_ready'
    $script:EgFirstFailureRef = 'EG_LAUNCHER_BROWSER_CACHE_BIND_FAILED'
    Exit-EgPreflightFailure
}

exit $childExitCode
